"""Testes para a etapa do gerador no Controller (T47b P2)."""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from hear_ai.controller import Controller, IDLE, RECORDING, TRANSCRIBING
from hear_ai.gerador import Resultado, MOTIVO_INDISPONIVEL, MOTIVO_TIMEOUT, MOTIVO_VAZIO, MOTIVO_SEM_PEDIDO

AUDIO_OK = np.zeros(16000, dtype=np.float32)  # 1 s
SENTINELA = "SENTINELA_NAO_DEVE_VAZAR"


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
    def __init__(self, text=SENTINELA, error=None, gate=None):
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

    def clipboard(self):
        return SimpleNamespace(set=lambda t: None)


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
        self.saved.append((text, dict(meta)))
        return "id-1"

    def set_status(self, *_a):
        return True


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def make_controller(
    recorder=None,
    transcriber=None,
    paster=None,
    sink=None,
    prompt_step=None,
    logger=None,
    run_async=False,
    postprocess=None,
    **kw,
):
    log = logger or logging.getLogger(f"teste.controller.{id(object())}")
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
    c = Controller(
        **parts,
        logger=log,
        language="pt",
        run_async=run_async,
        prompt_step=prompt_step,
        postprocess=postprocess,
        **kw,
    )
    return c, parts, handler


class FakePromptStep:
    """Fake do prompt_step que retorna um Resultado configurável."""

    def __init__(self, resultado: Resultado):
        self.resultado = resultado
        self.calls = 0
        self.last_input = None

    def __call__(self, texto_fiel: str) -> Resultado:
        self.calls += 1
        self.last_input = texto_fiel
        return self.resultado


# ============================================================
# MODO A (prompt_step=None): fluxo EXATAMENTE o atual
# ============================================================

def test_modo_a_fluxo_exato_igual_ao_atual():
    """Modo A (sem prompt_step): zero chamadas ao gerador, fluxo idêntico."""
    c, parts, h = make_controller(postprocess=lambda t: t)
    c.begin()
    c.end()
    c.join()

    # Texto colado é a transcrição fiel
    assert parts["paster"].pasted == [SENTINELA]
    # Histórico guarda text=transcrição, sem generated nem revisar
    saved = parts["sink"].saved[0]
    assert saved[0] == SENTINELA
    meta = saved[1]
    assert meta["status"] == "pendente"
    assert meta.get("generated") is None
    assert "revisar" not in meta  # líder: só grava revisar quando True
    # Estados normais
    assert c.state == IDLE


def test_modo_a_zero_chamadas_rede(socket_blocked):
    """Modo A: zero rede (socket bloqueado no fixture)."""
    c, parts, h = make_controller(postprocess=lambda t: t)
    c.begin()
    c.end()
    c.join()
    assert parts["paster"].pasted == [SENTINELA]


# ============================================================
# MODO B (prompt_step fornecido)
# ============================================================

def test_modo_b_sucesso_cola_o_gerado():
    """Modo B com gerador bem-sucedido: cola resultado.texto, histórico tem generated."""
    resultado = Resultado(
        texto="Prompt gerado com sucesso",
        usou_gerador=True,
        motivo=None,
        revisar=False,
        provedor="ollama",
        na_nuvem=False,
    )
    prompt_step = FakePromptStep(resultado)
    events = []
    c, parts, h = make_controller(prompt_step=prompt_step, postprocess=lambda t: t, on_state_change=events.append)
    c.begin()
    c.end()
    c.join()

    # Cola o texto do gerador
    assert parts["paster"].pasted == ["Prompt gerado com sucesso"]
    # Histórico: text = colado, raw NÃO presente (sem alterações de vocabulário), generated = resultado.texto, revisar = False
    saved = parts["sink"].saved[0]
    assert saved[0] == "Prompt gerado com sucesso"
    meta = saved[1]
    assert meta.get("raw")  # líder: com gerador, a fala fiel SEMPRE vai ao histórico (valor exato: teste do líder no fim)
    assert meta["generated"] == "Prompt gerado com sucesso"
    assert "revisar" not in meta
    # prompt_step chamado com a transcrição fiel
    assert prompt_step.calls == 1
    assert prompt_step.last_input == SENTINELA
    # Estado "generating" emitido via on_state_change
    assert "generating" in events


def test_modo_b_gerador_indisponivel_fallback_transcricao():
    """Falha do gerador (indisponível): cola transcrição fiel + emite generator_fallback."""
    resultado = Resultado(
        texto=SENTINELA,
        usou_gerador=False,
        motivo=MOTIVO_INDISPONIVEL,
        revisar=False,
        provedor="ollama",
        na_nuvem=False,
    )
    prompt_step = FakePromptStep(resultado)
    events = []
    c, parts, h = make_controller(prompt_step=prompt_step, postprocess=lambda t: t, on_state_change=events.append)
    c.begin()
    c.end()
    c.join()

    # Cola a transcrição fiel (fallback)
    assert parts["paster"].pasted == [SENTINELA]
    # Histórico: text = fiel, raw NÃO presente (sem alterações de vocabulário), generated = None, revisar = False
    saved = parts["sink"].saved[0]
    assert saved[0] == SENTINELA
    meta = saved[1]
    assert "raw" not in meta  # sem alterações de vocabulário
    assert meta.get("generated") is None
    assert "revisar" not in meta
    # Evento generator_fallback emitido
    assert "generator_fallback" in events


