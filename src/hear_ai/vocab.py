"""Vocabulário contextual e aprendizagem confirmada (v1.3, T45a). Lógica pura, sem Qt e sem rede.

Fidelidade primeiro: só corrige variantes que NÃO são palavras legítimas (``undex``, ``cloude``) ou
que aparecem num contexto inequívoco (``cloud code`` → ``Claude Code``). Em dúvida, não muda.

Ordem de prioridade quando duas regras disputam o mesmo trecho:
correções confirmadas pelo usuário > dicionário do config (``extras``) > regras embutidas.
Nada é aprendido sem ``VocabStore.confirm`` (chamado só por ação explícita do usuário).
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Pattern

log = logging.getLogger("hear_ai.vocab")

MAX_TERM_CHARS = 60
PROMPT_MAX_TERMS = 40
PROMPT_MAX_CHARS = 300
SUGGEST_MAX_WORDS = 3
SUGGEST_MAX_FRACTION = 0.40

# palavras curtas/comuns que nunca podem ser o "ouvido" de uma correção aprendida
STOPWORDS_PT_EN = frozenset("""
a o e é as os um uma uns umas de da do das dos em na no nas nos para pra por com sem que se
mas ou eu tu ele ela nós vós eles elas me te lhe isso isto aquilo não sim já ai aí lá cá
the an and or of to in on at is it i you he she we they be are was not yes no this that
""".split())

# letras que o Whisper às vezes devolve acentuadas: cada letra casa também com as variantes
_ACCENT_VARIANTS = {
    "a": "aáàâãä", "e": "eéèêë", "i": "iíìîï", "o": "oóòôõö", "u": "uúùûü", "c": "cç", "n": "nñ",
}


def _normalize(text: str) -> str:
    """Minúsculas, sem acentos, espaços colapsados (para comparar termos)."""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join(text.split())


def _term_regex(term: str) -> str:
    """Regex de um termo inteiro: sem caixa/acento por letra, espaços flexíveis entre palavras."""
    words = []
    for word in term.split():
        parts = []
        for ch in word:
            base = _normalize(ch)
            if len(base) == 1 and base in _ACCENT_VARIANTS:
                parts.append("[" + _ACCENT_VARIANTS[base] + "]")
            else:
                parts.append(re.escape(ch))
        words.append("".join(parts))
    return r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)"


@dataclass(frozen=True)
class Rule:
    """Regra embutida. Se o padrão tiver o grupo ``alvo``, só esse trecho é trocado."""
    id: str
    pattern: Pattern[str]
    replacement: Callable[[re.Match], str] | str


def _rx(pattern: str) -> Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


_PRODUCTS = {"code": "Code", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "desktop": "Desktop"}

BUILTIN_RULES: tuple[Rule, ...] = (
    # Ondex: variantes que não são palavras de verdade (sem contexto)
    Rule("ondex", _rx(r"(?<!\w)(?:undex|aondex|ondecs|[oóòôõ]nd[eéèê]x|on\s+dex|a\s+um\s+dex)(?!\w)"), "Ondex"),
    # Hear AI: SEMPRE exige "ai" / "a i" / "a.i" depois; "here"/"hear" sozinhos nunca mudam
    Rule("hear_ai", _rx(r"(?<!\w)(?:hear|heer|here|hir)\s+(?:a\.\s?i|a\s+i|ai)(?!\w)"), "Hear AI"),
    # Claude: grafias que não existem como palavra
    Rule("claude", _rx(r"(?<!\w)(?:cloude|cl[oô]de|claud)(?!\w)"), "Claude"),
    # "cloud" + produto da Anthropic → "Claude <Produto>"
    Rule("cloud_produto",
         _rx(r"(?<!\w)(?P<alvo>cloud\s+(?P<prod>code|opus|sonnet|haiku|desktop))(?![\w-])"),
         lambda m: "Claude " + _PRODUCTS[m.group("prod").lower()]),
    # "cloud" com contexto forte de conversa com o assistente
    Rule("cloud_contexto",
         _rx(r"(?<!\w)(?:usar\s+o|pergunta\s+ao|pergunte\s+ao|perguntar\s+ao|falar\s+com\s+o|conversar\s+com\s+o)"
             r"\s+(?P<alvo>cloud)(?!\w)"),
         "Claude"),
)

# termos canônicos das regras embutidas (vão para o initial_prompt do Whisper)
BUILTIN_TERMS: tuple[str, ...] = ("Ondex", "Hear AI", "Claude", "Claude Code")


@dataclass(frozen=True)
class Mudanca:
    ouvido: str      # trecho como veio do Whisper
    correto: str     # como ficou
    regra: str       # "confirmado", "dicionario" ou o id da regra embutida
    posicao: int     # índice no texto ORIGINAL


@dataclass(frozen=True)
class Resultado:
    texto: str
    mudancas: tuple[Mudanca, ...] = field(default_factory=tuple)


def _pair_rules(pairs: dict | None, rule_id: str) -> list[Rule]:
    rules = []
    if not isinstance(pairs, dict):
        return rules
    # mais palavras / mais longo primeiro (mais específico)
    for heard, correct in sorted(pairs.items(), key=lambda kv: (-len(str(kv[0]).split()), -len(str(kv[0])))):
        if not isinstance(heard, str) or not isinstance(correct, str):
            continue
        heard, correct = heard.strip(), correct.strip()
        if not heard or not correct:
            continue
        rules.append(Rule(rule_id, _rx(_term_regex(heard)), correct))
    return rules


def corrigir(text: str, extras: dict | None = None, confirmados: dict | None = None) -> Resultado:
    """Corrige termos conhecidos. Devolve o texto e a lista do que mudou (para a interface)."""
    if not text:
        return Resultado(text or "")
    layers = [_pair_rules(confirmados, "confirmado"), _pair_rules(extras, "dicionario"), list(BUILTIN_RULES)]

    # candidatos no texto ORIGINAL; um trecho já correto também "reserva" o lugar (protege)
    candidates = []  # (prioridade, início, fim, novo, ouvido, regra)
    for prio, rules in enumerate(layers):
        for rule in rules:
            for m in rule.pattern.finditer(text):
                if "alvo" in rule.pattern.groupindex and m.group("alvo") is not None:
                    start, end = m.span("alvo")
                else:
                    start, end = m.span()
                new = rule.replacement(m) if callable(rule.replacement) else rule.replacement
                candidates.append((prio, start, end, new, text[start:end], rule.id))
    candidates.sort(key=lambda c: (c[0], c[1], -(c[2] - c[1])))

    accepted = []
    for cand in candidates:
        _p, start, end = cand[0], cand[1], cand[2]
        if any(start < a_end and a_start < end for _, a_start, a_end, *_rest in accepted):
            continue
        accepted.append(cand)

    out = text
    changes = []
    for _p, start, end, new, heard, rule_id in sorted(accepted, key=lambda c: c[1], reverse=True):
        if heard == new:
            continue
        out = out[:start] + new + out[end:]
        changes.append(Mudanca(heard, new, rule_id, start))
    changes.sort(key=lambda m: m.posicao)
    if changes:
        log.debug("vocabulário: %d correção(ões)", len(changes))  # nunca o texto
    return Resultado(out, tuple(changes))


# --- aprendizagem confirmada ------------------------------------------------------------

def _valid_pair(heard: str, correct: str) -> bool:
    if not isinstance(heard, str) or not isinstance(correct, str):
        return False
    heard, correct = heard.strip(), correct.strip()
    if not heard or not correct:
        return False
    if len(heard) > MAX_TERM_CHARS or len(correct) > MAX_TERM_CHARS:
        return False
    if any(c in heard + correct for c in "\r\n\t"):
        return False
    if _normalize(heard) == _normalize(correct) and heard.lower() == correct.lower():
        return False
    norm = _normalize(heard)
    if len(norm) <= 1 or norm in STOPWORDS_PT_EN:
        return False
    return True


class VocabStore:
    """Correções confirmadas pelo usuário, em JSON local (``vocabulario.json`` ao lado do config).

    Só guarda pares ouvido→correto: nada de áudio, nada de frases inteiras."""

    VERSION = 1

    def __init__(self, path):
        self.path = Path(path)
        self._entries: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        raw = data.get("confirmados") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {k.strip(): v.strip() for k, v in raw.items() if _valid_pair(k, v)}

    def _save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({"version": self.VERSION, "confirmados": self._entries},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError as e:
            log.warning("não foi possível salvar o vocabulário: %s", type(e).__name__)
            return False

    def entries(self) -> dict[str, str]:
        return dict(self._entries)

    def confirm(self, ouvido: str, correto: str) -> bool:
        """ÚNICA forma de aprender. Devolve False se o par for recusado (vazio, longo, stopword...)."""
        if not _valid_pair(ouvido, correto):
            return False
        ouvido, correto = ouvido.strip(), correto.strip()
        # mesma chave sem caixa/acento substitui a antiga
        for key in [k for k in self._entries if _normalize(k) == _normalize(ouvido)]:
            del self._entries[key]
        self._entries[ouvido] = correto
        return self._save()

    def remove(self, ouvido: str) -> bool:
        keys = [k for k in self._entries if _normalize(k) == _normalize(ouvido or "")]
        for key in keys:
            del self._entries[key]
        return bool(keys) and self._save()

    def forget_all(self) -> bool:
        self._entries = {}
        return self._save()


# --- sugestões a partir de uma edição do usuário ---------------------------------------

_EDGE_PUNCT = ".,;:!?()[]{}\"'«»“”‘’…"


def _words(text: str) -> list[str]:
    return [w for w in (text or "").split() if w.strip(_EDGE_PUNCT)]


def suggest_pairs(raw: str, final: str) -> list[tuple[str, str]]:
    """Pares curtos que mudaram entre o texto do Whisper e o texto editado pelo usuário.

    Só SUGERE (a interface mostra e o usuário confirma). Reescrita grande não é correção: devolve []."""
    a, b = _words(raw), _words(final)
    if not a or not b:
        return []
    # com caixa e acento: "heer ai" -> "Hear AI" precisa sair inteiro (não só "heer" -> "Hear")
    key = lambda w: w.strip(_EDGE_PUNCT)  # noqa: E731
    sm = difflib.SequenceMatcher(a=[key(w) for w in a], b=[key(w) for w in b], autojunk=False)
    changed = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")
    if changed / max(len(a), 1) > SUGGEST_MAX_FRACTION:
        return []
    pairs: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace":
            continue
        if i2 - i1 > SUGGEST_MAX_WORDS or j2 - j1 > SUGGEST_MAX_WORDS:
            continue
        heard = " ".join(w.strip(_EDGE_PUNCT) for w in a[i1:i2])
        correct = " ".join(w.strip(_EDGE_PUNCT) for w in b[j1:j2])
        if _valid_pair(heard, correct) and (heard, correct) not in pairs:
            pairs.append((heard, correct))
    return pairs


# --- initial_prompt do Whisper ----------------------------------------------------------

def prompt_terms(store: VocabStore | None, extras: dict | None) -> str | None:
    """Termos corretos para o ``initial_prompt``: confirmados, depois dicionário, depois embutidos.
    Únicos (sem caixa/acento), curtos, no máximo 40 termos / 300 caracteres. Ordem estável."""
    values: list[str] = []
    if store is not None:
        values += list(store.entries().values())
    if isinstance(extras, dict):
        values += [v for v in extras.values() if isinstance(v, str)]
    values += list(BUILTIN_TERMS)
    terms: list[str] = []
    seen: set[str] = set()
    total = 0
    for value in values:
        value = " ".join(value.split())
        norm = _normalize(value)
        if not value or norm in seen or len(value) > 40:
            continue
        extra = len(value) + (2 if terms else 0)
        if len(terms) >= PROMPT_MAX_TERMS or total + extra + 1 > PROMPT_MAX_CHARS:
            break
        terms.append(value)
        seen.add(norm)
        total += extra
    return (", ".join(terms) + ".") if terms else None
