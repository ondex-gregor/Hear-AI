"""Transcrição em blocos DURANTE a gravação (A3).

Enquanto o usuário fala, uma thread de fundo olha quanto áudio já foi gravado; quando há
um bloco inteiro (~27–32 s), corta num silêncio (`chunking.plan_next`) e transcreve esse
bloco. Ao parar, `finish()` só precisa transcrever o pedaço final, então a espera depois
de soltar o atalho fica curta mesmo em ditados de vários minutos.

Regras:
- No máximo 1 bloco em transcrição (a thread é sequencial: se a transcrição atrasar, ela
  não enfileira outros; o áudio só acumula).
- Plano B: se um bloco falhar (exceção) ou o atraso passar de `max_lag_blocks` blocos de
  áudio não transcritos, os blocos são abandonados e `finish()` transcreve o áudio inteiro
  de uma vez, como antes.
- Log só com tipo do erro e tamanhos (segundos); nunca o texto.
- T35: cada gravação tem uma GERAÇÃO; a thread de uma gravação antiga (cancelada com um bloco
  ainda na GPU) nunca escreve no estado da nova. `finish()` espera o bloco em curso no máximo
  `finish_wait_s`; se a GPU travar, abandona os blocos e cai no plano B (áudio inteiro).
"""

from __future__ import annotations

import threading
import time

import numpy as np

from hear_ai.chunking import merge_texts, plan_next

BLOCK_MAX_S = 32.0  # o mesmo `max_s` padrão de chunking.find_cut: abaixo disso não há corte
FINISH_WAIT_S = 30.0  # T35: espera máxima pelo bloco em curso ao parar (GPU travada)
CANCEL_WAIT_S = 2.0  # T35: espera máxima pela thread ao cancelar


