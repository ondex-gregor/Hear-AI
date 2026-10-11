"""Testes da transcrição em blocos durante a gravação (T13)."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pytest

from hear_ai.streaming import StreamingTranscriber

SR = 16000
log = logging.getLogger("teste.streaming")


# --- áudio sintético: cada "palavra" é um trecho com amplitude única ---------------------

BASE, STEP = 0.1, 1e-4


def palavra_audio(n_palavras, palavra_s=0.5, pausa_cada=6, pausa_s=0.6):
    """Palavras de `palavra_s` segundos; a cada `pausa_cada` palavras, um silêncio de `pausa_s`.
    pausa_cada=0: fala contínua (sem pausa nenhuma → corte forçado com sobreposição)."""
    partes = []
    for i in range(n_palavras):
        partes.append(np.full(int(palavra_s * SR), BASE + i * STEP, np.float32))
        if pausa_cada and (i + 1) % pausa_cada == 0:
            partes.append(np.zeros(int(pausa_s * SR), np.float32))
    return np.concatenate(partes)


def esperado(n):
    return " ".join(f"palavra{i}" for i in range(n))


class FakeTranscriber:
    """Devolve as palavras cujas amplitudes aparecem no bloco (≥ 0,1 s), em ordem."""

    def __init__(self, delay=0.0, fail_on_call=None):
        self.delay = delay
        self.fail_on_call = fail_on_call
        self.calls: list[int] = []  # tamanho (amostras) de cada chamada
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def transcribe(self, audio):
        with self._lock:
            self.calls.append(len(audio))
            n = len(self.calls)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail_on_call == n:
                raise RuntimeError("CUDA out of memory (simulado)")
            vals = np.round((audio[audio > BASE / 2] - BASE) / STEP).astype(int)
            idx, counts = np.unique(vals, return_counts=True)
            ordem = [i for i, c in zip(idx, counts) if c >= 0.1 * SR]
            return " ".join(f"palavra{i}" for i in ordem)
        finally:
            with self._lock:
                self.active -= 1


class ManualRecorder:
    """Gravador falso: o teste decide quanto áudio já foi 'gravado'."""

    def __init__(self, audio):
        self.audio = audio
        self.frames = 0
        self.lock = threading.Lock()

    @property
    def frames_recorded(self):
        with self.lock:
            return self.frames

    def feed_to(self, seconds):
        with self.lock:
            self.frames = min(len(self.audio), int(seconds * SR))

    def feed_all(self):
        with self.lock:
            self.frames = len(self.audio)

    def snapshot(self, start):
        with self.lock:
            return self.audio[start:self.frames].copy()


class FeedRecorder:
    """Gravador falso em 'tempo real' acelerado: o áudio chega à velocidade `speed`x."""

    def __init__(self, audio, speed=1.0):
        self.audio = audio
        self.speed = speed
        self.t0 = None

    def start(self):
        self.t0 = time.perf_counter()

    @property
    def frames_recorded(self):
        return min(len(self.audio), int((time.perf_counter() - self.t0) * SR * self.speed))

    def done(self):
        return self.frames_recorded >= len(self.audio)

    def snapshot(self, start):
        return self.audio[start:self.frames_recorded].copy()


def wait_until(cond, timeout=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.005)
    return False


def make(audio, transcriber=None, **kw):
    rec = ManualRecorder(audio)
    tr = transcriber or FakeTranscriber()
    st = StreamingTranscriber(rec, tr, log, poll_s=0.005, **kw)
    return rec, tr, st


# --- testes rápidos ------------------------------------------------------------------------

def test_gravacao_curta_sem_blocos_finish_transcreve_tudo_de_uma_vez():
    audio = palavra_audio(20)  # ~12 s
    rec, tr, st = make(audio)
    st.start()
    rec.feed_all()
    time.sleep(0.05)
    assert tr.calls == []  # menos de um bloco: nada em fundo
    assert st.finish(audio) == esperado(20)
    assert tr.calls == [len(audio)]


def test_blocos_durante_a_gravacao_e_finish_so_transcreve_o_resto():
    n = 200
    audio = palavra_audio(n)  # ~120 s, com pausas
    rec, tr, st = make(audio)
    st.start()
    for s in range(0, 121, 5):  # a gravação avança
        rec.feed_to(s)
        time.sleep(0.01)
    rec.feed_all()
    assert wait_until(lambda: len(audio) - st.consumed < st.block_frames)
    em_fundo = len(tr.calls)
    assert em_fundo >= 3
    texto = st.finish(audio)
    assert texto == esperado(n)  # completo, em ordem, sem repetição
    assert not st.fallback
    resto = tr.calls[em_fundo:]
    assert all(c <= 32 * SR for c in resto) and len(resto) <= 2
    assert sum(resto) < 33 * SR  # ao parar, só o pedaço final
    assert all(20 * SR <= (e - s) <= 32 * SR for s, e, _ in st.blocks[:em_fundo])  # cortes nas pausas


def test_corte_forcado_sem_pausas_nao_duplica_na_emenda():
    n = 160
    audio = palavra_audio(n, pausa_cada=0)  # 80 s de fala contínua
    rec, tr, st = make(audio)
    st.start()
    rec.feed_all()
    texto = st.finish(audio)
    assert texto == esperado(n)
    palavras = texto.split()
    assert len(palavras) == len(set(palavras))
    # houve sobreposição (o bloco seguinte começou antes do fim do anterior)
    assert any(b2[0] < b1[1] for b1, b2 in zip(st.blocks, st.blocks[1:]))


def test_no_maximo_um_bloco_pendente():
    audio = palavra_audio(250)
    tr = FakeTranscriber(delay=0.05)
    rec, tr, st = make(audio, tr, max_lag_blocks=10)
    st.start()
    rec.feed_all()
    texto = st.finish(audio)
    assert tr.max_active == 1
    assert texto == esperado(250)


def test_atraso_grande_aciona_plano_b(caplog):
    audio = palavra_audio(250)  # ~150 s
    tr = FakeTranscriber()
    rec, tr, st = make(audio, tr, max_lag_blocks=2)
    with caplog.at_level(logging.WARNING, logger="teste.streaming"):
        st.start()
        rec.feed_all()  # de repente 150 s sem transcrever (> 2 blocos de 32 s)
        assert wait_until(lambda: st.fallback)
        texto = st.finish(audio)
    assert texto == esperado(250)
    assert tr.calls == [len(audio)]  # nenhum bloco; tudo de uma vez
    assert "atraso" in st.fallback_reason
    assert "blocos abandonados" in caplog.text


def test_falha_em_bloco_aciona_plano_b_sem_vazar_texto(caplog):
    audio = palavra_audio(200)
    tr = FakeTranscriber(fail_on_call=2)
    rec, tr, st = make(audio, tr, max_lag_blocks=10)
    with caplog.at_level(logging.INFO, logger="teste.streaming"):
        st.start()
        for s in range(0, 121, 4):
            rec.feed_to(s)
            time.sleep(0.01)
        rec.feed_all()
        texto = st.finish(audio)
    assert st.fallback and "RuntimeError" in st.fallback_reason
    assert texto == esperado(200)
    assert tr.calls[-1] == len(audio)  # plano B: o áudio inteiro
    assert "palavra" not in caplog.text  # log sem texto
    assert "RuntimeError" in caplog.text


def test_falha_no_resto_ao_parar_tambem_cai_no_plano_b():
    audio = palavra_audio(100)  # ~60 s
    calls = {"n": 0}
    base = FakeTranscriber()

    class FalhaNoFim:
        def transcribe(self, a):
            calls["n"] += 1
            if calls["n"] == 2:  # o bloco final, já dentro do finish()
                raise OSError("disco")
            return base.transcribe(a)

    rec, _, st = make(audio, FalhaNoFim())
    st.start()
    rec.feed_all()
    assert wait_until(lambda: st.blocks)
    assert st.finish(audio) == esperado(100)
    assert st.fallback and calls["n"] == 3


def test_cancelar_para_sem_transcrever():
    audio = palavra_audio(200)
    tr = FakeTranscriber(delay=0.05)
    rec, tr, st = make(audio, tr, max_lag_blocks=10)
    st.start()
    rec.feed_all()
    assert wait_until(lambda: tr.calls)
    st.cancel()
    n = len(tr.calls)
    time.sleep(0.2)
    assert len(tr.calls) == n  # nada novo depois do cancelamento
    assert st.finish(audio) == ""
    assert len(tr.calls) == n


def test_reinicia_entre_gravacoes():
    a1, a2 = palavra_audio(80), palavra_audio(30)
    rec, tr, st = make(a1)
    st.start()
    rec.feed_all()
    assert st.finish(a1) == esperado(80)
    rec.audio, rec.frames = a2, 0
    st.start()
    rec.feed_all()
    assert st.finish(a2) == esperado(30)
    assert not st.fallback and st.consumed == len(a2)


def test_finish_sem_start_transcreve_tudo():
    audio = palavra_audio(70)
    rec, tr, st = make(audio)
    assert st.finish(audio) == esperado(70)


# --- fixtures longas reais (modelo de verdade) --------------------------------------------

FIXTURES = Path(os.environ.get("HEAR_AI_LONG_FIXTURES") or Path(__file__).parent / "fixtures" / "longas")
MODELS = Path(os.environ.get("HEAR_AI_MODELS_DIR") or Path(__file__).resolve().parents[1] / "models")
SPEED = float(os.environ.get("HEAR_AI_STREAM_SPEED", "8"))


def norm(text):
    text = unicodedata.normalize("NFD", text.lower().replace("%", " por cento "))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", text)


def coverage(ref, hyp):
    sm = SequenceMatcher(None, ref, hyp, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(ref)


def extra_trigrams(ref, hyp):
    cr = Counter(tuple(ref[i:i + 3]) for i in range(len(ref) - 2))
    ch = Counter(tuple(hyp[i:i + 3]) for i in range(len(hyp) - 2))
    return sum(max(0, c - max(cr.get(t, 0), 1)) for t, c in ch.items())


def seam_repeats(blocks):
    """Emendas em que o bloco seguinte começa repetindo ≥ 2 palavras do fim do anterior."""
    n = 0
    for (_, _, a), (_, _, b) in zip(blocks, blocks[1:]):
        na, nb = norm(a), norm(b)
        if any(na[-k:] == nb[:k] for k in range(2, 7) if len(na) >= k and len(nb) >= k):
            n += 1
    return n


def immediate_repeats(ref, hyp):
    """Repetições coladas ('a b a b') no texto final que não existem na referência."""
    out = []
    for n in (1, 2, 3):
        for i in range(len(hyp) - 2 * n + 1):
            if hyp[i:i + n] == hyp[i + n:i + 2 * n]:
                seq = hyp[i:i + 2 * n]
                if not any(ref[j:j + 2 * n] == seq for j in range(len(ref) - 2 * n + 1)):
                    out.append(" ".join(seq))
    return out


def end_present(ref, hyp, k=15):
    sm = SequenceMatcher(None, ref[-k:], hyp[-3 * k:], autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / k


@pytest.fixture(scope="module")
def real_transcriber():
    if not (MODELS / "faster-whisper-small" / "model.bin").exists():
        pytest.skip("modelo 'small' não baixado")
    from hear_ai.transcriber import Transcriber

    t = Transcriber("small", MODELS, device=os.environ.get("HEAR_AI_STREAM_DEVICE", "auto"), language="pt")
    t.load()
    return t


def load_fixture(name):
    wav = FIXTURES / f"fala_{name}.wav"
    if not wav.exists():
        pytest.skip(f"{wav} não existe (gere com scripts/gerar_fixtures_longas.ps1 ou defina HEAR_AI_LONG_FIXTURES)")
    from hear_ai.audio import load_wav

    ref = norm((FIXTURES / f"fala_{name}.txt").read_text(encoding="utf-8"))
    return load_wav(wav), ref


def run_streaming(transcriber, audio, speed):
    rec = FeedRecorder(audio, speed)
    st = StreamingTranscriber(rec, transcriber, log, poll_s=0.25)
    rec.start()
    st.start()
    while not rec.done():
        time.sleep(0.05)
    t0 = time.perf_counter()
    text = st.finish(audio)
    return text, time.perf_counter() - t0, st


def measure(transcriber, name, speed):
    audio, ref = load_fixture(name)
    t0 = time.perf_counter()
    whole = transcriber.transcribe(audio)
    wait_whole = time.perf_counter() - t0
    text, wait_blocks, st = run_streaming(transcriber, audio, speed)
    h_whole, h_blocks = norm(whole), norm(text)
    return {
        "fixture": name, "device": transcriber.device_used, "speed": speed,
        "audio_s": len(audio) / SR, "blocks": len(st.blocks), "fallback": st.fallback_reason,
        "wait_blocks": wait_blocks, "wait_whole": wait_whole,
        "cov_blocks": coverage(ref, h_blocks), "cov_whole": coverage(ref, h_whole),
        "dup_blocks": extra_trigrams(ref, h_blocks), "dup_whole": extra_trigrams(ref, h_whole),
        "seams_rep": seam_repeats(st.blocks), "imm_blocks": immediate_repeats(ref, h_blocks),
        "overlaps": sum(1 for b1, b2 in zip(st.blocks, st.blocks[1:]) if b2[0] < b1[1]),
        "end_blocks": end_present(ref, h_blocks), "end_whole": end_present(ref, h_whole),
    }


def fmt(r):
    return (f"{r['fixture']:<6} {r['device']:<5} {r['speed']:>4.0f}x audio={r['audio_s']:6.1f}s blocos={r['blocks']:>2} "
            f"espera: blocos={r['wait_blocks']:6.2f}s inteira={r['wait_whole']:6.2f}s | "
            f"cobertura: {100 * r['cov_blocks']:.1f}% x {100 * r['cov_whole']:.1f}% | "
            f"dup: {r['dup_blocks']} x {r['dup_whole']} | emendas c/ repetição: {r['seams_rep']}"
            f" (sobrepostas {r['overlaps']}) | rep. coladas: {len(r['imm_blocks'])} | fim: {100 * r['end_blocks']:.0f}% x {100 * r['end_whole']:.0f}%"
            f" | plano B: {r['fallback']}")


@pytest.mark.slow
@pytest.mark.parametrize("name", ["2min", "6min", "12min"])
def test_fixture_longa_em_blocos(real_transcriber, name):
    if name == "12min" and real_transcriber.device_used != "cuda":
        pytest.skip("12 min só com GPU")
    r = measure(real_transcriber, name, SPEED)
    print("\n" + fmt(r))
    assert r["fallback"] is None
    assert r["blocks"] >= 3
    assert r["cov_blocks"] >= r["cov_whole"] - 0.02
    # emendas sem palavras duplicadas. (A contagem global de trigramas repetidos, `dup`, só é
    # impressa: na voz sintética ela mede frases-modelo com número mal reconhecido, não emendas.)
    assert r["seams_rep"] == 0
    assert r["imm_blocks"] == []
    assert r["end_blocks"] >= 0.8
    assert r["wait_blocks"] < r["wait_whole"]


@pytest.mark.slow
def test_fixture_falha_em_bloco_real_cai_no_plano_b(real_transcriber):
    audio, ref = load_fixture("2min")

    class FalhaNoSegundo:
        def __init__(self):
            self.n = 0

        def transcribe(self, a):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("falha simulada")
            return real_transcriber.transcribe(a)

    text, _, st = run_streaming(FalhaNoSegundo(), audio, SPEED)
    assert st.fallback and coverage(ref, norm(text)) >= 0.85


@pytest.mark.slow
def test_bench_espera(real_transcriber):
    """Tabela de medições (só com HEAR_AI_STREAM_BENCH=1). Para CPU em tempo real:
    HEAR_AI_STREAM_DEVICE=cpu HEAR_AI_STREAM_SPEED=1 HEAR_AI_STREAM_BENCH=2min"""
    which = os.environ.get("HEAR_AI_STREAM_BENCH")
    if not which:
        pytest.skip("defina HEAR_AI_STREAM_BENCH=1 (ou uma lista: 2min,6min)")
    names = ["2min", "6min", "12min"] if which == "1" else which.split(",")
    for name in names:
        print("\n" + fmt(measure(real_transcriber, name, SPEED)), flush=True)


def test_corte_em_pausa_nao_apaga_repeticao_legitima_da_fala():
    """Se o falante repete palavras logo depois de uma pausa, a junção não pode apagá-las."""

    class Repete:
        def __init__(self):
            self.n = 0

        def transcribe(self, a):
            self.n += 1
            return "eu disse isso" if self.n == 1 else "eu disse isso de novo"

    audio = palavra_audio(100)  # ~60 s com pausas: corte em pausa, sem sobreposição
    rec, _, st = make(audio, Repete())
    st.start()
    rec.feed_all()
    texto = st.finish(audio)
    assert all(b[2] for b in st.blocks)
    assert st.blocks[0][1] == st.blocks[1][0]  # sem sobreposição
    assert texto == "eu disse isso eu disse isso de novo"
