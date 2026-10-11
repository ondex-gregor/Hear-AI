"""
Testes de diagnóstico para falhas conhecidas (T10).
Cada teste marca o comportamento CORRETO desejado com @pytest.mark.xfail(strict=True).
Hoje eles falham (provam o bug); quando o conserto chegar, passam e o strict obriga a remover a marca.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

import numpy as np
import pytest

from hear_ai.audio import Recorder
from hear_ai.controller import Controller, IDLE, RECORDING, TRANSCRIBING
from hear_ai.hotkey import wait_keys_released
from hear_ai.paster import Paster


# =============================================================================
# (a) Corte em 120 s
# =============================================================================

class LongAudioStream:
    """Stream falso que entrega 150 s de áudio em blocos de 1600 amostras (0.1 s cada)."""
    def __init__(self, blocks_per_second: int = 10, total_seconds: int = 150):
        self.blocks_per_second = blocks_per_second
        self.total_seconds = total_seconds
        self.total_blocks = blocks_per_second * total_seconds
        self.block_size = 16000 // blocks_per_second  # 1600 amostras = 0.1s a 16kHz
        self.callback = None
        self.started = False
        self.closed = False

    def __call__(self, **kwargs):
        self.callback = kwargs["callback"]
        return self

    def start(self):
        self.started = True
        for i in range(self.total_blocks):
            block = np.full(self.block_size, 0.1, dtype=np.float32)
            # callback recebe (indata, frames, time_info, status)
            # indata shape: (frames, channels) -> (1600, 1)
            self.callback(block.reshape(-1, 1), self.block_size, None, None)

    def stop(self):
        pass

    def close(self):
        self.closed = True


def test_corte_120s_comportamento_atual():
    """
    NÃO-xfail: documenta o comportamento ATUAL (bug).
    Recorder padrão max_seconds=120 ignora áudio após 120s.
    Nada é logado/avisado.
    """
    caplog = logging.getLogger("hear_ai.audio")
    caplog.setLevel(logging.DEBUG)

    stream = LongAudioStream(total_seconds=150)
    r = Recorder(stream_factory=stream, max_seconds=120)
    r.start()
    out = r.stop()

    # Bug: devolve 120s em vez de 150s
    expected_bug = 120 * 16000
    assert len(out) == expected_bug, f"esperado {expected_bug} (bug atual), got {len(out)}"

    # Verifica que nada foi logado sobre truncamento
    # (o logger não tem handler, mas podemos verificar se não houve warning no módulo)
    # Este teste apenas documenta o fato; a ausência de log é implícita.


def test_corte_120s_comportamento_correto():
    """
    XFAIL: comportamento CORRETO desejado.
    Recorder deve gravar todo o áudio que o stream entregar (até max_seconds).
    Se max_seconds=120, mas o stream entrega 150s, o callback para de gravar aos 120s.
    O teste prova que HOJE o limite é respeitado mas não avisado.
    O comportamento correto seria: ou gravar tudo (sem limite hardcoded) OU logar aviso.
    """
    # T17: usa o Recorder PADRÃO (limite de 30 min, T12). Com max_seconds=120 explícito o
    # limite vale de propósito (excedente ignorado com aviso no log), ver entregas/T12-agente1.md.
    stream = LongAudioStream(total_seconds=150)
    r = Recorder(stream_factory=stream)
    r.start()
    out = r.stop()

    # Comportamento correto desejado: 150s (sem limite artificial OU com aviso)
    expected_correct = 150 * 16000
    assert len(out) == expected_correct, f"esperado {expected_correct}, got {len(out)}"


# =============================================================================
# (b) Colagem de texto grande
# =============================================================================

class TimelineClipboard:
    """Clipboard falso que registra linha do tempo (t, valor) para cada set()."""
    def __init__(self):
        self._content = "conteúdo anterior"
        self.timeline: list[tuple[float, str]] = [(0.0, self._content)]
        self._clock = 0.0

    def set_clock(self, clock: float):
        self._clock = clock

    def get(self) -> str | None:
        return self._content

    def set(self, text: str) -> None:
        self._content = text
        self.timeline.append((self._clock, text))

    def value_at(self, t: float) -> str:
        """Valor vigente no tempo t."""
        # Procura o último set() com tempo <= t
        for ts, val in reversed(self.timeline):
            if ts <= t:
                return val
        return self.timeline[0][1]


class FakeSendPaste:
    """send_paste que registra quando foi chamado e qual o tempo."""
    def __init__(self, clipboard: TimelineClipboard):
        self.clipboard = clipboard
        self.called_at: float | None = None

    def __call__(self) -> None:
        self.called_at = self.clipboard._clock


def test_colagem_texto_grande_comportamento_atual():
    """
    NÃO-xfail: documenta o comportamento ATUAL (bug) com parâmetros antigos.
    Com base_s=0.15, chars_per_s=2500, max_s=3.0 (valores padrão antigos),
    texto de 5000 chars: delay = 0.15 + 5000/2500 = 2.15s.
    t_leitura = 0.05 + 5000/10000 = 0.55s.
    Como delay (2.15s) > t_leitura (0.55s), NÃO há bug com esses parâmetros.
    O bug original era com delay_s fixo = 0.15s (sem ajuste por tamanho).
    """
    clip = TimelineClipboard()
    send = FakeSendPaste(clip)

    def fake_sleep(delay: float):
        clip.set_clock(clip._clock + delay)

    # Parâmetros antigos (sem ajuste por tamanho): delay_s=0.15 fixo
    paster = Paster(clip, send, delay_s=0.15, chars_per_s=999999, max_s=0.15, sleep=fake_sleep)

    texto_5000 = "x" * 5000
    paster.paste(texto_5000)

    # Modelo: destino lê em t_leitura = 0.05 + len/10000
    t_leitura = 0.05 + len(texto_5000) / 10000  # 0.55s
    valor_lido = clip.value_at(t_leitura)

    # Bug com delay fixo 0.15s: destino lê o conteúdo antigo (restauração aconteceu antes)
    assert valor_lido == "conteúdo anterior", f"bug: destino leu '{valor_lido[:20]}...' em t={t_leitura}s"


def test_colagem_texto_grande_comportamento_correto_1500():
    """Com delay dinâmico, 1500 chars -> delay = 0.25 + 1500/2500 = 0.85s > 0.20s (t_leitura)."""
    clip = TimelineClipboard()
    send = FakeSendPaste(clip)

    def fake_sleep(delay: float):
        clip.set_clock(clip._clock + delay)

    # base_s=0.25, chars_per_s=2500, max_s=3.0
    paster = Paster(clip, send, delay_s=0.25, chars_per_s=2500, max_s=3.0, sleep=fake_sleep)

    texto_1500 = "y" * 1500
    result = paster.paste(texto_1500)

    assert result.pasted is True
    assert result.restored is True
    assert result.reason == "ok"

    t_leitura = 0.05 + len(texto_1500) / 10000  # 0.20s
    valor_lido = clip.value_at(t_leitura)

    assert valor_lido == texto_1500, f"esperado texto ditado, got '{valor_lido[:20]}...' em t={t_leitura}s"


def test_colagem_texto_grande_comportamento_correto_5000():
    """Com delay dinâmico, 5000 chars -> delay = 0.25 + 5000/2500 = 2.25s > 0.55s (t_leitura)."""
    clip = TimelineClipboard()
    send = FakeSendPaste(clip)

    def fake_sleep(delay: float):
        clip.set_clock(clip._clock + delay)

    # base_s=0.25, chars_per_s=2500, max_s=3.0
    paster = Paster(clip, send, delay_s=0.25, chars_per_s=2500, max_s=3.0, sleep=fake_sleep)

    texto_5000 = "z" * 5000
    result = paster.paste(texto_5000)

    assert result.pasted is True
    assert result.restored is True
    assert result.reason == "ok"

    t_leitura = 0.05 + len(texto_5000) / 10000  # 0.55s
    valor_lido = clip.value_at(t_leitura)

    assert valor_lido == texto_5000, f"esperado texto ditado, got '{valor_lido[:20]}...' em t={t_leitura}s"


# =============================================================================
# (c) Espera de 2 s pelas teclas
# =============================================================================

def test_espera_teclas_timeout_comportamento_atual():
    """
    NÃO-xfail: documenta que wait_keys_released devolve False após 2s (timeout).
    """
    clock = {"t": 0.0}
    held = {"val": True}

    def is_pressed(k):
        return held["val"]

    def fake_sleep(d):
        clock["t"] += d

    def fake_clock():
        return clock["t"]

    # Tecla pressionada por 2.5s, timeout=2.0s
    def held_for_2_5(k):
        return clock["t"] < 2.5

    result = wait_keys_released(
        ["ctrl", "alt", "space"],
        held_for_2_5,
        timeout=2.0,
        sleep=fake_sleep,
        clock=fake_clock,
        poll=0.02
    )

    assert result is False, "deve retornar False após timeout de 2s"
    # Tempo gasto deve ser ~2.0s
    assert 1.9 <= clock["t"] <= 2.1, f"tempo gasto {clock['t']}s != 2.0s"


class FakeKeyboard:
    """Keyboard falso para wait_keys_released."""
    def __init__(self, hold_duration: float):
        self.hold_duration = hold_duration
        self.clock = {"t": 0.0}

    def is_pressed(self, k: str) -> bool:
        return self.clock["t"] < self.hold_duration


class FakeTranscriber:
    def __init__(self, text="texto ditado"):
        self.text = text

    def transcribe(self, audio):
        return self.text


class FakeRecorder:
    def __init__(self, audio=None):
        self.audio = audio or np.zeros(16000, dtype=np.float32)  # 1s
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return self.audio


class FakePaster:
    def __init__(self):
        self.pasted = []
        self.clipboard = None  # será injetado depois

    def paste(self, text):
        self.pasted.append(text)


class FakeFeedback:
    def __init__(self):
        self.events = []

    def start(self): self.events.append("start")
    def stop(self): self.events.append("stop")
    def done(self): self.events.append("done")
    def error(self): self.events.append("error")


class FakeSink:
    def __init__(self):
        self.saved = []

    def save(self, text, meta):
        self.saved.append((text, meta))
        return f"id-{len(self.saved)}"  # como o HistoryStore (T15): devolve o id


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def make_controller(before_paste=None, run_async=False, **kw):
    log = logging.getLogger(f"teste.diag.{id(object())}")
    log.setLevel(logging.DEBUG)
    handler = ListHandler()
    log.addHandler(handler)

    parts = dict(
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        paster=FakePaster(),
        feedback=FakeFeedback(),
        sink=FakeSink(),
    )
    parts["paster"].clipboard = parts["paster"]  # dummy, não usado no _leave_in_clipboard
    c = Controller(
        recorder=parts["recorder"],
        transcriber=parts["transcriber"],
        paster=parts["paster"],
        feedback=parts["feedback"],
        sink=parts["sink"],
        logger=log,
        language="pt",
        run_async=run_async,
        before_paste=before_paste,
        **kw
    )
    return c, parts, handler


def test_espera_teclas_timeout_controller_salva_historico_e_avisa():
    """
    XFAIL: comportamento CORRETO desejado.
    Se before_paste() retorna False (timeout 2s), o Controller deve:
    1. Deixar texto no clipboard (já faz via _leave_in_clipboard)
    2. Salvar no histórico/sink (já faz via _leave_in_clipboard -> _save)
    3. Gerar um estado de aviso "texto salvo — Ctrl+V" via on_state_change
       Hoje NÃO existe esse estado: on_state_change só recebe idle/recording/transcribing.
    """
    states = []
    clock = {"t": 0.0}

    def fake_sleep(d):
        clock["t"] += d

    def fake_clock():
        return clock["t"]

    kb = FakeKeyboard(hold_duration=2.5)

    def before_paste():
        return wait_keys_released(
            ["ctrl", "alt", "space"],
            kb.is_pressed,
            timeout=2.0,
            sleep=fake_sleep,
            clock=fake_clock,
            poll=0.02
        )

    c, parts, handler = make_controller(before_paste=before_paste, run_async=False, on_state_change=states.append)

    c.begin()
    c.end()

    # Verificações do comportamento correto:
    assert parts["paster"].pasted == []  # NÃO colou
    assert parts["sink"].saved[0][0] == "texto ditado"  # salvou no histórico
    assert parts["feedback"].events[-1] == "error"  # beep de erro

    # NOVO: deve ter um estado de aviso "aviso" ou similar antes de voltar a idle
    # Hoje não existe: states termina em [RECORDING, TRANSCRIBING, IDLE]
    # Comportamento correto: deve incluir um estado "warning" ou similar
    # Como não existe, o teste falha (xfail) provando o bug
    # T17: o aviso chega como o evento "saved" ("texto salvo — Ctrl+V")
    warning_states = [s for s in states if s.lower() in ("saved",) or "aviso" in s.lower()
                      or "warning" in s.lower() or "salvo" in s.lower()]
    assert len(warning_states) >= 1, f"nenhum estado de aviso recebido: {states}"


def test_espera_teclas_timeout_tempo_gasto():
    """
    NÃO-xfail: verifica que o tempo gasto é ~2.0s quando estoura o timeout.
    """
    clock = {"t": 0.0}

    def fake_sleep(d):
        clock["t"] += d

    def fake_clock():
        return clock["t"]

    kb = FakeKeyboard(hold_duration=2.5)

    result = wait_keys_released(
        ["ctrl", "alt", "space"],
        kb.is_pressed,
        timeout=2.0,
        sleep=fake_sleep,
        clock=fake_clock,
        poll=0.02
    )

    assert result is False
    assert 1.9 <= clock["t"] <= 2.1, f"tempo gasto {clock['t']}s deveria ser ~2.0s"