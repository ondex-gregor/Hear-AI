"""Recuperação de gravações que sobraram em disco (app fechado/travado antes de colar).

Cada gravação com cópia em disco é `gravacao-*.pcm` (int16 mono cru) + `gravacao-*.json`
(`{"samplerate", "started", "version"}`). Um .pcm que ainda existe é um "órfão": o ditado
não chegou a ser confirmado. Arquivo truncado (byte ímpar) e .json ausente/corrompido são
tolerados (assume 16 kHz).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from hear_ai.audio import resample

log = logging.getLogger("hear_ai.recovery")

DEFAULT_RATE = 16000


@dataclass
class Orphan:
    path: Path  # o .pcm
    samplerate: int
    frames: int
    started: datetime | None  # do .json; None se ausente/corrompido
    modified: datetime  # última escrita do .pcm

    @property
    def meta_path(self) -> Path:
        return self.path.with_suffix(".json")

    @property
    def duration_s(self) -> float:
        return self.frames / self.samplerate

    @property
    def when(self) -> datetime:
        """Melhor data disponível para mostrar ao usuário."""
        return self.started or self.modified


def _read_meta(pcm: Path) -> tuple[int, datetime | None]:
    try:
        meta = json.loads(pcm.with_suffix(".json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("json não é objeto")
    except (OSError, ValueError):
        return DEFAULT_RATE, None
    rate = meta.get("samplerate")
    if not isinstance(rate, int) or isinstance(rate, bool) or not 1000 <= rate <= 192000:
        rate = DEFAULT_RATE
    started = None
    if isinstance(meta.get("started"), str):
        try:
            started = datetime.fromisoformat(meta["started"])
        except ValueError:
            started = None
    return rate, started


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _excluded(path: Path, exclude) -> bool:
    if exclude is None:
        return False
    if isinstance(exclude, (str, Path)):
        exclude = [exclude]
    return any(e is not None and _same(path, Path(e)) for e in exclude)


def find_orphans(spool_dir, exclude=None) -> list[Orphan]:
    """Gravações que sobraram em `spool_dir`, mais recentes primeiro.

    `exclude`: caminho(s) a ignorar, como o `Recorder.current_spool_path` da gravação em curso.
    Arquivos vazios (0 quadros) não são listados (`discard_older_than` os limpa).
    """
    spool_dir = Path(spool_dir)
    if not spool_dir.is_dir():
        return []
    found = []
    for pcm in spool_dir.glob("gravacao-*.pcm"):
        if _excluded(pcm, exclude):
            continue
        try:
            st = pcm.stat()
        except OSError:
            continue
        frames = st.st_size // 2  # byte ímpar no fim = escrita interrompida; ignorado
        if frames == 0:
            continue
        rate, started = _read_meta(pcm)
        found.append(Orphan(pcm, rate, frames, started, datetime.fromtimestamp(st.st_mtime)))
    found.sort(key=lambda o: o.modified, reverse=True)
    return found


def load_orphan(orphan: Orphan, target_rate: int = DEFAULT_RATE) -> np.ndarray:
    """Áudio float32 mono em `target_rate` (16 kHz)."""
    raw = orphan.path.read_bytes()
    raw = raw[: len(raw) // 2 * 2]
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return resample(data, orphan.samplerate, target_rate)


def discard(orphan: Orphan) -> None:
    for p in (orphan.path, orphan.meta_path):
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.warning("não foi possível apagar gravação antiga: %s", type(e).__name__)


def discard_older_than(spool_dir, days: float, exclude=None, now: datetime | None = None) -> int:
    """Apaga .pcm (e .json) com última escrita há mais de `days` dias, inclusive vazios.
    Também remove .json sem .pcm. Devolve quantas gravações foram apagadas."""
    spool_dir = Path(spool_dir)
    if not spool_dir.is_dir():
        return 0
    limit = (now or datetime.now()) - timedelta(days=days)
    count = 0
    for pcm in spool_dir.glob("gravacao-*.pcm"):
        if _excluded(pcm, exclude):
            continue
        try:
            mtime = datetime.fromtimestamp(pcm.stat().st_mtime)
        except OSError:
            continue
        if mtime < limit:
            discard(Orphan(pcm, DEFAULT_RATE, 0, None, mtime))
            count += 1
    for meta in spool_dir.glob("gravacao-*.json"):
        if meta.with_suffix(".pcm").exists() or _excluded(meta.with_suffix(".pcm"), exclude):
            continue
        try:
            if datetime.fromtimestamp(meta.stat().st_mtime) < limit:
                meta.unlink(missing_ok=True)
        except OSError:
            pass
    return count
