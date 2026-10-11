"""Modo B — gerador de prompts (v1.3, T47a). Lógica pura: sem Qt; HTTP injetável nos testes.

Fluxo: transcrição fiel (Whisper local) → ``gerar_com_fallback`` → texto a colar.
Regra de ouro: NUNCA perder a fala. Qualquer falha devolve a transcrição exata.
Privacidade: o provedor local só aceita 127.0.0.1/localhost/::1; o de nuvem exige consentimento
explícito; a chave vem de uma variável de ambiente e nunca aparece em repr, log ou exceção.
Logs: só estados (provedor, duração, ok, tipo do erro) — nunca a fala, o prompt ou a chave.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger("hear_ai.gerador")

SEM_PEDIDO = "[[SEM_PEDIDO]]"
PROMPT_FILE = "gerador-de-prompts.md"
PREFS_FILE = "preferencias-permanentes.md"
TAGS = ("aplicativo_ativo", "destino", "vocabulario", "preferencias_permanentes", "transcricao")
DESTINOS = ("auto", "agente_de_codigo", "chat", "pesquisa", "escrita", "criacao")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
LOOPBACK_IPS = {"127.0.0.1", "::1"}

# motivos CONSTANTES (revisão 022): nunca a mensagem que veio do provedor
MOTIVO_INDISPONIVEL = "gerador indisponível"
MOTIVO_TIMEOUT = "o gerador demorou demais"
MOTIVO_VAZIO = "o gerador devolveu resposta vazia"
MOTIVO_SEM_PEDIDO = "sem pedido na fala"
MOTIVO_SEM_CONSENTIMENTO = "consentimento da nuvem não dado"
MOTIVO_CONFIG = "gerador mal configurado"

# (url, payload ou None para GET, timeout_s) -> dict ; levanta OSError/TimeoutError/ValueError
HttpFn = Callable[[str, "dict | None", float], dict]


# --- erros e tipos ----------------------------------------------------------------------

class GeradorErro(Exception):
    """Base. A mensagem nunca contém a fala, o prompt nem a chave."""


class GeradorIndisponivel(GeradorErro):
    pass


class GeradorTimeout(GeradorErro):
    pass


class GeradorRespostaVazia(GeradorErro):
    pass


@dataclass(frozen=True)
class Contexto:
    app: str | None = None
    destino: str = "auto"
    vocabulario: tuple[str, ...] = ()
    preferencias: str | None = None


@dataclass(frozen=True)
class Resultado:
    texto: str
    usou_gerador: bool
    motivo: str | None = None
    revisar: bool = False
    faltando: tuple[str, ...] = ()
    provedor: str | None = None
    na_nuvem: bool = False


class GeradorDePrompts(Protocol):
    nome: str
    na_nuvem: bool

    def completar(self, system_prompt: str, mensagem: str) -> str: ...


# --- entrada segura ---------------------------------------------------------------------

_TAG_RX = re.compile(r"<\s*/?\s*(?:" + "|".join(TAGS) + r")\s*>", re.IGNORECASE)


def escapar(texto: str) -> str:
    """Neutraliza marcações vindas da fala: ``<transcricao>`` vira ``‹transcricao›``.
    Só a CÓPIA enviada ao gerador é escapada; a transcrição original nunca muda."""
    return _TAG_RX.sub(lambda m: m.group(0).replace("<", "‹").replace(">", "›"), texto or "")


def app_valido(app: str | None) -> str | None:
    """Nome de processo (nunca título de janela nem caminho): sem barras, até 60 caracteres."""
    if not isinstance(app, str):
        return None
    app = app.strip()
    if not app or "\\" in app or "/" in app or len(app) > 60 or "\n" in app:
        return None
    return app


def montar_mensagem(transcricao: str, contexto: Contexto) -> str:
    partes = []
    app = app_valido(contexto.app)
    if app:
        partes.append(f"<aplicativo_ativo>{escapar(app)}</aplicativo_ativo>")
    destino = contexto.destino if contexto.destino in DESTINOS else "auto"
    partes.append(f"<destino>{destino}</destino>")
    vocab = [escapar(v.strip()) for v in contexto.vocabulario if isinstance(v, str) and v.strip()]
    if vocab:
        partes.append(f"<vocabulario>{', '.join(vocab)}</vocabulario>")
    if contexto.preferencias and contexto.preferencias.strip():
        partes.append(f"<preferencias_permanentes>{escapar(contexto.preferencias.strip())}</preferencias_permanentes>")
    partes.append(f"<transcricao>{escapar(transcricao)}</transcricao>")
    return "\n".join(partes)


# --- arquivos de prompt -----------------------------------------------------------------

def _ler(pasta, nome) -> str | None:
    if pasta is None:
        return None
    try:
        texto = (Path(pasta) / nome).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return texto if texto.strip() else None


def carregar_prompt_sistema(pasta_config, pasta_padrao) -> str:
    """A cópia do usuário (pasta do config.json) tem precedência sobre a do projeto (`prompts/`)."""
    texto = _ler(pasta_config, PROMPT_FILE) or _ler(pasta_padrao, PROMPT_FILE)
    if texto is None:
        raise GeradorIndisponivel("prompt do gerador não encontrado (prompts/gerador-de-prompts.md)")
    return texto


def carregar_preferencias(pasta_config, pasta_padrao) -> str | None:
    """Só as linhas que começam com "- " (o resto do arquivo é explicação para o usuário)."""
    texto = _ler(pasta_config, PREFS_FILE) or _ler(pasta_padrao, PREFS_FILE)
    if texto is None:
        return None
    linhas = [ln.strip() for ln in texto.splitlines() if ln.strip().startswith("- ")]
    return "\n".join(linhas) or None


# --- HTTP (urllib, sem dependência nova) ------------------------------------------------

class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    """Redirecionamento 30x vira erro (revisão 022): a fala/chave nunca seguem para outro endereço."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirecionamento bloqueado", headers, fp)


