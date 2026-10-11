"""D3 (T30): mede a qualidade da transcrição ANTES de adotar mudanças.

Compara, nas fixtures:
  A = como o app está (sem initial_prompt)
  B = initial_prompt com os termos do dicionário ("Ondex, Maestri, Groq, Hear AI, Claude Code.")
cada uma "crua" e depois do pós-texto (dicionário + comandos de voz, T25).
Fixtures longas passam pelos blocos da T13 (StreamingTranscriber): o prompt vai em CADA bloco.

Métricas: cobertura (palavras da referência presentes em ordem), WER aproximado, nomes próprios
escritos certos (Ondex, Maestri, Groq, Hear AI, Claude Code; com maiúsculas), números em dígito.
Também: 3 s de silêncio / ruído baixo (a saída deve ficar VAZIA: o Whisper às vezes "copia" o
prompt) e idioma "auto" (frase_pt / frase_en / nomes_pt).

Uso:
  .venv\\Scripts\\python.exe scripts\\diag_qualidade.py --gerar-fixture   (cria tests/fixtures/nomes_pt.wav)
  set HEAR_AI_MODELS_DIR=...  &  set HEAR_AI_LONG_FIXTURES=...
  .venv\\Scripts\\python.exe scripts\\diag_qualidade.py [--device auto|cpu|cuda] [--sem-longas]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "tests" / "fixtures"
NAMES = ("Ondex", "Maestri", "Groq", "Hear AI", "Claude Code")
SR = 16000

_PS_TTS = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$s.SelectVoice('Microsoft Maria Desktop'); "
    "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
    "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
    "$s.SetOutputToWaveFile($env:HEARAI_WAV, $f); "
    "$s.Speak([System.IO.File]::ReadAllText($env:HEARAI_TXT, [System.Text.Encoding]::UTF8)); "
    "$s.Dispose()"
)


def gerar_fixture(nome: str = "nomes_pt") -> Path:
    """Sintetiza tests/fixtures/<nome>.wav a partir do .txt com a voz Maria (caminhos por env)."""
    txt, wav = FIXTURES / f"{nome}.txt", FIXTURES / f"{nome}.wav"
    env = dict(os.environ, HEARAI_TXT=str(txt), HEARAI_WAV=str(wav))
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_TTS],
                   env=env, check=True, timeout=120)
    return wav


# --- métricas ----------------------------------------------------------------------------------

def words(text: str) -> list[str]:
    text = unicodedata.normalize("NFD", text.lower().replace("%", " por cento "))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", text)


def coverage(ref, hyp) -> float:
    if not ref:
        return 1.0
    sm = SequenceMatcher(None, ref, hyp, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(ref)


def wer(ref, hyp) -> float:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / max(len(ref), 1)


def count_name(text: str, name: str) -> int:
    """Ocorrências escritas EXATAMENTE certo (maiúsculas inclusive), palavra inteira."""
    return len(re.findall(rf"(?<!\w){re.escape(name)}(?!\w)", text))


def names_score(ref_text: str, hyp_text: str) -> tuple[int, int]:
    total = sum(count_name(ref_text, n) for n in NAMES)
    certos = sum(min(count_name(ref_text, n), count_name(hyp_text, n)) for n in NAMES)
    return certos, total


def numbers_score(ref_text: str, hyp_text: str) -> tuple[int, int]:
    ref = Counter(re.findall(r"\d+", ref_text))
    hyp = Counter(re.findall(r"\d+", hyp_text))
    return sum(min(c, hyp.get(n, 0)) for n, c in ref.items()), sum(ref.values())


# --- execução ----------------------------------------------------------------------------------

class _AllAtOnceRecorder:
    """Gravador falso com o áudio inteiro já 'gravado' (para passar pelos blocos da T13)."""

    def __init__(self, audio):
        self.audio = audio

    @property
    def frames_recorded(self):
        return len(self.audio)

    def snapshot(self, start):
        return self.audio[start:].copy()


def transcribe_blocks(transcriber, audio) -> str:
    from hear_ai.streaming import StreamingTranscriber

    st = StreamingTranscriber(_AllAtOnceRecorder(audio), transcriber, logging.getLogger("diag"), poll_s=3600)
    st.start()  # a thread de fundo nem chega a acordar (poll alto); finish() faz os blocos em ordem
    text = st.finish(audio)
    if st.fallback:
        print(f"    (plano B: {st.fallback_reason})", flush=True)
    return text


def load_fixtures(long_dir: Path | None, include_long: bool):
    from hear_ai.audio import load_wav

    out = []
    for nome in ("frase_pt", "nomes_pt"):
        wav, txt = FIXTURES / f"{nome}.wav", FIXTURES / f"{nome}.txt"
        if wav.exists():
            out.append((nome, load_wav(wav), txt.read_text(encoding="utf-8"), False))
    if include_long and long_dir is not None:
        for nome in ("fala_15s", "fala_2min", "fala_6min"):
            wav, txt = long_dir / f"{nome}.wav", long_dir / f"{nome}.txt"
            if wav.exists():
                out.append((nome, load_wav(wav), txt.read_text(encoding="utf-8"), nome != "fala_15s"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--gerar-fixture", action="store_true")
    ap.add_argument("--sem-longas", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "evidencias" / "D3-qualidade.txt"))
    args = ap.parse_args(argv)

    if args.gerar_fixture:
        print("gerado:", gerar_fixture())
        return 0

    from hear_ai.postprocess import DEFAULT_DICTIONARY, postprocess
    from hear_ai.transcriber import Transcriber, prompt_from_dictionary

    models = os.environ.get("HEAR_AI_MODELS_DIR") or str(ROOT / "models")
    long_dir = Path(os.environ["HEAR_AI_LONG_FIXTURES"]) if os.environ.get("HEAR_AI_LONG_FIXTURES") else None
    prompt = prompt_from_dictionary(DEFAULT_DICTIONARY)
    tr = Transcriber("small", models, device=args.device, language="pt")
    tr.load()
    lines = [f"D3 qualidade — modelo small, device={tr.device_used}, prompt B = {prompt!r}", ""]
    header = (f"{'fixture':<10}{'cfg':<4}{'pós':<5}{'tempo_s':>8}{'cobert%':>9}{'WER%':>7}"
              f"{'nomes':>8}{'números':>9}")
    lines += [header, "-" * len(header)]
    print("\n".join(lines), flush=True)

    textos = []
    for nome, audio, ref_text, longa in load_fixtures(long_dir, not args.sem_longas):
        ref = words(ref_text)
        for cfg_name, p in (("A", None), ("B", prompt)):
            tr.initial_prompt = p
            t0 = time.perf_counter()
            raw = transcribe_blocks(tr, audio) if longa else tr.transcribe(audio)
            secs = time.perf_counter() - t0
            for pos_name, text in (("não", raw), ("sim", postprocess(raw))):
                hyp = words(text)
                nc, nt = names_score(ref_text, text)
                uc, ut = numbers_score(ref_text, text)
                linha = (f"{nome:<10}{cfg_name:<4}{pos_name:<5}{secs:>8.1f}{100 * coverage(ref, hyp):>9.1f}"
                         f"{100 * wer(ref, hyp):>7.1f}{f'{nc}/{nt}':>8}{f'{uc}/{ut}':>9}")
                lines.append(linha)
                print(linha, flush=True)
            if not longa:
                textos.append((nome, cfg_name, raw, postprocess(raw)))

    # silêncio / ruído baixo: a saída deve ficar vazia (o prompt não pode "vazar")
    lines += ["", "Silêncio (3 s) — a saída deve ser vazia:"]
    rng = np.random.default_rng(0)
    for rotulo, audio in (("zeros", np.zeros(3 * SR, np.float32)),
                          ("ruído baixo", (rng.standard_normal(3 * SR) * 0.003).astype(np.float32))):
        for cfg_name, p in (("A", None), ("B", prompt)):
            tr.initial_prompt = p
            out = tr.transcribe(audio)
            linha = f"  {rotulo:<12} cfg {cfg_name}: {out!r}"
            lines.append(linha)
            print(linha, flush=True)

    # idioma automático
    lines += ["", "Idioma 'auto' (detecção; < 0,5 ou fora de pt/en -> pt):"]
    from hear_ai.audio import load_wav

    tr.initial_prompt = None
    tr.language = "auto"
    for nome in ("frase_pt", "frase_en", "nomes_pt"):
        wav = FIXTURES / f"{nome}.wav"
        if not wav.exists():
            continue
        audio = load_wav(wav)
        lang, prob, _ = tr._model.detect_language(audio)
        out = tr.transcribe(audio)
        linha = f"  {nome:<10} detectado={lang} ({prob:.2f}) -> usado={tr.last_language}: {out!r}"
        lines.append(linha)
        print(linha, flush=True)
    tr.language = "pt"

    lines += ["", "Transcrições (fixtures curtas):"]
    for nome, cfg_name, raw, pos in textos:
        lines.append(f"  {nome} {cfg_name} cru : {raw}")
        lines.append(f"  {nome} {cfg_name} pós : {pos}")
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nsalvo em {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
