"""Testes para chunking.py (lógica pura, sem arquivos externos)."""

from __future__ import annotations

import numpy as np
import pytest

from hear_ai.chunking import ChunkPlan, find_cut, merge_texts, plan_next


# =============================================================================
# Helpers para gerar áudio sintético
# =============================================================================

def make_audio_with_silence(
    sr: int = 16000,
    segments: list[tuple[float, float, float]] | None = None,
) -> np.ndarray:
    """
    Gera áudio sintético com tons e silêncios.
    segments: lista de (duration_s, frequency_hz, amplitude) onde amplitude=0 = silêncio.
    """
    if segments is None:
        segments = [(10.0, 440, 0.1), (0.5, 0, 0), (10.0, 880, 0.1)]

    parts = []
    for dur, freq, amp in segments:
        n = int(sr * dur)
        if freq == 0 or amp == 0:
            parts.append(np.zeros(n, dtype=np.float32))
        else:
            t = np.arange(n) / sr
            parts.append((amp * np.sin(2 * np.pi * freq * t)).astype(np.float32))
    return np.concatenate(parts)


def make_continuous_tone(sr: int = 16000, duration_s: float = 40.0, freq: float = 440, amp: float = 0.1) -> np.ndarray:
    """Áudio contínuo sem pausas (para testar corte forçado)."""
    n = int(sr * duration_s)
    t = np.arange(n) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# =============================================================================
# Testes find_cut
# =============================================================================

def test_find_cut_nao_corta_se_curto():
    """Áudio menor que max_s (32s) retorna None."""
    audio = np.zeros(16000 * 10, dtype=np.float32)  # 10s
    assert find_cut(audio) is None


def test_find_cut_cai_no_silencio():
    """Corte deve cair no meio do silêncio (entre 20s e 32s, perto de 27s)."""
    # 20s tom + 2s silêncio + 20s tom = 42s total
    # min=20s, max=32s, target=27s → silêncio está em 20-22s
    # O silêncio deve ser detectado e corte cai no meio dele
    audio = make_audio_with_silence(segments=[
        (20.0, 440, 0.1),
        (2.0, 0, 0),      # silêncio de 2s (>= 300ms)
        (20.0, 880, 0.1),
    ])
    cut = find_cut(audio)
    assert cut is not None
    # Silêncio começa em 20s (320000 samples), meio ~21s (336000)
    # target é 27s (432000), mas silêncio mais próximo é em 21s
    assert 20.0 * 16000 <= cut <= 22.0 * 16000


def test_find_cut_multiplos_silencios_prefere_perto_target():
    """Com múltiplos silêncios, escolhe o mais próximo de target_s (27s)."""
    # Silêncio em 15s (fora da janela min=20s), em 25s, em 30s
    audio = make_audio_with_silence(segments=[
        (15.0, 440, 0.1),
        (0.5, 0, 0),      # 15s - ignorado (< min_s)
        (10.0, 440, 0.1),
        (0.5, 0, 0),      # 25.5s - dentro da janela
        (5.0, 440, 0.1),
        (0.5, 0, 0),      # 31s - dentro da janela
        (5.0, 440, 0.1),
    ])
    cut = find_cut(audio)
    assert cut is not None
    # Deve preferir o em 25.5s (mais perto de 27s) ao em 31s
    assert 25.0 * 16000 <= cut <= 26.0 * 16000


def test_find_cut_sem_silencio_corte_forcado():
    """Áudio contínuo sem pausas → corte forçado em max_s (32s)."""
    audio = make_continuous_tone(duration_s=40.0)
    cut = find_cut(audio)
    assert cut == 32 * 16000


def test_find_cut_silencio_curto_ignorado():
    """Silêncio < 300ms deve ser ignorado."""
    # Silêncio de 100ms (ignorado) + silêncio de 500ms (válido)
    audio = make_audio_with_silence(segments=[
        (20.0, 440, 0.1),
        (0.1, 0, 0),      # 100ms - ignorado
        (5.0, 440, 0.1),
        (0.5, 0, 0),      # 500ms - válido
        (10.0, 440, 0.1),
    ])
    cut = find_cut(audio)
    assert cut is not None
    # Deve cair no silêncio de 500ms (em ~25.5s), não no de 100ms (20.1s)
    assert 25.0 * 16000 <= cut <= 26.0 * 16000