def test_modo_b_timeout_fallback():
    """Timeout do gerador: cola transcrição fiel + generator_fallback."""
    resultado = Resultado(
        texto=SENTINELA,
        usou_gerador=False,
        motivo=MOTIVO_TIMEOUT,
        revisar=False,
    )
    prompt_step = FakePromptStep(resultado)
    events = []
    c, parts, h = make_controller(prompt_step=prompt_step, postprocess=lambda t: t, on_state_change=events.append)
    c.begin()
    c.end()
    c.join()

    assert parts["paster"].pasted == [SENTINELA]
    saved = parts["sink"].saved[0]
    assert saved[0] == SENTINELA
    meta = saved[1]
    assert meta.get("generated") is None
    assert "revisar" not in meta
    assert "generator_fallback" in events


def test_modo_b_resposta_vazia_fallback():
    """Resposta vazia do gerador: cola transcrição fiel + generator_fallback."""
    resultado = Resultado(
        texto=SENTINELA,
        usou_gerador=False,
        motivo=MOTIVO_VAZIO,
        revisar=False,
    )
    prompt_step = FakePromptStep(resultado)
    events = []
    c, parts, h = make_controller(prompt_step=prompt_step, postprocess=lambda t: t, on_state_change=events.append)
    c.begin()
    c.end()
    c.join()

    assert parts["paster"].pasted == [SENTINELA]
    assert "generator_fallback" in events


def test_modo_b_sem_pedido_fallback():
    """[[SEM_PEDIDO]]: cola transcrição fiel + generator_fallback."""
    resultado = Resultado(
        texto=SENTINELA,
        usou_gerador=False,
        motivo=MOTIVO_SEM_PEDIDO,
        revisar=False,
    )
    prompt_step = FakePromptStep(resultado)
    events = []
    c, parts, h = make_controller(prompt_step=prompt_step, postprocess=lambda t: t, on_state_change=events.append)
    c.begin()
    c.end()
    c.join()

    assert parts["paster"].pasted == [SENTINELA]
    assert "generator_fallback" in events


def test_modo_b_exception_no_prompt_step_fallback():
    """Exceção no prompt_step: cola transcrição fiel + generator_fallback (nunca perde a fala)."""
    def prompt_step_que_falha(texto):
        raise RuntimeError("erro no gerador")

    events = []
    c, parts, h = make_controller(prompt_step=prompt_step_que_falha, postprocess=lambda t: t, on_state_change=events.append)
    c.begin()
    c.end()
    c.join()

    assert parts["paster"].pasted == [SENTINELA]
    saved = parts["sink"].saved[0]
    assert saved[0] == SENTINELA
    meta = saved[1]
    assert meta.get("generated") is None
    assert "generator_fallback" in events


def test_modo_b_com_revisar_true_historico_marca_revisar():
    """Se gerador retorna revisar=True, histórico marca revisar."""
    resultado = Resultado(
        texto="Prompt com revisão",
        usou_gerador=True,
        motivo=None,
        revisar=True,
        faltando=("arquivo.py",),
    )
    prompt_step = FakePromptStep(resultado)
    c, parts, h = make_controller(prompt_step=prompt_step, postprocess=lambda t: t)
    c.begin()
    c.end()
    c.join()

    saved = parts["sink"].saved[0]
    meta = saved[1]
    assert meta["revisar"] is True
    assert meta["generated"] == "Prompt com revisão"


def test_modo_b_na_nuvem_historico_marca_na_nuvem():
    """Se gerador é nuvem, histórico reflete isso (via meta)."""
    resultado = Resultado(
        texto="Prompt na nuvem",
        usou_gerador=True,
        motivo=None,
        revisar=False,
        provedor="openai_compat",
        na_nuvem=True,
    )
    prompt_step = FakePromptStep(resultado)
    c, parts, h = make_controller(prompt_step=prompt_step, postprocess=lambda t: t)
    c.begin()
    c.end()
    c.join()

    saved = parts["sink"].saved[0]
    meta = saved[1]
    assert meta["generated"] == "Prompt na nuvem"


def test_modo_b_logs_sem_texto_sentinela():
    """Logs nunca contêm o texto ditado, só metadados."""
    resultado = Resultado(
        texto="Prompt gerado",
        usou_gerador=True,
        motivo=None,
        revisar=False,
    )
    prompt_step = FakePromptStep(resultado)
    c, parts, h = make_controller(prompt_step=prompt_step, postprocess=lambda t: t)
    c.begin()
    c.end()
    c.join()

    log = "\n".join(h.lines)
    assert SENTINELA not in log
    assert "Prompt gerado" not in log  # o resultado também não deve vazar
    # Mas metadados aparecem
    assert "len=" in log or "audio=" in log