class StreamingTranscriber:
    def __init__(
        self,
        recorder,
        transcriber,
        logger,
        poll_s: float = 1.0,
        target_s: float = 27,
        samplerate: int = 16000,
        max_lag_blocks: int = 2,
        block_max_s: float = BLOCK_MAX_S,
        finish_wait_s: float = FINISH_WAIT_S,
        cancel_wait_s: float = CANCEL_WAIT_S,
    ):
        self.recorder = recorder
        self.transcriber = transcriber
        self.logger = logger
        self.poll_s = poll_s
        self.target_s = target_s  # informativo: o corte usa o padrão de chunking (27 s, 20–32 s)
        self.samplerate = samplerate
        self.max_lag_blocks = max_lag_blocks
        self.block_frames = int(block_max_s * samplerate)
        self.finish_wait_s = finish_wait_s
        self.cancel_wait_s = cancel_wait_s
        self._gen = 0  # T35: geração da gravação atual
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self.text = ""
        self.consumed = 0  # início do próximo bloco (em amostras)
        self._last_overlap = 0  # sobreposição que o próximo bloco repete do anterior
        self.blocks: list[tuple[int, int, str]] = []  # (início, fim, texto) — diagnóstico/testes
        self.fallback_reason: str | None = None
        self.cancelled = False
        self.transcribe_s = 0.0  # tempo gasto transcrevendo blocos em fundo

    @property
    def fallback(self) -> bool:
        return self.fallback_reason is not None

    # --- ciclo -------------------------------------------------------------------------
    def start(self) -> None:
        """Começa a acompanhar a gravação (chamar logo depois de `recorder.start()`).

        Sempre zera o estado e abre uma geração nova — mesmo que a thread de uma gravação
        cancelada ainda esteja terminando um bloco (antes, o `start` retornava cedo e o
        `cancelled=True` antigo fazia o ditado novo voltar vazio)."""
        old = self._thread
        if old is not None and old.is_alive():
            self._stop.set()  # a antiga sai no próximo laço; o resultado dela é descartado pela geração
        stop = threading.Event()
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._reset()
            self._stop = stop
        self._thread = threading.Thread(target=self._run, args=(gen, stop), name="hear-ai-streaming", daemon=True)
        self._thread.start()

    def _current(self, gen: int) -> bool:
        return gen == self._gen

    def _run(self, gen: int | None = None, stop: threading.Event | None = None) -> None:
        gen = self._gen if gen is None else gen
        stop = stop or self._stop
        while not stop.wait(self.poll_s):
            if not self._current(gen) or self.fallback:
                return
            try:
                total = self.recorder.frames_recorded
            except Exception as e:
                self._give_up(f"gravador: {type(e).__name__}")
                return
            pending = total - self.consumed
            if pending > self.max_lag_blocks * self.block_frames:
                self._give_up(f"atraso: {pending / self.samplerate:.0f} s sem transcrever")
                return
            if pending < self.block_frames:
                continue  # ainda não há um bloco inteiro
            if not self._do_block(final=False, gen=gen):
                return

    def _do_block(self, final: bool, audio_total: np.ndarray | None = None, gen: int | None = None) -> bool:
        """Transcreve o próximo bloco. False se caiu no plano B (ou se a gravação foi cancelada/trocada)."""
        gen = self._gen if gen is None else gen
        start = self.consumed
        if audio_total is None:
            tail = self.recorder.snapshot(start)
        else:
            tail = audio_total[start:]
        if len(tail) == 0:
            return True
        plan = plan_next(start + len(tail), start, tail)
        if plan is None:
            return True
        if not final and plan.end >= start + len(tail) and len(tail) < self.block_frames:
            return True  # sem corte possível ainda; espera mais áudio
        block = tail[: plan.end - start]
        if self.cancelled or not self._current(gen):
            return False  # T35: cancelado antes de transcrever — não gasta GPU à toa
        t0 = time.perf_counter()
        try:
            text = self.transcriber.transcribe(block)
        except Exception as e:
            self._give_up(f"bloco {start / self.samplerate:.0f}–{plan.end / self.samplerate:.0f} s: {type(e).__name__}")
            return False
        self.transcribe_s += time.perf_counter() - t0
        with self._lock:
            if self.cancelled or not self._current(gen):
                return False  # gravação cancelada ou já substituída por outra: descarta
            text = (text or "").strip()
            if text:
                if not self.text:
                    self.text = text
                elif self._last_overlap > 0:
                    # o bloco começou antes do fim do anterior: tira as palavras repetidas
                    self.text = merge_texts(self.text, text)
                else:
                    # corte numa pausa: nada se repete; juntar direto evita apagar uma
                    # repetição legítima da fala
                    self.text = f"{self.text} {text}"
            self.blocks.append((plan.start, plan.end, text))
            # com corte forçado (sem pausa), o próximo bloco repete `overlap` amostras;
            # merge_texts remove as palavras repetidas
            self.consumed = max(plan.end - plan.overlap, start + 1)
            self._last_overlap = plan.overlap
        self.logger.info("bloco transcrito: %.1f–%.1f s (sobreposição %.1f s)",
                         plan.start / self.samplerate, plan.end / self.samplerate, plan.overlap / self.samplerate)
        return True

    def _give_up(self, reason: str) -> None:
        with self._lock:
            if self.fallback_reason is None:
                self.fallback_reason = reason
        self.logger.warning("blocos abandonados (%s); o áudio inteiro será transcrito ao parar", reason)

    def finish(self, audio_total: np.ndarray) -> str:
        """Chamado ao parar: espera o bloco em curso, transcreve o resto e devolve o texto juntado."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.finish_wait_s)
            if self._thread.is_alive():
                # GPU travada no bloco em curso: abandona os blocos (a thread, quando voltar,
                # não escreve mais nada — geração trocada) e transcreve o áudio inteiro
                with self._lock:
                    self._gen += 1
                self._give_up(f"bloco em curso passou de {self.finish_wait_s:.0f} s")
        audio_total = np.asarray(audio_total, dtype=np.float32)
        if self.cancelled:
            return ""
        if not self.fallback and self.consumed > len(audio_total):
            self._give_up("áudio final menor que o já transcrito")
        if not self.fallback:
            while self.consumed < len(audio_total):
                if not self._do_block(final=True, audio_total=audio_total):
                    break
        if self.fallback:
            self.logger.info("plano B: transcrevendo %.1f s de uma vez", len(audio_total) / self.samplerate)
            return self.transcriber.transcribe(audio_total).strip()
        return self.text.strip()

    def cancel(self, wait_s: float | None = None) -> None:
        """Para de acompanhar sem transcrever mais nada (Esc / descartar).

        T35: espera a thread terminar no máximo `wait_s` (padrão `cancel_wait_s`); se o bloco em
        curso demorar mais, segue — o resultado dele é descartado (flag + geração)."""
        with self._lock:
            self.cancelled = True
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(self.cancel_wait_s if wait_s is None else wait_s)
