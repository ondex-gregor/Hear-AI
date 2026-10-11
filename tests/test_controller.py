import logging
import threading

import numpy as np

from hear_ai.controller import IDLE, RECORDING, TRANSCRIBING, Controller

SEGREDO = "senha super secreta do usuario"
AUDIO_OK = np.zeros(16000, dtype=np.float32)  # 1 s


class FakeRecorder:
    def __init__(self, audio=AUDIO_OK, fail_start=False):
        self.audio = audio
        self.fail_start = fail_start
        self.started = 0
        self.stopped = 0

    def start(self):
        if self.fail_start:
            raise OSError("sem microfone")
        self.started += 1

    def stop(self):
        self.stopped += 1
        return self.audio


class FakeTranscriber:
    def __init__(self, text=SEGREDO, error=None, gate=None):
        self.text = text
        self.error = error
        self.gate = gate
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.error:
            raise self.error
        return self.text


class FakePaster:
    def __init__(self, error=None, events=None):
        self.pasted = []
        self.error = error
        self.events = events if events is not None else []

    def paste(self, text):
        self.events.append("paste")
        if self.error:
            raise self.error
        self.pasted.append(text)


class FakeFeedback:
    def __init__(self):
        self.events = []

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")

    def done(self):
        self.events.append("done")

    def error(self):
        self.events.append("error")


class FakeSink:
    def __init__(self, error=None):
        self.saved = []
        self.error = error

    def save(self, text, meta):
        if self.error:
            raise self.error
        self.saved.append((text, meta))


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        # formata como o handler de arquivo faria, incluindo traceback
        self.lines.append(self.format(record))


def make(recorder=None, transcriber=None, paster=None, sink=None, **kw):
    log = logging.getLogger(f"teste.controller.{id(object())}")
    log.setLevel(logging.DEBUG)
    handler = ListHandler()
    log.addHandler(handler)
    parts = dict(
        recorder=recorder or FakeRecorder(),
        transcriber=transcriber or FakeTranscriber(),
        paster=paster or FakePaster(),
        feedback=FakeFeedback(),
        sink=sink or FakeSink(),
    )
    c = Controller(**parts, logger=log, language="pt", run_async=kw.pop("run_async", False), **kw)
    return c, parts, handler


def test_ciclo_completo():
    estados = []
    c, p, _ = make(on_state_change=estados.append)
    assert c.state == IDLE
    c.begin()
    assert c.state == RECORDING and p["recorder"].started == 1
    c.end()
    assert c.state == IDLE
    assert p["paster"].pasted == [SEGREDO]
    assert p["sink"].saved[0][0] == SEGREDO
    assert p["sink"].saved[0][1]["language"] == "pt"
    assert p["sink"].saved[0][1]["duration_s"] == 1.0
    assert p["feedback"].events == ["start", "stop", "done"]
    assert estados == [RECORDING, TRANSCRIBING, IDLE]


def test_toggle():
    c, p, _ = make()
    c.toggle()
    assert c.state == RECORDING
    c.toggle()
    assert c.state == IDLE and p["paster"].pasted == [SEGREDO]


def test_eventos_fora_de_hora_sao_ignorados():
    c, p, _ = make()
    c.end()  # idle: nada
    assert p["recorder"].stopped == 0 and c.state == IDLE
    c.begin()
    c.begin()  # já gravando
    assert p["recorder"].started == 1


def test_begin_durante_transcricao_e_ignorado():
    gate = threading.Event()
    c, p, _ = make(transcriber=FakeTranscriber(gate=gate), run_async=True)
    c.begin()
    c.end()
    assert c.state == TRANSCRIBING
    c.begin()
    c.toggle()
    c.end()
    assert p["recorder"].started == 1 and p["recorder"].stopped == 1
    gate.set()
    c.join()
    assert c.state == IDLE
    assert p["transcriber"].calls == 1
    assert p["paster"].pasted == [SEGREDO]


def test_um_ciclo_por_vez_com_duas_transcricoes_seguidas():
    gate = threading.Event()
    t = FakeTranscriber(gate=gate)
    c, p, _ = make(transcriber=t, run_async=True)
    c.begin()
    c.end()
    for _ in range(5):
        c.begin()
    gate.set()
    c.join()
    assert t.calls == 1 and len(p["paster"].pasted) == 1


