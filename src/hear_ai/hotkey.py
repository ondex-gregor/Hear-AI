"""Atalho global: segurar (hold) ou alternar (toggle), via RegisterHotKey do Windows.

Por que RegisterHotKey (T16, ver docs/ATALHO.md):
- o Windows CONSOME a combinação: ela não chega ao programa ativo (antes, Ctrl+Alt+Espaço
  também abria outro programa);
- se outro programa já registrou a mesma combinação, o registro FALHA (erro 1409) e o app
  sabe que há conflito, em vez de disparar os dois;
- não há hook global com supressão: um callback Python dentro do hook pode esperar o GIL
  por mais de 300 ms (medido em spikes/t16_latencia_gil.py) e o Windows remove o hook em silêncio.

Modo segurar: o "apertou" vem do WM_HOTKEY; o "soltou" vem de uma thread que lê
GetAsyncKeyState a cada ~15 ms e considera solto quando QUALQUER tecla da combinação
estiver solta em 2 leituras seguidas.
"""

from __future__ import annotations

import logging
import threading
import time

DEFAULT_HOTKEY = "ctrl+alt+space"

log = logging.getLogger("hear_ai.hotkey")

# --- nomes de teclas <-> códigos virtuais (VK) do Windows ------------------------

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
MODIFIERS = ("ctrl", "alt", "shift", "win")  # ordem canônica
MOD_FLAGS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
# VKs lidos por GetAsyncKeyState para saber se o modificador está apertado (qualquer lado)
MOD_VKS = {"ctrl": (0x11,), "alt": (0x12,), "shift": (0x10,), "win": (0x5B, 0x5C)}

ALIASES = {
    "control": "ctrl", "left ctrl": "ctrl", "right ctrl": "ctrl", "ctl": "ctrl",
    "menu": "alt", "left alt": "alt", "right alt": "alt",
    "left shift": "shift", "right shift": "shift",
    "windows": "win", "left windows": "win", "right windows": "win", "super": "win", "cmd": "win",
    "spacebar": "space", "espaço": "space", "espaco": "space",
    "return": "enter", "escape": "esc", "del": "delete", "ins": "insert",
    "pgup": "page up", "pageup": "page up", "pgdn": "page down", "pagedown": "page down",
    "backspace": "backspace", "prtsc": "print screen", "printscreen": "print screen",
    "arrow left": "left", "arrow right": "right", "arrow up": "up", "arrow down": "down",
}

KEY_VK: dict[str, int] = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "page up": 0x21, "page down": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pause": 0x13, "print screen": 0x2C, "scroll lock": 0x91,
}
KEY_VK.update({chr(c).lower(): c for c in range(0x41, 0x5B)})  # a-z
KEY_VK.update({str(d): 0x30 + d for d in range(10)})  # 0-9
KEY_VK.update({f"f{n}": 0x6F + n for n in range(1, 25)})  # f1-f24
KEY_VK.update({f"num {d}": 0x60 + d for d in range(10)})  # teclado numérico
VK_KEY = {vk: name for name, vk in KEY_VK.items()}

# Combinações do Windows que não devem virar atalho do app.
RESERVED = {
    "win+l": "bloqueio de tela do Windows",
    "ctrl+alt+delete": "tela de segurança do Windows",
    "alt+tab": "troca de janelas",
    "alt+shift+tab": "troca de janelas",
    "ctrl+alt+tab": "troca de janelas",
    "alt+f4": "fecha a janela",
    "alt+esc": "troca de janelas",
    "alt+space": "menu da janela / PowerToys Run",
    "ctrl+esc": "menu Iniciar",
    "ctrl+shift+esc": "Gerenciador de Tarefas",
}
# No teclado ABNT2, Ctrl+Alt = AltGr: estas teclas digitam / ? ° ₢ ¹ ² ³ £ ¢ ¬.
ALTGR_ABNT2 = set("qwec123456")

