"""Testes para postprocess.py (lógica pura, sem I/O)."""

from __future__ import annotations

import pytest

from hear_ai.postprocess import (
    DEFAULT_DICTIONARY,
    PT_VOICE_COMMANDS,
    EN_VOICE_COMMANDS,
    apply_dictionary,
    apply_voice_commands,
    postprocess,
)


# =============================================================================
# Testes: apply_dictionary
# =============================================================================

def test_apply_dictionary_substituicao_simples():
    """Substitui termo simples."""
    text = "uso o ondex todos os dias"
    result = apply_dictionary(text)
    assert result == "uso o Ondex todos os dias"


def test_apply_dictionary_preserva_resto():
    """Preserva resto do texto (maiúsculas, acentos, pontuação)."""
    text = "O Ondex é ótimo! O ondex ajuda."
    result = apply_dictionary(text)
    assert result == "O Ondex é ótimo! O Ondex ajuda."


def test_apply_dictionary_sem_diferenciar_maiusculas_acentos():
    """Busca case/accent-insensitive."""
    text = "O ONDEX e o OnDeX são iguais"
    result = apply_dictionary(text)
    assert result == "O Ondex e o Ondex são iguais"


def test_apply_dictionary_fronteira_palavra():
    """Não substitui dentro de outra palavra."""
    text = "ondexx ondex ondextral"
    result = apply_dictionary(text)
    assert result == "ondexx Ondex ondextral"


def test_apply_dictionary_termos_multiplas_palavras_primeiro():
    """Termos de várias palavras processados primeiro."""
    custom_dict = {"hear ai": "Hear AI", "ai": "IA"}
    text = "uso hear ai e ai"
    result = apply_dictionary(text, custom_dict)
    assert result == "uso Hear AI e IA"


def test_apply_dictionary_variantes_whisper():
    """Aceita variantes ouvidas pelo Whisper se estiverem no dicionário."""
    custom_dict = {"maestre": "Maestri", "rir ai": "Hear AI", "hir ai": "Hear AI"}
    text = "o maestre disse rir ai e hir ai"
    result = apply_dictionary(text, custom_dict)
    assert result == "o Maestri disse Hear AI e Hear AI"


def test_apply_dictionary_varias_ocorrencias():
    """Múltiplas ocorrências do mesmo termo."""
    text = "ondex ondex ondex"
    result = apply_dictionary(text)
    assert result == "Ondex Ondex Ondex"


def test_apply_dictionary_texto_vazio():
    """Texto vazio retorna vazio."""
    assert apply_dictionary("") == ""
    assert apply_dictionary(None) is None  # type: ignore


def test_apply_dictionary_dicionario_vazio():
    """Dicionário vazio não altera o texto."""
    text = "ondex maestri"
    result = apply_dictionary(text, {})
    assert result == text


def test_apply_dictionary_padrao_inclui_termos_conhecidos():
    """DEFAULT_DICTIONARY tem os termos esperados."""
    assert "ondex" in DEFAULT_DICTIONARY
    assert "maestri" in DEFAULT_DICTIONARY
    assert "groq" in DEFAULT_DICTIONARY
    assert "hear ai" in DEFAULT_DICTIONARY
    assert "claude code" in DEFAULT_DICTIONARY


def test_apply_dictionary_unicode_word_boundary():
    """Fronteira de palavra Unicode funciona com acentos."""
    text = "maestri. maestri, maestri!"
    result = apply_dictionary(text)
    assert result == "Maestri. Maestri, Maestri!"


def test_apply_dictionary_preserva_maiusculas_substituicao():
    """Substituição usa exatamente o case do dicionário."""
    text = "uso o ondex"
    result = apply_dictionary(text, {"ondex": "ONDEX"})
    assert result == "uso o ONDEX"


# =============================================================================
# Testes: apply_voice_commands
# =============================================================================

def test_apply_voice_commands_nova_linha_pt():
    """'nova linha' → \n (português)."""
    text = "fim. nova linha. começo"
    result = apply_voice_commands(text)
    assert result == "fim.\nComeço"


def test_apply_voice_commands_novo_paragrafo_pt():
    """'novo parágrafo' → \n\n (português)."""
    text = "fim. novo parágrafo. começo"
    result = apply_voice_commands(text)
    assert result == "fim.\n\nComeço"


def test_apply_voice_commands_novo_paragrafo_sem_acento():
    """'novo paragrafo' (sem acento) também funciona."""
    text = "fim. novo paragrafo. começo"
    result = apply_voice_commands(text)
    assert result == "fim.\n\nComeço"


def test_apply_voice_commands_new_line_en():
    """'new line' → \n (inglês)."""
    text = "end. new line. start"
    result = apply_voice_commands(text)
    assert result == "end.\nStart"


def test_apply_voice_commands_new_paragraph_en():
    """'new paragraph' → \n\n (inglês)."""
    text = "end. new paragraph. start"
    result = apply_voice_commands(text)
    assert result == "end.\n\nStart"


