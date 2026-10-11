import os
import re
import unicodedata
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
MODELS_DIR = Path(os.environ.get("HEAR_AI_MODELS_DIR") or ROOT / "models")


def read_wav(path) -> np.ndarray:
    """Lê wav 16-bit mono 16 kHz como float32 em [-1, 1]."""
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        assert w.getsampwidth() == 2
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def words(text: str) -> list[str]:
    """Minúsculas, sem acentos nem pontuação."""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", text)


def word_match_ratio(expected: str, got: str) -> float:
    exp = words(expected)
    got_set = set(words(got))
    return sum(w in got_set for w in exp) / len(exp)


@pytest.fixture
def fixture_pt():
    return read_wav(FIXTURES / "frase_pt.wav"), (FIXTURES / "frase_pt.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def model_small():
    if not (MODELS_DIR / "faster-whisper-small" / "model.bin").exists():
        pytest.skip("modelo 'small' não baixado; rode: python scripts/baixar_modelo.py")
    return "small"


@pytest.fixture(scope="session")
def transcriber_small(model_small):
    from hear_ai.transcriber import Transcriber

    t = Transcriber(model_small, MODELS_DIR, device="cpu", compute_type="int8", language="pt")
    t.load()
    return t
