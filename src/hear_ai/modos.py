"""Modo de operação (T47b P4): liga o gerador de prompts (Modo B) ao app, sem Qt.

- Modo A "Transcrição fiel" (padrão): o fluxo de sempre; zero rede.
- Modo B "Gerador de prompts": transcrição fiel → `gerador.gerar_com_fallback` → colar. Qualquer falha cola a
  transcrição fiel (nunca perde a fala).

Aqui ficam: o provedor a partir do config, o ESTADO HONESTO do provedor (para as Configurações), o `prompt_step` que o
`Controller` chama (em thread de trabalho), a aplicação do modo ao controller e o atalho do modo.
Nada daqui registra texto ditado, chave de API ou título de janela.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from hear_ai import gerador
from hear_ai.config import PROJECT_ROOT
from hear_ai.hotkey import HotkeyBinder, parse_hotkey, validate_hotkey

try:  # P3 (contexto do app) pode ainda não existir: sem ele o contexto do aplicativo é simplesmente None
    from hear_ai import app_context
except Exception:  # noqa: BLE001
    app_context = None

log = logging.getLogger("hear_ai.modos")

MODE_FIEL = "fiel"
MODE_PROMPT = "prompt"
MODE_LABELS = {MODE_FIEL: "Transcrição fiel", MODE_PROMPT: "Gerador de prompts"}
MODE_BADGES = {MODE_FIEL: "A", MODE_PROMPT: "B"}

UNAVAILABLE_LOCAL = "indisponível: gerador local não encontrado"
FALLBACK_NOTICE = "gerador indisponível — colei a transcrição"
NO_PROVIDER_WARNING = "Sem o gerador disponível, o Modo B cola a transcrição fiel da sua fala."
CLOUD_CONSENT_TEXT = ("Este modo envia o TEXTO da sua fala (nunca o áudio), o nome do aplicativo ativo e seu "
                      "vocabulário para {provedor}.")

# Tamanhos conferidos no catálogo oficial do Ollama em 2026-10-10 (ollama.com/library/<modelo>/tags)
OLLAMA_INSTRUCTIONS = (
    "1) Instale o Ollama: ollama.com/download (não é instalado pelo Hear AI).",
    "2) Baixe um modelo no terminal, por exemplo:  ollama pull qwen3:8b  (5,2 GB)  ou  ollama pull llama3.1:8b "
    "(4,9 GB);  para pouca memória,  ollama pull qwen3:4b  (2,5 GB). Confira o tamanho no catálogo do Ollama "
    "(ollama.com/library) antes de baixar.",
    "3) Volte aqui e clique em “Verificar de novo”.",
)


def is_prompt_mode(cfg) -> bool:
    return getattr(cfg, "operation_mode", MODE_FIEL) == MODE_PROMPT


def host_of(url) -> str:
    """Hostname normalizado (minúsculas, sem porta/caminho/ponto final) de uma URL; "" se não houver."""
    try:
        host = urlparse(url or "").hostname
    except ValueError:
        host = None
    return (host or "").lower().rstrip(".")


def consent_ok(cfg) -> bool:
    """O aceite da nuvem vale AGORA? Só se `cloud_consent` for verdadeiro E o host atual de `cloud_base_url` for
    exatamente o host para o qual o aceite foi dado (`cloud_consent_host`). Editar a URL no config, fora da janela,
    invalida o aceite; config antigo (aceite sem host) é "pendente"."""
    host = host_of(cfg.cloud_base_url)
    aceito = (getattr(cfg, "cloud_consent_host", "") or "").strip().lower().rstrip(".")
    return bool(cfg.cloud_consent) and bool(host) and host == aceito


def uses_cloud(cfg) -> bool:
    """Modo B ligado, provedor de nuvem escolhido e consentimento VÁLIDO: cada uso envia TEXTO para fora."""
    return is_prompt_mode(cfg) and cfg.generator_provider == "openai_compat" and consent_ok(cfg)


def cloud_host(cfg) -> str:
    """Nome do provedor para o texto do consentimento: o host da URL configurada."""
    try:
        host = urlparse(cfg.cloud_base_url or "").hostname
    except ValueError:
        host = None
    return host or "o provedor configurado"


def consent_text(cfg) -> str:
    return CLOUD_CONSENT_TEXT.format(provedor=cloud_host(cfg))


# --- provedor ----------------------------------------------------------------------------------------------

def build_provider(cfg, *, http=None):
    """O provedor configurado, ou None se não puder ser usado (URL fora do loopback, nuvem sem consentimento).
    A nuvem só é criada com `cloud_consent` verdadeiro: a interface checa o aceite ANTES de qualquer chamada."""
    if cfg.generator_provider == "openai_compat":
        if not consent_ok(cfg):  # aceite ausente, antigo (sem host) ou dado para OUTRO host: nada é criado
            return None
        return gerador.OpenAICompatProvider(cfg.cloud_base_url, cfg.cloud_model, cfg.cloud_key_env, True,
                                            cfg.generator_timeout_s, http=http)
    try:
        return gerador.OllamaProvider(cfg.ollama_model, cfg.ollama_url, cfg.generator_timeout_s, http=http)
    except ValueError:
        return None


@dataclass(frozen=True)
class ProviderStatus:
    kind: str  # "ollama" | "openai_compat"
    ok: bool  # dá para gerar prompts agora?
    text: str  # frase para a janela
    models: tuple[str, ...] = ()
    na_nuvem: bool = False
    instructions: tuple[str, ...] = ()  # o que fazer, quando o gerador local não foi encontrado


def provider_status(cfg, *, http=None, environ=None) -> ProviderStatus:
    """Estado honesto do provedor. Ollama: pergunta ao servidor local (127.0.0.1) quais modelos há — PODE bloquear
    até ~3 s: chame fora da thread do Qt. Nuvem: só olha o config (nunca chama a rede para "testar")."""
    environ = os.environ if environ is None else environ
    if cfg.generator_provider == "openai_compat":
        faltam = [nome for nome, valor in (("URL", cfg.cloud_base_url), ("modelo", cfg.cloud_model),
                                           ("nome da variável da chave", cfg.cloud_key_env)) if not valor]
        if faltam:
            return ProviderStatus("openai_compat", False, "nuvem incompleta: faltam " + ", ".join(faltam),
                                  na_nuvem=True)
        if not environ.get(cfg.cloud_key_env):  # só se existe; o valor da chave nunca é lido para fora
            return ProviderStatus("openai_compat", False,
                                  f"a variável de ambiente ‘{cfg.cloud_key_env}’ não está definida", na_nuvem=True)
        if not consent_ok(cfg):
            aceito = (getattr(cfg, "cloud_consent_host", "") or "").strip()
            if cfg.cloud_consent and aceito:  # o aceite existe, mas é de outro endereço
                texto = f"o aceite foi dado para ‘{aceito}’; aceite de novo para ‘{host_of(cfg.cloud_base_url)}’"
            else:
                texto = "falta o seu aceite do envio do texto da fala"
            return ProviderStatus("openai_compat", False, texto, na_nuvem=True)
        return ProviderStatus("openai_compat", True,
                              f"configurado (nuvem: {cloud_host(cfg)}); a conexão só é feita ao ditar", na_nuvem=True)
    try:
        provedor = gerador.OllamaProvider(cfg.ollama_model, cfg.ollama_url, cfg.generator_timeout_s, http=http)
    except ValueError:
        return ProviderStatus("ollama", False, "URL do Ollama inválida (só 127.0.0.1 ou localhost)")
    try:
        modelos = tuple(provedor.listar_modelos())
    except Exception:  # noqa: BLE001 - qualquer falha = não encontrado
        return ProviderStatus("ollama", False, UNAVAILABLE_LOCAL, instructions=OLLAMA_INSTRUCTIONS)
    if not modelos:
        return ProviderStatus("ollama", False, "Ollama encontrado, mas sem nenhum modelo instalado",
                              instructions=OLLAMA_INSTRUCTIONS[1:])
    lista = ", ".join(modelos)
    if cfg.ollama_model and cfg.ollama_model in modelos:
        return ProviderStatus("ollama", True, f"encontrado, modelos: {lista}; usando ‘{cfg.ollama_model}’", modelos)
    if cfg.ollama_model:
        return ProviderStatus("ollama", False,
                              f"encontrado, modelos: {lista}; o modelo ‘{cfg.ollama_model}’ não está instalado", modelos)
    return ProviderStatus("ollama", False, f"encontrado, modelos: {lista}; escolha um modelo", modelos)


# --- o passo do Controller ---------------------------------------------------------------------------------

def _vocab_terms(cfg, vocab_store) -> tuple[str, ...]:
    from hear_ai.vocab import prompt_terms

    texto = prompt_terms(vocab_store, cfg.dictionary)
    return tuple(t.strip() for t in texto.rstrip(".").split(",") if t.strip()) if texto else ()


def make_prompt_step(cfg, *, config_dir, prompts_dir=None, vocab_store=None, http=None):
    """O callable `(transcricao_fiel) -> gerador.Resultado` que o `Controller(prompt_step=...)` chama numa thread de
    trabalho (nunca na do Qt). Lê o `cfg` ao vivo a cada chamada: trocar modelo/URL/consentimento nas Configurações
    vale no ditado seguinte, sem remontar nada. Nunca levanta por falta de provedor: devolve a transcrição fiel."""
    pasta_prompts = Path(prompts_dir) if prompts_dir is not None else PROJECT_ROOT / "prompts"

    def passo(transcricao: str) -> gerador.Resultado:
        if cfg.generator_provider == "openai_compat" and not consent_ok(cfg):
            # a interface checa o aceite (válido para o host ATUAL) ANTES de chamar o provedor; o motivo do
            # fallback é genérico de propósito
            return gerador.Resultado(transcricao, False, gerador.MOTIVO_SEM_CONSENTIMENTO,
                                     provedor="openai_compat", na_nuvem=True)
        provedor = build_provider(cfg, http=http)
        try:
            sistema = gerador.carregar_prompt_sistema(config_dir, pasta_prompts)
        except gerador.GeradorErro:
            return gerador.Resultado(transcricao, False, gerador.MOTIVO_CONFIG,
                                     provedor=getattr(provedor, "nome", None),
                                     na_nuvem=bool(getattr(provedor, "na_nuvem", False)))
        app, destino = None, "auto"
        if cfg.generator_app_context and app_context is not None:  # desligado: o Win32 nem é consultado
            try:
                app = app_context.foreground_process_name()
                destino = app_context.destino_para(app)
            except Exception as e:  # noqa: BLE001
                log.warning("contexto do aplicativo indisponível: %s", type(e).__name__)
                app, destino = None, "auto"
        preferencias = None
        if cfg.generator_permanent_prefs:
            preferencias = gerador.carregar_preferencias(config_dir, pasta_prompts)
        contexto = gerador.Contexto(app=app, destino=destino, vocabulario=_vocab_terms(cfg, vocab_store),
                                    preferencias=preferencias)
        return gerador.gerar_com_fallback(provedor, transcricao, contexto, sistema)

    return passo


def apply_operation_mode(controller, cfg, config_path, *, vocab_store=None, http=None) -> None:
    """Liga/desliga o Modo B no controller: `prompt_step` só existe com `operation_mode == "prompt"`."""
    if is_prompt_mode(cfg):
        controller.prompt_step = make_prompt_step(cfg, config_dir=Path(config_path).parent,
                                                  vocab_store=vocab_store, http=http)
    else:
        controller.prompt_step = None


# --- atalho do modo ----------------------------------------------------------------------------------------

class ModeHotkey(HotkeyBinder):
    """Atalho global que ALTERNA o modo (A ⇄ B). É um segundo `RegisterHotKey` com id próprio. Conflito (é o mesmo do
    ditado, ou outro programa já usa) → não registra, avisa por `on_conflict`, e o resto do app funciona."""

    HOTKEY_ID = 0x4842  # "HB"

    def __init__(self, hotkey, on_toggle, main_hotkey=None, api=None, on_conflict=None, **kw):
        ok, canon = validate_hotkey(hotkey)
        self._invalid = None if ok else canon
        super().__init__(hotkey if ok else "ctrl+alt+m", "toggle", None, api=api, on_conflict=on_conflict, **kw)
        self.on_toggle = on_toggle
        ok_main, main_canon = validate_hotkey(main_hotkey) if main_hotkey else (False, None)
        self.main_hotkey = main_canon if ok_main else None

    def install(self, timeout: float = 5.0) -> bool:
        if self._invalid is not None:
            return self._fail(f"atalho do modo inválido: {self._invalid}")
        if self.main_hotkey and self.hotkey == self.main_hotkey:
            return self._fail(f"o atalho do modo ({self.hotkey}) é o mesmo do ditado", conflict=True)
        return super().install(timeout)

    def _on_hotkey(self) -> None:
        try:
            self.on_toggle()
        except Exception as e:  # noqa: BLE001 - o atalho nunca derruba a thread de mensagens
            log.warning("alternar o modo falhou: %s", type(e).__name__)

    def rebind(self, hotkey, main_hotkey=None) -> bool:
        """Troca o atalho (Configurações): solta o antigo e tenta registrar o novo."""
        self.uninstall()
        ok, canon = validate_hotkey(hotkey)
        self._invalid = None if ok else canon
        if ok:
            self.hotkey = canon
            self._mods, self._key = parse_hotkey(canon)
            self.key_names = list(self._mods) + [self._key]
        if main_hotkey is not None:
            self.main_hotkey = main_hotkey
        return self.install()
