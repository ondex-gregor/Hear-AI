"""Vigia da bolha (T40): "a bolha que some do nada".

Sem Qt neste arquivo (só `ctypes` no adaptador Win32, importado sob demanda): a decisão e o
registro de diagnóstico são testáveis em qualquer lugar; quem monta a leitura da janela e aplica as
correções é o `QtUi` (ui/qt_app.py).

- `BubbleProbe`: retrato da bolha num instante (visível? opaca? dentro de alguma tela? topmost?).
- `decide(probe, should_be_visible)`: lista PURA das correções necessárias.
- `DiagLog`: `logs/bolha-diag.log` (rotativo, 1 MB x 3 arquivos). SÓ estados/geometria/eventos:
  os valores passam por um filtro que aceita números, booleanos e identificadores curtos —
  texto ditado não tem como entrar.
- `Win32Topmost`: lê/reafirma o estilo topmost da janela SEM nunca ativá-la (SWP_NOACTIVATE).
- `classify_native_message`: reconhece as mensagens do Windows que podem derrubar a bolha
  (troca de tela, bloqueio/desbloqueio, suspensão/retomada, Explorer reiniciado).
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from hear_ai.ui.bubble import PAD, WIN_H, WIN_W

# --- correções possíveis ------------------------------------------------------------------
ACTION_SHOW = "show"  # janela escondida (Qt ou Win32) -> show()
ACTION_OPACITY = "opacity"  # opacidade da janela baixou -> 1.0
ACTION_REPOSITION = "reposition"  # fora de todas as telas -> posição salva/padrão
ACTION_TOPMOST = "topmost"  # perdeu o topmost (ou sumiu no Win32) -> SetWindowPos(HWND_TOPMOST)
ACTIONS = (ACTION_SHOW, ACTION_OPACITY, ACTION_REPOSITION, ACTION_TOPMOST)

OPACITY_OK = 0.98  # o app nunca muda a opacidade da JANELA (a do repouso é pintada); abaixo disso é "de fora"
REACHABLE_FRACTION = 0.5  # a forma precisa ter ao menos metade sobre alguma tela
IDLE_VISUALS = ("idle", "hidden")

DIAG_LOG_NAME = "bolha-diag.log"
DIAG_MAX_BYTES = 1_000_000
DIAG_BACKUPS = 2  # 1 arquivo ativo + 2 antigos = 3 arquivos de até 1 MB
DIAG_PERIOD_S = 5.0


@dataclass(frozen=True)
class BubbleProbe:
    visible: bool  # QWidget.isVisible()
    opacity: float  # QWidget.windowOpacity()
    x: int
    y: int
    inside_screen: bool  # a forma da bolha (>= 50%) está sobre alguma tela
    screen_name: str | None
    topmost: bool | None  # WS_EX_TOPMOST; None = não dá para saber (fora do Windows)
    visual: str  # estado visual: idle / recording / transcribing / ...
    hide_when_idle: bool
    native_visible: bool | None = None  # IsWindowVisible (o Qt pode achar que está visível e o Win32 não)
    z_above: int | None = None  # quantas janelas estão acima na ordem Z (limitado)
    frame_opacity: float | None = None  # opacidade PINTADA pelo modelo (repouso 0,85)
    hwnd_valid: bool | None = None  # T35: False = a janela nativa sumiu (HWND inválido); None = sem Win32


def hidden_by_design(probe: BubbleProbe) -> bool:
    """`bubble_hide_when_idle` ligado e em repouso: esconder é o comportamento pedido."""
    return probe.hide_when_idle and probe.visual in IDLE_VISUALS


def decide(probe: BubbleProbe, should_be_visible: bool = True) -> list[str]:
    """Correções necessárias, na ordem em que devem ser aplicadas (lista vazia = tudo certo).

    Pura: não olha o relógio nem o Qt, só o retrato recebido."""
    if not should_be_visible or hidden_by_design(probe):
        return []
    actions: list[str] = []
    if not probe.visible or probe.hwnd_valid is False:
        actions.append(ACTION_SHOW)  # T35: HWND inválido também se recupera mostrando de novo
    if probe.opacity < OPACITY_OK:
        actions.append(ACTION_OPACITY)
    if not probe.inside_screen:
        actions.append(ACTION_REPOSITION)
    if probe.topmost is False or probe.native_visible is False or probe.hwnd_valid is False:
        actions.append(ACTION_TOPMOST)
    return actions


# --- geometria (sem Qt) --------------------------------------------------------------------

def shape_fraction_on_screens(pos, screens, win_w: int = WIN_W, win_h: int = WIN_H) -> float:
    """Fração (0..1) da FORMA da bolha (sem a margem transparente) que cai sobre as telas.

    `screens`: [(nome, (x, y, w, h))] com a geometria COMPLETA da tela (inclui a barra de tarefas).
    A janela que alguém arrastou para perto da barra continua "alcançável": só some de verdade
    quem ficou fora das telas."""
    sx, sy = pos[0] + PAD, pos[1] + PAD
    sw, sh = win_w - 2 * PAD, win_h - 2 * PAD
    area = sw * sh
    if area <= 0:
        return 0.0
    covered = 0
    for _name, (ax, ay, aw, ah) in screens:
        ix = min(sx + sw, ax + aw) - max(sx, ax)
        iy = min(sy + sh, ay + ah) - max(sy, ay)
        if ix > 0 and iy > 0:
            covered += ix * iy
    return min(1.0, covered / area)


def reachable(pos, screens, win_w: int = WIN_W, win_h: int = WIN_H) -> bool:
    return shape_fraction_on_screens(pos, screens, win_w, win_h) >= REACHABLE_FRACTION


def screen_at(point, screens) -> str | None:
    """Nome da tela que contém `point` (None se o ponto está fora de todas)."""
    px, py = point
    for name, (x, y, w, h) in screens:
        if x <= px < x + w and y <= py < y + h:
            return name
    return None


# --- mensagens nativas do Windows (sem Qt) ---------------------------------------------------
WM_DISPLAYCHANGE = 0x007E
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WTS_CONSOLE_CONNECT = 0x1
WTS_CONSOLE_DISCONNECT = 0x2
WTS_REMOTE_CONNECT = 0x3
WTS_REMOTE_DISCONNECT = 0x4
WTS_SESSION_LOGON = 0x5
WTS_SESSION_LOGOFF = 0x6
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0

_SESSION_EVENTS = {
    WTS_CONSOLE_CONNECT: "session_console_connect",
    WTS_CONSOLE_DISCONNECT: "session_console_disconnect",
    WTS_REMOTE_CONNECT: "session_remote_connect",
    WTS_REMOTE_DISCONNECT: "session_remote_disconnect",
    WTS_SESSION_LOGON: "session_logon",
    WTS_SESSION_LOGOFF: "session_logoff",
    WTS_SESSION_LOCK: "session_lock",
    WTS_SESSION_UNLOCK: "session_unlock",
}
_POWER_EVENTS = {
    PBT_APMSUSPEND: "power_suspend",
    PBT_APMRESUMESUSPEND: "power_resume",
    PBT_APMRESUMEAUTOMATIC: "power_resume",
}
WATCHED_MESSAGES = frozenset({WM_DISPLAYCHANGE, WM_POWERBROADCAST, WM_WTSSESSION_CHANGE})


def classify_native_message(message: int, wparam: int, taskbar_created: int = 0) -> str | None:
    """Nome do evento (para o log e para a reafirmação) ou None se a mensagem não interessa."""
    if message == WM_DISPLAYCHANGE:
        return "display_change"
    if message == WM_WTSSESSION_CHANGE:
        return _SESSION_EVENTS.get(wparam, "session_other")
    if message == WM_POWERBROADCAST:
        return _POWER_EVENTS.get(wparam)
    if taskbar_created and message == taskbar_created:
        return "taskbar_created"  # o Explorer reiniciou: a ordem Z dos topmost foi refeita
    return None


class Debouncer:
    """Junta vários pedidos em UMA reafirmação, `delay_ms` depois do ÚLTIMO pedido (T35, revisão 021),
    mas nunca mais que `max_wait_ms` depois do primeiro (uma rajada longa não adia para sempre).

    O Windows entrega WM_DISPLAYCHANGE a cada janela de topo; sem isto seriam várias reafirmações
    seguidas para o mesmo evento. `schedule(ms, fn)` é injetável (QTimer.singleShot no app); cada
    pedido agenda um disparo, e só o mais recente vale (os outros são ignorados por ficha)."""

    def __init__(self, schedule, delay_ms: int, action, max_wait_ms: int = 3000, clock=None):
        import time as _time

        self._schedule = schedule
        self._delay_ms = delay_ms
        self._max_wait_ms = max(delay_ms, max_wait_ms)
        self._action = action
        self._clock = clock or _time.monotonic
        self._pending: list[str] = []
        self._first_t = 0.0
        self._token = 0

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(self._pending)

    def request(self, reason: str) -> bool:
        """True se este pedido agendou a reafirmação (False = juntou-se a uma já agendada)."""
        first = not self._pending
        now = self._clock()
        if first:
            self._first_t = now
        if reason not in self._pending:
            self._pending.append(reason)
        self._token += 1
        token = self._token
        left_ms = self._max_wait_ms - (now - self._first_t) * 1000
        self._schedule(int(max(0, min(self._delay_ms, left_ms))), lambda: self._fire(token))
        return first

    def _fire(self, token: int | None = None) -> None:
        if token is not None and token != self._token:
            return  # chegou outro pedido depois deste agendamento: vale o mais recente
        reasons, self._pending = self._pending, []
        if reasons:
            self._action("+".join(reasons))


# --- diagnóstico em arquivo ------------------------------------------------------------------
_SAFE_TEXT = re.compile(r"[^A-Za-z0-9_.,:+\\/-]")


# T35 (revisão 021): só estas chaves entram no log; qualquer outra é descartada
ALLOWED_KEYS = frozenset({
    "name", "platform", "win32", "wanted", "screens", "ok", "reason", "actions", "action", "native_ok",
    "was_visible", "was_native_visible", "was_opacity", "was_inside", "was_topmost", "state",
    "visible", "native_visible", "opacity", "frame_opacity", "hide_when_idle", "x", "y",
    "visible_on_any", "screen", "topmost", "z_above", "hwnd_valid", "topmost_after",
})


def _fmt(value) -> str:
    """Só números, booleanos e identificadores curtos entram no log (nunca texto livre)."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.2f}"
    return _SAFE_TEXT.sub("_", str(value))[:60] or "-"


