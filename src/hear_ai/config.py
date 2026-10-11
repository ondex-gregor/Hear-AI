"""Leitura e gravação de config.json, com validação e valores padrão."""

from __future__ import annotations

import re

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger("hear_ai.config")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONFIG_VERSION = 2

# Padrões que a v1 gravava no config.json e que mudaram na v1.1. Num arquivo SEM
# `config_version` (v1), um valor IGUAL ao padrão antigo é tratado como "não escolhido"
# e recebe o padrão novo só em memória (D16); o arquivo não é regravado.
V1_CHANGED_DEFAULTS = {
    "hotkey": "ctrl+alt+space",  # novo: ctrl+alt+h (D17, docs/ATALHO.md)
    "history_enabled": False,  # novo: True (A5)
}
V1_PASTE_DELAY_MS = 150  # padrão antigo de paste_delay_ms

# limites (inclusive) de campos numéricos
_RANGES = {
    "max_record_minutes": (1, 180),
    "history_retention_days": (1, 3650),
    "paste_delay_ms": (0, 10_000),
    "paste_base_ms": (0, 10_000),
    "paste_chars_per_s": (100, 1_000_000),
    "paste_max_ms": (0, 30_000),
    "config_version": (1, 1_000),
    "generator_timeout_s": (5, 300),
}
# limites de campos decimais (float)
_FLOAT_RANGES = {
    "bubble_idle_opacity": (0.3, 1.0),
}

_CHOICES = {
    "mode": ("hold", "toggle"),
    "language": ("pt", "en", "auto"),  # auto: o Whisper detecta (pt/en), na dúvida "pt" (D3)
    "device": ("cpu", "cuda", "auto"),
    "compute_type": ("int8", "int8_float16", "int8_float32", "float16", "float32"),
    "operation_mode": ("fiel", "prompt"),
    "generator_provider": ("ollama", "openai_compat"),
}


def _default_dictionary() -> dict:
    from hear_ai.postprocess import DEFAULT_DICTIONARY

    return dict(DEFAULT_DICTIONARY)


@dataclass
class Config:
    config_version: int = CONFIG_VERSION
    hotkey: str = "ctrl+alt+h"
    mode: str = "hold"
    language: str = "pt"
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    beep: bool = True
    restore_clipboard: bool = True
    paste_delay_ms: int = V1_PASTE_DELAY_MS  # v1; hoje só migra para paste_base_ms
    paste_base_ms: int = 250  # espera mínima antes de restaurar a área de transferência
    paste_chars_per_s: int = 2500  # + 1 s a cada N caracteres colados
    paste_max_ms: int = 3000  # espera máxima
    max_record_minutes: int = 30
    history_enabled: bool = True
    history_retention_days: int = 30
    history_paused: bool = False
    history_dir: str = "historico"
    export_dir: str | None = None
    models_dir: str = "models"
    log_dir: str = "logs"
    # T23 (janela de Configurações)
    input_device: str | None = None  # nome do microfone; None = padrão do Windows
    bubble_hide_when_idle: bool = False
    bubble_idle_opacity: float = 0.85  # opacidade da bolha em repouso (0,3–1,0)
    start_with_windows: bool = False  # espelho do atalho na pasta Inicializar (startup.py)
    # T30 (D3 qualidade): dicionário de substituições, comandos de voz e prompt do Whisper
    dictionary: dict = field(default_factory=_default_dictionary)
    voice_commands: bool = True
    use_initial_prompt: bool = True  # medido na T30 (evidencias/D3-qualidade.txt): nomes 0% -> 100%
    # T32: primeira execução guiada já vista? (ausente no config da v1 -> False: quem já usava
    # também vê uma vez, porque o atalho mudou)
    first_run_done: bool = False
    # T47b (P1): Modo de operação e gerador de prompts
    operation_mode: str = "fiel"  # "fiel" | "prompt"
    generator_provider: str = "ollama"  # "ollama" | "openai_compat"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    cloud_base_url: str = ""
    cloud_model: str = ""
    cloud_key_env: str = ""  # NOME da variável de ambiente; a chave NUNCA vai para config/log/Git
    cloud_consent: bool = False
    # host (minúsculas) para o qual o aceite foi dado; o aceite só vale se o host ATUAL de cloud_base_url for este.
    # Config antigo (cloud_consent=true sem host) = aceite pendente.
    cloud_consent_host: str = ""
    generator_app_context: bool = False
    generator_permanent_prefs: bool = False
    mode_hotkey: str = "ctrl+alt+m"
    generator_timeout_s: int = 60


