"""Gravação de microfone (16 kHz mono), cópia em disco à prova de falha e leitura de wav."""

from __future__ import annotations

import bisect
import json
import logging
import os
import queue
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger("hear_ai.audio")

SPOOL_VERSION = 1
FLUSH_INTERVAL_S = 0.5  # o arquivo em disco nunca fica mais de ~0,5 s atrás do microfone


def to_int16(block: np.ndarray) -> np.ndarray:
    return (np.clip(block, -1.0, 1.0) * 32767.0).astype("<i2")


class _SpoolWriter:
    """Acrescenta blocos int16 crus num .pcm em uma thread própria (fora do callback de áudio)."""

    def __init__(self, path: Path):
        self.path = path
        self.failed = False
        self._queue: queue.Queue = queue.Queue()
        self._file = open(path, "ab")
        self._thread = threading.Thread(target=self._run, name="hear-ai-spool", daemon=True)
        self._thread.start()

    def put(self, block: np.ndarray) -> None:
        self._queue.put(to_int16(block).tobytes())

    def _run(self) -> None:
        last_flush = time.monotonic()
        pending = False
        while True:
            try:
                item = self._queue.get(timeout=FLUSH_INTERVAL_S)
            except queue.Empty:
                item = b""
            if item is None:
                break
            if item and not self.failed:
                try:
                    self._file.write(item)
                    pending = True
                except OSError as e:
                    self.failed = True
                    log.warning("falha ao gravar cópia em disco: %s; seguindo só em memória", type(e).__name__)
            if pending and time.monotonic() - last_flush >= FLUSH_INTERVAL_S:
                self._flush()
                pending = False
                last_flush = time.monotonic()
        self._flush()
        if not self.failed:
            try:
                os.fsync(self._file.fileno())  # garante o fim da gravação no disco antes de devolver
            except OSError as e:
                log.warning("falha ao sincronizar cópia em disco: %s", type(e).__name__)
        try:
            self._file.close()
        except OSError as e:
            log.warning("falha ao fechar cópia em disco: %s", type(e).__name__)

    def _flush(self) -> None:
        if self.failed:
            return
        try:
            self._file.flush()
        except OSError as e:
            self.failed = True
            log.warning("falha ao descarregar cópia em disco: %s", type(e).__name__)

    def close(self, timeout: float = 30.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.warning("escritor da cópia em disco não terminou a tempo")


def _refresh_sounddevice() -> None:
    """Reinicia o PortAudio para enxergar microfones novos / o novo dispositivo padrão.
    Só é chamado depois que o nosso stream morto foi fechado."""
    import sounddevice as sd

    sd._terminate()
    sd._initialize()


def _new_spool_path(spool_dir: Path, samplerate: int) -> Path:
    spool_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = spool_dir / f"gravacao-{stamp}.pcm"
    n = 1
    while path.exists() or path.with_suffix(".json").exists():
        path = spool_dir / f"gravacao-{stamp}-{n}.pcm"
        n += 1
    meta = {
        "samplerate": samplerate,
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": SPOOL_VERSION,
    }
    path.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
    return path


class Recorder:
    """Grava o microfone em memória e (opcional) acrescenta tudo num arquivo .pcm em disco.

    Limite: `max_seconds` (padrão 30 min). `warn_before_s` antes do fim chama
    `on_limit_warning(segundos_restantes)` uma vez; ao atingir o limite chama
    `on_limit_reached()` uma vez. O que passar do limite é ignorado, com aviso no log.
    Os callbacks rodam na thread de áudio: devem ser rápidos (só sinalizar).

    Microfone desconectado / dispositivo padrão trocado (T31): um vigia (thread) percebe o stream
    morto (inativo, ou sem áudio chegando por `watchdog_s`), avisa `on_device_event("lost")`,
    reabre no dispositivo PADRÃO atual (até `reconnect_attempts` vezes, `reconnect_delay` s entre
    elas) e avisa "reconnected"; se não der, avisa "failed" (o controlador encerra e transcreve o
    que já tem). O que já foi gravado (memória + disco) NUNCA se perde.
    """

    def __init__(
        self,
        samplerate=16000,
        device=None,
        max_seconds=1800,
        spool_dir=None,
        on_limit_warning=None,
        warn_before_s=60,
        on_limit_reached=None,
        stream_factory=None,
        on_device_event=None,
        watchdog_s="auto",
        reconnect_attempts=3,
        reconnect_delay=0.5,
        refresh_devices=None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.samplerate = samplerate
        self.device = device
        self.max_seconds = max_seconds
        self.spool_dir = Path(spool_dir) if spool_dir is not None else None
        self.on_limit_warning = on_limit_warning
        self.warn_before_s = warn_before_s
        self.on_limit_reached = on_limit_reached
        self._stream_factory = stream_factory
        self._stream = None
        self._lock = threading.Lock()
        self.on_device_event = on_device_event
        # vigia: ligado por padrão só com o stream real (streams falsos de teste param sozinhos)
        if watchdog_s == "auto":
            watchdog_s = 1.0 if stream_factory is None else None
        self.watchdog_s = watchdog_s
        self.reconnect_attempts = reconnect_attempts
        self.reconnect_delay = reconnect_delay
        if refresh_devices is None and stream_factory is None:
            refresh_devices = _refresh_sounddevice
        self._refresh_devices = refresh_devices
        self._sleep = sleep
        self._clock = clock
        self._stream_lock = threading.RLock()  # troca de stream (vigia) x stop()
        self._watch_stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._last_audio = 0.0
        self.reconnects = 0  # quantas vezes reconectou nesta gravação
        self._writer: _SpoolWriter | None = None
        self.current_spool_path: Path | None = None
        self.last_spool_path: Path | None = None
        self._reset()

    def _reset(self) -> None:
        self._chunks: list[np.ndarray] = []
        self._starts: list[int] = []  # quadro inicial de cada bloco (para snapshot)
        self._frames = 0
        self._dropped = 0
        self._warned = False
        self._limited = False
        self._level = 0.0

    @property
    def max_frames(self) -> int:
        return int(self.max_seconds * self.samplerate)

    @property
    def frames_recorded(self) -> int:
        with self._lock:
            return self._frames

    def level(self) -> float:
        """Nível RMS do bloco mais recente, 0..1."""
        return self._level

    # --- thread de áudio -------------------------------------------------------
    def _callback(self, indata, frames, time_info, status):
        self._last_audio = self._clock()
        block = np.array(indata, dtype=np.float32)
        if block.ndim > 1:
            block = block[:, 0]
        if len(block):
            self._level = float(min(1.0, np.sqrt(np.mean(block * block))))
        fire_warn = fire_limit = None
        with self._lock:
            room = self.max_frames - self._frames
            if room <= 0:
                self._dropped += len(block)
                return
            if len(block) > room:
                self._dropped += len(block) - room
                block = block[:room]
            self._chunks.append(block)
            self._starts.append(self._frames)
            self._frames += len(block)
            remaining = self.max_frames - self._frames
            if not self._warned and remaining <= self.warn_before_s * self.samplerate:
                self._warned = True
                fire_warn = remaining / self.samplerate
            if not self._limited and remaining <= 0:
                self._limited = True
                fire_limit = True
            writer = self._writer
        if writer is not None:
            writer.put(block)
        if fire_warn is not None:
            log.info("gravação perto do limite: %.0f s restantes", fire_warn)
            self._safe_call(self.on_limit_warning, fire_warn)
        if fire_limit:
            log.warning("limite de gravação atingido (%s s); o excedente será ignorado", self.max_seconds)
            self._safe_call(self.on_limit_reached)

    @staticmethod
    def _safe_call(fn, *args) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            log.warning("callback de limite falhou: %s", type(e).__name__)

    # --- controle ------------------------------------------------------------
    def _default_factory(self, **kwargs):
        import sounddevice as sd

        return sd.InputStream(**kwargs)

    def _open_spool(self) -> None:
        self.current_spool_path = None
        if self.spool_dir is None:
            return
        try:
            path = _new_spool_path(self.spool_dir, self.samplerate)
            self._writer = _SpoolWriter(path)
            self.current_spool_path = path
        except OSError as e:
            self._writer = None
            log.warning("sem cópia em disco desta gravação: %s", type(e).__name__)

    def _close_spool(self) -> Path | None:
        writer, self._writer = self._writer, None
        path, self.current_spool_path = self.current_spool_path, None
        if writer is not None:
            writer.close()
        return path

    def _open_stream(self, device):
        """Cria e inicia um stream; se falhar, fecha o que abriu e repassa o erro."""
        factory = self._stream_factory or self._default_factory
        stream = factory(
            samplerate=self.samplerate,
            channels=1,
            dtype="float32",
            device=device,
            callback=self._callback,
        )
        try:
            stream.start()
        except BaseException:
            self._close_quietly(stream)  # não deixa o dispositivo preso
            raise
        return stream

    def start(self) -> None:
        if self._stream is not None:
            return
        with self._lock:
            self._reset()
        self.reconnects = 0
        self._open_spool()
        try:
            stream = self._open_stream(self.device)
        except BaseException:
            self.last_spool_path = self._close_spool()
            raise
        with self._stream_lock:
            self._stream = stream
        self._last_audio = self._clock()
        if self.watchdog_s:
            self._watch_stop.clear()
            self._watcher = threading.Thread(target=self._watch, name="hear-ai-mic-vigia", daemon=True)
            self._watcher.start()

    # --- vigia do microfone (T31) --------------------------------------------
    def _emit(self, kind: str) -> None:
        log.info("microfone: %s", kind)
        self._safe_call(self.on_device_event, kind)

    def stream_alive(self) -> bool:
        stream = self._stream
        if stream is None:
            return False
        if not getattr(stream, "active", True):
            return False
        return (self._clock() - self._last_audio) <= (self.watchdog_s or float("inf"))

    def _watch(self) -> None:
        step = max(0.05, min(0.25, (self.watchdog_s or 1.0) / 4))
        while not self._watch_stop.wait(step):
            if self._stream is None:
                return
            if self.stream_alive():
                continue
            if not self._reconnect():
                return  # falhou: o controlador encerra a gravação

    def _reconnect(self) -> bool:
        """Fecha o stream morto e tenta reabrir no dispositivo padrão. True se voltou."""
        self._emit("lost")
        with self._stream_lock:
            dead, self._stream = self._stream, None
        if dead is not None:
            self._close_quietly(dead)
        for attempt in range(1, self.reconnect_attempts + 1):
            if self._watch_stop.wait(self.reconnect_delay):
                return False  # pararam a gravação enquanto esperava
            if self._refresh_devices is not None:
                try:
                    self._refresh_devices()  # o PortAudio só vê o "novo padrão" depois de recarregar
                except Exception as e:
                    log.warning("recarregar a lista de microfones falhou: %s", type(e).__name__)
            try:
                stream = self._open_stream(None)  # dispositivo padrão ATUAL do Windows
            except Exception as e:
                log.warning("reabrir o microfone (tentativa %d) falhou: %s", attempt, type(e).__name__)
                continue
            with self._stream_lock:
                if self._watch_stop.is_set():  # stop() chegou enquanto abria
                    self._close_quietly(stream)
                    return False
                self._stream = stream
            self._last_audio = self._clock()
            self.reconnects += 1
            self._emit("reconnected")
            return True
        self._emit("failed")
        return False

    @staticmethod
    def _close_quietly(stream) -> None:
        """Fecha o stream; erro de fechamento é só registrado (tipo), nunca propagado."""
        for step in (stream.stop, stream.close):
            try:
                step()
            except Exception as e:
                log.warning("falha ao fechar stream de áudio: %s", type(e).__name__)

    def stop(self) -> np.ndarray:
        """Para e devolve o áudio (float32). O caminho da cópia em disco fica em `last_spool_path`
        (o arquivo NÃO é apagado aqui; só depois da colagem confirmada)."""
        self._watch_stop.set()
        watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(self.reconnect_delay * 2 + 2)
        self._watcher = None
        with self._stream_lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            # erro ao parar/fechar (ex.: USB desconectado) não pode perder o áudio já gravado
            self._close_quietly(stream)
        with self._lock:
            chunks, self._chunks = self._chunks, []
            self._starts = []
            dropped = self._dropped
            self._frames = 0
            self._dropped = 0
            self._level = 0.0
        if self._writer is not None or self.current_spool_path is not None:
            self.last_spool_path = self._close_spool()
        if dropped:
            log.warning("%.1f s de áudio passaram do limite de %s s e foram ignorados",
                        dropped / self.samplerate, self.max_seconds)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32, copy=False)

    def snapshot(self, start_frame: int = 0) -> np.ndarray:
        """Cópia do áudio gravado a partir de `start_frame` (thread-safe; a gravação continua)."""
        with self._lock:
            if start_frame >= self._frames or not self._chunks:
                return np.zeros(0, dtype=np.float32)
            start_frame = max(0, start_frame)
            i = bisect.bisect_right(self._starts, start_frame) - 1
            parts = list(self._chunks[i:])
            offset = start_frame - self._starts[i]
        parts[0] = parts[0][offset:]
        return np.concatenate(parts).astype(np.float32, copy=True)


def load_wav(path: str | Path, target_rate: int = 16000) -> np.ndarray:
    """Lê wav PCM (8/16/32 bits) como float32 mono em target_rate."""
    with wave.open(str(path), "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"largura de amostra não suportada: {width} bytes")
    if channels > 1:
        data = data[: len(data) // channels * channels].reshape(-1, channels).mean(axis=1)
    return resample(data, rate, target_rate)


def resample(data: np.ndarray, rate: int, target_rate: int = 16000) -> np.ndarray:
    """Reamostragem linear simples."""
    if rate != target_rate and len(data) > 1:
        n = int(round(len(data) * target_rate / rate))
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)
    return np.ascontiguousarray(data, dtype=np.float32)