def test_find_cut_silencio_no_inicio_ignorado():
    """Silêncio antes de min_s (20s) deve ser ignorado."""
    audio = make_audio_with_silence(segments=[
        (10.0, 0, 0),     # silêncio no início - ignorado
        (20.0, 440, 0.1),
        (0.5, 0, 0),      # silêncio em 30s - válido
        (10.0, 440, 0.1),
    ])
    cut = find_cut(audio)
    assert cut is not None
    assert 29.0 * 16000 <= cut <= 31.0 * 16000


def test_find_cut_audio_exato_max_s():
    """Áudio exatamente max_s (32s) ainda precisa cortar se for > max_s."""
    audio = make_continuous_tone(duration_s=32.0)
    cut = find_cut(audio)
    assert cut == 32 * 16000


# =============================================================================
# Testes merge_texts
# =============================================================================

def test_merge_texts_remove_sobreposicao_simples():
    """Remove sobreposição de palavras idênticas."""
    prev = "olá mundo tudo bem"
    new = "tudo bem como vai"
    result = merge_texts(prev, new)
    assert result == "olá mundo tudo bem como vai"


def test_merge_texts_sobreposicao_com_acentos_pontuacao():
    """Normalização remove acentos/pontuação mas preserva formatação de new."""
    prev = "Olá, mundo! Tudo bem."
    new = "tudo bem, como vai?"
    result = merge_texts(prev, new)
    # Overlap "tudo bem" removido de new, suffix ", como vai?" preservado
    # Previous termina com ".", suffix começa com "," → SEM espaço antes da vírgula
    assert result == "Olá, mundo! Tudo bem., como vai?"


def test_merge_texts_sem_espaco_antes_virgula():
    """Não insere espaço antes de vírgula no suffix."""
    prev = "foi para casa"
    new = "para casa, ontem fomos"
    result = merge_texts(prev, new)
    assert result == "foi para casa, ontem fomos"


def test_merge_texts_sem_espaco_antes_ponto():
    """Não insere espaço antes de ponto no suffix."""
    prev = "ele chegou lá"
    new = "chegou lá. O dia estava bonito."
    result = merge_texts(prev, new)
    # Overlap "chegou lá" (2 palavras) - suffix começa com "."
    assert result == "ele chegou lá. O dia estava bonito."


def test_merge_texts_sem_espaco_antes_interrogacao():
    """Não insere espaço antes de interrogação no suffix."""
    prev = "você sabe onde"
    new = "sabe onde fica?"
    result = merge_texts(prev, new)
    # Overlap "sabe onde" (2 palavras) - suffix começa com "?"
    assert result == "você sabe onde fica?"


def test_merge_texts_sem_espaco_antes_reticencias():
    """Não insere espaço antes de reticências no suffix."""
    prev = "ele disse que sim"
    new = "que sim... não vem mais"
    result = merge_texts(prev, new)
    # Overlap "que sim" (2 palavras) - suffix começa com "..."
    assert result == "ele disse que sim... não vem mais"


def test_merge_texts_sem_sobreposicao():
    """Sem sobreposição → concatena com espaço."""
    prev = "olá mundo"
    new = "tchau terra"
    result = merge_texts(prev, new)
    assert result == "olá mundo tchau terra"


def test_merge_texts_sobreposicao_1_palavra_nao_remove():
    """Sobreposição de 1 palavra NÃO é removida (mínimo 2)."""
    prev = "olá mundo"
    new = "mundo cruel"
    result = merge_texts(prev, new)
    # "mundo" sozinha não basta
    assert result == "olá mundo mundo cruel"


