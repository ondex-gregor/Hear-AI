"""Testes para app_context (Python puro, API falsa injetada)."""

from __future__ import annotations

import pytest

from hear_ai.app_context import (
    AppContextAPI,
    destino_para,
    foreground_process_name,
)


class FakeAPI:
    """API falsa para testes - não chama Win32 real."""

    def __init__(self, hwnd: int = 123, pid: int = 456, image_path: str = "C:\\Apps\\Code.exe"):
        self._hwnd = hwnd
        self._pid = pid
        self._image_path = image_path
        self.calls = []

    def foreground_hwnd(self) -> int:
        self.calls.append("foreground_hwnd")
        return self._hwnd

    def pid_of(self, hwnd: int) -> int:
        self.calls.append(("pid_of", hwnd))
        return self._pid

    def image_path(self, pid: int) -> str:
        self.calls.append(("image_path", pid))
        return self._image_path


class TestForegroundProcessName:
    """Testes de foreground_process_name com API falsa."""

    def test_retorna_nome_sem_caminho_nem_exe(self):
        api = FakeAPI(image_path="C:\\Apps\\Code.exe")
        assert foreground_process_name(api) == "code"

    def test_retorna_nome_sem_exe_ja_nao_tem(self):
        api = FakeAPI(image_path="C:\\Apps\\Code")
        assert foreground_process_name(api) == "code"

    def test_case_insensitive(self):
        api = FakeAPI(image_path="C:\\Apps\\WINWORD.EXE")
        assert foreground_process_name(api) == "winword"

    def test_hwnd_zero_retorna_none(self):
        api = FakeAPI(hwnd=0, image_path="C:\\Apps\\Code.exe")
        assert foreground_process_name(api) is None

    def test_pid_zero_retorna_none(self):
        api = FakeAPI(pid=0, image_path="C:\\Apps\\Code.exe")
        assert foreground_process_name(api) is None

    def test_image_path_vazio_retorna_none(self):
        api = FakeAPI(image_path="")
        assert foreground_process_name(api) is None

    def test_excecao_na_api_retorna_none(self):
        class BrokenAPI:
            def foreground_hwnd(self) -> int:
                raise OSError("falhou")
            def pid_of(self, hwnd: int) -> int:
                return 1
            def image_path(self, pid: int) -> str:
                return ""

        assert foreground_process_name(BrokenAPI()) is None

    def test_chama_api_nas_ordem_correta(self):
        api = FakeAPI(image_path="C:\\Apps\\Code.exe")
        result = foreground_process_name(api)
        assert result == "code"
        assert api.calls == ["foreground_hwnd", ("pid_of", 123), ("image_path", 456)]

    def test_caminho_com_espacos(self):
        api = FakeAPI(image_path="C:\\Program Files\\My App\\app.exe")
        assert foreground_process_name(api) == "app"

    def test_caminho_unix_style(self):
        api = FakeAPI(image_path="/usr/bin/code")
        assert foreground_process_name(api) == "code"


class TestDestinoPara:
    """Testes de destino_para - mapeamento case-insensitive."""

    # Terminais/editores -> agente_de_codigo
    @pytest.mark.parametrize("app", [
        "windowsterminal", "WindowsTerminal", "windowsterminal",
        "cmd", "CMD", "Cmd",
        "powershell", "PowerShell", "POWERSHELL",
        "pwsh", "PWSH",
        "code", "Code", "CODE",
        "cursor", "Cursor",
        "idea64", "Idea64",
        "pycharm64", "PyCharm64",
        "wezterm-gui", "WezTerm-GUI",
        "alacritty", "Alacritty",
    ])
    def test_terminais_editores(self, app):
        assert destino_para(app) == "agente_de_codigo"

    # Navegadores -> pesquisa
    @pytest.mark.parametrize("app", [
        "chrome", "Chrome", "CHROME",
        "firefox", "Firefox",
        "msedge", "MsEdge", "MSEDGE",
        "brave", "Brave",
        "opera", "Opera",
        "vivaldi", "Vivaldi",
        "safari", "Safari",
    ])
    def test_navegadores(self, app):
        assert destino_para(app) == "pesquisa"

    # Email -> escrita
    @pytest.mark.parametrize("app", [
        "outlook", "Outlook", "OUTLOOK",
        "thunderbird", "Thunderbird",
    ])
    def test_email(self, app):
        assert destino_para(app) == "escrita"

    # Resto/None -> auto
    @pytest.mark.parametrize("app", [
        None, "", "notepad", "explorer", "randomapp", "word", "excel",
    ])
    def test_auto(self, app):
        assert destino_para(app) == "auto"

    def test_string_vazia_e_none(self):
        assert destino_para("") == "auto"
        assert destino_para(None) == "auto"


class TestIntegracao:
    """Testes de integração foreground_process_name + destino_para."""

    def test_vscode_vai_para_agente_de_codigo(self):
        api = FakeAPI(image_path="C:\\Users\\User\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe")
        app = foreground_process_name(api)
        destino = destino_para(app)
        assert app == "code"
        assert destino == "agente_de_codigo"

    def test_windowsterminal_vai_para_agente_de_codigo(self):
        api = FakeAPI(image_path="C:\\Program Files\\WindowsApps\\Microsoft.WindowsTerminal_1.0\\WindowsTerminal.exe")
        app = foreground_process_name(api)
        destino = destino_para(app)
        assert app == "windowsterminal"
        assert destino == "agente_de_codigo"

    def test_chrome_vai_para_pesquisa(self):
        api = FakeAPI(image_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
        app = foreground_process_name(api)
        destino = destino_para(app)
        assert app == "chrome"
        assert destino == "pesquisa"

    def test_outlook_vai_para_escrita(self):
        api = FakeAPI(image_path="C:\\Program Files\\Microsoft Office\\root\\Office16\\OUTLOOK.EXE")
        app = foreground_process_name(api)
        destino = destino_para(app)
        assert app == "outlook"
        assert destino == "escrita"

    def test_app_desconhecido_vai_para_auto(self):
        api = FakeAPI(image_path="C:\\Windows\\System32\\notepad.exe")
        app = foreground_process_name(api)
        destino = destino_para(app)
        assert app == "notepad"
        assert destino == "auto"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# --- líder (revisão P3): a API REAL precisa funcionar no Windows (só leitura) ---------------

import sys as _sys


@pytest.mark.skipif(_sys.platform != "win32", reason="só no Windows")
def test_lider_api_real_le_o_proprio_processo():
    import os

    from hear_ai.app_context import _real_api
    api = _real_api()
    caminho = api.image_path(os.getpid())  # o próprio python do teste: sempre legível
    assert caminho.lower().endswith(".exe") and "python" in caminho.lower()
    nome = foreground_process_name(api)  # o que estiver em primeiro plano: str ou None, nunca exceção
    assert nome is None or (isinstance(nome, str) and "\\" not in nome and not nome.endswith(".exe"))