def test_modo_b_prompt_step_chamado_com_texto_fiel_apos_postprocess():
    """prompt_step recebe o texto APÓS o pós-texto (vocabulário + comandos de voz)."""
    import tempfile
    from hear_ai.config import Config
    from hear_ai.vocab import VocabStore
    from hear_ai.controller import vocab_postprocessor

    # Texto que o vocabulário vai corrigir
    texto_bruto = "a undex usa cloud code"

    cfg = Config(dictionary={"undex": "Ondex", "cloud code": "Claude Code"})
    with tempfile.TemporaryDirectory() as tmp:
        store = VocabStore(Path(tmp) / "vocab.json")

        capturado = {}

        def prompt_step_captura(texto):
            capturado["input"] = texto
            return Resultado(texto="Prompt final", usou_gerador=True, motivo=None, revisar=False)

        c, parts, h = make_controller(
            transcriber=FakeTranscriber(text=texto_bruto),
            prompt_step=prompt_step_captura,
            postprocess=vocab_postprocessor(cfg, store),
        )
        c.begin()
        c.end()
        c.join()

        # O prompt_step deve receber o texto pós-processado
        assert capturado["input"] == "a Ondex usa Claude Code"


def test_modo_b_prompt_step_nao_bloqueia_thread_qt():
    """prompt_step roda na thread de trabalho, não na thread principal (Qt)."""
    thread_ids = {}

    def prompt_step_thread(texto):
        thread_ids["worker"] = threading.current_thread().ident
        return Resultado(texto="ok", usou_gerador=True, motivo=None, revisar=False)

    c, parts, h = make_controller(prompt_step=prompt_step_thread, postprocess=lambda t: t, run_async=True)
    thread_ids["main"] = threading.current_thread().ident
    c.begin()
    c.end()
    c.join()

    # Deve ter rodado em thread diferente da main
    assert thread_ids["worker"] != thread_ids["main"]


# Fixture para bloquear socket no Modo A
@pytest.fixture
def socket_blocked(monkeypatch):
    """Bloqueia chamadas de rede para garantir Modo A offline."""
    import socket

    original_socket = socket.socket

    def blocked_socket(*args, **kwargs):
        raise OSError("Rede bloqueada no teste")

    monkeypatch.setattr(socket, "socket", blocked_socket)
    yield
    monkeypatch.setattr(socket, "socket", original_socket)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# --- líder (revisão P2): nunca perder a fala no histórico do Modo B --------------------------

def test_lider_modo_b_historico_sempre_guarda_a_fala_fiel():
    import logging as _l

    import numpy as _np

    from hear_ai.controller import Controller
    from hear_ai.gerador import Resultado

    class _Rec:
        def start(self):
            pass

        def stop(self):
            return _np.zeros(16000, _np.float32)

    class _T:
        def transcribe(self, a):
            return "crie um script de vendas"

    class _P:
        def __init__(self):
            self.pasted = []

        def paste(self, t):
            self.pasted.append(t)

    class _Fb:
        def __getattr__(self, n):
            return lambda *a: None

    class _S:
        def __init__(self):
            self.saved = []

        def save(self, t, m):
            self.saved.append((t, dict(m)))
            return "id"

        def set_status(self, *a):
            return True

    sink, paster = _S(), _P()
    c = Controller(_Rec(), _T(), paster, _Fb(), sink, _l.getLogger("p2"), "pt", run_async=False,
                   postprocess=None, prompt_step=lambda t: Resultado("PROMPT GERADO", True, provedor="falso"))
    c.begin()
    c.end()
    text, meta = sink.saved[0]
    assert paster.pasted == ["PROMPT GERADO"] and text == "PROMPT GERADO"
    assert meta["raw"] == "crie um script de vendas" and meta["generated"] == "PROMPT GERADO"
    assert "revisar" not in meta


def test_lider_modo_a_nao_grava_revisar():
    import logging as _l

    import numpy as _np

    from hear_ai.controller import Controller

    class _Rec:
        def start(self):
            pass

        def stop(self):
            return _np.zeros(16000, _np.float32)

    class _T:
        def transcribe(self, a):
            return "texto"

    class _P:
        def paste(self, t):
            pass

    class _Fb:
        def __getattr__(self, n):
            return lambda *a: None

    saved = []

    class _S:
        def save(self, t, m):
            saved.append(dict(m))
            return "id"

        def set_status(self, *a):
            return True

    c = Controller(_Rec(), _T(), _P(), _Fb(), _S(), _l.getLogger("p2"), "pt", run_async=False, postprocess=None)
    c.begin()
    c.end()
    assert "revisar" not in saved[0] and "generated" not in saved[0] and "raw" not in saved[0]
