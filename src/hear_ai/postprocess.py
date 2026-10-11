"""Pós-processamento de texto: dicionário de substituições + comandos de voz."""

from __future__ import annotations

import re
import unicodedata
from typing import Pattern

# Dicionário padrão de substituições (termos conhecidos que o Whisper pode errar)
DEFAULT_DICTIONARY: dict[str, str] = {
    "ondex": "Ondex",
    "maestri": "Maestri",
    "groq": "Groq",
    "hear ai": "Hear AI",
    "claude code": "Claude Code",
}


# --- Normalização para comparação ---

def _normalize(text: str) -> str:
    """Normaliza para comparação: minúsculas, sem acentos."""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


def _compile_pattern(term: str) -> Pattern[str]:
    """
    Compila regex para termo inteiro (fronteira de palavra Unicode).
    Escape especial chars, usa \b para fronteiras de palavra Unicode.
    """
    escaped = re.escape(term)
    return re.compile(rf"\b{escaped}\b", re.IGNORECASE | re.UNICODE)


# --- Dicionário de substituições ---

# letras que o Whisper às vezes devolve acentuadas (ex.: "ÔNDEX"): cada letra do termo casa
# também com as variantes acentuadas (T30; maiúsculas vêm do re.IGNORECASE)
_ACCENT_VARIANTS = {
    "a": "aáàâãä", "e": "eéèêë", "i": "iíìîï", "o": "oóòôõö", "u": "uúùûü", "c": "cç", "n": "nñ",
}


def _accent_insensitive(term: str) -> str:
    """Regex do termo em que cada vogal/c/n aceita as formas acentuadas."""
    parts = []
    for ch in term:
        base = _normalize(ch)
        if len(base) == 1 and base in _ACCENT_VARIANTS:
            parts.append("[" + _ACCENT_VARIANTS[base] + "]")
        else:
            parts.append(re.escape(ch))
    return "".join(parts)


def apply_dictionary(text: str, entries: dict[str, str] | None = None) -> str:
    """
    Substitui termos inteiros no texto conforme o dicionário.

    - Busca case/accent-insensitive
    - Fronteira de palavra Unicode (\b)
    - Termos de várias palavras processados primeiro (mais específicos)
    - Não substitui dentro de outra palavra
    - Preserva o resto do texto (incluindo maiúsculas/acentos originais)

    Args:
        text: Texto a processar
        entries: Dicionário {termo_original: substituição}. Se None, usa DEFAULT_DICTIONARY.

    Returns:
        Texto com substituições aplicadas.
    """
    if not text:
        return text

    if entries is None:
        entries = DEFAULT_DICTIONARY

    if not entries:
        return text

    # Ordena termos: primeiro por número de palavras (desc), depois por comprimento (desc)
    def sort_key(kv):
        term = kv[0]
        word_count = len(term.split())
        return (-len(term.split()), -len(term))

    sorted_terms = sorted(entries.items(), key=sort_key)

    # Cria um mapa normalizado -> substituição (para busca rápida)
    # Normaliza a chave (lowercase, sem acento) para busca
    norm_to_replacement: dict[str, str] = {}
    for original, replacement in sorted(entries.items(), key=lambda kv: (-len(kv[0].split()), -len(kv[0]))):
        norm = _normalize(original)
        if norm not in norm_to_replacement:
            norm_to_replacement[norm] = replacement

    # Cria regex único que casa qualquer termo (ordem: mais palavras primeiro, depois mais longo)
    # Usa callback para decidir a substituição
    sorted_terms_for_regex = sorted(
        [t for t in entries if t],
        key=lambda t: (-len(t.split()), -len(t))
    )

    if not sorted_terms_for_regex:
        return text

    # Escapa e cria alternation regex
    escaped_terms = [_accent_insensitive(t) for t in sorted_terms_for_regex]
    combined_regex = re.compile(
        r"\b(" + "|".join(escaped_terms) + r")\b",
        re.IGNORECASE | re.UNICODE
    )

    def replacer(match):
        matched = match.group(0)
        norm_matched = _normalize(matched)
        return norm_to_replacement.get(norm_matched, matched)

    return combined_regex.sub(replacer, text)


# --- Comandos de voz ---

