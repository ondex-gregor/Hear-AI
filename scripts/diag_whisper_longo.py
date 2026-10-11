"""Diagnóstico: como o Whisper (modelo small) se comporta em fala longa.

Para cada fixture de tests/fixtures/longas e cada configuração de `model.transcribe`,
mede tempo, cobertura, WER aproximado, duplicação (trigramas repetidos que não se repetem
na referência), maior trecho repetido e se o final do texto aparece.

Não altera o app nem o config.json: usa o Transcriber do projeto com parâmetros diretos.

Uso (na raiz do projeto ou da worktree):
  set HEAR_AI_MODELS_DIR=<pasta do projeto>\\models
  .venv\\Scripts\\python.exe scripts\\diag_whisper_longo.py [--device cuda|cpu|auto] [--fixtures 15s 2min] [--configs A B]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hear_ai.audio import load_wav  # noqa: E402
from hear_ai.transcriber import Transcriber  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "longas"
NOMES = ["15s", "2min", "6min", "12min"]

BASE = {"beam_size": 5, "vad_filter": True}  # o que o app usa hoje (transcriber._run)
NO_COND = {**BASE, "condition_on_previous_text": False}
CONFIGS = {
    "A": ("atual do app (beam 5, VAD padrão)", BASE),
    "B": ("A + condition_on_previous_text=False", NO_COND),
    "C": ("B + VAD min_silence=500ms, speech_pad=200ms",
          {**NO_COND, "vad_parameters": {"min_silence_duration_ms": 500, "speech_pad_ms": 200}}),
    "D": ("B + repetition_penalty=1.1", {**NO_COND, "repetition_penalty": 1.1}),
    "E": ("B + no_repeat_ngram_size=5", {**NO_COND, "no_repeat_ngram_size": 5}),
}


# --- métricas ------------------------------------------------------------------

def words(text: str) -> list[str]:
    text = unicodedata.normalize("NFD", text.lower().replace("%", " por cento "))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", text)


def coverage(ref: list[str], hyp: list[str]) -> float:
    """% das palavras da referência que aparecem na saída, em ordem."""
    if not ref:
        return 1.0
    sm = SequenceMatcher(None, ref, hyp, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(ref)


def wer(ref: list[str], hyp: list[str]) -> float:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / max(len(ref), 1)


def ngrams(ws: list[str], n: int) -> Counter:
    return Counter(tuple(ws[i:i + n]) for i in range(len(ws) - n + 1))


def duplication(ref: list[str], hyp: list[str]) -> int:
    """Ocorrências extras de trigramas que se repetem na saída além do que a referência repete."""
    cr, ch = ngrams(ref, 3), ngrams(hyp, 3)
    return sum(max(0, c - max(cr.get(t, 0), 1)) for t, c in ch.items())


def longest_repeat(ref: list[str], hyp: list[str], cap: int = 400) -> int:
    """Maior n tal que algum n-grama aparece ≥2 vezes na saída e <2 vezes na referência."""
    best = 0
    for n in range(3, min(cap, len(hyp)) + 1):
        cr, ch = ngrams(ref, n), ngrams(hyp, n)
        if any(c >= 2 and cr.get(t, 0) < 2 for t, c in ch.items()):
            best = n
        else:
            break
    return best


def end_present(ref: list[str], hyp: list[str], k: int = 15) -> float:
    """Fração das últimas k palavras da referência achadas (em ordem) no fim da saída."""
    tail_ref = ref[-k:]
    tail_hyp = hyp[-(3 * k):]
    sm = SequenceMatcher(None, tail_ref, tail_hyp, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / max(len(tail_ref), 1)


# --- execução ------------------------------------------------------------------

def run_case(model, audio, cfg: dict) -> tuple[str, float, int]:
    t0 = time.perf_counter()
    segments, _info = model.transcribe(audio, language="pt", **cfg)
    segs = [s.text.strip() for s in segments]
    return " ".join(segs).strip(), time.perf_counter() - t0, len(segs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu", "auto"])
    ap.add_argument("--fixtures", nargs="+", default=NOMES, choices=NOMES)
    ap.add_argument("--configs", nargs="+", default=list(CONFIGS), choices=list(CONFIGS))
    ap.add_argument("--out", default=str(ROOT / "evidencias" / "A1-whisper-longo.txt"))
    ap.add_argument("--textos", default=str(ROOT / "evidencias" / "A1-whisper"))
    args = ap.parse_args(argv)

    models_dir = os.environ.get("HEAR_AI_MODELS_DIR") or "models"
    tr = Transcriber("small", models_dir, device=args.device, language="pt")
    t0 = time.perf_counter()
    tr.load()
    load_s = time.perf_counter() - t0
    model = tr._model  # parâmetros de transcribe variam por configuração

    textos = Path(args.textos)
    textos.mkdir(parents=True, exist_ok=True)
    header = (
        f"Diagnóstico Whisper em fala longa — modelo small, device pedido={args.device}, "
        f"device usado={tr.device_used}, carga={load_s:.1f}s\n"
        + "\n".join(f"  {k}: {CONFIGS[k][0]}" for k in args.configs)
        + "\n\nMétricas: cobertura = % das palavras da referência presentes em ordem; WER aproximado;"
        "\n  dup = ocorrências extras de trigramas repetidos que não se repetem na referência;"
        "\n  maior_rep = maior trecho repetido (palavras) que não se repete na referência;"
        "\n  fim = % das últimas 15 palavras da referência achadas no fim da saída;"
        "\n  RTF = tempo de transcrição / duração do áudio.\n\n"
    )
    cols = f"{'fixture':<8}{'cfg':<4}{'audio_s':>8}{'tempo_s':>9}{'RTF':>7}{'segs':>6}{'ref_pal':>8}{'saida_pal':>10}" \
           f"{'cobert%':>9}{'WER%':>7}{'dup':>6}{'maior_rep':>10}{'fim%':>6}"
    linhas = [cols, "-" * len(cols)]
    print(header + cols, flush=True)

    for nome in args.fixtures:
        wav = FIXTURES / f"fala_{nome}.wav"
        if not wav.exists():
            print(f"{wav} não existe; rode scripts/gerar_fixtures_longas.ps1", file=sys.stderr)
            return 2
        audio = load_wav(wav)
        dur = len(audio) / 16000
        ref = words((FIXTURES / f"fala_{nome}.txt").read_text(encoding="utf-8"))
        for k in args.configs:
            texto, secs, nsegs = run_case(model, audio, CONFIGS[k][1])
            (textos / f"fala_{nome}_{k}.txt").write_text(texto + "\n", encoding="utf-8")
            hyp = words(texto)
            linha = (
                f"{nome:<8}{k:<4}{dur:>8.1f}{secs:>9.1f}{secs / dur:>7.3f}{nsegs:>6}{len(ref):>8}{len(hyp):>10}"
                f"{100 * coverage(ref, hyp):>9.1f}{100 * wer(ref, hyp):>7.1f}{duplication(ref, hyp):>6}"
                f"{longest_repeat(ref, hyp):>10}{100 * end_present(ref, hyp):>6.0f}"
            )
            linhas.append(linha)
            print(linha, flush=True)

    Path(args.out).write_text(header + "\n".join(linhas) + "\n", encoding="utf-8")
    print(f"\nTabela: {args.out}\nTextos: {textos}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
