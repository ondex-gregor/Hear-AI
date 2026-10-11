"""Modelo do formulário de configurações (lógica pura, sem Qt/I/O)."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from hear_ai.config import (Config, DEFAULTS, _CHOICES, _RANGES, _valid, _valid_consent_host,
                            _valid_env_var_name)
from hear_ai.hotkey import validate_hotkey
from hear_ai.model_download import size_label
from hear_ai.gerador import validar_url

if TYPE_CHECKING:
    from hear_ai.config import Config


# --- Constantes de validação adicionais para campos do formulário ---
_BUBBLE_IDLE_OPACITY_RANGE = (0.3, 1.0)


# --- Campos que exigem reinício do modelo ---
_NEEDS_MODEL_RELOAD = {"model", "device", "compute_type"}

# --- Campos que aplicam imediatamente (sem reinício) ---
_APPLY_IMMEDIATELY = {"hotkey", "mode", "language"}


# --- modelos de voz (T46) -------------------------------------------------------------------------------
MODEL_ORDER = ("tiny", "base", "small", "medium", "large-v2", "large-v3")
ALWAYS_LISTED = ("small", "medium")  # aparecem sempre; baixado ou não
# texto honesto (decisão do líder/usuário): não promete velocidade
MODEL_HINTS = {
    "medium": "O ‘medium’ tende a errar menos, mas é mais lento e usa mais memória da placa de vídeo.",
}


@dataclass(frozen=True)
class ModelOption:
    name: str
    downloaded: bool
    label: str


def model_label(name: str, downloaded: bool) -> str:
    """"small" (baixado) ou "medium — não baixado (~1,5 GB)"."""
    if downloaded:
        return name
    size = size_label(name)
    return f"{name} — não baixado ({size})" if size else f"{name} — não baixado"


def model_options(downloaded, current: str | None = None) -> list[ModelOption]:
    """Opções da caixa "Modelo": `small` e `medium` SEMPRE (baixados ou não), mais qualquer outro já baixado e o
    modelo do config (mesmo que tenha sumido do disco: aparece como "não baixado", em vez de sumir da lista).
    `downloaded`: nomes com model.bin no disco."""
    have = set(downloaded)
    names = set(ALWAYS_LISTED) | have
    if current:
        names.add(current)
    ordered = sorted(names, key=lambda n: (MODEL_ORDER.index(n) if n in MODEL_ORDER else len(MODEL_ORDER), n))
    return [ModelOption(n, n in have, model_label(n, n in have)) for n in ordered]


def describe_model_failure(exc: BaseException) -> str:
    """Motivo curto e sem jargão para o aviso quando um modelo não carrega (nunca inclui texto ditado nem
    caminhos: só o tipo do problema)."""
    low = f"{type(exc).__name__} {exc}".lower()
    if isinstance(exc, FileNotFoundError) or "not found" in low or "não encontrado" in low:
        return "o arquivo do modelo não está na pasta de modelos"
    if isinstance(exc, MemoryError) or "out of memory" in low or "bad_alloc" in low or "alloc" in low:
        return "faltou memória"
    if "cuda" in low or "cublas" in low or "cudnn" in low:
        return "problema com a placa de vídeo"
    return "o arquivo pode estar corrompido ou incompleto"


def download_offer_text(name: str) -> str:
    size = size_label(name)
    where = f" ({size})" if size else ""
    return (f"O modelo ‘{name}’{where} ainda não foi baixado. Depois de baixado ele fica só neste computador e "
            "funciona 100% offline; a internet é usada apenas para este download.")


def download_confirm_text(name: str) -> str:
    size = size_label(name)
    return f"Isto vai baixar {size} da internet. Continuar?" if size else "Isto vai baixar o modelo da internet. Continuar?"


def download_progress_text(name: str, frac: float, done_bytes: int) -> str:
    pct = round(max(0.0, min(1.0, frac)) * 100)
    size = size_label(name)
    of = f" de {size}" if size else ""
    return f"Baixando ‘{name}’… {pct}% ({done_bytes / 1e6:.0f} MB{of})"


@dataclass
class SettingsForm:
    """Modelo do formulário de configurações (Python puro, sem Qt/I/O)."""

    hotkey: str
    mode: str
    language: str
    input_device: str | None
    model: str
    device: str
    history_enabled: bool
    history_retention_days: int
    history_paused: bool
    bubble_hide_when_idle: bool
    bubble_idle_opacity: float
    start_with_windows: bool
    # T47b (P1): Modo de operação e gerador
    operation_mode: str = "fiel"
    generator_provider: str = "ollama"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    cloud_base_url: str = ""
    cloud_model: str = ""
    cloud_key_env: str = ""
    cloud_consent: bool = False
    cloud_consent_host: str = ""
    generator_app_context: bool = False
    generator_permanent_prefs: bool = False
    mode_hotkey: str = "ctrl+alt+m"
    generator_timeout_s: int = 60

    @classmethod
    def from_config(cls, cfg: Config) -> "SettingsForm":
        """Cria o formulário a partir de um objeto Config."""
        return cls(
            hotkey=cfg.hotkey,
            mode=cfg.mode,
            language=cfg.language,
            input_device=None,  # não está no Config; é None = padrão do sistema
            model=cfg.model,
            device=cfg.device,
            history_enabled=cfg.history_enabled,
            history_retention_days=cfg.history_retention_days,
            history_paused=cfg.history_paused,
            bubble_hide_when_idle=getattr(cfg, "bubble_hide_when_idle", False),
            bubble_idle_opacity=getattr(cfg, "bubble_idle_opacity", 0.85),
            start_with_windows=False,  # não está no Config; gerenciado externamente
            # T47b (P1): Modo de operação e gerador
            operation_mode=getattr(cfg, "operation_mode", "fiel"),
            generator_provider=getattr(cfg, "generator_provider", "ollama"),
            ollama_url=getattr(cfg, "ollama_url", "http://127.0.0.1:11434"),
            ollama_model=getattr(cfg, "ollama_model", ""),
            cloud_base_url=getattr(cfg, "cloud_base_url", ""),
            cloud_model=getattr(cfg, "cloud_model", ""),
            cloud_key_env=getattr(cfg, "cloud_key_env", ""),
            cloud_consent=getattr(cfg, "cloud_consent", False),
            cloud_consent_host=getattr(cfg, "cloud_consent_host", ""),
            generator_app_context=getattr(cfg, "generator_app_context", False),
            generator_permanent_prefs=getattr(cfg, "generator_permanent_prefs", False),
            mode_hotkey=getattr(cfg, "mode_hotkey", "ctrl+alt+m"),
            generator_timeout_s=getattr(cfg, "generator_timeout_s", 60),
        )

    def validate(self, downloaded=None, current_model: str | None = None) -> dict[str, str]:
        """Valida todos os campos. Retorna dict {campo: mensagem_erro} (vazio = ok).

        `downloaded` (T46): nomes de modelos já baixados. Se informado, ESCOLHER um modelo diferente do atual
        (`current_model`) que ainda não foi baixado é recusado: o config não pode apontar para um modelo que não
        existe. Manter o modelo atual não é recusado (mesmo que o arquivo tenha sumido), senão nenhuma outra
        configuração poderia ser salva."""
        errors: dict[str, str] = {}

        # hotkey
        ok, msg = validate_hotkey(self.hotkey)
        if not ok:
            errors["hotkey"] = msg

        # mode
        if self.mode not in _CHOICES.get("mode", ()):
            errors["mode"] = f"modo inválido: '{self.mode}' (use 'hold' ou 'toggle')"

        # language
        if self.language not in _CHOICES.get("language", ()):
            errors["language"] = f"idioma inválido: '{self.language}' (use 'pt' ou 'en')"

        # device
        if self.device not in _CHOICES.get("device", ()):
            errors["device"] = f"dispositivo inválido: '{self.device}' (use 'cpu', 'cuda' ou 'auto')"

        # model (string não vazia)
        if not isinstance(self.model, str) or not self.model.strip():
            errors["model"] = "modelo deve ser uma string não vazia (ex.: 'small', 'base')"
        elif downloaded is not None and self.model != current_model and self.model not in set(downloaded):
            errors["model"] = (f"o modelo ‘{self.model}’ ainda não foi baixado: clique em “Baixar agora” "
                               "ou escolha outro modelo")

        # history_retention_days
        if not isinstance(self.history_retention_days, int) or isinstance(self.history_retention_days, bool):
            errors["history_retention_days"] = "dias de retenção deve ser inteiro"
        else:
            lo, hi = _RANGES.get("history_retention_days", (1, 3650))
            if not (lo <= self.history_retention_days <= hi):
                errors["history_retention_days"] = f"dias de retenção deve estar entre {lo} e {hi}"

        # bubble_idle_opacity
        if not isinstance(self.bubble_idle_opacity, (int, float)):
            errors["bubble_idle_opacity"] = "opacidade deve ser número"
        else:
            lo, hi = _BUBBLE_IDLE_OPACITY_RANGE
            if not (lo <= self.bubble_idle_opacity <= hi):
                errors["bubble_idle_opacity"] = f"opacidade deve estar entre {lo} e {hi}"

        # bubble_hide_when_idle (bool)
        if not isinstance(self.bubble_hide_when_idle, bool):
            errors["bubble_hide_when_idle"] = "deve ser verdadeiro ou falso"

        # history_enabled (bool)
        if not isinstance(self.history_enabled, bool):
            errors["history_enabled"] = "deve ser verdadeiro ou falso"

        # history_paused (bool)
        if not isinstance(self.history_paused, bool):
            errors["history_paused"] = "deve ser verdadeiro ou falso"

        # start_with_windows (bool)
        if not isinstance(self.start_with_windows, bool):
            errors["start_with_windows"] = "deve ser verdadeiro ou falso"

        # mode (bool)
        if not isinstance(self.mode, str):
            errors["mode"] = "modo deve ser string"

        # language (bool)
        if not isinstance(self.language, str):
            errors["language"] = "idioma deve ser string"

        # T47b (P1): validações do modo de operação e gerador
        # operation_mode
        if self.operation_mode not in _CHOICES.get("operation_mode", ()):
            errors["operation_mode"] = f"modo de operação inválido: '{self.operation_mode}' (use 'fiel' ou 'prompt')"

        # generator_provider
        if self.generator_provider not in _CHOICES.get("generator_provider", ()):
            errors["generator_provider"] = f"provedor do gerador inválido: '{self.generator_provider}' (use 'ollama' ou 'openai_compat')"

        # ollama_url (loopback only)
        if self.ollama_url and not _valid_ollama_url(self.ollama_url):
            errors["ollama_url"] = "URL do Ollama deve ser http://127.0.0.1:porta ou http://localhost:porta"

        # cloud_base_url (https required, loopback allowed without https)
        if self.cloud_base_url and not _valid_cloud_url(self.cloud_base_url):
            errors["cloud_base_url"] = "URL da nuvem deve ser https://host:porta (exceto loopback 127.0.0.1/::1)"

        # cloud_key_env (only alphanumeric and underscore)
        if self.cloud_key_env and not _valid_env_var_name(self.cloud_key_env):
            errors["cloud_key_env"] = "nome da variável de ambiente deve conter apenas letras, números e _"

        # generator_timeout_s (5-300)
        if not isinstance(self.generator_timeout_s, int) or isinstance(self.generator_timeout_s, bool):
            errors["generator_timeout_s"] = "timeout do gerador deve ser inteiro"
        elif not (5 <= self.generator_timeout_s <= 300):
            errors["generator_timeout_s"] = "timeout do gerador deve estar entre 5 e 300 segundos"

        # mode_hotkey
        ok, msg = validate_hotkey(self.mode_hotkey)
        if not ok:
            errors["mode_hotkey"] = msg

        # cloud_consent (bool)
        if not isinstance(self.cloud_consent, bool):
            errors["cloud_consent"] = "deve ser verdadeiro ou falso"

        # cloud_consent_host (vazio ou hostname simples em minúsculas)
        if not _valid_consent_host(self.cloud_consent_host):
            errors["cloud_consent_host"] = "host do aceite inválido (use só o nome do servidor, em minúsculas)"

        # generator_app_context (bool)
        if not isinstance(self.generator_app_context, bool):
            errors["generator_app_context"] = "deve ser verdadeiro ou falso"

        # generator_permanent_prefs (bool)
        if not isinstance(self.generator_permanent_prefs, bool):
            errors["generator_permanent_prefs"] = "deve ser verdadeiro ou falso"

        # ollama_model, cloud_model (strings)
        if not isinstance(self.ollama_model, str):
            errors["ollama_model"] = "deve ser string"
        if not isinstance(self.cloud_model, str):
            errors["cloud_model"] = "deve ser string"

        return errors

    def apply_to(self, cfg: Config) -> tuple[Config, set[str], set[str]]:
        """
        Aplica as alterações ao Config.

        Retorna:
            (novo_cfg, campos_mudaram, precisa_reiniciar)
        """
        changed: set[str] = set()
        needs_restart: set[str] = set()

        # Helper para comparar e atualizar
        def update(field_name: str, value) -> bool:
            current = getattr(cfg, field_name, None)
            if current != value:
                setattr(cfg, field_name, value)
                changed.add(field_name)
                if field_name in _NEEDS_MODEL_RELOAD:
                    needs_restart.add(field_name)
                return True
            return False

        # Campos diretos do Config
        update("hotkey", self.hotkey)
        update("mode", self.mode)
        update("language", self.language)
        update("model", self.model)
        update("device", self.device)
        update("history_enabled", self.history_enabled)
        update("history_retention_days", self.history_retention_days)
        update("history_paused", self.history_paused)

        # Campos extras que podem estar no Config (atributos dinâmicos)
        if hasattr(cfg, "bubble_hide_when_idle"):
            update("bubble_hide_when_idle", self.bubble_hide_when_idle)
        if hasattr(cfg, "bubble_idle_opacity"):
            update("bubble_idle_opacity", self.bubble_idle_opacity)

        # T47b (P1): Campos do modo de operação e gerador
        update("operation_mode", self.operation_mode)
        update("generator_provider", self.generator_provider)
        update("ollama_url", self.ollama_url)
        update("ollama_model", self.ollama_model)
        update("cloud_base_url", self.cloud_base_url)
        update("cloud_model", self.cloud_model)
        update("cloud_key_env", self.cloud_key_env)
        update("cloud_consent", self.cloud_consent)
        update("cloud_consent_host", self.cloud_consent_host)
        update("generator_app_context", self.generator_app_context)
        update("generator_permanent_prefs", self.generator_permanent_prefs)
        update("mode_hotkey", self.mode_hotkey)
        update("generator_timeout_s", self.generator_timeout_s)

        # Campos que não estão no Config (gerenciados externamente)
        # input_device, start_with_windows -> não aplicam ao Config

        return cfg, changed, needs_restart


def _valid_ollama_url(url: str) -> bool:
    """Valida URL do Ollama (loopback only)."""
    try:
        validar_url(url, so_loopback=True)
        return True
    except ValueError:
        return False


def _valid_cloud_url(url: str) -> bool:
    """Valida URL da nuvem (HTTPS required, loopback allowed without HTTPS)."""
    try:
        validar_url(url, so_loopback=False, exigir_https=True)
        return True
    except ValueError:
        return False