def test_audio_curto_nao_cola():
    c, p, _ = make(recorder=FakeRecorder(np.zeros(1600, dtype=np.float32)))  # 0,1 s
    c.begin()
    c.end()
    assert c.state == IDLE
    assert p["paster"].pasted == [] and p["sink"].saved == []
    assert "done" not in p["feedback"].events


def test_audio_vazio_nao_cola():
    c, p, _ = make(recorder=FakeRecorder(np.zeros(0, dtype=np.float32)))
    c.begin()
    c.end()
    assert p["paster"].pasted == [] and c.state == IDLE


def test_texto_vazio_nao_cola():
    c, p, _ = make(transcriber=FakeTranscriber(text="   "))
    c.begin()
    c.end()
    assert p["paster"].pasted == [] and p["sink"].saved == [] and c.state == IDLE


def test_erro_na_transcricao_volta_a_idle_e_nao_vaza_texto():
    c, p, h = make(transcriber=FakeTranscriber(error=RuntimeError(f"falhou em: {SEGREDO}")))
    c.begin()
    c.end()
    assert c.state == IDLE
    assert p["feedback"].events[-1] == "error"
    log = "\n".join(h.lines)
    assert "RuntimeError" in log
    assert SEGREDO not in log


def test_erro_na_colagem_nao_vaza_texto_e_volta_a_idle():
    c, p, h = make(paster=FakePaster(error=OSError(f"clipboard com {SEGREDO}")))
    c.begin()
    c.end()
    assert c.state == IDLE and p["feedback"].events[-1] == "error"
    assert SEGREDO not in "\n".join(h.lines)
    # T17 (revisão 010 item 1): o texto vai para o histórico ANTES de colar, então mesmo
    # com a colagem falhando ele já está salvo (antes: saved == []).
    assert p["sink"].saved[0][0] == SEGREDO and p["sink"].saved[0][1]["status"] == "pendente"


def test_log_de_sucesso_nao_tem_o_texto_so_metadados():
    c, p, h = make()
    c.begin()
    c.end()
    log = "\n".join(h.lines)
    assert SEGREDO not in log
    assert f"len={len(SEGREDO)}" in log


def test_falha_no_historico_nao_derruba_a_ditada():
    c, p, h = make(sink=FakeSink(error=OSError(f"disco cheio {SEGREDO}")))
    c.begin()
    c.end()
    assert p["paster"].pasted == [SEGREDO]
    assert p["feedback"].events[-1] == "done"
    assert c.state == IDLE
    assert SEGREDO not in "\n".join(h.lines)


def test_falha_ao_iniciar_gravacao():
    c, p, _ = make(recorder=FakeRecorder(fail_start=True))
    c.begin()
    assert c.state == IDLE and p["feedback"].events == ["error"]
    c.end()  # não deve quebrar
    assert c.state == IDLE


def test_espera_modificadoras_antes_de_colar():
    ordem = []
    paster = FakePaster(events=ordem)
    c, p, _ = make(paster=paster, before_paste=lambda: ordem.append("espera"))
    c.begin()
    c.end()
    assert ordem == ["espera", "paste"]


def test_callback_de_estado_com_erro_nao_quebra():
    def ruim(_):
        raise ValueError("ui quebrou")

    c, p, _ = make(on_state_change=ruim)
    c.begin()
    c.end()
    assert p["paster"].pasted == [SEGREDO] and c.state == IDLE


def test_notificacao_final_reflete_estado_atual_quando_begin_chega_durante_o_fim_do_ciclo():
    """_process termina (idle) e begin() chega antes da notificação do idle ser entregue."""
    reached, go = threading.Event(), threading.Event()
    armed = {"on": True}

    class Corrida(Controller):
        def _notify(self):
            if armed["on"] and self._state == IDLE and threading.current_thread() is not threading.main_thread():
                armed["on"] = False
                reached.set()
                go.wait(5)
            super()._notify()

    seen = []
    log = logging.getLogger("teste.corrida")
    c = Corrida(
        FakeRecorder(), FakeTranscriber(), FakePaster(), FakeFeedback(), FakeSink(),
        log, "pt", on_state_change=seen.append, run_async=True,
    )
    c.begin()
    c.end()
    assert reached.wait(5)  # thread do ciclo já mudou para idle e está prestes a notificar
    c.begin()  # chega agora
    assert c.state == RECORDING
    go.set()
    c.join()
    assert c.state == RECORDING
    assert seen[-1] == RECORDING  # a bandeja termina no estado real, não em "idle" velho


