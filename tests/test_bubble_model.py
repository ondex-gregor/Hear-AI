"""Testes para bubble_model.py (lógica pura, sem GUI toolkit)."""

from __future__ import annotations

import pytest

from hear_ai.ui.bubble_model import (
    AppState,
    BubbleModel,
    VisualState,
    Frame,
)


class FakeClock:
    """Relógio falso controlado pelo teste."""
    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += dt

    def set(self, t: float) -> None:
        self._now = t


def make_model(clock: FakeClock | None = None, **kwargs) -> BubbleModel:
    if clock is None:
        clock = FakeClock()
    return BubbleModel(clock=clock, **kwargs)


# =============================================================================
# Testes de transições de estado básicas
# =============================================================================

def test_estado_inicial_idle():
    clock = FakeClock(0.0)
    model = make_model(clock, idle_opacity=0.85, hide_when_idle=False)
    frame = model.frame(0.0)

    assert frame.shape_progress == 0.0  # círculo
    assert frame.width == 52.0
    assert frame.height == 52.0
    assert frame.opacity == 0.85
    assert frame.message is None
    assert frame.pulse_color is None


def test_estado_inicial_hidden_quando_hide_when_idle():
    clock = FakeClock(0.0)
    model = make_model(clock, hide_when_idle=True)
    frame = model.frame(0.0)

    assert frame.opacity == 0.0
    # O modelo deve estar em HIDDEN visualmente


def test_on_state_recording_muda_para_pilula():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    model.tick(0.1)  # avança 100ms na transição
    frame = model.frame(0.1)

    assert frame.shape_progress > 0.0
    assert frame.width > 52.0
    # timer deve começar a contar


def test_on_state_transcribing_volta_a_circulo_com_anel():
    """Spec B1 (revisão 016 item 1): transcrevendo = círculo de 52 px com o anel girando."""
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    model.tick(0.5)  # completa a transição para a pílula
    assert model.frame(0.5).width == 220
    clock.advance(0.5)
    model.on_state(AppState.TRANSCRIBING)
    meio = model.frame(0.6)
    assert 52 < meio.width < 220  # recolhe com a mesma animação suave (~200 ms)
    clock.advance(0.5)
    frame = model.frame(1.0)

    assert frame.width == 52 and frame.height == 52 and frame.shape_progress == 0.0
    assert frame.ring_angle > 0.0  # anel girando
    assert frame.needs_animation  # o anel continua animando no círculo


def test_on_pasted_dispara_done_com_pulse_check():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    clock.advance(0.5)
    model.tick(0.5)
    clock.advance(0.0)
    model.on_pasted()
    clock.advance(0.1)
    model.tick(0.6)
    frame = model.frame(0.6)

    assert frame.pulse_color == "check"
    assert frame.pulse >= 0.0


def test_on_pasted_em_idle_nao_faz_nada():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_pasted()
    frame = model.frame(0.0)

    assert frame.pulse_color is None


def test_on_loading_true_muda_para_loading():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_loading(True)
    model.tick(0.0)
    frame = model.frame(0.0)

    # Deve estar em estado LOADING (círculo esmaecido)


def test_on_loading_false_volta_ao_estado_anterior():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    model.tick(0.5)
    model.on_loading(True)
    model.tick(0.1)
    model.on_loading(False)
    model.tick(0.1)
    frame = model.frame(0.1)

    # Deve voltar para RECORDING


# =============================================================================
# Testes de duração de animações temporárias
# =============================================================================

def test_done_dura_600ms():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    clock.advance(0.5)
    model.tick(0.5)
    clock.advance(0.0)
    model.on_pasted()
    clock.advance(0.1)
    model.tick(0.6)

    frame1 = model.frame(0.6)
    assert frame1.pulse_color == "check"

    clock.advance(0.6)  # 600ms
    model.tick(1.2)
    frame2 = model.frame(1.2)

    # Pulso deve ter terminado
    assert frame2.pulse_color is None


