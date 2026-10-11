"""
Modelo da bolha de estado (lógica pura, sem GUI toolkit).

A janela real (T21) importa este modelo, injeta o relógio e desenha
conforme o `Frame` retornado por `frame(now)`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from hear_ai.ui.nivel import VisualizadorNivel


# Estados do Controller (entrada via on_state)
class AppState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    GENERATING = "generating"  # T47b: Modo B, gerando o prompt depois de transcrever
    SAVED = "saved"
    ERROR = "error"


# Estados visuais da bolha (internos)
class VisualState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    GENERATING = "generating"
    DONE = "done"
    SAVED = "saved"
    ERROR = "error"
    LOADING = "loading"
    HIDDEN = "hidden"


@dataclass(frozen=True)
class Frame:
    """Snapshot imutável do que desenhar no instante `now`."""
    width: float
    height: float
    shape_progress: float          # 0 = círculo, 1 = pílula (ease-in-out cúbico)
    ring_angle: float              # graus; só avança em transcribing
    bars: tuple[float, ...]        # N alturas 0..1: histórico rolante da onda (mais antiga à esquerda)
    timer_text: str                # "mm:ss" desde início da gravação
    pulse: float                   # 0..1 (animação de pulso)
    pulse_color: Literal["check", "red"] | None
    message: str | None
    opacity: float
    needs_animation: bool
    badge: str | None = None       # T47b: "prompt" = selo do Modo B (gerador de prompts); None = Modo A
    cloud: bool = False            # T47b: o gerador é de nuvem (cada uso envia TEXTO para fora)


class BubbleModel:
    """
    Máquina de estados visuais para a bolha flutuante.

    Recebe eventos do app e um relógio injetável (para testes);
    expõe `frame(now) -> Frame` com o que desenhar.
    """

    # Constantes de animação
    CIRCLE_W = 52
    CIRCLE_H = 52
    PILL_W = 220
    PILL_H = 52
    EASE_DURATION = 0.2       # 200 ms transição círculo↔pílula
    IDLE_OPACITY = 0.85
    DONE_DURATION = 0.6       # ~600 ms
    SAVED_DURATION = 2.5      # ~2.5 s
    ERROR_DURATION = 2.5      # ~2.5 s
    RING_SPEED = 180.0        # graus por segundo (meia volta/s)
    BAR_DECAY = 2.0           # fora da gravação as barras caem a zero a essa taxa (s⁻¹)
    PULSE_DURATION = 0.5      # duração do pulso
    N_BARS = 18               # cabem na pílula (~102 px úteis => ~5,7 px por barra)
    WAVE_HZ = 30.0            # amostras da onda por segundo (T41b; ver docs/ONDA.md)
    WAVE_WARMUP_S = 0.25      # nível exatamente 0.0 nesse início = "1º bloco do microfone ainda não chegou"

    def __init__(
        self,
        *,
        clock,                          # callable() -> float (segundos)
        idle_opacity: float = IDLE_OPACITY,
        hide_when_idle: bool = False,
        ring_speed: float = RING_SPEED,
        bar_decay: float = BAR_DECAY,
        wave: VisualizadorNivel | None = None,
    ) -> None:
        self._clock = clock
        self._idle_opacity = idle_opacity
        self._hide_when_idle = hide_when_idle
        self._ring_speed = ring_speed
        self._bar_decay = bar_decay

        # Estado interno
        self._visual: VisualState = VisualState.HIDDEN if hide_when_idle else VisualState.IDLE
        self._app_state: AppState | None = None
        self._loading = False

        # Timers e animações
        self._state_enter_time: float = self._clock()
        self._shape_target = 0.0          # 0=círculo, 1=pílula
        self._shape_progress = 0.0
        self._shape_transition_start: float = 0.0
        self._shape_transition_start_progress: float = 0.0

        self._ring_angle = 0.0
        # T41b: a onda vem do VisualizadorNivel (dB + piso adaptativo + histórico rolante); `_bars` é o que o
        # Frame mostra (= o histórico durante a gravação, decaindo a zero fora dela)
        self._wave = wave or VisualizadorNivel(n_barras=self.N_BARS, taxa_hz=self.WAVE_HZ)
        self._bars = [0.0] * len(self._wave.barras())
        self._wave_t0: float = 0.0      # relógio no início da gravação: o visualizador recebe tempo relativo
        self._wave_heard = False        # já chegou algum nível > 0 nesta gravação?
        self._last_bar_time: float = 0.0

        self._rec_start_time: float | None = None
        self._pulse_end: float = 0.0
        self._pulse_color: Literal["check", "red"] | None = None

        self._message: str | None = None
        self._message_end: float = 0.0
        self._badge: str | None = None
        self._cloud = False
        self._notice_pill = False  # aviso temporário em repouso: a bolha vira pílula só enquanto ele dura

        self._opacity = idle_opacity if not hide_when_idle else 0.0
        self._opacity_target = self._opacity

        self._needs_animation = False

    # ---------- API de eventos ----------

    def on_state(self, state: AppState) -> None:
        """Recebe mudança de estado do Controller."""
        now = self._clock()
        self._app_state = state

        if state == AppState.IDLE:
            self._set_visual(VisualState.IDLE)
            if self._shape_target != 0.0:
                self._shape_target = 0.0
                self._shape_transition_start = now
                self._shape_transition_start_progress = self._shape_progress
            self._rec_start_time = None
        elif state == AppState.RECORDING:
            if self._visual != VisualState.RECORDING:
                self._start_wave(now)  # gravação nova começa limpa: sem sobras da anterior
            self._set_visual(VisualState.RECORDING)
            if self._shape_target != 1.0:
                self._shape_target = 1.0
                self._shape_transition_start = now
                self._shape_transition_start_progress = self._shape_progress
            if self._rec_start_time is None:
                self._rec_start_time = now
        elif state == AppState.TRANSCRIBING:
            # spec B1: "Transcrevendo: volta a círculo, com um anel azul girando ao redor do isotipo"
            self._set_visual(VisualState.TRANSCRIBING)
            if self._shape_target != 0.0:
                self._shape_target = 0.0
                self._shape_transition_start = now
                self._shape_transition_start_progress = self._shape_progress
        elif state == AppState.GENERATING:
            # T47b: Modo B — "Gerando prompt…" na pílula, com o anel girando no isotipo
            self._set_visual(VisualState.GENERATING)
            if self._shape_target != 1.0:
                self._shape_target = 1.0
                self._shape_transition_start = now
                self._shape_transition_start_progress = self._shape_progress
        elif state == AppState.SAVED:
            self._set_visual(VisualState.SAVED)
            if self._shape_target != 1.0:
                self._shape_target = 1.0
                self._shape_transition_start = now
                self._shape_transition_start_progress = self._shape_progress
            self._message = "Texto salvo — Ctrl+V"
            self._message_end = now + self.SAVED_DURATION
        elif state == AppState.ERROR:
            self._set_visual(VisualState.ERROR)
            if self._shape_target != 1.0:
                self._shape_target = 1.0
                self._shape_transition_start = now
                self._shape_transition_start_progress = self._shape_progress
            self._message = "Erro na ditado"
            self._message_end = now + self.ERROR_DURATION
            self._pulse_color = "red"
            self._pulse_end = now + self.PULSE_DURATION

    def on_pasted(self) -> None:
        """Chamado após colagem bem-sucedida (→ done)."""
        now = self._now()
        if self._visual in (VisualState.RECORDING, VisualState.TRANSCRIBING, VisualState.GENERATING,
                            VisualState.SAVED):
            self._set_visual(VisualState.DONE)
            self._pulse_color = "check"
            self._pulse_end = now + self.DONE_DURATION
            self._needs_animation = True

    def on_loading(self, loading: bool) -> None:
        """Modelo sendo carregado (true) ou pronto (false)."""
        now = self._now()
        if loading:
            self._loading = True
            self._set_visual(VisualState.LOADING)
        else:
            self._loading = False
            if self._app_state == AppState.IDLE:
                self._set_visual(VisualState.IDLE)
            elif self._app_state == AppState.RECORDING:
                self._set_visual(VisualState.RECORDING)
            elif self._app_state == AppState.TRANSCRIBING:
                self._set_visual(VisualState.TRANSCRIBING)
            elif self._app_state == AppState.GENERATING:
                self._set_visual(VisualState.GENERATING)

    def set_operation(self, prompt: bool, cloud: bool = False) -> None:
        """T47b: selo do Modo B (e, na nuvem, a cor de aviso). O Modo A não tem selo."""
        self._badge = "prompt" if prompt else None
        self._cloud = bool(cloud and prompt)
        self._needs_animation = True

    def on_notice(self, text: str, duration: float = 1.5) -> None:
        """T47b: aviso curto mesmo em repouso (ex.: "Modo: Gerador de prompts"): a bolha vira pílula só enquanto
        a mensagem dura e volta a círculo."""
        now = self._now()
        self._message = text
        self._message_end = now + duration
        if self._visual in (VisualState.IDLE, VisualState.HIDDEN):
            self._notice_pill = True
            if self._shape_target != 1.0:
                self._shape_target = 1.0
                self._shape_transition_start = now
                self._shape_transition_start_progress = self._shape_progress
        self._needs_animation = True

    def on_level(self, level: float) -> None:
        """Nível RMS linear do microfone (0..1), amostrado pela interface (QTimer). Só vale gravando."""
        if self._visual != VisualState.RECORDING:
            return
        try:
            rms = float(level)
        except (TypeError, ValueError):
            rms = 0.0
        t = max(0.0, self._clock() - self._wave_t0)
        if rms > 0.0:
            self._wave_heard = True
        elif not self._wave_heard and t < self.WAVE_WARMUP_S:
            # Recorder.level() vale 0.0 até o 1º bloco chegar; tratar isso como silêncio medido fixaria o piso
            # em -114 dB e encheria as barras com o ruído normal da sala no começo de toda gravação
            return
        self._wave.amostra(rms, t)

    def _start_wave(self, now: float) -> None:
        self._wave.reset()
        self._wave_t0 = now
        self._wave_heard = False
        self._bars = [0.0] * len(self._bars)

    def _now(self) -> float:
        """Retorna o tempo do último tick (ou clock se nenhum tick ainda)."""
        return getattr(self, "_last_tick_time", self._clock())

    def on_warning(self, text: str, duration: float | None = None) -> None:
        """Aviso temporário (ex.: '1 min para o limite')."""
        now = self._now()
        self._message = text
        self._message_end = now + (duration if duration is not None else self.SAVED_DURATION)

    def on_cancel(self) -> None:
        """Cancelamento (Esc) → volta a idle sem pulso."""
        now = self._now()
        self._set_visual(VisualState.IDLE)
        self._shape_target = 0.0
        self._shape_transition_start = now
        self._shape_transition_start_progress = self._shape_progress
        self._rec_start_time = None
        self._message = None
        self._pulse_color = None
        self._pulse_end = 0.0

    def tick(self, now: float) -> None:
        """Avança animações (chamado periodicamente, ex.: 60 Hz)."""
        self._last_tick_time = now
        self._animate_shape(now)
        self._animate_bars(now)
        self._animate_ring(now)
        self._animate_pulse(now)
        self._animate_message(now)
        self._animate_opacity(now)
        self._update_needs_animation()

    def frame(self, now: float) -> Frame:
        """Retorna snapshot imutável para desenho."""
        self.tick(now)
        return Frame(
            width=self.CIRCLE_W + (self.PILL_W - self.CIRCLE_W) * self._shape_progress,
            height=self.CIRCLE_H,
            shape_progress=self._shape_progress,
            ring_angle=self._ring_angle,
            bars=tuple(self._bars),
            timer_text=self._format_timer(now),
            pulse=self._pulse_progress(now),
            pulse_color=self._pulse_color,
            message=self._message,
            opacity=self._opacity,
            needs_animation=self._needs_animation,
            badge=self._badge,
            cloud=self._cloud,
        )

    # ---------- Internos ----------

    def _set_visual(self, visual: VisualState) -> None:
        if self._visual == visual:
            return
        self._visual = visual
        self._state_enter_time = self._clock()
        # Atualiza target de opacity baseado no novo estado visual
        if self._hide_when_idle:
            if visual == VisualState.IDLE:
                self._opacity_target = 0.0
            else:
                self._opacity_target = self._idle_opacity
        self._needs_animation = True

    def _animate_shape(self, now: float) -> None:
        if self._shape_progress == self._shape_target:
            return
        if not hasattr(self, "_shape_transition_start"):
            self._shape_transition_start = now
            self._shape_transition_start_progress = self._shape_progress
        # t = tempo decorrido / duração total
        t = (now - self._shape_transition_start) / self.EASE_DURATION
        if t >= 1.0:
            self._shape_progress = self._shape_target
            self._shape_transition_start = now
        else:
            start_progress = self._shape_transition_start_progress
            target = self._shape_target
            eased_t = self._ease_in_out_cubic(t)
            self._shape_progress = start_progress + (target - start_progress) * eased_t
        self._needs_animation = True

    @staticmethod
    def _ease_in_out_cubic(t: float) -> float:
        t = max(0.0, min(1.0, t))
        return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2)**3 / 2

    def _animate_bars(self, now: float) -> None:
        dt = now - self._last_bar_time
        self._last_bar_time = now
        if dt <= 0:
            dt = 1.0 / 60.0
        if self._visual == VisualState.RECORDING:
            self._bars = list(self._wave.barras())  # cada barra = uma amostra do histórico
        else:
            drop = self._bar_decay * dt
            self._bars = [max(0.0, b - drop) for b in self._bars]
        if any(b > 0.001 for b in self._bars):
            self._needs_animation = True

    def _animate_ring(self, now: float) -> None:
        if self._visual in (VisualState.TRANSCRIBING, VisualState.GENERATING):
            dt = now - self._state_enter_time
            self._ring_angle = (dt * self._ring_speed) % 360.0
            self._needs_animation = True
        else:
            self._ring_angle = 0.0

    def _animate_pulse(self, now: float) -> None:
        if self._pulse_color is not None and now < self._pulse_end:
            self._needs_animation = True
        elif self._pulse_color is not None:
            self._pulse_color = None
            self._pulse_end = 0.0

    def _pulse_progress(self, now: float) -> float:
        if self._pulse_color is None:
            return 0.0
        if now >= self._pulse_end:
            return 1.0
        pulse_start = self._pulse_end - self.PULSE_DURATION
        progress = (now - pulse_start) / self.PULSE_DURATION
        return max(0.0, min(1.0, progress))

    def _animate_message(self, now: float) -> None:
        if self._message and now >= self._message_end:
            self._message = None
            self._message_end = 0.0
            self._needs_animation = True
            if self._notice_pill:  # o aviso em repouso acabou: a pílula volta a ser círculo
                self._notice_pill = False
                if self._visual in (VisualState.IDLE, VisualState.HIDDEN) and self._shape_target != 0.0:
                    self._shape_target = 0.0
                    self._shape_transition_start = now
                    self._shape_transition_start_progress = self._shape_progress

    def _animate_opacity(self, now: float) -> None:
        if self._opacity == self._opacity_target:
            return
        if not hasattr(self, "_last_opacity_time"):
            self._last_opacity_time = now
        dt = now - self._last_opacity_time
        self._last_opacity_time = now
        if dt <= 0:
            dt = 1.0 / 60.0
        if self._opacity < self._opacity_target:
            self._opacity = min(self._opacity_target, self._opacity + 2.0 * dt)
        else:
            self._opacity = max(self._opacity_target, self._opacity - 2.0 * dt)
        self._needs_animation = True

    def _update_needs_animation(self) -> None:
        animating = (
            self._shape_progress != self._shape_target
            or self._visual in (VisualState.TRANSCRIBING, VisualState.GENERATING)
            or self._pulse_color is not None
            or self._message is not None
            or any(b > 0.001 for b in self._bars)
            or self._opacity != self._opacity_target
        )
        self._needs_animation = animating

        if not animating and self._hide_when_idle and self._visual == VisualState.IDLE:
            self._opacity_target = 0.0
            if self._opacity > 0.0:
                self._needs_animation = True
            else:
                self._visual = VisualState.HIDDEN
        elif self._hide_when_idle and self._visual != VisualState.IDLE:
            self._opacity_target = self._idle_opacity

    def _format_timer(self, now: float) -> str:
        if self._rec_start_time is None:
            return "00:00"
        elapsed = now - self._rec_start_time
        if elapsed < 0:
            return "00:00"
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        if mins >= 60:
            return f"{mins}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"