def test_teclas_seguradas_apos_limite_nao_cola_e_deixa_texto_na_area_de_transferencia():
    class Clip:
        def __init__(self):
            self.content = "anterior"

        def set(self, t):
            self.content = t

    paster = FakePaster()
    paster.clipboard = Clip()
    c, p, h = make(paster=paster, before_paste=lambda: False)
    c.begin()
    c.end()
    assert paster.pasted == []  # Ctrl+V não foi enviado
    assert paster.events == []
    assert paster.clipboard.content == SEGREDO  # fica para colar manualmente (sem restaurar)
    assert p["feedback"].events[-1] == "error" and "done" not in p["feedback"].events
    assert c.state == IDLE
    assert SEGREDO not in "\n".join(h.lines)
    assert p["sink"].saved[0][0] == SEGREDO  # a transcrição não se perde no histórico


def test_teclas_seguradas_com_clipboard_falhando_nao_vaza_nem_trava():
    class ClipRuim:
        def set(self, t):
            raise OSError(f"travado com {SEGREDO}")

    paster = FakePaster()
    paster.clipboard = ClipRuim()
    c, p, h = make(paster=paster, before_paste=lambda: False)
    c.begin()
    c.end()
    assert c.state == IDLE and p["feedback"].events[-1] == "error"
    assert SEGREDO not in "\n".join(h.lines)


# --- T17: fluxo integrado (histórico → colar → status → spool), avisos, limite, recuperação ---

from types import SimpleNamespace  # noqa: E402

from hear_ai.controller import ERROR, LIMIT_WARNING, SAVED  # noqa: E402


class Ordem(list):
    """Linha do tempo compartilhada entre os fakes."""


class SpoolRecorder(FakeRecorder):
    """Recorder com cópia em disco de verdade (arquivo .pcm + .json em tmp_path)."""

    def __init__(self, tmp_path, ordem, audio=AUDIO_OK):
        super().__init__(audio)
        self.tmp_path = tmp_path
        self.ordem = ordem
        self.last_spool_path = None
        self.n = 0

    def start(self):
        super().start()
        self.n += 1
        self.last_spool_path = self.tmp_path / f"gravacao-{id(self)}-{self.n}.pcm"
        self.last_spool_path.write_bytes(b"\0\0" * 10)
        self.last_spool_path.with_suffix(".json").write_text("{}")

    def stop(self):
        self.ordem.append("stop")
        return super().stop()


class Historico:
    def __init__(self, ordem, ativo=True, falha=False):
        self.ordem = ordem
        self.ativo = ativo
        self.falha = falha
        self.itens = {}

    def save(self, text, meta):
        self.ordem.append(f"save:{meta['status']}")
        if self.falha:
            raise OSError("disco cheio")
        if not self.ativo:
            return None  # NullSink / pausado
        rid = f"id{len(self.itens) + 1}"
        self.itens[rid] = dict(meta, text=text)
        return rid

    def set_status(self, rid, status):
        self.ordem.append(f"status:{status}")
        self.itens[rid]["status"] = status


class PasterResultado:
    def __init__(self, ordem, pasted=True, reason="ok"):
        self.ordem = ordem
        self.pasted_ok = pasted
        self.reason = reason
        self.colados = []
        self.clipboard = SimpleNamespace(content=None)
        self.clipboard.set = lambda t: setattr(self.clipboard, "content", t)
        self.spool_check = None

    def paste(self, text):
        self.ordem.append("paste")
        if self.spool_check:
            self.spool_check()
        # como o Paster real (revisão 014): só "send_failed" deixa o texto na área de transferência;
        # "clipboard_busy" (set falhou) e "empty" não
        in_clipboard = self.pasted_ok or self.reason == "send_failed"
        if self.pasted_ok:
            self.colados.append(text)
        elif in_clipboard:
            self.clipboard.set(text)
        return SimpleNamespace(pasted=self.pasted_ok, restored=self.pasted_ok, reason=self.reason,
                               in_clipboard=in_clipboard)


