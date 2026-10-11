"""Histórico de transcrições (JSONL por dia), com retenção, pausa, busca e apagar.

Formato: `<dir>/historico-AAAA-MM-DD.jsonl`, uma linha JSON por ditado:
`{"id", "ts", "text", "status", "duration_s", "language", ...}` (+ `raw`/`changes` quando o
vocabulário corrigiu algo — v1.3, T45b).

- `id` é um uuid gravado na linha: continua o mesmo depois de reabrir, apagar ou mudar status.
  Linhas antigas (v1.0, sem `id`) recebem um id derivado de `ts` + `text` (também estável).
- `status`: "pendente" (gravado antes de colar), "colado" ou "salvo" (ficou só na área de
  transferência / histórico).
- Todas as leituras e mutações acontecem sob um `threading.RLock`. `save` acrescenta uma linha;
  `set_status`/`delete` reescrevem o arquivo do dia num temporário da mesma pasta + `os.replace`
  (atômico: ou o arquivo antigo, ou o novo, nunca meio escrito).
- Erro de disco nunca sai daqui como exceção: loga só o TIPO do erro (nunca o texto ditado).
- Linha corrompida é ignorada na leitura e preservada como está nas reescritas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

log = logging.getLogger("hear_ai.history")

STATUSES = ("pendente", "colado", "salvo")
DEFAULT_STATUS = "salvo"
FILE_RE = re.compile(r"^historico-(\d{4}-\d{2}-\d{2})\.jsonl$")


class TranscriptSink(Protocol):
    """Protocolo para sinks de transcrição."""

    def save(self, text: str, meta: dict) -> str | None: ...


class NullSink:
    """Sink que não faz nada (histórico desativado)."""

    def save(self, text: str, meta: dict) -> None:
        return None


@dataclass
class Entry:
    id: str
    ts: datetime | None
    text: str
    duration_s: float | None
    language: str | None
    status: str
    day: str  # AAAA-MM-DD do arquivo onde está
    raw: str | None = None  # v1.3: texto do Whisper ANTES do vocabulário (só quando algo mudou)
    changes: tuple = ()  # v1.3: ((ouvido, correto), ...) corrigidos pelo vocabulário
    # T47b (P1): campos do Modo B
    generated: str | None = None  # prompt gerado (se usou gerador)
    revisar: bool = False  # verificação de fidelidade apontou falta


def _fold(s: str) -> str:
    """Minúsculas e sem acentos, para busca."""
    s = unicodedata.normalize("NFD", s.casefold())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _legacy_id(record: dict) -> str:
    raw = f"{record.get('ts', '')}\x00{record.get('text', '')}".encode("utf-8", "replace")
    return "h-" + hashlib.sha1(raw).hexdigest()[:16]


def _parse(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("text"), str):
        return None
    return rec


def _entry(rec: dict, day: str) -> Entry:
    ts = None
    if isinstance(rec.get("ts"), str):
        try:
            ts = datetime.fromisoformat(rec["ts"])
        except ValueError:
            ts = None
    try:
        duration = float(rec["duration_s"]) if rec.get("duration_s") is not None else None
    except (TypeError, ValueError):
        duration = None
    status = rec.get("status") if rec.get("status") in STATUSES else DEFAULT_STATUS
    rid = rec.get("id") if isinstance(rec.get("id"), str) and rec.get("id") else _legacy_id(rec)
    lang = rec.get("language") if isinstance(rec.get("language"), str) else None
    raw = rec.get("raw") if isinstance(rec.get("raw"), str) else None
    changes = tuple((c[0], c[1]) for c in rec.get("changes") or ()
                    if isinstance(c, (list, tuple)) and len(c) == 2 and all(isinstance(x, str) for x in c))
    # T47b (P1): novos campos
    generated = rec.get("generated") if isinstance(rec.get("generated"), str) else None
    revisar = rec.get("revisar") is True  # só o booleano verdadeiro (texto "false" não vale)
    return Entry(rid, ts, rec["text"], duration, lang, status, day, raw, changes, generated, revisar)


class HistoryStore:
    """Histórico em disco. Implementa `TranscriptSink.save` (devolve o id da entrada)."""

    def __init__(self, dir: str | Path, retention_days: int | None = 30,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self.dir = Path(dir)
        self.retention_days = retention_days
        self.clock = clock
        self._paused = False
        self._lock = threading.RLock()

    # --- pausa -------------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = bool(value)

    # --- arquivos ----------------------------------------------------------------------
    def _path_for(self, day: date | str) -> Path:
        day = day if isinstance(day, str) else day.isoformat()
        return self.dir / f"historico-{day}.jsonl"

    def _files(self) -> list[tuple[str, Path]]:
        """(dia, caminho) de todos os arquivos de histórico, do mais novo para o mais antigo."""
        found = []
        try:
            if not self.dir.is_dir():
                return []
            for p in self.dir.iterdir():
                m = FILE_RE.match(p.name)
                if m and p.is_file():
                    found.append((m.group(1), p))
        except OSError as e:
            log.warning("falha ao listar histórico: %s", type(e).__name__)
            return []
        return sorted(found, reverse=True)

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()

    def _rewrite(self, path: Path, lines: list[str]) -> None:
        """Troca o arquivo inteiro de forma atômica (temporário na mesma pasta + os.replace)."""
        if not lines:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    # --- gravar ------------------------------------------------------------------------
    def save(self, text: str, meta: dict | None = None) -> str | None:
        """Acrescenta uma entrada e devolve o id. Pausado ou erro de disco: None."""
        if self._paused:
            return None
        meta = dict(meta or {})
        status = meta.pop("status", DEFAULT_STATUS)
        if status not in STATUSES:
            status = DEFAULT_STATUS
        now = self.clock()
        rid = uuid.uuid4().hex
        record = {**meta, "id": rid, "ts": now.isoformat(), "text": text, "status": status}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)  # pasta criada no 1º save
                with open(self._path_for(now.date()), "a", encoding="utf-8", newline="\n") as f:
                    f.write(line + "\n")
            except OSError as e:
                log.warning("falha ao salvar histórico: %s", type(e).__name__)
                return None
        return rid

    # --- ler ---------------------------------------------------------------------------
    def entries(self, limit: int | None = None, query: str | None = None) -> list[Entry]:
        """Entradas mais recentes primeiro. `query`: trecho do texto, sem diferenciar acento/maiúscula."""
        q = _fold(query.strip()) if query and query.strip() else None
        out: list[Entry] = []
        with self._lock:
            for day, path in self._files():
                try:
                    lines = self._read_lines(path)
                except OSError as e:
                    log.warning("falha ao ler histórico: %s", type(e).__name__)
                    continue
                day_entries = []
                for line in lines:
                    rec = _parse(line)
                    if rec is None:
                        continue
                    e = _entry(rec, day)
                    if q is None or q in _fold(e.text):
                        day_entries.append(e)
                # arquivos vêm do dia mais novo; dentro do dia, a ordem das linhas é a de gravação
                out.extend(reversed(day_entries))
        return out[:limit] if limit is not None else out

    def get(self, entry_id: str) -> Entry | None:
        for e in self.entries():
            if e.id == entry_id:
                return e
        return None

    # --- mudar / apagar ----------------------------------------------------------------
    def _mutate(self, entry_id: str, change: Callable[[dict], dict | None]) -> bool:
        """Aplica `change` à linha com `entry_id` (None = apagar) e reescreve o arquivo dela."""
        with self._lock:
            for day, path in self._files():
                try:
                    lines = self._read_lines(path)
                except OSError as e:
                    log.warning("falha ao ler histórico: %s", type(e).__name__)
                    continue
                for i, line in enumerate(lines):
                    rec = _parse(line)
                    if rec is None or _entry(rec, day).id != entry_id:
                        continue
                    new = change(rec)
                    if new is None:
                        del lines[i]
                    else:
                        lines[i] = json.dumps(new, ensure_ascii=False, default=str)
                    try:
                        self._rewrite(path, lines)
                    except OSError as e:
                        log.warning("falha ao reescrever histórico: %s", type(e).__name__)
                        return False
                    return True
        return False

    def set_status(self, entry_id: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(f"status inválido: {status!r}")

        def change(rec):
            rec = dict(rec)
            rec.setdefault("id", entry_id)  # linha antiga ganha o id que já era mostrado
            rec["status"] = status
            return rec

        return self._mutate(entry_id, change)

    def delete(self, entry_id: str) -> bool:
        return self._mutate(entry_id, lambda rec: None)

    def clear_all(self) -> int:
        """Apaga todos os arquivos de histórico. Devolve quantos foram apagados."""
        n = 0
        with self._lock:
            for _day, path in self._files():
                try:
                    path.unlink()
                    n += 1
                except OSError as e:
                    log.warning("falha ao apagar histórico: %s", type(e).__name__)
        return n

    def purge_expired(self) -> int:
        """Apaga arquivos com data < hoje − retention_days. Devolve quantos foram apagados."""
        if not self.retention_days or self.retention_days <= 0:
            return 0
        limit = self.clock().date() - timedelta(days=self.retention_days)
        n = 0
        with self._lock:
            for day, path in self._files():
                try:
                    if date.fromisoformat(day) < limit:
                        path.unlink()
                        n += 1
                except (OSError, ValueError) as e:
                    log.warning("falha ao apagar histórico antigo: %s", type(e).__name__)
        return n


# compatibilidade com o nome da v1.0
JsonlHistorySink = HistoryStore


def make_sink(cfg, base_dir: str | Path) -> TranscriptSink:
    """`HistoryStore` em `base_dir / cfg.history_dir` se `cfg.history_enabled`, senão `NullSink`.

    Lê também `history_retention_days` (padrão 30) e `history_paused` (padrão False), e já
    apaga o que passou da retenção. Futuro: FolderExportSink para `cfg.export_dir`.
    """
    if not getattr(cfg, "history_enabled", False):
        return NullSink()
    store = HistoryStore(
        Path(base_dir) / getattr(cfg, "history_dir", "historico"),
        retention_days=getattr(cfg, "history_retention_days", 30),
    )
    store.paused = getattr(cfg, "history_paused", False)
    store.purge_expired()
    return store