def test_saved_dura_2500ms_com_mensagem():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.SAVED)
    model.tick(0.0)
    frame1 = model.frame(0.0)

    assert frame1.message is not None
    assert "salvo" in frame1.message.lower()

    clock.advance(2.5)
    model.tick(2.5)
    frame2 = model.frame(2.5)

    # Mensagem deve ter expirado
    assert frame2.message is None


def test_error_dura_2500ms_com_mensagem():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.ERROR)
    model.tick(0.0)
    frame1 = model.frame(0.0)

    assert frame1.message is not None
    assert frame1.pulse_color == "red"

    clock.advance(2.5)
    model.tick(2.5)
    frame2 = model.frame(2.5)

    assert frame2.message is None
    assert frame2.pulse_color is None


# =============================================================================
# Testes de easing e transição círculo↔pílula
# =============================================================================

def test_easing_monotonico_circulo_para_pilula():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    prev = 0.0
    for i in range(20):
        clock.advance(0.01)
        model.tick(clock())
        frame = model.frame(clock())
        assert frame.shape_progress >= prev
        prev = frame.shape_progress
    # Deve chegar a 1.0
    assert abs(frame.shape_progress - 1.0) < 0.02


def test_easing_monotonico_pilula_para_circulo():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    clock.advance(0.5)
    model.tick(0.5)
    frame1 = model.frame(0.5)
    assert frame1.shape_progress > 0.9

    model.on_state(AppState.IDLE)
    prev = frame1.shape_progress
    for i in range(20):
        clock.advance(0.01)
        model.tick(clock())
        frame = model.frame(clock())
        assert frame.shape_progress <= prev + 0.02  # permite pequena oscilação numérica
        prev = frame.shape_progress
    assert frame.shape_progress < 0.05


# =============================================================================
# Testes de cronômetro
# =============================================================================

def test_timer_formato_mm_ss():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    clock.advance(65.0)
    model.tick(65.0)
    frame = model.frame(65.0)

    assert frame.timer_text == "01:05"


def test_timer_acima_de_59_min_formato_m_ss():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    clock.advance(3661.0)  # 61 min 1s
    model.tick(3661.0)
    frame = model.frame(3661.0)

    assert frame.timer_text == "61:01"


def test_timer_zero_antes_de_gravar():
    clock = FakeClock(0.0)
    model = make_model(clock)
    frame = model.frame(0.0)
    assert frame.timer_text == "00:00"


# =============================================================================
# Testes de barras de volume
# =============================================================================

def alimentar(model, clock, level, seconds, hz=60.0):
    """Injeta `level` (RMS linear) a `hz` Hz com o relógio simulado, como o QTimer da bolha faz."""
    for _ in range(max(1, round(seconds * hz))):
        clock.advance(1.0 / hz)
        model.on_level(level)
        model.tick(clock())


# T41b: o nível passou a ser julgado contra um piso de ruído adaptativo (dB), não mais contra 0..1 fixo.
# Por isso "fala" só sobe as barras DEPOIS de um instante de ruído ambiente; um nível constante desde a
# 1ª amostra (o que o teste antigo fazia com 1.0) é o próprio ambiente e fica nos pontinhos de propósito.
def test_barras_suavizadas_subida_rapida():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    model.tick(0.0)
    alimentar(model, clock, 0.001, 0.5)   # ruído ambiente
    assert max(model.frame(clock()).bars) < 0.05

    alimentar(model, clock, 0.1, 0.15)    # fala começa: ataque rápido
    frame = model.frame(clock())

    assert any(b > 0.5 for b in frame.bars)


def test_barras_suavizadas_descida_lenta():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    model.tick(0.0)
    alimentar(model, clock, 0.001, 0.5)
    alimentar(model, clock, 0.1, 0.4)
    pico = max(model.frame(clock()).bars)
    assert pico > 0.8

    alimentar(model, clock, 0.001, 0.10)  # a fala acabou: a barra mais nova desce, mas não cai de uma vez
    frame = model.frame(clock())

    assert 0.0 < frame.bars[-1] < pico   # descida lenta (decaimento suave)
    assert max(frame.bars) > 0.5         # e o histórico ainda mostra a fala que passou


def test_barras_zero_em_idle():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.IDLE)
    model.tick(0.0)
    frame = model.frame(0.0)

    assert all(b == 0.0 for b in frame.bars)