def montar(tmp_path, ativo=True, falha_hist=False, pasted=True, reason="ok", before=None, **kw):
    ordem = Ordem()
    rec = SpoolRecorder(tmp_path, ordem)
    hist = Historico(ordem, ativo=ativo, falha=falha_hist)
    estados = []
    paster = PasterResultado(ordem, pasted=pasted, reason=reason)
    c, p, h = make(recorder=rec, paster=paster, sink=hist, on_state_change=estados.append,
                   before_paste=before, **kw)
    return c, p, h, ordem, rec, hist, paster, estados


def spool_existe(rec):
    return rec.last_spool_path.exists() or rec.last_spool_path.with_suffix(".json").exists()


def test_fluxo_completo_salva_antes_de_colar_e_marca_colado(tmp_path):
    c, p, h, ordem, rec, hist, paster, estados = montar(tmp_path)
    paster.spool_check = lambda: ordem.append(f"spool_na_colagem:{spool_existe(rec)}")
    c.begin()
    c.end()
    assert ordem == ["stop", "save:pendente", "paste", "spool_na_colagem:False", "status:colado"]
    assert hist.itens["id1"]["status"] == "colado" and hist.itens["id1"]["text"] == SEGREDO
    assert not spool_existe(rec)  # apagado assim que o texto ficou salvo no histórico
    assert paster.colados == [SEGREDO]
    assert p["feedback"].events[-1] == "done"
    assert estados == [RECORDING, TRANSCRIBING, IDLE]  # sucesso: sem aviso
    assert SEGREDO not in "\n".join(h.lines)


def test_nao_colou_marca_salvo_e_avisa_saved(tmp_path):
    c, p, h, ordem, rec, hist, paster, estados = montar(tmp_path, pasted=False, reason="send_failed")
    c.begin()
    c.end()
    assert ordem == ["stop", "save:pendente", "paste", "status:salvo"]
    assert hist.itens["id1"]["status"] == "salvo"
    assert paster.clipboard.content == SEGREDO
    assert estados == [RECORDING, TRANSCRIBING, SAVED, IDLE]
    assert p["feedback"].events[-1] == "error"
    assert SEGREDO not in "\n".join(h.lines)


def test_teclas_seguradas_avisa_saved_sem_colar(tmp_path):
    c, p, h, ordem, rec, hist, paster, estados = montar(tmp_path, before=lambda: False)
    c.begin()
    c.end()
    assert "paste" not in ordem
    assert ordem == ["stop", "save:pendente", "status:salvo"]
    assert paster.clipboard.content == SEGREDO
    assert SAVED in estados and estados[-1] == IDLE


def test_sem_historico_apaga_spool_so_depois_de_colar(tmp_path):
    c, p, h, ordem, rec, hist, paster, estados = montar(tmp_path, ativo=False)
    paster.spool_check = lambda: ordem.append(f"spool_na_colagem:{spool_existe(rec)}")
    c.begin()
    c.end()
    assert ordem == ["stop", "save:pendente", "paste", "spool_na_colagem:True"]
    assert not spool_existe(rec)  # colou: agora pode apagar


def test_sem_historico_e_sem_colar_mantem_spool_para_recuperar(tmp_path):
    c, p, h, ordem, rec, hist, paster, estados = montar(tmp_path, ativo=False, pasted=False, reason="send_failed")
    c.begin()
    c.end()
    assert spool_existe(rec)  # o único lugar seguro do ditado: fica para recuperar
    assert SAVED in estados  # está na área de transferência


def test_historico_com_erro_nao_derruba_e_apaga_spool_so_depois_de_colar(tmp_path):
    c, p, h, ordem, rec, hist, paster, estados = montar(tmp_path, falha_hist=True)
    paster.spool_check = lambda: ordem.append(f"spool_na_colagem:{spool_existe(rec)}")
    c.begin()
    c.end()
    assert paster.colados == [SEGREDO] and p["feedback"].events[-1] == "done"
    assert "spool_na_colagem:True" in ordem and not spool_existe(rec)
    assert "OSError" in "\n".join(h.lines)


def test_nada_salvo_em_lugar_nenhum_avisa_error(tmp_path):
    c, p, h, ordem, rec, hist, paster, estados = montar(tmp_path, ativo=False, pasted=False, reason="empty")
    c.begin()
    c.end()
    assert ERROR in estados and SAVED not in estados
    assert spool_existe(rec)


