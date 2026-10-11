"""Colagem de texto com restauração da área de transferência."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("hear_ai.paster")


class Clipboard(Protocol):
    def get(self) -> str | None: ...
    def set(self, text: str) -> None: ...


class SendPaste(Protocol):
    def __call__(self) -> None: ...


@dataclass(frozen=True)
class PasteResult:
    """Resultado da operação de colagem.

    reason:
      "ok"                colou (e restaurou, se havia o que restaurar)
      "empty"             texto vazio: nada foi feito
      "clipboard_busy"    área de transferência travada: com pasted=False, nem deu para pôr o
                          texto nela; com pasted=True, colou mas não deu para ler/conferir
                          o conteúdo para restaurar
      "send_failed"       o texto foi para a área de transferência, mas o Ctrl+V falhou
      "clipboard_changed" outro programa/usuário copiou algo durante a espera: não restaura
      "restore_failed"    colou, mas não deu para pôr de volta o conteúdo anterior
    in_clipboard: o texto ditado ESTÁ na área de transferência agora (o usuário pode Ctrl+V).
    """
    pasted: bool          # True se o Ctrl+V foi enviado sem erro
    restored: bool        # True se o conteúdo anterior foi restaurado
    reason: str
    in_clipboard: bool = False


class Paster:
    """Cola texto na janela ativa e restaura a área de transferência."""

    def __init__(
        self,
        clipboard: Clipboard,
        send_paste: SendPaste,
        delay_s: float = 0.25,      # alias para base_s (compatibilidade)
        base_s: float | None = None,  # espera mínima antes de restaurar
        chars_per_s: float = 2500,    # + 1 s a cada `chars_per_s` caracteres
        max_s: float = 3.0,           # espera máxima
        restore: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not chars_per_s or chars_per_s <= 0:
            raise ValueError("chars_per_s precisa ser maior que zero")
        self.clipboard = clipboard
        self.send_paste = send_paste
        self._sleep = sleep
        self.restore = restore
        self._lock = threading.Lock()

        # delay_s é alias para base_s (compatibilidade com código antigo)
        self.base_s = base_s if base_s is not None else delay_s
        self.delay_s = self.base_s  # mantém para compatibilidade
        self.chars_per_s = chars_per_s
        self.max_s = max_s

    def _restore_delay(self, text: str) -> float:
        """clamp(base_s + len/chars_per_s, base_s, max_s). Com base_s=0 vale só a parte proporcional."""
        base = max(0.0, self.base_s)
        delay = base + len(text) / self.chars_per_s
        result = max(base, min(delay, self.max_s))
        return round(result, 3)  # evita ruído de ponto flutuante

    @staticmethod
    def _normalize_for_compare(text: str) -> str:
        """Normaliza para comparar: CRLF e CR viram LF, e strip() nas duas pontas."""
        return text.replace("\r\n", "\n").replace("\r", "\n").strip()

    def paste(self, text: str) -> PasteResult:
        """Cola o texto e devolve um `PasteResult`; nunca levanta exceção por falha de colagem.

        - vazio/só espaços: nada é feito (`empty`);
        - não deu para pôr o texto na área de transferência: `clipboard_busy`, pasted=False,
          in_clipboard=False (o texto NÃO está lá);
        - o Ctrl+V falhou: `send_failed`, o texto FICA na área de transferência (in_clipboard=True);
        - outro programa copiou algo durante a espera: não restaura (`clipboard_changed`);
        - não deu para ler o conteúdo anterior/atual: cola sem restaurar (`clipboard_busy`);
        - não deu para restaurar: `restore_failed` (o texto ditado continua lá).
        Nunca registra o texto no log.
        """
        if not text or not text.strip():
            return PasteResult(pasted=False, restored=False, reason="empty")

        with self._lock:
            old: str | None = None
            get_failed = False
            if self.restore:
                for attempt in range(3):
                    try:
                        old = self.clipboard.get()
                        break
                    except Exception as e:
                        log.debug("clipboard.get falhou: %s; tentando novamente", type(e).__name__)
                        if attempt < 2:
                            self._sleep(0.02)
                else:
                    log.warning("clipboard.get falhou após tentativas; colando sem restaurar")
                    get_failed = True
                    old = None

            try:
                self.clipboard.set(text)
            except Exception as e:
                log.warning("falha ao pôr o texto na área de transferência: %s", type(e).__name__)
                return PasteResult(pasted=False, restored=False, reason="clipboard_busy", in_clipboard=False)
            try:
                self.send_paste()
            except Exception as e:
                # o texto já está na área de transferência: o usuário pode colar com Ctrl+V
                log.warning("falha ao colar: %s", type(e).__name__)
                return PasteResult(pasted=False, restored=False, reason="send_failed", in_clipboard=True)

            # espera proporcional ao tamanho, para o programa de destino ler o texto
            self._sleep(self._restore_delay(text))

            if not self.restore:
                return PasteResult(pasted=True, restored=False, reason="ok", in_clipboard=True)
            if get_failed:
                return PasteResult(pasted=True, restored=False, reason="clipboard_busy", in_clipboard=True)
            if old is None:  # nada (texto) para restaurar
                return PasteResult(pasted=True, restored=False, reason="ok", in_clipboard=True)

            try:
                current = self.clipboard.get()
            except Exception as e:
                # não dá para saber se o usuário copiou algo: não restaura (conservador)
                log.debug("clipboard.get falhou na verificação: %s; não restaura", type(e).__name__)
                return PasteResult(pasted=True, restored=False, reason="clipboard_busy", in_clipboard=False)

            if current is None or self._normalize_for_compare(current) != self._normalize_for_compare(text):
                log.debug("clipboard mudou durante a espera; não restaura")
                return PasteResult(pasted=True, restored=False, reason="clipboard_changed", in_clipboard=False)

            try:
                self.clipboard.set(old)
                return PasteResult(pasted=True, restored=True, reason="ok", in_clipboard=False)
            except Exception as e:
                log.warning("falha ao restaurar clipboard: %s", type(e).__name__)
                return PasteResult(pasted=True, restored=False, reason="restore_failed", in_clipboard=True)


class SystemClipboard:
    """Backend real usando pyperclip."""

    def get(self) -> str | None:
        import pyperclip
        return pyperclip.paste()

    def set(self, text: str) -> None:
        import pyperclip
        pyperclip.copy(text)


def send_ctrl_v() -> None:
    """Envia Ctrl+V via keyboard."""
    import keyboard
    keyboard.send("ctrl+v")

# --- janela de destino (T31): de administrador? ----------------------------------------------

ERROR_ACCESS_DENIED = 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20  # TOKEN_INFORMATION_CLASS.TokenElevation


class Win32Windows:
    """Só leitura: qual janela está em primeiro plano e se o processo dela é elevado (admin).

    Um app comum não consegue mandar Ctrl+V para uma janela de administrador (o Windows bloqueia,
    UIPI). Se não der nem para consultar o processo (acesso negado), trata como elevado:
    é o caso típico de janela de admin vista por um app sem admin.
    """

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
        self._u, self._k, self._a = u, k, a
        self._self_elevated: bool | None = None

    def foreground(self):
        hwnd = self._u.GetForegroundWindow()
        return int(hwnd) if hwnd else None

    def process_id(self, hwnd) -> int:
        pid = self._wt.DWORD()
        self._u.GetWindowThreadProcessId(hwnd, self._ct.byref(pid))
        return pid.value

    def _token_elevated(self, process_handle) -> bool:
        token = self._wt.HANDLE()
        if not self._a.OpenProcessToken(process_handle, TOKEN_QUERY, self._ct.byref(token)):
            raise PermissionError(self._ct.get_last_error())
        try:
            value = self._wt.DWORD()
            size = self._wt.DWORD()
            if not self._a.GetTokenInformation(token, TOKEN_ELEVATION, self._ct.byref(value),
                                               self._ct.sizeof(value), self._ct.byref(size)):
                raise PermissionError(self._ct.get_last_error())
            return bool(value.value)
        finally:
            self._k.CloseHandle(token)

    def is_elevated(self, hwnd) -> bool:
        return self.is_pid_elevated(self.process_id(hwnd))

    def is_pid_elevated(self, pid: int) -> bool:
        if not pid:
            return False
        handle = self._k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # acesso negado = processo protegido/elevado; outro erro (processo sumiu) = não elevado
            return self._ct.get_last_error() == ERROR_ACCESS_DENIED
        try:
            return self._token_elevated(handle)
        except PermissionError as e:
            return e.args[0] == ERROR_ACCESS_DENIED if e.args else True
        finally:
            self._k.CloseHandle(handle)

    def self_elevated(self) -> bool:
        if self._self_elevated is None:
            try:
                self._self_elevated = self._token_elevated(self._k.GetCurrentProcess())
            except PermissionError:
                self._self_elevated = False
        return self._self_elevated