class DiagLog:
    """`<pasta>/bolha-diag.log`: uma linha de estado (`sample`, o app chama a cada 5 s) e uma por
    evento/correção (`event`).

    `log_dir=None` desliga (nada é criado). Nunca levanta: diagnóstico não pode derrubar o app."""

    def __init__(self, log_dir):
        self._logger = None
        self._handler = None
        self.path: Path | None = None
        if log_dir is None:
            return
        try:
            folder = Path(log_dir)
            folder.mkdir(parents=True, exist_ok=True)
            self.path = folder / DIAG_LOG_NAME
            handler = logging.handlers.RotatingFileHandler(
                self.path, maxBytes=DIAG_MAX_BYTES, backupCount=DIAG_BACKUPS, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s",
                                                   datefmt="%Y-%m-%d %H:%M:%S"))
            # Logger avulso (fora do gerenciador do `logging`): nada vai parar no hear-ai.log
            logger = logging.Logger(f"hear_ai.bolha_diag.{id(self)}")
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            self._logger, self._handler = logger, handler
        except OSError as e:
            logging.getLogger("hear_ai.ui").warning("log de diagnóstico da bolha indisponível: %s",
                                                    type(e).__name__)

    @property
    def enabled(self) -> bool:
        return self._logger is not None

    def _write(self, kind: str, fields: dict) -> None:
        if self._logger is None:
            return
        try:
            body = " ".join(f"{key}={_fmt(value)}" for key, value in fields.items() if key in ALLOWED_KEYS)
            self._logger.info("%s %s", kind, body)
        except Exception:  # noqa: BLE001 - diagnóstico nunca derruba nada
            pass

    def event(self, name: str, **fields) -> None:
        """Um evento (tela mudou, bloqueio, correção aplicada...). Sempre grava."""
        self._write("EVT", {"name": name, **fields})

    def sample(self, probe: BubbleProbe) -> None:
        """Linha com o estado completo da bolha (o app chama a cada `DIAG_PERIOD_S`)."""
        self._write("STATE", probe_fields(probe))

    def close(self) -> None:
        if self._handler is not None:
            try:
                self._handler.close()
            finally:
                self._logger.removeHandler(self._handler)
                self._logger = self._handler = None