def test_merge_texts_sobreposicao_maxima_12_palavras():
    """Limita sobreposição a max_overlap_words (padrão 12)."""
    # Cria texto onde o final de prev bate com início de new
    common = " ".join([f"w{i}" for i in range(12)])  # 12 palavras comuns
    prev_only = " ".join([f"p{i}" for i in range(8)])  # 8 palavras só no prev
    new_only = " ".join([f"n{i}" for i in range(8)])   # 8 palavras só no new

    prev = prev_only + " " + common
    new = common + " " + new_only

    result = merge_texts(prev, new, max_overlap_words=12)
    # Deve remover as 12 palavras comuns
    result_words = result.split()
    # prev_only (8) + common (12) + new_only (8) = 28
    assert len(result_words) == 28
    # Verifica que as palavras comuns aparecem só uma vez
    assert result_words.count("w0") == 1
    assert result_words.count("n0") == 1


def test_merge_texts_entrada_vazia():
    """Entradas vazias são tratadas corretamente."""
    assert merge_texts("", "novo") == "novo"
    assert merge_texts("antigo", "") == "antigo"
    assert merge_texts("", "") == ""


def test_merge_texts_preserva_capitalizacao_new():
    """Pontuação/capitalização de new é preservada."""
    prev = "fim da frase."
    new = "Nova frase começa."
    result = merge_texts(prev, new)
    assert result == "fim da frase. Nova frase começa."


def test_merge_texts_sobreposicao_parcial_palavra():
    """Não remove sobreposição parcial de palavra."""
    prev = "computador"
    new = "computação"
    result = merge_texts(prev, new)
    # "computador" vs "computação" - palavras diferentes após normalização
    assert result == "computador computação"


def test_merge_texts_acentos_portugues_nao():
    """Teste do líder: 'não' não deve ser cortado em 'n'+'o'."""
    prev = "Eu já disse que não é possível"
    new = "não é possível fazer isso agora."
    result = merge_texts(prev, new)
    assert result == "Eu já disse que não é possível fazer isso agora."


def test_merge_texts_acentos_portugues_reuniao():
    """Teste do líder: 'reunião' e 'amanhã' com acentos."""
    prev = "Vamos à reunião amanhã"
    new = "reunião amanhã às dez horas."
    result = merge_texts(prev, new)
    assert result == "Vamos à reunião amanhã às dez horas."


def test_merge_texts_palavras_com_cedilha_til():
    """Palavras com ç, ã, é, ô. Overlap de 1 palavra NÃO remove."""
    prev = "Façam o favor de vir"
    new = "vir até aqui agora."
    result = merge_texts(prev, new)
    # "vir" sozinha (1 palavra) não basta para remover
    assert result == "Façam o favor de vir vir até aqui agora."


def test_merge_texts_hifen():
    """Palavra com hífen ('guarda-chuva') conta como uma palavra."""
    prev = "Leve o guarda-chuva azul"
    new = "guarda-chuva azul pois vai chover."
    result = merge_texts(prev, new)
    assert result == "Leve o guarda-chuva azul pois vai chover."


def test_merge_texts_numeros_com_virgula():
    """Números com vírgula ('10,5') são tratados corretamente."""
    prev = "O valor é 10,5 reais"
    new = "10,5 reais por unidade."
    result = merge_texts(prev, new)
    assert result == "O valor é 10,5 reais por unidade."


# =============================================================================
# Testes plan_next
# =============================================================================

def test_plan_next_acabou_retorna_none():
    """Se consumed_frame >= total_frames, retorna None."""
    assert plan_next(1000, 1000, np.zeros(100)) is None
    assert plan_next(1000, 1500, np.zeros(100)) is None


def test_plan_next_corte_em_pausa_sem_overlap():
    """Corte em pausa → overlap = 0."""
    sr = 16000
    # 30s áudio com silêncio em 25s
    audio = make_audio_with_silence(segments=[
        (25.0, 440, 0.1),
        (0.5, 0, 0),
        (10.0, 440, 0.1),
    ])
    consumed = 0
    plan = plan_next(len(audio), consumed, audio)
    assert plan is not None
    assert plan.start == 0
    assert plan.overlap == 0
    # Corte deve estar no silêncio (~25s)
    assert 24.5 * sr <= plan.end <= 26.0 * sr


def test_plan_next_corte_forcado_com_overlap():
    """Corte forçado (sem pausa) → aplica overlap de 0.8s."""
    sr = 16000
    # Áudio contínuo 40s
    audio = make_continuous_tone(duration_s=40.0)
    consumed = 0
    plan = plan_next(len(audio), consumed, audio)
    assert plan is not None
    assert plan.start == 0
    assert plan.end == 32 * sr
    assert plan.overlap == int(0.8 * sr)  # 12800 samples