ERROR_HOTKEY_ALREADY_REGISTERED = 1409


class HotkeyError(ValueError):
    pass


def normalize_key(name: str) -> str:
    n = " ".join(name.strip().lower().split())
    return ALIASES.get(n, n)


def parse_hotkey(hotkey: str) -> tuple[tuple[str, ...], str]:
    """'Ctrl+Alt+H' -> (('ctrl', 'alt'), 'h'). Levanta HotkeyError se não for uma combinação válida."""
    if not isinstance(hotkey, str) or not hotkey.strip():
        raise HotkeyError("atalho vazio")
    mods, keys = set(), []
    for part in hotkey.split("+"):
        k = normalize_key(part)
        if not k:
            raise HotkeyError("atalho mal formado")
        if k in MODIFIERS:
            mods.add(k)
        elif k in KEY_VK:
            keys.append(k)
        else:
            raise HotkeyError(f"tecla desconhecida: {k}")
    if len(keys) != 1:
        raise HotkeyError("o atalho precisa de exatamente uma tecla comum")
    return tuple(m for m in MODIFIERS if m in mods), keys[0]


def format_hotkey(mods, key: str) -> str:
    return "+".join([m for m in MODIFIERS if m in mods] + [key])


def validate_hotkey(hotkey: str) -> tuple[bool, str]:
    """(True, forma canônica) ou (False, motivo em português)."""
    try:
        mods, key = parse_hotkey(hotkey)
    except HotkeyError as e:
        return False, str(e)
    canon = format_hotkey(mods, key)
    if not set(mods) & {"ctrl", "alt", "win"}:
        return False, "use pelo menos Ctrl, Alt ou Win (só Shift atrapalharia a digitação)"
    if canon in RESERVED:
        return False, f"{canon} é reservado: {RESERVED[canon]}"
    if set(mods) == {"win"}:
        return False, "Win + tecla é reservado para atalhos do Windows; combine com Ctrl ou Alt"
    if set(mods) == {"ctrl", "alt"} and key in ALTGR_ABNT2:
        return False, f"{canon} é AltGr+{key.upper()} no teclado ABNT2 (digita um símbolo)"
    return True, canon


# --- acesso ao Win32 (injetável em testes) -----------------------------------------

class Win32HotkeyApi:
    """Chamadas reais (ctypes). Nada aqui injeta teclas."""

    WM_HOTKEY, WM_QUIT, WM_USER = 0x0312, 0x0012, 0x0400

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                   wintypes.UINT]
        u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        self._u, self._k = u, k

    def thread_id(self) -> int:
        return self._k.GetCurrentThreadId()

    def ensure_queue(self) -> None:
        msg = self._wt.MSG()
        self._u.PeekMessageW(self._ct.byref(msg), None, self.WM_USER, self.WM_USER, 0)

    def register(self, hk_id: int, mods: int, vk: int) -> tuple[bool, int]:
        ok = bool(self._u.RegisterHotKey(None, hk_id, mods, vk))
        return ok, (0 if ok else self._ct.get_last_error())

    def unregister(self, hk_id: int) -> None:
        self._u.UnregisterHotKey(None, hk_id)

    def get_message(self):
        """Bloqueia até a próxima mensagem. (message, wparam) ou None em WM_QUIT/erro."""
        msg = self._wt.MSG()
        r = self._u.GetMessageW(self._ct.byref(msg), None, 0, 0)
        if r in (0, -1):
            return None
        return msg.message, msg.wParam

    def post_quit(self, thread_id: int) -> None:
        self._u.PostThreadMessageW(thread_id, self.WM_QUIT, 0, 0)

    def is_down(self, vk: int) -> bool:
        return bool(self._u.GetAsyncKeyState(vk) & 0x8000)


# --- binder --------------------------------------------------------------------------