def test_erro_na_transcricao_avisa_error_e_mantem_spool(tmp_path):
    ordem = Ordem()
    rec = SpoolRecorder(tmp_path, ordem)
    estados = []
    c, p, h = make(recorder=rec, transcriber=FakeTranscriber(error=RuntimeError(SEGREDO)),
                   on_state_change=estados.append)
    c.begin()
    c.end()
    assert estados == [RECORDING, TRANSCRIBING, ERROR, IDLE]
    assert spool_existe(rec)  # transcrição falhou: o áudio fica para recuperar
    assert SEGREDO not in "\n".join(h.lines)


def test_audio_curto_e_texto_vazio_apagam_spool(tmp_path):
    for kw in (dict(audio=np.zeros(1000, np.float32)), dict(texto="  ")):
        ordem = Ordem()
        rec = SpoolRecorder(tmp_path, ordem, audio=kw.get("audio", AUDIO_OK))
        c, p, h = make(recorder=rec, transcriber=FakeTranscriber(text=kw.get("texto", SEGREDO)))
        c.begin()
        c.end()
        assert not spool_existe(rec)
        assert p["paster"].pasted == []


class FakeStreaming:
    def __init__(self, text=SEGREDO):
        self.text = text
        self.eventos = []

    def start(self):
        self.eventos.append("start")

    def finish(self, audio):
        self.eventos.append(("finish", len(audio)))
        return self.text

    def cancel(self):
        self.eventos.append("cancel")


def test_usa_streaming_quando_disponivel():
    st = FakeStreaming()
    c, p, h = make(streaming=st)
    c.begin()
    c.end()
    assert st.eventos == ["start", ("finish", 16000)]
    assert p["transcriber"].calls == 0  # o texto veio dos blocos
    assert p["paster"].pasted == [SEGREDO]


def test_streaming_com_erro_no_start_nao_impede_o_ditado():
    class Quebrado(FakeStreaming):
        def start(self):
            raise RuntimeError("x")

    c, p, h = make(streaming=Quebrado())
    c.begin()
    assert c.state == RECORDING
    c.end()
    assert p["paster"].pasted == [SEGREDO]


def test_audio_curto_cancela_streaming():
    st = FakeStreaming()
    c, p, h = make(recorder=FakeRecorder(np.zeros(100, np.float32)), streaming=st)
    c.begin()
    c.end()
    assert st.eventos == ["start", "cancel"]


def test_cancelar_durante_a_gravacao_descarta_tudo(tmp_path):
    ordem = Ordem()
    rec = SpoolRecorder(tmp_path, ordem)
    st = FakeStreaming()
    estados = []
    c, p, h = make(recorder=rec, streaming=st, on_state_change=estados.append)
    assert c.cancel() is False  # ocioso: nada a cancelar
    c.begin()
    assert c.cancel() is True
    assert c.state == IDLE
    assert "cancel" in st.eventos and ("finish", 16000) not in st.eventos
    assert not spool_existe(rec)
    assert p["paster"].pasted == [] and p["sink"].saved == []
    c.end()  # soltar o atalho depois do Esc não faz nada
    assert p["transcriber"].calls == 0
    assert estados[-1] == IDLE


def test_limite_avisa_e_encerra_transcrevendo():
    estados = []
    c, p, h = make(on_state_change=estados.append, run_async=True)
    c.begin()
    c.on_limit_warning(60)
    assert LIMIT_WARNING in estados and p["feedback"].events[-1] == "error"
    assert c.state == RECORDING
    c.on_limit_reached()  # na thread de áudio: só dispara o fim em outra thread
    for _ in range(300):
        if c.state == IDLE and p["paster"].pasted:
            break
        threading.Event().wait(0.01)
    c.join()
    assert p["paster"].pasted == [SEGREDO]  # nunca descarta: transcreve e cola
    assert c.state == IDLE


# --- recuperação ----------------------------------------------------------------------

def recuperar(c, orfaos, audios, falhar=()):
    descartados = []

    def load(o):
        if o in falhar:
            raise OSError("arquivo truncado")
        return audios[o]

    ok = c.recover(orfaos, load, descartados.append)
    return ok, descartados