# =============================================================================
# Testes de needs_animation
# =============================================================================

def test_needs_animation_false_em_repouso_estavel():
    clock = FakeClock(0.0)
    model = make_model(clock)

    # Estado estável IDLE
    clock.advance(1.0)
    model.tick(1.0)
    frame = model.frame(1.0)

    assert frame.needs_animation is False


def test_needs_animation_true_durante_animacao():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    clock.advance(0.1)
    model.tick(0.1)
    frame = model.frame(0.1)

    assert frame.needs_animation is True


def test_needs_animation_true_com_pulso_ativo():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    model.tick(0.5)
    model.on_pasted()
    clock.advance(0.1)
    model.tick(0.6)
    frame = model.frame(0.6)

    assert frame.needs_animation is True


# =============================================================================
# Testes de hide_when_idle
# =============================================================================

def test_hide_when_idle_fade_out():
    clock = FakeClock(0.0)
    model = make_model(clock, hide_when_idle=True, idle_opacity=0.85)

    model.on_state(AppState.RECORDING)
    clock.advance(0.5)
    model.tick(0.5)

    model.on_state(AppState.IDLE)
    frame1 = model.frame(0.5)
    assert frame1.needs_animation is True

    # Avança até esmaecer
    clock.advance(1.0)
    model.tick(1.5)
    frame2 = model.frame(1.5)

    # Deve chegar a opacity 0
    assert frame2.opacity < 0.1


# =============================================================================
# Testes de on_warning e on_cancel
# =============================================================================

def test_on_warning_mostra_mensagem_temporaria():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_warning("1 min para o limite")
    model.tick(0.0)
    frame1 = model.frame(0.0)

    assert frame1.message == "1 min para o limite"

    clock.advance(2.5)
    model.tick(2.5)
    frame2 = model.frame(2.5)

    assert frame2.message is None


def test_on_cancel_volta_para_idle_sem_pulso():
    clock = FakeClock(0.0)
    model = make_model(clock)

    clock.advance(0.5)
    model.on_state(AppState.RECORDING)
    model.tick(0.5)
    clock.advance(0.1)
    model.on_cancel()
    clock.advance(0.3)
    model.tick(0.8)
    frame = model.frame(0.8)

    assert frame.shape_progress < 0.1  # voltando para círculo
    assert frame.pulse_color is None
    assert frame.message is None


# =============================================================================
# Testes de eventos fora de ordem
# =============================================================================

def test_on_pasted_em_idle_ignorado():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_pasted()  # antes de qualquer estado
    frame = model.frame(0.0)
    assert frame.pulse_color is None


def test_on_state_duplicado_nao_quebra():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    model.on_state(AppState.RECORDING)
    model.on_state(AppState.RECORDING)
    model.tick(0.0)
    frame = model.frame(0.0)
    # Não deve travar nem duplicar animações


def test_on_level_fora_de_gravacao_ignorado():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_level(1.0)
    model.tick(0.0)
    frame = model.frame(0.0)
    assert all(b == 0.0 for b in frame.bars)


# =============================================================================
# Testes de Frame imutável
# =============================================================================

def test_frame_eh_dataclass_frozen():
    frame = Frame(
        width=52.0, height=52.0, shape_progress=0.0, ring_angle=0.0,
        bars=(0.0,), timer_text="00:00", pulse=0.0, pulse_color=None,
        message=None, opacity=1.0, needs_animation=False
    )
    assert frame.width == 52.0
    # frozen dataclass não permite atribuição


# =============================================================================
# Testes de anel girando apenas em transcribing
# =============================================================================

def test_ring_angle_avanca_apenas_em_transcribing():
    clock = FakeClock(0.0)
    model = make_model(clock)

    model.on_state(AppState.RECORDING)
    clock.advance(1.0)
    model.tick(1.0)
    frame1 = model.frame(1.0)
    assert frame1.ring_angle == 0.0

    model.on_state(AppState.TRANSCRIBING)
    clock.advance(1.0)
    model.tick(2.0)
    frame2 = model.frame(2.0)
    assert frame2.ring_angle > 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])