def validar_url(base_url: str, *, so_loopback: bool, exigir_https: bool = False) -> str:
    """URL estrita: http/https, sem usuário/senha, sem query/fragmento. `localhost` → 127.0.0.1.
    `so_loopback`: só 127.0.0.1/::1. `exigir_https`: https, exceto IP loopback literal."""
    u = urllib.parse.urlsplit((base_url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("endereço inválido: use http(s)://host:porta")
    if u.username is not None or u.password is not None or "@" in u.netloc:
        raise ValueError("endereço com usuário/senha não é aceito")
    if u.query or u.fragment:
        raise ValueError("endereço com parâmetros não é aceito")
    host = u.hostname.lower()
    if host == "localhost":
        host = "127.0.0.1"
    loopback = host in LOOPBACK_IPS
    if so_loopback and not loopback:
        raise ValueError("o provedor local só aceita 127.0.0.1/localhost")
    if exigir_https and u.scheme != "https" and not loopback:
        raise ValueError("endereço da nuvem precisa ser https")
    try:
        port = u.port
    except ValueError:
        raise ValueError("porta inválida") from None
    netloc = (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "")
    return urllib.parse.urlunsplit((u.scheme, netloc, u.path.rstrip("/"), "", ""))


def _http_padrao(url: str, payload: dict | None, timeout: float, headers: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="GET" if payload is None else "POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    # sem proxy do sistema: o Ollama local nunca deve passar por um proxy
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SemRedirecionar())
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _traduzir_erro(e: Exception) -> GeradorErro:
    if isinstance(e, GeradorErro):
        return e
    if isinstance(e, TimeoutError) or "timed out" in type(e).__name__.lower():
        return GeradorTimeout(MOTIVO_TIMEOUT)
    if isinstance(e, urllib.error.URLError) and isinstance(getattr(e, "reason", None), TimeoutError):
        return GeradorTimeout(MOTIVO_TIMEOUT)
    return GeradorIndisponivel(MOTIVO_INDISPONIVEL)


# --- provedores -------------------------------------------------------------------------

class OllamaProvider:
    """Ollama local. Só aceita loopback — qualquer outro host é recusado na criação."""

    nome = "ollama"
    na_nuvem = False

    def __init__(self, modelo: str, base_url: str = "http://127.0.0.1:11434", timeout_s: float = 60.0,
                 num_ctx: int = 8192, http: HttpFn | None = None):
        self.base_url = validar_url(base_url, so_loopback=True)
        self.modelo = modelo
        self.timeout_s = float(timeout_s)
        self.num_ctx = max(8192, int(num_ctx))
        self._http = http or (lambda url, payload, timeout: _http_padrao(url, payload, timeout))

    def disponivel(self) -> bool:
        try:
            self._http(self.base_url + "/api/tags", None, 1.5)
            return True
        except Exception:
            return False

    def listar_modelos(self) -> list[str]:
        try:
            data = self._http(self.base_url + "/api/tags", None, 3.0)
        except Exception as e:
            raise _traduzir_erro(e) from None
        modelos = data.get("models") if isinstance(data, dict) else None
        return [m["name"] for m in modelos or [] if isinstance(m, dict) and isinstance(m.get("name"), str)]

    def completar(self, system_prompt: str, mensagem: str) -> str:
        if not self.modelo:
            raise GeradorIndisponivel("nenhum modelo do Ollama escolhido")
        payload = {
            "model": self.modelo,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": mensagem}],
            "stream": False,
            "think": False,
            "keep_alive": "2m",
            "options": {"temperature": 0.2, "num_ctx": self.num_ctx},
        }
        try:
            data = self._http(self.base_url + "/api/chat", payload, self.timeout_s)
        except Exception as e:
            raise _traduzir_erro(e) from None
        msg = data.get("message") if isinstance(data, dict) else None
        texto = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(texto, str) or not texto.strip():
            raise GeradorRespostaVazia("o gerador devolveu resposta vazia")
        return texto

    def __repr__(self) -> str:
        return f"OllamaProvider(modelo={self.modelo!r})"


class OpenAICompatProvider:
    """Endpoint compatível com a API de chat da OpenAI. Só funciona com consentimento explícito.
    A chave é lida da variável de ambiente `chave_env` a CADA chamada e não é guardada."""

    nome = "openai_compat"
    na_nuvem = True

    def __init__(self, base_url: str, modelo: str, chave_env: str, aceito: bool, timeout_s: float = 60.0,
                 http: Callable[..., dict] | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.modelo = modelo
        self.chave_env = chave_env
        self.aceito = bool(aceito)
        self.timeout_s = float(timeout_s)
        self._http = http or _http_padrao

    def completar(self, system_prompt: str, mensagem: str) -> str:
        if not self.aceito:
            raise GeradorIndisponivel(MOTIVO_SEM_CONSENTIMENTO)
        try:
            base = validar_url(self.base_url, so_loopback=False, exigir_https=True)
        except ValueError:
            raise GeradorIndisponivel(MOTIVO_CONFIG) from None
        if not self.modelo or not self.chave_env:
            raise GeradorIndisponivel("nuvem sem modelo ou sem variável da chave configurada")
        chave = os.environ.get(self.chave_env)
        if not chave:
            raise GeradorIndisponivel("variável de ambiente da chave não definida")
        payload = {"model": self.modelo, "temperature": 0.2,
                   "messages": [{"role": "system", "content": system_prompt},
                                {"role": "user", "content": mensagem}]}
        try:
            data = self._http(base + "/chat/completions", payload, self.timeout_s,
                              {"Authorization": "Bearer " + chave})
        except Exception as e:
            raise _traduzir_erro(e) from None
        finally:
            chave = None  # noqa: F841 - não deixa a chave viva além da chamada
        try:
            texto = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            texto = None
        if not isinstance(texto, str) or not texto.strip():
            raise GeradorRespostaVazia("o gerador devolveu resposta vazia")
        return texto

    def __repr__(self) -> str:
        return f"OpenAICompatProvider(modelo={self.modelo!r}, aceito={self.aceito})"


# --- verificação de fidelidade (sem LLM) ------------------------------------------------

_NUM_RX = re.compile(r"(?:R\$\s?)?\d+(?:[.,]\d+)*\s?%?")
_URL_RX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_WINPATH_RX = re.compile(r"[A-Za-z]:\\[^\s<>\"'|?*]+")
_PATH_RX = re.compile(r"(?<![\w/])(?:[\w.-]+/)+[\w.-]+")
_FILE_RX = re.compile(r"(?<![\w.])[\w-]+\.(?:py|md|json|txt|csv|xlsx?|docx?|pdf|js|ts|tsx|html|css|toml|yaml|yml|ini|bat|ps1|sh|png|jpg)\b",
                      re.IGNORECASE)


def _limpa(item: str) -> str:
    return item.strip().rstrip(".,;:)!?")


def itens_criticos(transcricao: str, vocabulario=()) -> list[str]:
    """Números, URLs, caminhos, nomes de arquivo e termos do vocabulário presentes na fala."""
    texto = transcricao or ""
    itens: list[str] = []

    def add(x):
        x = _limpa(x)
        if x and x not in itens:
            itens.append(x)

    for rx in (_URL_RX, _WINPATH_RX):
        for m in rx.finditer(texto):
            add(m.group(0))
    sem_urls = _WINPATH_RX.sub(" ", _URL_RX.sub(" ", texto))
    for m in _PATH_RX.finditer(sem_urls):
        add(m.group(0))
    for m in _FILE_RX.finditer(sem_urls):
        add(m.group(0))
    for m in _NUM_RX.finditer(sem_urls):
        if any(ch.isdigit() for ch in m.group(0)):
            add(m.group(0))
    baixo = texto.lower()
    for termo in vocabulario or ():
        if isinstance(termo, str) and termo.strip() and termo.strip().lower() in baixo:
            add(termo.strip())
    return itens


def _norm_num(s: str) -> str:
    return re.sub(r"\s", "", s).replace(",", ".").lower()


def faltando(itens, saida: str) -> list[str]:
    """Itens que não aparecem na saída (sem caixa; números aceitam vírgula/ponto e espaços)."""
    baixa = (saida or "").lower()
    norm = _norm_num(saida or "")
    falta = []
    for item in itens:
        if item.lower() in baixa:
            continue
        if any(ch.isdigit() for ch in item) and _norm_num(item) in norm:
            continue
        falta.append(item)
    return falta


# --- orquestração -----------------------------------------------------------------------

_CERCA_RX = re.compile(r"^\s*```[\w-]*\s*\n(?P<corpo>.*)\n\s*```\s*$", re.DOTALL)
_PREAMBULO_RX = re.compile(r"^\s*(?:aqui está|aqui esta|segue)[^\n]*:\s*\n+", re.IGNORECASE)


def _limpar_saida(texto: str) -> str:
    """Tira só embrulhos óbvios: cerca de código em volta de TUDO, ou uma linha "Aqui está…:" no início."""
    m = _CERCA_RX.match(texto)
    if m:
        texto = m.group("corpo")
    texto = _PREAMBULO_RX.sub("", texto, count=1)
    return texto.strip()


def gerar_com_fallback(provedor, transcricao: str, contexto: Contexto, system_prompt: str,
                       tentar_de_novo: bool = False, relogio=time.monotonic) -> Resultado:
    """Nunca perde a fala: qualquer falha → ``Resultado(texto=transcricao, usou_gerador=False)``."""
    nome = getattr(provedor, "nome", None)
    nuvem = bool(getattr(provedor, "na_nuvem", False))

    def fiel(motivo: str) -> Resultado:
        return Resultado(transcricao, False, motivo, provedor=nome, na_nuvem=nuvem)

    if not transcricao or not transcricao.strip():
        return fiel("transcrição vazia")
    if provedor is None:
        return fiel("gerador indisponível")
    mensagem = montar_mensagem(transcricao, contexto)
    itens = itens_criticos(transcricao, contexto.vocabulario)

    def uma_vez() -> tuple[str | None, str | None]:
        inicio = relogio()
        try:
            bruto = provedor.completar(system_prompt, mensagem)
        except GeradorErro as e:
            log.info("gerador: provedor=%s ok=0 erro=%s ms=%d", nome, type(e).__name__, (relogio() - inicio) * 1000)
            # motivo constante por TIPO (revisão 022): nunca o texto da exceção do provedor
            if isinstance(e, GeradorTimeout):
                return None, MOTIVO_TIMEOUT
            if isinstance(e, GeradorRespostaVazia):
                return None, MOTIVO_VAZIO
            return None, MOTIVO_INDISPONIVEL
        except Exception as e:  # provedor de terceiros com erro inesperado
            log.info("gerador: provedor=%s ok=0 erro=%s ms=%d", nome, type(e).__name__, (relogio() - inicio) * 1000)
            return None, "gerador indisponível"
        log.info("gerador: provedor=%s ok=1 ms=%d", nome, (relogio() - inicio) * 1000)
        return _limpar_saida(bruto or ""), None

    saida, erro = uma_vez()
    if saida is None:
        return fiel(erro or "gerador indisponível")
    if not saida:
        return fiel("o gerador devolveu resposta vazia")
    if saida.strip() == SEM_PEDIDO or saida.strip().startswith(SEM_PEDIDO):
        return fiel("sem pedido na fala")

    falta = faltando(itens, saida)
    if falta and tentar_de_novo and not nuvem:
        saida2, _erro2 = uma_vez()
        if saida2 and SEM_PEDIDO not in saida2:
            falta2 = faltando(itens, saida2)
            if len(falta2) < len(falta):
                saida, falta = saida2, falta2
    return Resultado(saida, True, None, revisar=bool(falta), faltando=tuple(falta), provedor=nome, na_nuvem=nuvem)
