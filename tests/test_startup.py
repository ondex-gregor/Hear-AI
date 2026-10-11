"""Testes do "iniciar com o Windows" (T23). SEMPRE com pasta Inicializar falsa em tmp_path:
nenhum teste cria ou apaga atalho na pasta Inicializar real do usuário."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hear_ai import startup


def test_pasta_inicializar_padrao_vem_do_appdata():
    p = startup.default_startup_dir({"APPDATA": r"C:\Users\x\AppData\Roaming"})
    assert p == Path(r"C:\Users\x\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup")
    assert startup.default_startup_dir({}) is None


class Runner:
    """Faz o papel do PowerShell: registra a chamada e cria o arquivo pedido."""

    def __init__(self, returncode=0, cria=True, erro=None):
        self.chamadas = []
        self.returncode = returncode
        self.cria = cria
        self.erro = erro

    def __call__(self, cmd, env=None, **kw):
        self.chamadas.append((cmd, env, kw))
        if self.erro:
            raise self.erro
        if self.cria and self.returncode == 0:
            Path(env["HEARAI_LNK"]).write_bytes(b"lnk")
        return SimpleNamespace(returncode=self.returncode)


def test_ligar_passa_caminhos_por_variaveis_de_ambiente(tmp_path):
    pasta = tmp_path / "Inicializar Falsa"
    alvo = tmp_path / "Hear AI" / "iniciar.bat"
    r = Runner()
    assert startup.enable(pasta, target=alvo, runner=r) is True
    assert startup.is_enabled(pasta)
    (cmd, env, kw), = r.chamadas
    assert cmd[0] == "powershell.exe" and "-NoProfile" in cmd and "-NonInteractive" in cmd
    script = cmd[-1]
    assert str(alvo) not in script and str(pasta) not in script  # nada de caminho no texto do comando
    assert env["HEARAI_LNK"] == str(pasta / "Hear AI.lnk")
    assert env["HEARAI_TARGET"] == str(alvo)
    assert env["HEARAI_WORKDIR"] == str(alvo.parent)  # pasta de trabalho = projeto
    assert env["HEARAI_STYLE"] == "7"  # minimizado
    assert kw["timeout"] > 0


def test_falha_do_powershell_nao_quebra(tmp_path, caplog):
    pasta = tmp_path / "s"
    with caplog.at_level("WARNING", logger="hear_ai.startup"):
        assert startup.enable(pasta, target=tmp_path / "x.bat", runner=Runner(returncode=1)) is False
        assert startup.enable(pasta, target=tmp_path / "x.bat", runner=Runner(erro=OSError("sem ps"))) is False
        assert startup.enable(pasta, target=tmp_path / "x.bat",
                              runner=Runner(erro=subprocess.TimeoutExpired("ps", 30))) is False
    assert not startup.is_enabled(pasta)
    assert "OSError" in caplog.text


def test_sem_appdata_nao_faz_nada():
    assert startup.enable(None, runner=Runner()) is False
    assert startup.disable(None) is True
    assert startup.is_enabled(None) is False


def test_desligar_remove_so_o_nosso_atalho(tmp_path):
    pasta = tmp_path / "s"
    pasta.mkdir()
    (pasta / "Hear AI.lnk").write_bytes(b"x")
    (pasta / "Outro Programa.lnk").write_bytes(b"y")
    assert startup.disable(pasta) is True
    assert not (pasta / "Hear AI.lnk").exists() and (pasta / "Outro Programa.lnk").exists()
    assert startup.disable(pasta) is True  # já desligado


def test_set_enabled_devolve_o_estado_real(tmp_path):
    pasta = tmp_path / "s"
    assert startup.set_enabled(pasta, True, target=tmp_path / "x.bat", runner=Runner()) is True
    assert startup.set_enabled(pasta, False) is False
    assert startup.set_enabled(pasta, True, target=tmp_path / "x.bat", runner=Runner(returncode=1)) is False


@pytest.mark.skipif(sys.platform != "win32", reason="só Windows")
def test_powershell_de_verdade_cria_lnk_em_pasta_falsa(tmp_path):
    """Cria um .lnk REAL, mas numa pasta temporária (nunca na Inicializar do usuário)."""
    pasta = tmp_path / "Startup falsa com espaço e acentuação ç"
    alvo = tmp_path / "proj eto" / "iniciar.bat"
    alvo.parent.mkdir()
    alvo.write_text("@echo off\n", encoding="ascii")
    assert startup.enable(pasta, target=alvo) is True
    lnk = pasta / "Hear AI.lnk"
    assert lnk.exists() and lnk.stat().st_size > 0
    # conferir o alvo lendo o atalho de volta pelo próprio WScript.Shell
    import os

    env = dict(os.environ, HEARAI_LNK=str(lnk))
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:HEARAI_LNK); "
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $s.TargetPath; $s.WorkingDirectory; $s.WindowStyle"],
        env=env, capture_output=True, timeout=30,
    )
    linhas = out.stdout.decode("utf-8", "replace").strip().splitlines()
    assert linhas[0] == str(alvo) and linhas[1] == str(alvo.parent) and linhas[2] == "7"
    assert startup.disable(pasta) and not lnk.exists()