# Comandos de voz em português
PT_VOICE_COMMANDS: dict[str, str] = {
    "nova linha": "\n",
    "novo paragrafo": "\n\n",
    "novo parágrafo": "\n\n",
}

# Comandos de voz em inglês
EN_VOICE_COMMANDS: dict[str, str] = {
    "new line": "\n",
    "new paragraph": "\n\n",
}

# Todos os comandos combinados
ALL_VOICE_COMMANDS: dict[str, str] = {**PT_VOICE_COMMANDS, **EN_VOICE_COMMANDS}


def _compile_voice_patterns() -> list[tuple[Pattern[str], str]]:
    """
    Compila padrões regex para comandos de voz.
    Ordena por comprimento decrescente para comandos mais longos primeiro.
    """
    patterns = []
    for cmd, replacement in sorted(ALL_VOICE_COMMANDS.items(), key=lambda kv: len(kv[0]), reverse=True):
        # ANTES: espaços em branco opcionais + pontuação opcional (ponto, vírgula, etc) com espaços opcionais
        # DEPOIS: pontuação opcional + espaços opcionais (serão removidos)
        # Ambos os lados usam o mesmo padrão para capturar pontuação com espaços adjacentes
        pattern = re.compile(
            rf"(\s*[.,;:!?]?\s*){re.escape(cmd)}(\s*[.,;:!?]?\s*)",
            re.IGNORECASE | re.UNICODE
        )
        patterns.append((pattern, replacement))
    return patterns


_VOICE_PATTERNS = _compile_voice_patterns()


def _normalize(text: str) -> str:
    """Normaliza para comparação: minúsculas, sem acentos."""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


def _capitalize_after_newline(text: str) -> str:
    """
    Capitaliza a primeira letra de cada linha (após \n ou \n\n).
    """
    lines = text.split("\n")
    result = []
    for i, line in enumerate(lines):
        if i == 0:
            result.append(line)
        else:
            # Pula espaços iniciais, capitaliza primeira letra não-espaço
            stripped = line.lstrip()
            if stripped:
                first_alpha = next((i for i, c in enumerate(stripped) if c.isalpha()), None)
                if first_alpha is not None:
                    prefix = stripped[:first_alpha]
                    rest = stripped[first_alpha:]
                    line = line[:len(line) - len(stripped)] + prefix + rest[0].upper() + rest[1:]
            result.append(line)
    return "\n".join(result)


def apply_voice_commands(text: str) -> str:
    """
    Processa comandos de voz no texto.

    - "nova linha" / "new line" → \n
    - "novo parágrafo" / "novo paragrafo" / "new paragraph" → \n\n
    - Remove pontuação/espaço adjacente que o Whisper coloca DEPOIS do comando
    - Capitaliza primeira letra após a quebra

    Args:
        text: Texto a processar

    Returns:
        Texto com comandos de voz processados.
    """
    if not text:
        return text

    result = text
    for pattern, replacement in _VOICE_PATTERNS:
        # Usa função para processar a substituição
        def repl(match):
            before = match.group(1)
            after = match.group(2)
            # Remove espaços em branco no final do "before" (espaço antes do comando)
            before = before.rstrip()
            # Remove pontuação final se houver espaço antes da pontuação
            # (ex.: "fim ." → "fim", mas "fim." mantém o ponto)
            before = re.sub(r'\s+([.,;:!?]+)$', '', before)
            return before + replacement

        result = pattern.sub(repl, result)

    # Capitaliza após quebras
    result = _capitalize_after_newline(result)

    return result


# --- Pipeline completo ---

def postprocess(text: str, dictionary: dict[str, str] | None = None, voice_commands: bool = True) -> str:
    """
    Pipeline completo de pós-processamento.

    Aplica na ordem:
    1. Dicionário de substituições (apply_dictionary)
    2. Comandos de voz (apply_voice_commands) - se voice_commands=True

    Args:
        text: Texto a processar
        dictionary: Dicionário customizado (None = DEFAULT_DICTIONARY)
        voice_commands: Se True, aplica comandos de voz

    Returns:
        Texto processado.
    """
    if not text:
        return text

    result = apply_dictionary(text, dictionary)
    if voice_commands:
        result = apply_voice_commands(result)
    return result