def test_apply_voice_commands_remove_pontuacao_adjacente():
    """Remove pontuação/espaço adjacente que o Whisper coloca."""
    text = "fim . nova linha , começo"
    result = apply_voice_commands(text)
    assert result == "fim\nComeço"


def test_apply_voice_commands_no_inicio():
    """Comando no início do texto."""
    text = "nova linha. começo"
    result = apply_voice_commands(text)
    assert result == "\nComeço"


def test_apply_voice_commands_no_fim():
    """Comando no fim do texto."""
    text = "fim. nova linha"
    result = apply_voice_commands(text)
    assert result == "fim.\n"


def test_apply_voice_commands_multiplos_comandos():
    """Múltiplos comandos no mesmo texto."""
    text = "primeira linha. nova linha. segunda. novo parágrafo. terceira"
    result = apply_voice_commands(text)
    assert result == "primeira linha.\nSegunda.\n\nTerceira"


def test_apply_voice_commands_capitaliza_apos_quebra():
    """Capitaliza primeira letra após quebra."""
    text = "fim. nova linha. começo minusculo"
    result = apply_voice_commands(text)
    assert result == "fim.\nComeço minusculo"


def test_apply_voice_commands_texto_vazio():
    """Texto vazio retorna vazio."""
    assert apply_voice_commands("") == ""
    assert apply_voice_commands(None) is None  # type: ignore


def test_apply_voice_commands_sem_comandos():
    """Texto sem comandos não muda."""
    text = "texto normal sem comandos"
    result = apply_voice_commands(text)
    assert result == text


def test_apply_voice_commands_comando_dentro_palavra_nao_troca():
    """Comando dentro de outra palavra não troca."""
    text = "anovalinha nova linha"
    result = apply_voice_commands(text)
    assert "anovalinha" in result
    assert "\n" in result


# =============================================================================
# Testes: postprocess (pipeline completo)
# =============================================================================

def test_postprocess_aplica_dicionario_e_comandos():
    """Pipeline aplica dicionário e depois comandos de voz."""
    text = "uso ondex. nova linha. maestri diz oi"
    result = postprocess(text)
    assert result == "uso Ondex.\nMaestri diz oi"


def test_postprocess_sem_voice_commands():
    """Se voice_commands=False, só aplica dicionário."""
    text = "ondex. nova linha"
    result = postprocess(text, voice_commands=False)
    assert result == "Ondex. nova linha"


def test_postprocess_dicionario_customizado():
    """Pipeline aceita dicionário customizado."""
    text = "ondex e maestre"
    result = postprocess(text, dictionary={"ondex": "ONDEX", "maestre": "MAESTRI"})
    assert result == "ONDEX e MAESTRI"


def test_postprocess_texto_vazio():
    """Texto vazio retorna vazio."""
    assert postprocess("") == ""
    assert postprocess(None) is None  # type: ignore


def test_postprocess_preserva_pontuacao_original():
    """Pontuação original preservada (exceto adjacente a comandos)."""
    text = "ondex, maestri! ondex?"
    result = postprocess(text, voice_commands=False)
    assert result == "Ondex, Maestri! Ondex?"


def test_postprocess_comando_no_meio_da_frase():
    """Comando de voz no meio da frase."""
    text = "primeiro. nova linha. segundo"
    result = postprocess(text)
    assert result == "primeiro.\nSegundo"


def test_postprocess_multiplas_linhas_nova_linha():
    """Múltiplas 'nova linha' criam múltiplas quebras."""
    text = "a. nova linha. b. nova linha. c"
    result = postprocess(text)
    assert result == "a.\nB.\nC"


def test_postprocess_novo_paragrafo_duas_quebras():
    """'novo parágrafo' cria \n\n."""
    text = "primeiro. novo parágrafo. segundo"
    result = postprocess(text)
    assert result == "primeiro.\n\nSegundo"


def test_postprocess_english_commands():
    """Comandos em inglês funcionam."""
    text = "end. new paragraph. start"
    result = postprocess(text)
    assert result == "end.\n\nStart"


# =============================================================================
# Testes: integração dicionário + comandos (ordem correta)
# =============================================================================

def test_postprocess_ordem_dicionario_depois_comandos():
    """Dicionário aplicado ANTES dos comandos de voz."""
    # Se 'nova linha' virasse 'Nova Linha' pelo dicionário, o comando não funcionaria
    text = "ondex nova linha maestri"
    result = postprocess(text)
    # ondex → Ondex, depois 'nova linha' → \n, depois maestri → Maestri
    assert result == "Ondex\nMaestri"


def test_postprocess_dicionario_nao_quebra_comandos():
    """Dicionário não deve interferir nos comandos de voz."""
    # Se o dicionário tivesse 'nova' → 'Nova', 'nova linha' deixaria de funcionar
    custom_dict = {"nova": "Nova"}
    text = "nova linha"
    result = postprocess(text, dictionary=custom_dict)
    # 'nova linha' deve virar \n, não 'Nova linha'
    assert result == "\n"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])