def probe_fields(probe: BubbleProbe) -> dict:
    return {
        "visible": probe.visible, "native_visible": probe.native_visible,
        "opacity": probe.opacity, "frame_opacity": probe.frame_opacity,
        "state": probe.visual, "hide_when_idle": probe.hide_when_idle,
        "x": probe.x, "y": probe.y, "visible_on_any": probe.inside_screen,
        "screen": probe.screen_name, "topmost": probe.topmost, "z_above": probe.z_above,
        "hwnd_valid": probe.hwnd_valid,
    }


# --- Win32 (só no Windows; injetável nos testes) ----------------------------------------------
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
REASSERT_FLAGS = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW  # nunca ativa a janela
GW_HWNDPREV = 3
Z_LIMIT = 64


class Win32Topmost:
    """Lê e reafirma o topmost de uma janela. Nunca ativa nem tira o foco de ninguém."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetWindowPos.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetWindow.restype = wintypes.HWND
        self._u = u

    def ex_style(self, hwnd: int) -> int | None:
        if not hwnd or not self._u.IsWindow(hwnd):
            return None
        return int(self._u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF

    def is_topmost(self, hwnd: int) -> bool | None:
        style = self.ex_style(hwnd)
        return None if style is None else bool(style & WS_EX_TOPMOST)

    def is_visible(self, hwnd: int) -> bool | None:
        if not hwnd or not self._u.IsWindow(hwnd):
            return None
        return bool(self._u.IsWindowVisible(hwnd))

    def z_above(self, hwnd: int, limit: int = Z_LIMIT) -> int | None:
        """Janelas acima da nossa na ordem Z (a bolha é topmost: quase sempre 0 ou poucas)."""
        if not hwnd or not self._u.IsWindow(hwnd):
            return None
        count, cur = 0, hwnd
        while count < limit:
            cur = self._u.GetWindow(cur, GW_HWNDPREV)
            if not cur:
                return count
            count += 1
        return count

    def reassert(self, hwnd: int) -> bool:
        """HWND_TOPMOST sem mover, sem redimensionar, SEM ativar e mostrando a janela."""
        if not hwnd or not self._u.IsWindow(hwnd):
            return False
        return bool(self._u.SetWindowPos(hwnd, self._ct.c_void_p(HWND_TOPMOST), 0, 0, 0, 0, REASSERT_FLAGS))


class Win32Session:
    """Registro das notificações de sessão e da mensagem `TaskbarCreated` (wtsapi32/user32)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self._wts.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
        self._wts.WTSRegisterSessionNotification.restype = wintypes.BOOL
        self._wts.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
        self._wts.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
        self._u = ctypes.WinDLL("user32", use_last_error=True)
        self._u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        self._u.RegisterWindowMessageW.restype = wintypes.UINT

    def register(self, hwnd: int) -> bool:
        return bool(self._wts.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION))

    def unregister(self, hwnd: int) -> bool:
        return bool(self._wts.WTSUnRegisterSessionNotification(hwnd))

    def taskbar_created_message(self) -> int:
        return int(self._u.RegisterWindowMessageW("TaskbarCreated"))


def make_win32_apis(platform_name: str):
    """(Win32Topmost, Win32Session) na plataforma "windows" do Qt; (None, None) em qualquer outra
    (offscreen/testes: o `winId()` não é um HWND de verdade e não podemos mexer nele)."""
    if sys.platform != "win32" or platform_name != "windows":
        return None, None
    try:
        return Win32Topmost(), Win32Session()
    except Exception as e:  # noqa: BLE001
        logging.getLogger("hear_ai.ui").warning("vigia Win32 indisponível: %s", type(e).__name__)
        return None, None
