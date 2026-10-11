"""Iniciar com o Windows (C3): atalho "Hear AI.lnk" na pasta Inicializar do usuário.

Escolha (T23): o atalho é criado pelo PowerShell com `New-Object -ComObject WScript.Shell`,
chamado por subprocess (o pywin32/win32com não está instalado; IShellLink por ctypes seria
bem mais código). Os caminhos vão por VARIÁVEIS DE AMBIENTE para o PowerShell, nunca colados
no texto do comando (nada de problema com aspas, espaços ou "Usuário" no caminho).

O atalho aponta para `iniciar.bat` (que já checa .venv/modelo e abre o pythonw), com a pasta
do projeto como pasta de trabalho e a janela minimizada (o .bat só pisca ao abrir).
Tudo recebe `startup_dir` por parâmetro: os testes usam uma pasta falsa, nunca a real.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from hear_ai.config import PROJECT_ROOT

log = logging.getLogger("hear_ai.startup")

LINK_NAME = "Hear AI.lnk"
WINDOW_MINIMIZED = 7

_PS_CREATE = (
    "$ErrorActionPreference = 'Stop'; "
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:HEARAI_LNK); "
    "$s.TargetPath = $env:HEARAI_TARGET; "
    "$s.WorkingDirectory = $env:HEARAI_WORKDIR; "
    "$s.WindowStyle = [int]$env:HEARAI_STYLE; "
    "$s.Description = 'Hear AI - ditado por voz'; "
    "if ($env:HEARAI_ICON) { $s.IconLocation = $env:HEARAI_ICON }; "
    "$s.Save()"
)


def default_startup_dir(env=None) -> Path | None:
    """%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup (pasta Inicializar do usuário)."""
    env = os.environ if env is None else env
    appdata = env.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def default_target(root: Path = PROJECT_ROOT) -> Path:
    return root / "iniciar.bat"


def link_path(startup_dir) -> Path:
    return Path(startup_dir) / LINK_NAME


def is_enabled(startup_dir) -> bool:
    return startup_dir is not None and link_path(startup_dir).exists()


def enable(startup_dir, target: Path | None = None, workdir: Path | None = None,
           icon: str | None = None, runner=subprocess.run) -> bool:
    """Cria (ou recria) o atalho. True se o .lnk existe no fim."""
    if startup_dir is None:
        log.warning("pasta Inicializar não encontrada (APPDATA ausente)")
        return False
    startup_dir = Path(startup_dir)
    explicit = target is not None
    target = Path(target or default_target())
    workdir = Path(workdir or target.parent)
    if not explicit and not target.exists():
        # T35 (lacuna 019): atalho para um alvo que não existe abriria um erro a cada logon
        log.warning("alvo do atalho de inicialização não existe; nada foi criado")
        return False
    try:
        startup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("não foi possível criar a pasta Inicializar: %s", type(e).__name__)
        return False
    env = dict(os.environ)
    env.update({
        "HEARAI_LNK": str(link_path(startup_dir)),
        "HEARAI_TARGET": str(target),
        "HEARAI_WORKDIR": str(workdir),
        "HEARAI_STYLE": str(WINDOW_MINIMIZED),
        "HEARAI_ICON": icon or "",
    })
    try:
        result = runner(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_CREATE],
            env=env, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("não foi possível criar o atalho de inicialização: %s", type(e).__name__)
        return False
    if getattr(result, "returncode", 1) != 0:
        log.warning("PowerShell falhou ao criar o atalho de inicialização (código %s)",
                    getattr(result, "returncode", "?"))
        return False
    return link_path(startup_dir).exists()


def disable(startup_dir) -> bool:
    """Remove o atalho. True se ele não existe mais no fim."""
    if startup_dir is None:
        return True
    try:
        link_path(startup_dir).unlink(missing_ok=True)
    except OSError as e:
        log.warning("não foi possível remover o atalho de inicialização: %s", type(e).__name__)
        return False
    return True


def set_enabled(startup_dir, enabled: bool, **kwargs) -> bool:
    """Liga/desliga; devolve o estado REAL depois da tentativa."""
    if enabled:
        enable(startup_dir, **kwargs)
    else:
        disable(startup_dir)
    return is_enabled(startup_dir)
