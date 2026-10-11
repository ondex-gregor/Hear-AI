"""Corte e junção de blocos de áudio/texto para ditado longo."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ChunkPlan:
    """Plano para o próximo bloco de transcrição."""
    start: int          # índice de amostra inicial
    end: int            # índice de amostra final (exclusivo)
    overlap: int        # amostras de sobreposição com bloco anterior (0 se corte em pausa)


def find_cut(
    audio: np.ndarray,
    samplerate: int = 16000,
    target_s: float = 27.0,
    min_s: float = 20.0,
    max_s: float = 32.0,
    frame_ms: int = 30,
) -> int | None:
    """
    Escolhe o índice de corte para um bloco que começa em 0.

    Procura o trecho mais silencioso (menor energia RMS em janelas de `frame_ms`,
    considerando ≥ 300 ms contínuos abaixo de um limiar relativo ao nível mediano)
    entre `min_s` e `max_s`, preferindo o mais perto de `target_s`.

    Se `len(audio) < max_s * sr` → `None` (ainda não precisa cortar).
    Se não houver pausa, devolve `int(max_s * sr)` (corte forçado).
    """
    sr = samplerate
    total_frames = len(audio)
    max_frames = int(max_s * sr)
    min_frames = int(min_s * sr)
    target_frames = int(target_s * sr)

    if total_frames < max_frames:
        return None  # ainda não precisa cortar

    # Tamanho do frame em amostras
    frame_size = int(sr * frame_ms / 1000)
    if frame_size < 1:
        frame_size = 1

    # Calcula energia RMS por frame
    num_frames = total_frames // frame_size
    if num_frames < 2:
        return max_frames

    rms = np.zeros(num_frames, dtype=np.float32)
    for i in range(num_frames):
        start = i * frame_size
        end = start + frame_size
        frame = audio[start:end]
        rms[i] = np.sqrt(np.mean(frame.astype(np.float64) ** 2))

    # Limiar relativo ao nível mediano
    median_rms = np.median(rms)
    threshold = median_rms * 0.1  # 10% da energia mediana

    # Mínimo de frames contínuos para considerar "pausa" (300 ms)
    min_pause_frames = max(1, int(300 / frame_ms))

    # Busca entre min_s e max_s
    start_idx = min_frames // frame_size
    end_idx = min(max_frames // frame_size, num_frames)

    if start_idx >= end_idx:
        return max_frames

    best_idx = None
    best_score = float('inf')  # menor energia = melhor
    best_dist = float('inf')   # mais perto de target = melhor

    i = start_idx
    while i < end_idx:
        # Procura sequência de frames abaixo do limiar
        if rms[i] < threshold:
            j = i
            while j < end_idx and rms[j] < threshold:
                j += 1
            pause_len = j - i
            if pause_len >= min_pause_frames:
                # Centro da pausa
                center = i + pause_len // 2
                center_frame = center * frame_size
                # Score: energia média na pausa + distância do target
                pause_energy = np.mean(rms[i:j])
                dist = abs(center_frame - target_frames)
                # Prioriza menor energia, depois menor distância
                score = (pause_energy, dist)
                if score < (best_score, best_dist):
                    best_score = pause_energy
                    best_dist = dist
                    best_idx = center_frame
            i = j
        else:
            i += 1

    if best_idx is not None:
        return int(best_idx)

    # Sem pausa adequada → corte forçado em max_s
    return max_frames


def _normalize_words(text: str) -> list[str]:
    """Normaliza texto para comparação: minúsculas, sem acento, sem pontuação.
    Tokeniza por espaços e normaliza cada token (mesma lógica de _tokenize_and_normalize)."""
    words = []
    for token in re.finditer(r"\S+", text):
        word = token.group()
        norm = unicodedata.normalize("NFD", word.lower())
        norm = "".join(c for c in norm if unicodedata.category(c) != "Mn")
        norm = re.sub(r"[^a-z0-9]+", "", norm)
        if norm:
            words.append(norm)
    return words


def _tokenize_and_normalize(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """
    Tokeniza o texto original por espaços e normaliza cada token.
    Retorna (palavras_normalizadas, spans_no_original).
    O span aponta para a palavra SEM pontuação final (pontuação fica no suffix).
    """
    TRAILING_PUNCT = ".,;:!?\"'»”…"

    spans = []
    norm_words = []
    for token in re.finditer(r"\S+", text):
        word = token.group()
        # Remove pontuação final do core
        i = len(word)
        while i > 0 and word[i-1] in TRAILING_PUNCT:
            i -= 1
        core = word[:i]
        # Normaliza o core
        norm = unicodedata.normalize("NFD", core.lower())
        norm = "".join(c for c in norm if unicodedata.category(c) != "Mn")
        norm = re.sub(r"[^a-z0-9]+", "", norm)
        if norm:
            norm_words.append(norm)
            # Span aponta para o core (sem pontuação final)
            core_end = token.start() + len(core)
            spans.append((token.start(), core_end))
    return norm_words, spans


def merge_texts(previous: str, new: str, max_overlap_words: int = 12) -> str:
    """
    Junta o texto do bloco seguinte removendo a sobreposição.

    Encontra o maior sufixo de `previous` que bate com um prefixo de `new`,
    comparando palavras normalizadas, até `max_overlap_words`.
    Só remove se ≥ 2 palavras batem (evita cortar palavra legítima).
    Preserva a pontuação/capitalização de `new` após o corte; espaço único entre partes.
    """
    if not previous or not new:
        return previous + new

    prev_words = _normalize_words(previous)
    new_words, new_spans = _tokenize_and_normalize(new)

    if not prev_words or not new_words:
        return previous + " " + new if previous and new else previous + new

    max_check = min(max_overlap_words, len(prev_words), len(new_words))
    best_overlap = 0

    # Testa do maior para o menor
    for n in range(max_check, 1, -1):  # mínimo 2 palavras
        if prev_words[-n:] == new_words[:n]:
            best_overlap = n
            break

    if best_overlap == 0:
        return previous + " " + new

    # Reconstrói mantendo a formatação de `new`
    if best_overlap >= len(new_spans):
        return previous

    # Ponto de corte: fim da última palavra do overlap
    overlap_end = new_spans[best_overlap - 1][1]
    suffix = new[overlap_end:]
    if not suffix:
        return previous

    # Adiciona espaço se previous não termina com espaço
    if previous and not previous[-1].isspace():
        # Se suffix já começa com espaço, não adiciona outro
        if suffix and suffix[0].isspace():
            return previous + suffix
        # Se suffix começa com pontuação (incluindo reticências ...), NÃO adiciona espaço
        if suffix and suffix[0] in ".,;:!?":
            return previous + suffix
        # Se previous termina com pontuação e suffix começa com pontuação, adiciona espaço
        if previous[-1] in ".,;:!?" and suffix and suffix[0] in ".,;:!?":
            return previous + " " + suffix
        # Previous termina com pontuação, suffix com letra → adiciona espaço para legibilidade
        if previous[-1] in ".,;:!?":
            return previous + " " + suffix
        # Previous termina com letra/número → adiciona espaço
        return previous + " " + suffix
    return previous + suffix


def plan_next(
    total_frames: int,
    consumed_frame: int,
    audio_tail: np.ndarray,
    overlap_s: float = 0.8,
    samplerate: int = 16000,
    max_s: float = 32.0,
) -> ChunkPlan | None:
    """
    Planeja o próximo bloco de transcrição.

    Usa `find_cut` no áudio restante (`audio_tail`) e aplica sobreposição
    SÓ quando o corte foi forçado (sem pausa detectada).

    Args:
        total_frames: total de amostras do áudio completo
        consumed_frame: amostras já consumidas (início do bloco atual)
        audio_tail: áudio restante a partir de `consumed_frame`
        overlap_s: segundos de sobreposição quando corte forçado
        samplerate: taxa de amostragem do áudio
        max_s: duração máxima do bloco em segundos (para detectar corte forçado)

    Returns:
        ChunkPlan com start, end, overlap (em amostras), ou None se acabou.
    """
    if consumed_frame >= total_frames:
        return None

    sr = samplerate
    cut = find_cut(audio_tail, samplerate=sr, max_s=max_s)
    overlap_frames = int(overlap_s * sr)

    if cut is None:
        # Áudio restante cabe em um bloco (< max_s)
        end = total_frames
        overlap = 0
    else:
        cut_frame = cut
        # Verifica se foi corte forçado (em max_s)
        max_frames = int(max_s * sr)
        if cut_frame >= max_frames:
            # Corte forçado → aplica sobreposição
            end = consumed_frame + cut_frame
            overlap = min(overlap_frames, cut_frame)
        else:
            # Corte em pausa → sem sobreposição
            end = consumed_frame + cut_frame
            overlap = 0

    # Garante que não passa do total
    end = min(end, total_frames)

    return ChunkPlan(start=consumed_frame, end=end, overlap=overlap)