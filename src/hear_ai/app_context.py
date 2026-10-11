"""Contexto do aplicativo ativo (Python puro, sem Qt).

Fornece funções para obter o nome do processo em primeiro plano e mapear
para um destino do gerador de prompts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

# --- API injetável para testes ------------------------------------------------------

class AppContextAPI(Protocol):
    """Protocolo da API Win32 injetável. Implementação real usa ctypes."""
    def foreground_hwnd(self) -> int: ...
    def pid_of(self, hwnd: int) -> int: ...
    def image_path(self, pid: int) -> str: ...


# --- Implementação real (Windows/ctypes) -------------------------------------------

def _real_api() -> AppContextAPI:
    """API real (ctypes) com assinaturas explícitas: em 64 bits, sem `restype` os handles são truncados.
    `QueryFullProcessImageNameW` fica na kernel32 (revisão do líder na P3)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    class RealAPI:
        def foreground_hwnd(self) -> int:
            return int(user32.GetForegroundWindow() or 0)

        def pid_of(self, hwnd: int) -> int:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value)

        def image_path(self, pid: int) -> str:
            # PROCESS_QUERY_LIMITED_INFORMATION (0x1000): só leitura do caminho do executável
            h_process = kernel32.OpenProcess(0x1000, False, pid)
            if not h_process:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(h_process, 0, buf, ctypes.byref(size)):
                    return buf.value
                return ""
            finally:
                kernel32.CloseHandle(h_process)

    return RealAPI()


# --- API padrão (lazy) --------------------------------------------------------------

_DEFAULT_API: AppContextAPI | None = None

def _get_default_api() -> AppContextAPI:
    global _DEFAULT_API
    if _DEFAULT_API is None:
        _DEFAULT_API = _real_api()
    return _DEFAULT_API


# --- Funções públicas ---------------------------------------------------------------

def foreground_process_name(api: AppContextAPI | None = None) -> str | None:
    """Retorna o nome do executável em primeiro plano (sem caminho, sem .exe).

    Args:
        api: API injetável para testes. Se None, usa a implementação real (Win32/ctypes).

    Returns:
        Nome do executável em minúsculas (ex.: "code", "windowsterminal", "chrome"),
        ou None se não foi possível obter.
    """
    api = api or _get_default_api()
    try:
        hwnd = api.foreground_hwnd()
        if not hwnd:
            return None
        pid = api.pid_of(hwnd)
        if not pid:
            return None
        path = api.image_path(pid)
        if not path:
            return None
        name = Path(path).name
        # Remove .exe se houver
        if name.lower().endswith(".exe"):
            name = name[:-4]
        return name.lower()
    except Exception:
        return None


# --- Mapeamento de destino ----------------------------------------------------------

# Conjuntos em minúsculas para comparação case-insensitive
_TERMINAIS_EDITORES = {
    "windowsterminal", "cmd", "powershell", "pwsh",
    "code", "cursor", "idea64", "pycharm64",
    "wezterm-gui", "alacritty",
}
_NAVEGADORES = {
    "chrome", "firefox", "msedge", "brave", "opera", "vivaldi", "safari",
}
_EMAIL = {
    "outlook", "thunderbird",
}


def destino_para(app: str | None) -> str:
    """Mapeia nome do processo para destino do gerador.

    Args:
        app: Nome do executável (sem caminho, sem .exe). Case-insensitive.

    Returns:
        Destino: "agente_de_codigo", "pesquisa", "escrita" ou "auto".
    """
    if not app:
        return "auto"
    app_lower = app.lower()
    if app_lower in _TERMINAIS_EDITORES:
        return "agente_de_codigo"
    if app_lower in _NAVEGADORES:
        return "pesquisa"
    if app_lower in _EMAIL:
        return "escrita"
    return "auto"