DEFAULTS = Config()


def _valid(name: str, value) -> bool:
    default = getattr(DEFAULTS, name)
    if name in _CHOICES:
        return value in _CHOICES[name]
    if name == "dictionary":
        return (isinstance(value, dict) and len(value) <= 500
                and all(isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
                        for k, v in value.items()))
    if name in ("export_dir", "input_device"):
        return value is None or (isinstance(value, str) and bool(value.strip()))
    if name == "cloud_base_url":
        return value is None or (isinstance(value, str) and (value.strip() == "" or _valid_url_https(value)))
    if name == "ollama_url":
        return isinstance(value, str) and _valid_url_loopback(value)
    if name == "cloud_key_env":
        return isinstance(value, str) and (value == "" or _valid_env_var_name(value))
    if name == "cloud_consent_host":
        return _valid_consent_host(value)
    if name == "generator_timeout_s":
        return isinstance(value, int) and not isinstance(value, bool) and 5 <= value <= 300
    if name == "mode_hotkey":
        return isinstance(value, str) and _valid_mode_hotkey(value)
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        lo, hi = _FLOAT_RANGES.get(name, (float("-inf"), float("inf")))
        return lo <= value <= hi
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        lo, hi = _RANGES.get(name, (0, None))
        return value >= lo and (hi is None or value <= hi)
    if isinstance(default, str):
        return isinstance(value, str) and bool(value.strip())
    return False


def _valid_url_loopback(url: str) -> bool:
    """URL do Ollama local: a MESMA regra do gerador (uma só fonte da verdade)."""
    from hear_ai.gerador import validar_url

    try:
        validar_url(url, so_loopback=True)
        return True
    except (ValueError, TypeError):
        return False


def _valid_url_https(url: str) -> bool:
    """URL da nuvem: https (http só em IP loopback), sem credenciais — mesma regra do gerador."""
    from hear_ai.gerador import validar_url

    try:
        validar_url(url, so_loopback=False, exigir_https=True)
        return True
    except (ValueError, TypeError):
        return False


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _valid_env_var_name(name: str) -> bool:
    """Nome de variável de ambiente (ASCII): é o NOME, nunca a chave."""
    return isinstance(name, str) and bool(_ENV_NAME.fullmatch(name))


_HOST_NAME = re.compile(r"[a-z0-9:](?:[a-z0-9.:-]{0,251}[a-z0-9:])?")  # inclui IPv6 loopback ::1


def _valid_consent_host(value) -> bool:
    """Vazio (aceite pendente) ou um hostname simples em minúsculas (também IP/::1): nunca URL, caminho ou espaço."""
    return isinstance(value, str) and (value == "" or bool(_HOST_NAME.fullmatch(value)))


def _valid_mode_hotkey(hotkey: str) -> bool:
    """Valida hotkey do modo usando a mesma lógica do atalho principal."""
    from hear_ai.hotkey import validate_hotkey
    ok, _ = validate_hotkey(hotkey)
    return ok


def resolve_path(value: str | Path, root: Path | None = None) -> Path:
    """Caminhos relativos valem a partir da raiz do projeto."""
    p = Path(value)
    return p if p.is_absolute() else (root or PROJECT_ROOT) / p


def save_config(cfg: Config, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        cfg = Config()
        save_config(cfg, path)
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("raiz do JSON não é um objeto")
    except (OSError, ValueError) as exc:
        log.warning("config ilegível (%s); usando padrões", exc)
        return Config()

    known = {f.name for f in fields(Config)}
    values = {}
    for key, value in raw.items():
        if key not in known:
            log.warning("chave desconhecida ignorada em config: %s", key)
        elif _valid(key, value):
            values[key] = value
        else:
            log.warning("valor inválido para '%s'; usando padrão", key)
    if "config_version" not in raw:
        _migrate_v1(values)
    values["config_version"] = CONFIG_VERSION
    return Config(**values)


def _migrate_v1(values: dict) -> None:
    """Arquivo da v1 (sem config_version): ajusta SÓ em memória (D16)."""
    for key, old_default in V1_CHANGED_DEFAULTS.items():
        if key in values and values[key] == old_default:
            del values[key]  # "não escolhido": vale o padrão novo
            log.info("config v1: '%s' com o padrão antigo; usando o padrão novo", key)
    # quem mudou o atraso da colagem na v1 continua com ele como espera mínima
    old = values.get("paste_delay_ms")
    if old is not None and old != V1_PASTE_DELAY_MS and "paste_base_ms" not in values:
        values["paste_base_ms"] = old
