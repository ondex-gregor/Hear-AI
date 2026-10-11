"""Máquina de estados pura para cliques na bolha (Python puro, sem Qt)."""

from enum import Enum
from dataclasses import dataclass, field
from typing import Literal


class Action(Enum):
    """Ações possíveis da máquina de cliques."""
    NONE = "none"
    START = "start"
    STOP = "stop"
    DISCARD_AND_OPEN = "discard_and_open"
    OPEN_RECENT = "open_recent"
    DRAG_START = "drag_start"
    DRAG_END = "drag_end"


# Estados internos da máquina
_InternalState = Literal[
    "idle",
    "waiting_release",
    "waiting_double_click",
    "after_double_click",
    "dragging",
]


@dataclass
class ClickMachine:
    """
    Máquina de estados para interpretar cliques na bolha.

    Args:
        double_click_ms: Intervalo máximo (ms) entre cliques para contar como duplo clique.
        drag_px: Distância mínima (Manhattan) para considerar arraste.
    """
    double_click_ms: int = 500
    drag_px: int = 4

    # Estado interno
    _state: _InternalState = field(default="idle", init=False)
    _press_pos: tuple[int, int] = field(default=(0, 0), init=False)
    _press_time: float = field(default=0.0, init=False)
    _first_click_action: Action = field(default=Action.NONE, init=False)
    _first_click_time: float | None = field(default=None, init=False)  # None = nenhum 1º clique pendente
    _drag_started: bool = field(default=False, init=False)
    _swallow_until: float = field(default=0.0, init=False)

    def press(self, pos: tuple[int, int], now: float, button: str = "left") -> Action:
        """
        Processa evento de pressionar botão do mouse.

        Args:
            pos: Posição (x, y) do cursor.
            now: Timestamp em segundos (relógio simulado nos testes).
            button: Botão pressionado ("left", "right", "middle").

        Returns:
            Action imediata (geralmente NONE no press, exceto se for botão não-esquerdo).
        """
        if button != "left":
            return Action.NONE

        self._press_pos = pos
        self._press_time = now
        self._drag_started = False

        if self._state == "idle":
            self._state = "waiting_release"
        elif self._state == "waiting_double_click":
            # Nova interação (pode ser clique ou arraste) durante janela de duplo clique.
            # Transiciona para waiting_release; o release decidirá se é duplo clique
            # baseado no tempo decorrido desde _first_click_time.
            self._state = "waiting_release"
        elif self._state == "after_double_click":
            # Clique após duplo clique - engolido
            self._state = "waiting_release"
        # Estados "dragging" e "waiting_release" não devem receber press sem release anterior

        return Action.NONE

    def move(self, pos: tuple[int, int], now: float) -> Action:
        """
        Processa evento de movimento do mouse.

        Args:
            pos: Posição atual (x, y) do cursor.
            now: Timestamp em segundos.

        Returns:
            DRAG_START na primeira vez que passa do limite de arraste, NONE depois.
        """
        if self._state != "waiting_release":
            return Action.NONE

        dx = abs(pos[0] - self._press_pos[0])
        dy = abs(pos[1] - self._press_pos[1])
        manhattan = dx + dy

        if manhattan >= self.drag_px and not self._drag_started:
            self._drag_started = True
            self._first_click_time = None  # arraste nunca entra na contagem de duplo clique
            self._state = "dragging"
            return Action.DRAG_START

        return Action.NONE

    def release(
        self,
        pos: tuple[int, int],
        now: float,
        app_state: str,
        button: str = "left"
    ) -> Action:
        """
        Processa evento de soltar botão do mouse.

        Args:
            pos: Posição (x, y) do cursor.
            now: Timestamp em segundos.
            app_state: Estado do app ("idle", "recording", "transcribing", ...).
            button: Botão solto ("left", "right", "middle").

        Returns:
            Action resultante do clique/arraste.
        """
        if button != "left":
            self._reset_to_idle()
            return Action.NONE

        if self._state == "dragging":
            self._state = "idle"
            self._drag_started = False
            return Action.DRAG_END

        if self._state not in ("waiting_release", "waiting_double_click"):
            self._reset_to_idle()
            return Action.NONE

        # Verifica se foi arraste (movimento >= drag_px)
        dx = abs(pos[0] - self._press_pos[0])
        dy = abs(pos[1] - self._press_pos[1])
        manhattan = dx + dy

        if manhattan >= self.drag_px:
            # Foi arraste, mas DRAG_START já foi emitido no move
            # Se não emitiu (ex: move não chamado), emite DRAG_END aqui
            if not self._drag_started:
                self._state = "idle"
                return Action.DRAG_END
            self._state = "idle"
            return Action.NONE

        # Foi um clique (movimento < drag_px)
        elapsed_ms = (now - self._press_time) * 1000

        # Determina ação baseada no estado do app
        if app_state == "idle":
            click_action = Action.START
        elif app_state == "recording":
            click_action = Action.STOP
        else:  # transcribing ou outros
            click_action = Action.NONE

        # Janela de engolir cliques após duplo clique (evita triplo clique)
        if now < self._swallow_until:
            # engole TODOS os cliques até o fim da janela (triplo/quádruplo clique acidental)
            self._state = "idle"
            return Action.NONE

        # Verifica se é duplo clique: houve clique anterior dentro do intervalo
        is_double_click = (
            self._first_click_time is not None
            and (now - self._first_click_time) * 1000 <= self.double_click_ms
        )

        if is_double_click:
            # É o segundo clique de um duplo clique
            if self._first_click_action == Action.START:
                action = Action.DISCARD_AND_OPEN
            elif self._first_click_action == Action.STOP:
                action = Action.OPEN_RECENT
            else:  # NONE
                action = Action.OPEN_RECENT

            self._state = "after_double_click"
            self._first_click_time = None
            # Janela de engolir cliques acidentais (triplo clique)
            self._swallow_until = now + self.double_click_ms / 1000.0
            return action

        # Primeiro clique (ou clique fora do intervalo de duplo clique)
        self._first_click_action = click_action
        self._first_click_time = now

        if click_action != Action.NONE:
            self._state = "waiting_double_click"
        else:
            self._state = "idle"

        return click_action

    def reset(self) -> None:
        """Reseta a máquina para estado inicial."""
        self._reset_to_idle()

    def _reset_to_idle(self) -> None:
        """Reseta estado interno para idle."""
        self._state = "idle"
        self._press_pos = (0, 0)
        self._press_time = 0.0
        self._first_click_action = Action.NONE
        self._first_click_time = None
        self._drag_started = False
        self._swallow_until = 0.0