def test_plan_next_segundo_bloco_com_overlap():
    """Segundo bloco começa com overlap do corte forçado anterior."""
    sr = 16000
    # Áudio de 70s: primeiro bloco 32s, segundo 32s, terceiro 6s
    audio = make_continuous_tone(duration_s=70.0)
    # Primeiro bloco: 0 a 32s, overlap 0.8s
    plan1 = plan_next(len(audio), 0, audio)
    assert plan1 is not None
    assert plan1.end == 32 * sr
    assert plan1.overlap == int(0.8 * sr)

    # Segundo bloco: começa em 32s - 0.8s = 31.2s
    # Restante tem 70 - 31.2 = 38.8s > 32s, então precisa cortar
    consumed = plan1.end - plan1.overlap
    audio_tail = audio[consumed:]
    plan2 = plan_next(len(audio), consumed, audio_tail)
    assert plan2 is not None
    assert plan2.start == consumed
    # Segundo bloco também forçado → 32s a partir de consumed
    expected_end = consumed + 32 * sr
    assert plan2.end == min(expected_end, len(audio))
    assert plan2.overlap == int(0.8 * sr)

    # Terceiro bloco: começa em consumed2 - overlap
    consumed2 = plan2.end - plan2.overlap
    audio_tail2 = audio[consumed2:]
    plan3 = plan_next(len(audio), consumed2, audio_tail2)
    assert plan3 is not None
    assert plan3.start == consumed2
    # Último bloco (< max_s) sem overlap
    assert plan3.end == len(audio)
    assert plan3.overlap == 0


def test_plan_next_ultimo_bloco_curto():
    """Último bloco (< max_s) não tem overlap e vai até o fim."""
    sr = 16000
    audio = make_continuous_tone(duration_s=40.0)  # 40s total
    # Simula que já consumimos 32s (primeiro bloco forçado)
    consumed = 32 * sr
    audio_tail = audio[consumed:]  # 8s restantes
    plan = plan_next(len(audio), consumed, audio_tail)
    assert plan is not None
    assert plan.start == consumed
    assert plan.end == len(audio)  # até o fim
    assert plan.overlap == 0  # sem overlap no último


# =============================================================================
# Testes de integração (fluxo completo)
# =============================================================================

def test_fluxo_completo_com_pausas():
    """Simula transcrição de 60s com pausas naturais."""
    sr = 16000
    # 3 blocos de ~20s com pausas de 0.5s entre eles
    audio = make_audio_with_silence(segments=[
        (20.0, 440, 0.1),
        (0.5, 0, 0),
        (20.0, 880, 0.1),
        (0.5, 0, 0),
        (20.0, 440, 0.1),
    ])

    plans = []
    consumed = 0
    while consumed < len(audio):
        plan = plan_next(len(audio), consumed, audio[consumed:])
        if plan is None:
            break
        plans.append(plan)
        consumed = plan.end - plan.overlap
        if consumed >= len(audio):
            break

    assert len(plans) == 3
    assert all(p.overlap == 0 for p in plans)  # todos cortes em pausa
    # Verifica cobertura total
    assert plans[0].start == 0
    assert plans[-1].end == len(audio)


def test_fluxo_completo_sem_pausas():
    """Simula transcrição de 60s sem pausas (cortes forçados com overlap)."""
    sr = 16000
    audio = make_continuous_tone(duration_s=60.0)

    plans = []
    consumed = 0
    while consumed < len(audio):
        plan = plan_next(len(audio), consumed, audio[consumed:])
        if plan is None:
            break
        plans.append(plan)
        consumed = plan.end - plan.overlap
        if consumed >= len(audio):
            break

    # 60s / (32s - 0.8s) ≈ 1.9 → 2 blocos
    assert len(plans) == 2
    assert all(p.overlap == int(0.8 * sr) for p in plans[:-1])
    assert plans[-1].overlap == 0  # último sem overlap


if __name__ == "__main__":
    pytest.main([__file__, "-v"])