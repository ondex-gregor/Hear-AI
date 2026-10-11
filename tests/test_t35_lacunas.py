"""T35: lacunas de robustez das revisões 019 (Agente 3) e 021 (Codex)."""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import pytest

from hear_ai.streaming import StreamingTranscriber

SR = 16000


class Rec:
    """Gravador falso: `frames_recorded` e `snapshot` sobre um buffer que cresce."""

    def __init__(self, seconds: float = 0.0):
        self.audio = np.zeros(int(seconds * SR), dtype=np.float32)

    @property
    def frames_recorded(self):
        return len(self.audio)

    def snapshot(self, start):
        return self.audio[start:]


class GateTrans:
    """Transcritor cuja 1ª chamada fica presa até `gate` abrir; as demais respondem na hora."""

    def __init__(self, gate: threading.Event | None = None, text="bloco"):
        self.gate = gate
        self.text = text
        self.calls = 0
        self.entered = threading.Event()

    def transcribe(self, audio):
        self.calls += 1
        if self.calls == 1 and self.gate is not None:
            self.entered.set()
            self.gate.wait(10)
        return f"{self.text}{self.calls}"


def st(rec, trans, **kw):
    kw.setdefault("poll_s", 0.01)
    return StreamingTranscriber(rec, trans, logging.getLogger("t35"), **kw)


# --- 019 item 1: cancelar ANTES de transcrever --------------------------------------------

def test_bloco_nao_e_transcrito_se_ja_cancelado():
    trans = GateTrans()
    s = st(Rec(40), trans)
    s.cancelled = True
    assert s._do_block(final=True, audio_total=np.zeros(40 * SR, np.float32)) is False
    assert trans.calls == 0


# --- 019 item 2: finish com GPU travada ----------------------------------------------------

def test_finish_com_bloco_travado_cai_no_plano_b_dentro_do_prazo():
    gate = threading.Event()
    trans = GateTrans(gate)
    rec = Rec(40)
    s = st(rec, trans, finish_wait_s=0.3)
    s.start()
    assert trans.entered.wait(5)  # o 1º bloco está "na GPU"
    t0 = time.monotonic()
    out = s.finish(rec.audio)
    assert time.monotonic() - t0 < 3
    assert s.fallback and out == "bloco2"  # plano B: áudio inteiro numa chamada só
    gate.set()  # a GPU "volta": o resultado velho não pode mexer em nada
    time.sleep(0.1)
    assert s.text == "" and s.blocks == []


# --- 019 item 7 + bug achado pelo líder: cancelar e começar outro ditado --------------------

def test_cancelar_e_regravar_logo_nao_perde_o_ditado_novo():
    gate = threading.Event()
    trans = GateTrans(gate)
    rec = Rec(40)
    s = st(rec, trans, cancel_wait_s=0.05)
    s.start()
    assert trans.entered.wait(5)
    s.cancel()  # Esc/duplo clique com o bloco ainda na GPU (espera curta e segue)
    rec2 = Rec(5)
    s.recorder = rec2
    s.start()  # ANTES: voltava cedo (thread viva) e o cancelled=True velho zerava o ditado novo
    gate.set()
    time.sleep(0.1)
    out = s.finish(rec2.audio)
    assert out != "" and not s.cancelled


def test_cancel_espera_a_thread_com_limite():
    gate = threading.Event()
    trans = GateTrans(gate)
    s = st(Rec(40), trans, cancel_wait_s=0.2)
    s.start()
    assert trans.entered.wait(5)
    t0 = time.monotonic()
    s.cancel()
    assert 0.15 < time.monotonic() - t0 < 2
    gate.set()


# --- 019 item 3: falha no spool não perde o áudio -----------------------------------------

def test_falha_ao_gravar_spool_preserva_audio_em_memoria(tmp_path, caplog):
    from hear_ai.audio import Recorder

    class FailingFile:
        def write(self, _b):
            raise OSError("disco cheio")

        def flush(self):
            raise OSError("disco cheio")

        def fileno(self):
            raise OSError("x")

        def close(self):
            pass

    class Stream:
        def __init__(self, **kw):
            self.cb = kw["callback"]

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    holder = {}

    def make(**kw):
        holder["s"] = Stream(**kw)
        return holder["s"]

    caplog.set_level(logging.WARNING)
    r = Recorder(stream_factory=make, spool_dir=tmp_path)
    r.start()
    r._writer._file = FailingFile()
    for _ in range(3):
        holder["s"].cb(np.full((1600, 1), 0.1, np.float32), 1600, None, None)
    out = r.stop()
    assert len(out) == 4800  # tudo em memória
    assert "falha ao gravar cópia em disco" in caplog.text