class HotkeyBinder:
    HOTKEY_ID = 0x4841  # "HA"

    def __init__(self, hotkey, mode, controller, api=None, on_conflict=None,
                 sleep=time.sleep, clock=time.monotonic, poll_s=0.015, kb=None):
        # `kb` (lib keyboard) não é mais usado; aceito só por compatibilidade com chamadas antigas.
        self.controller = controller
        self.mode = mode if mode in ("hold", "toggle") else "hold"
        self.on_conflict = on_conflict
        self._api_obj = api
        self._sleep = sleep
        self._clock = clock
        self.poll_s = poll_s
        ok, canon = validate_hotkey(hotkey)
        if not ok:
            log.warning("atalho inválido (%s); usando %s", canon, DEFAULT_HOTKEY)
            canon = DEFAULT_HOTKEY
        self.hotkey = canon
        self._mods, self._key = parse_hotkey(canon)
        self.key_names = list(self._mods) + [self._key]
        self.conflict = False
        self.conflict_reason: str | None = None
        self.installed = False
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._holding = False
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None

    @property
    def api(self):
        if self._api_obj is None:
            self._api_obj = Win32HotkeyApi()
        return self._api_obj

    # vks a observar: um grupo por tecla (qualquer VK do grupo apertado = tecla apertada)
    def _key_vks(self) -> list[tuple[int, ...]]:
        return [MOD_VKS[m] for m in self._mods] + [(KEY_VK[self._key],)]

    def _down(self, vks: tuple[int, ...]) -> bool:
        return any(self.api.is_down(vk) for vk in vks)

    # --- instalação -------------------------------------------------------------------
    def install(self, timeout: float = 5.0) -> bool:
        """Registra o atalho. True se ficou ativo. Em conflito: False, `conflict=True`,
        `conflict_reason` e `on_conflict(reason)`; nada é instalado no lugar."""
        if self.installed:
            return True
        self.conflict, self.conflict_reason = False, None
        self._stop.clear()
        ready = threading.Event()
        result = {}

        def run():
            api = self.api
            try:
                api.ensure_queue()
                self._thread_id = api.thread_id()
                mods = sum(MOD_FLAGS[m] for m in self._mods) | MOD_NOREPEAT
                ok, err = api.register(self.HOTKEY_ID, mods, KEY_VK[self._key])
            except Exception as e:
                result["error"] = type(e).__name__
                ready.set()
                return
            result["ok"], result["err"] = ok, err
            ready.set()
            if not ok:
                return
            if result.get("abandoned"):  # install() desistiu de esperar: não deixa registro solto
                api.unregister(self.HOTKEY_ID)
                return
            try:
                self._message_loop(api)
            finally:
                api.unregister(self.HOTKEY_ID)

        self._thread = threading.Thread(target=run, name="hear-ai-hotkey", daemon=True)
        self._thread.start()
        if not ready.wait(timeout):
            result["abandoned"] = True
            return self._fail("o registro do atalho não respondeu")
        if "error" in result:
            return self._fail(f"não foi possível registrar o atalho ({result['error']})")
        if not result["ok"]:
            if result["err"] == ERROR_HOTKEY_ALREADY_REGISTERED:
                reason = f"o atalho {self.hotkey} já está em uso por outro programa"
            else:
                reason = f"o Windows recusou o atalho {self.hotkey} (erro {result['err']})"
            return self._fail(reason, conflict=True)
        self.installed = True
        log.info("atalho %s registrado (modo %s)", self.hotkey, self.mode)
        return True

    def _fail(self, reason: str, conflict: bool = False) -> bool:
        self.conflict = conflict
        self.conflict_reason = reason
        log.warning("atalho não instalado: %s", reason)
        if conflict and self.on_conflict is not None:
            try:
                self.on_conflict(reason)
            except Exception as e:
                log.warning("on_conflict falhou: %s", type(e).__name__)
        return False

    def _message_loop(self, api) -> None:
        while True:
            item = api.get_message()
            if item is None:
                return
            message, wparam = item
            if message == Win32HotkeyApi.WM_HOTKEY and wparam == self.HOTKEY_ID:
                self._on_hotkey()

    def _on_hotkey(self) -> None:
        if self.mode == "toggle":
            self.controller.toggle()
            return
        if self._holding:
            return  # MOD_NOREPEAT já evita; proteção extra
        self._holding = True
        self.controller.begin()
        self._poller = threading.Thread(target=self._poll_release, name="hear-ai-hotkey-release", daemon=True)
        self._poller.start()

    def _poll_release(self) -> None:
        groups = self._key_vks()
        released_reads = 0
        while not self._stop.is_set():
            self._sleep(self.poll_s)
            try:
                any_up = any(not self._down(g) for g in groups)
            except Exception:
                any_up = True
            released_reads = released_reads + 1 if any_up else 0
            if released_reads >= 2:
                break
        self._holding = False
        self.controller.end()

    def uninstall(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive() and self._thread_id is not None:
            try:
                self.api.post_quit(self._thread_id)
            except Exception as e:
                log.warning("falha ao encerrar o atalho: %s", type(e).__name__)
            self._thread.join(timeout)
        if self._poller is not None:
            self._poller.join(timeout)  # encerra uma gravação em modo segurar
        self._thread = self._poller = None
        self._thread_id = None
        self.installed = False

    def set_mode(self, mode: str) -> None:
        if mode in ("hold", "toggle"):
            self.mode = mode

    def wait_released(self, timeout: float = 2.0) -> bool:
        """Espera nenhuma tecla do atalho estar pressionada (evita Ctrl+Alt+V). False se estourou o tempo."""
        groups = self._key_vks()
        return wait_keys_released(groups, self._down, timeout, self._sleep, self._clock)


def wait_keys_released(key_names, is_pressed, timeout=2.0, sleep=time.sleep, clock=time.monotonic, poll=0.02) -> bool:
    deadline = clock() + timeout
    while True:
        try:
            held = any(is_pressed(k) for k in key_names)
        except Exception:
            return True
        if not held:
            return True
        if clock() >= deadline:
            return False
        sleep(poll)


# --- gravar um novo atalho -------------------------------------------------------------

def record_hotkey(timeout_s=10, kb=None, on_rejected=None, sleep=time.sleep, clock=time.monotonic,
                  poll_s=0.015) -> str | None:
    """Espera o usuário apertar uma combinação e devolve-a na forma canônica ('ctrl+alt+h').

    `kb`: fonte do estado das teclas, com `is_down(vk) -> bool` (padrão: GetAsyncKeyState).
    Só observa: nada é bloqueado nem injetado. Teclas já apertadas no início são ignoradas até
    serem soltas. Esc sozinho cancela (None). Combinação inválida/reservada chama
    `on_rejected(motivo)` e continua esperando. Tempo esgotado: None.
    """
    src = kb if kb is not None else Win32HotkeyApi()
    candidates = list(KEY_VK.values())
    mod_items = list(MOD_VKS.items())

    def down_keys() -> set[int]:
        return {vk for vk in candidates if src.is_down(vk)}

    def down_mods() -> set[str]:
        return {m for m, vks in mod_items if any(src.is_down(v) for v in vks)}

    deadline = clock() + timeout_s
    ignored = down_keys()
    prev = set(ignored)
    while clock() < deadline:
        now = down_keys()
        ignored &= now
        new = now - prev - ignored
        prev = now
        for vk in sorted(new):
            mods = down_mods()
            key = VK_KEY[vk]
            if key == "esc" and not mods:
                return None
            ok, canon = validate_hotkey(format_hotkey(mods, key))
            if ok:
                return canon
            log.info("combinação recusada: %s", canon)
            if on_rejected is not None:
                on_rejected(canon)
        sleep(poll_s)
    return None