def test_recuperacao_transcreve_salva_e_copia():
    hist = Historico(Ordem())
    paster = PasterResultado(Ordem())
    estados = []
    c, p, h = make(paster=paster, sink=hist, on_state_change=estados.append,
                   transcriber=FakeTranscriber(text="texto recuperado"))
    ok, desc = recuperar(c, ["o1", "o2"], {"o1": AUDIO_OK, "o2": AUDIO_OK})
    assert ok
    assert [i["status"] for i in hist.itens.values()] == ["salvo", "salvo"]
    assert paster.clipboard.content == "texto recuperado\n\ntexto recuperado"
    assert desc == ["o1", "o2"]
    assert estados == [TRANSCRIBING, SAVED, IDLE]
    assert p["feedback"].events[-1] == "done"
    assert "texto recuperado" not in "\n".join(h.lines)


def test_recuperacao_com_falha_mantem_o_arquivo():
    hist = Historico(Ordem())
    paster = PasterResultado(Ordem())
    c, p, h = make(paster=paster, sink=hist)
    ok, desc = recuperar(c, ["bom", "ruim"], {"bom": AUDIO_OK}, falhar=("ruim",))
    assert desc == ["bom"]  # o que falhou continua no disco para tentar de novo
    assert len(hist.itens) == 1


def test_recuperacao_so_com_o_app_ocioso():
    c, p, h = make()
    c.begin()
    ok, desc = recuperar(c, ["o1"], {"o1": AUDIO_OK})
    assert ok is False and desc == []
    assert c.state == RECORDING


def test_recuperacao_de_silencio_descarta_sem_avisar():
    estados = []
    c, p, h = make(transcriber=FakeTranscriber(text=""), on_state_change=estados.append)
    ok, desc = recuperar(c, ["o1"], {"o1": AUDIO_OK})
    assert desc == ["o1"] and SAVED not in estados and ERROR not in estados


# --- rodada 1: in_clipboard do PasteResult decide entre "saved" e "error" -------------------

def test_historico_desligado_e_set_falhando_avisa_error_nunca_saved(tmp_path):
    c, p, h, ordem, rec, hist, paster, estados = montar(tmp_path, ativo=False, pasted=False,
                                                         reason="clipboard_busy")
    c.begin()
    c.end()
    assert ERROR in estados and SAVED not in estados
    assert paster.clipboard.content is None  # o texto não está em lugar nenhum...
    assert spool_existe(rec)  # ...então o áudio fica para recuperar
    assert p["feedback"].events[-1] == "error"


def test_historico_ligado_e_set_falhando_avisa_saved(tmp_path):
    c, p, h, ordem, rec, hist, paster, estados = montar(tmp_path, ativo=True, pasted=False,
                                                         reason="clipboard_busy")
    c.begin()
    c.end()
    assert SAVED in estados and ERROR not in estados  # está a salvo no histórico
    assert hist.itens["id1"]["status"] == "salvo"


def test_paster_real_com_set_falhando_e_sem_historico_avisa_error():
    """Integração com o Paster de verdade: área de transferência travada + NullSink."""
    from hear_ai.history import NullSink
    from hear_ai.paster import Paster

    class ClipTravado:
        def __init__(self):
            self.content = "do usuario"

        def get(self):
            return self.content

        def set(self, t):
            raise RuntimeError(f"travado {t}")

    clip = ClipTravado()
    enviados = []
    paster = Paster(clip, lambda: enviados.append(1), delay_s=0.0, sleep=lambda _d: None)
    estados = []
    c, p, h = make(paster=paster, sink=NullSink(), on_state_change=estados.append)
    c.begin()
    c.end()
    assert enviados == []  # não mandou Ctrl+V sem o texto na área de transferência
    assert ERROR in estados and SAVED not in estados
    assert clip.content == "do usuario"
    assert SEGREDO not in "\n".join(h.lines)


def test_paster_real_com_send_falhando_e_sem_historico_avisa_saved():
    from hear_ai.history import NullSink
    from hear_ai.paster import Paster

    class Clip:
        content = "do usuario"

        def get(self):
            return self.content

        def set(self, t):
            self.content = t

    def send():
        raise OSError("sem foco")

    clip = Clip()
    estados = []
    paster = Paster(clip, send, delay_s=0.0, sleep=lambda _d: None)
    c, p, h = make(paster=paster, sink=NullSink(), on_state_change=estados.append)
    c.begin()
    c.end()
    assert SAVED in estados and clip.content == SEGREDO  # dá para colar com Ctrl+V