# --- 019 item 4: recuperação em que todos os órfãos falham --------------------------------

def test_recuperacao_com_todos_os_orfaos_falhando_emite_erro():
    from hear_ai.controller import Controller

    states = []

    class Fb:
        def __getattr__(self, _n):
            return lambda *a: None

    class Trans:
        def transcribe(self, a):
            raise RuntimeError("falhou")

    c = Controller(None, Trans(), None, Fb(), None, logging.getLogger("t"), "pt",
                   on_state_change=lambda *a: states.append(a), run_async=False)
    c._emit = lambda ev: states.append(("emit", ev))
    assert c.recover(["a", "b"], lambda o: np.zeros(16000, np.float32), lambda o: None)
    assert ("emit", "error") in states and ("emit", "saved") not in states


# --- 019 item 5: atalho de inicialização com alvo padrão inexistente ----------------------

def test_startup_alvo_padrao_inexistente_nao_chama_powershell(tmp_path, monkeypatch):
    from hear_ai import startup
    chamadas = []
    monkeypatch.setattr(startup, "default_target", lambda root=None: tmp_path / "nao-existe.bat")
    assert startup.enable(tmp_path / "Inicializar", runner=lambda *a, **k: chamadas.append(a)) is False
    assert chamadas == []


# --- 021: Debouncer, DiagLog, HWND inválido, WTS ------------------------------------------

def test_debouncer_reinicia_mas_respeita_o_teto():
    from hear_ai.ui.bubble_watch import Debouncer
    agendados, disparos, agora = [], [], [0.0]
    d = Debouncer(lambda ms, fn: agendados.append((ms, fn)), 800, disparos.append,
                  max_wait_ms=3000, clock=lambda: agora[0])
    d.request("a")
    agora[0] = 2.5
    d.request("b")
    assert agendados[-1][0] == 500  # faltavam 500 ms para o teto de 3 s
    agora[0] = 3.5
    d.request("c")
    assert agendados[-1][0] == 0  # teto estourado: dispara já
    agendados[-1][1]()
    assert disparos == ["a+b+c"]


def test_diaglog_descarta_chave_desconhecida(tmp_path):
    from hear_ai.ui.bubble_watch import DiagLog
    log = DiagLog(tmp_path)
    log.event("fix", reason="vigia", texto_ditado="SEGREDO", **{"chave nova": "x"})
    log.close()
    content = (tmp_path / "bolha-diag.log").read_text(encoding="utf-8")
    assert "reason=vigia" in content and "SEGREDO" not in content and "texto_ditado" not in content


def test_decide_hwnd_invalido_recupera():
    from hear_ai.ui.bubble_watch import ACTION_SHOW, ACTION_TOPMOST, BubbleProbe, decide
    p = BubbleProbe(visible=True, opacity=1.0, x=0, y=0, inside_screen=True, screen_name="a", topmost=None,
                    visual="idle", hide_when_idle=False, native_visible=None, hwnd_valid=False)
    assert decide(p) == [ACTION_SHOW, ACTION_TOPMOST]
    ok = BubbleProbe(visible=True, opacity=1.0, x=0, y=0, inside_screen=True, screen_name="a", topmost=None,
                     visual="idle", hide_when_idle=False, native_visible=None, hwnd_valid=None)
    assert decide(ok) == []  # sem Win32 (None) não é "inválido"


def test_wts_troca_de_hwnd_re_registra():
    from types import SimpleNamespace

    from hear_ai.ui.qt_app import QtUi

    class Sess:
        def __init__(self):
            self.log = []

        def register(self, h):
            self.log.append(("reg", h))
            return True

        def unregister(self, h):
            self.log.append(("unreg", h))
            return True

    sess = Sess()
    hwnd = [111]
    fake = SimpleNamespace(session=sess, _bubble_wanted=True, _session_hwnd=0, logger=logging.getLogger("t"),
                           diag=SimpleNamespace(event=lambda *a, **k: None), _hwnd=lambda: hwnd[0])
    fake._register_session = lambda: QtUi._register_session(fake)
    QtUi._ensure_session(fake)
    assert sess.log == [("reg", 111)] and fake._session_hwnd == 111
    QtUi._ensure_session(fake)
    assert sess.log == [("reg", 111)]  # mesmo HWND: nada
    hwnd[0] = 222
    QtUi._ensure_session(fake)
    assert sess.log[-2:] == [("unreg", 111), ("reg", 222)] and fake._session_hwnd == 222
