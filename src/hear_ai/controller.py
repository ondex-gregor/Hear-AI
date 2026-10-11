"""Máquina de estados: idle -> recording -> transcribing -> idle.

Fluxo de um ditado (Fase A, revisão 010):
1. gravar (Recorder com cópia em disco) + transcrição em blocos durante a gravação;
2. ao parar: texto final (`streaming.finish`, ou o áudio inteiro sem streaming);
3. salvar no histórico com status "pendente" ANTES de colar;
4. esperar as teclas do atalho serem soltas; colar; marcar "colado" ou "salvo";
5. apagar a cópia em disco (spool) quando o texto estiver salvo no histórico; sem histórico
   (pausado/desligado), só depois de colar com sucesso; senão fica para recuperar;
6. avisar: sucesso, ou o aviso `saved` ("texto salvo — Ctrl+V") quando não colou.

Avisos extras para a interface via `on_state_change`: "saved", "error", "limit_warning"
(não são estados: o estado volta a ser idle/recording logo em seguida).
T47b (P2): Modo B — após pós-texto, se `prompt_step` existir, chama-o (em thread de trabalho),
emite estado "generating", cola `resultado.texto`. Histórico: `text`=colado, `raw`=fiel,
`generated`=resultado.texto se `usou_gerador`, `revisar`=resultado.revisar. Se `usou_gerador`
for False, emite também evento "generator_fallback".
Nada aqui registra o texto ditado no log: só tamanhos e tipos de erro.
"""

from __future__ import annotations

import threading
from pathlib import Path

from hear_ai.logs import describe_text

IDLE = "idle"
RECORDING = "recording"
TRANSCRIBING = "transcribing"
# avisos (eventos de on_state_change; não são estados)
SAVED = "saved"
ERROR = "error"
LIMIT_WARNING = "limit_warning"
# T47b (P2): evento de fallback do gerador
GENERATOR_FALLBACK = "generator_fallback"
# T31 (robustez): microfone e janela de destino
MIC_LOST = "mic_lost"  # "microfone desconectado — reconectando…"
MIC_RECONNECTED = "mic_reconnected"  # "microfone reconectado"
MIC_FAILED = "mic_failed"  # não voltou: encerra e transcreve o que já tem
WINDOW_CHANGED = "window_changed"  # a janela em primeiro plano mudou: não cola (vem SAVED depois)
ADMIN_WINDOW = "admin_window"  # janela de administrador: Ctrl+V não chega (vem SAVED depois)

MIN_SECONDS = 0.3
SAMPLERATE = 16000


def discard_spool(path, logger=None) -> None:
    """Apaga o .pcm e o .json de uma gravação em disco."""
    if not path:
        return
    path = Path(path)
    for p in (path, path.with_suffix(".json")):
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            if logger is not None:
                logger.warning("não foi possível apagar áudio temporário: %s", type(e).__name__)


class Controller:
    def __init__(
        self,
        recorder,
        transcriber,
        paster,
        feedback,
        sink,
        logger,
        language,
        on_state_change=None,
        run_async=True,
        before_paste=None,
        min_seconds=MIN_SECONDS,
        samplerate=SAMPLERATE,
        streaming=None,
        postprocess="default",
        window_guard=None,
        prompt_step=None,
    ):
        self.recorder = recorder
        self.transcriber = transcriber
        self.paster = paster
        self.feedback = feedback
        self.sink = sink
        self.logger = logger
        self.language = language
        self.on_state_change = on_state_change
        self.run_async = run_async
        # chamado antes de colar (ex.: esperar o usuário soltar Ctrl/Alt); False = não colar
        self.before_paste = before_paste
        self.min_seconds = min_seconds
        self.samplerate = samplerate
        self.streaming = streaming  # StreamingTranscriber (opcional)
        # T30: pós-texto (dicionário + comandos de voz) ANTES de salvar/colar.
        # "default" = dicionário padrão da T25 + comandos de voz ligados; None = desligado;
        # ou um callable(texto) -> texto (ver `postprocessor_from_config`).
        if postprocess == "default":
            from hear_ai.postprocess import postprocess as default_postprocess

            postprocess = default_postprocess
        self.postprocess = postprocess
        # T31: janela de destino. `window_guard` tem foreground(), is_elevated(hwnd) e
        # self_elevated() (paster.Win32Windows); o app liga o real, os testes passam um falso.
        self.window_guard = window_guard
        self._target_window = None
        # T31: avisos do microfone (desconectou / reconectou / não voltou)
        if hasattr(recorder, "on_device_event") and getattr(recorder, "on_device_event", None) is None:
            recorder.on_device_event = self.on_device_event
        # T47b (P2): etapa do gerador de prompts (Modo B). None = Modo A (fluxo atual).
        # Callable (texto_fiel) -> gerador.Resultado. Roda em thread de trabalho, nunca na thread do Qt.
        self.prompt_step = prompt_step
        self._post_meta: dict | None = None  # v1.3: raw/changes do último pós-texto (vai ao histórico)
        self._state = IDLE
        self._lock = threading.Lock()
        self._notify_lock = threading.Lock()  # entrega ordenada à bandeja
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> str:
        return self._state

    # --- notificações ----------------------------------------------------------------
    def _set_state(self, new: str) -> None:
        with self._lock:
            self._state = new
        self._notify()

    def _notify(self, event: str | None = None) -> None:
        """Avisa a UI do estado ATUAL (ou de um aviso), uma notificação por vez.

        Lê o estado dentro da trava de notificação: se dois threads mudam o estado
        quase juntos (ex.: _process termina enquanto begin() chega), a última
        notificação entregue sempre reflete o estado final, nunca um valor velho.
        """
        with self._notify_lock:
            cb = self.on_state_change
            if cb is None:
                return
            try:
                cb(event if event is not None else self._state)
            except Exception as e:
                self.logger.warning("on_state_change falhou: %s", type(e).__name__)

    def _emit(self, event: str) -> None:
        self._notify(event)

    # --- gravação ----------------------------------------------------------------------
    def begin(self) -> None:
        with self._lock:
            if self._state != IDLE:
                return
            self._state = RECORDING
        try:
            self.recorder.start()
        except Exception as e:
            self.logger.error("falha ao iniciar gravação: %s", type(e).__name__)
            self.feedback.error()
            self._emit(ERROR)
            self._set_state(IDLE)
            return
        self._target_window = self._foreground()  # onde o usuário estava ao começar a falar
        if self.streaming is not None:
            try:
                self.streaming.start()
            except Exception as e:  # sem blocos, o ditado ainda funciona (áudio inteiro no fim)
                self.logger.warning("transcrição em blocos indisponível: %s", type(e).__name__)
        self.feedback.start()
        self._set_state(RECORDING)

    def end(self) -> None:
        with self._lock:
            if self._state != RECORDING:
                return
            self._state = TRANSCRIBING
        try:
            audio = self.recorder.stop()
        except Exception as e:
            self.logger.error("falha ao parar gravação: %s", type(e).__name__)
            self._cancel_streaming()
            self.feedback.error()
            self._emit(ERROR)
            self._set_state(IDLE)
            return
        spool = getattr(self.recorder, "last_spool_path", None)
        self.feedback.stop()
        self._set_state(TRANSCRIBING)
        if self.run_async:
            self._thread = threading.Thread(target=self._process, args=(audio, spool), daemon=True)
            self._thread.start()
        else:
            self._process(audio, spool)

    def toggle(self) -> None:
        state = self._state
        if state == IDLE:
            self.begin()
        elif state == RECORDING:
            self.end()
        # transcribing: ignora

    def cancel(self) -> bool:
        """Esc durante a gravação: para e DESCARTA (áudio e cópia em disco). True se cancelou."""
        with self._lock:
            if self._state != RECORDING:
                return False
            self._state = TRANSCRIBING  # bloqueia begin/end até terminar de descartar
        self._cancel_streaming()
        try:
            self.recorder.stop()
        except Exception as e:
            self.logger.warning("falha ao parar gravação cancelada: %s", type(e).__name__)
        discard_spool(getattr(self.recorder, "last_spool_path", None), self.logger)
        self.logger.info("gravação cancelada e descartada")
        self.feedback.stop()
        self._set_state(IDLE)
        return True

    def _cancel_streaming(self) -> None:
        if self.streaming is not None:
            try:
                self.streaming.cancel()
            except Exception as e:
                self.logger.warning("falha ao cancelar blocos: %s", type(e).__name__)

    # limite de gravação (callbacks do Recorder, chamados na thread de áudio: só sinalizam)
    def on_limit_warning(self, seconds_left: float) -> None:
        self.logger.info("gravação perto do limite (%.0f s restantes)", seconds_left)
        self.feedback.error()
        self._emit(LIMIT_WARNING)

    def on_limit_reached(self) -> None:
        # end() fecha o stream de áudio: não pode rodar dentro do callback do próprio stream
        threading.Thread(target=self.end, name="hear-ai-limit", daemon=True).start()

    # microfone (Recorder.on_device_event, chamado pela thread vigia do Recorder)
    def on_device_event(self, kind: str) -> None:
        if kind == "lost":
            self.logger.warning("microfone sumiu; tentando reconectar")
            self._emit(MIC_LOST)
        elif kind == "reconnected":
            self.logger.info("microfone reconectado")
            self._emit(MIC_RECONNECTED)
        elif kind == "failed":
            self.logger.warning("microfone não voltou; encerrando e transcrevendo o que já foi gravado")
            self.feedback.error()
            self._emit(MIC_FAILED)
            # nunca descarta: encerra como se o usuário tivesse soltado o atalho
            threading.Thread(target=self.end, name="hear-ai-mic-falhou", daemon=True).start()

    # janela de destino (T31)
    def _foreground(self):
        if self.window_guard is None:
            return None
        try:
            return self.window_guard.foreground()
        except Exception as e:
            self.logger.warning("não foi possível ver a janela em primeiro plano: %s", type(e).__name__)
            return None

    def _paste_block_reason(self) -> str | None:
        """Motivo para NÃO colar: a janela mudou desde o começo da gravação, ou é de administrador."""
        if self.window_guard is None:
            return None
        current = self._foreground()
        if not current:
            return None
        if self._target_window and current != self._target_window:
            return WINDOW_CHANGED
        try:
            if self.window_guard.is_elevated(current) and not self.window_guard.self_elevated():
                return ADMIN_WINDOW
        except Exception as e:
            self.logger.warning("não foi possível checar a janela de destino: %s", type(e).__name__)
        return None

    def join(self, timeout: float = 10.0) -> None:
        """Espera a transcrição em andamento (útil em testes)."""
        t = self._thread
        if t is not None:
            t.join(timeout)

    # --- depois de parar ---------------------------------------------------------------
    def _process(self, audio, spool=None) -> None:
        try:
            seconds = len(audio) / self.samplerate
            if seconds < self.min_seconds:
                self.logger.info("áudio curto (%.2fs); ignorado", seconds)
                self._cancel_streaming()
                discard_spool(spool, self.logger)
                return
            if self.streaming is not None:
                text = self.streaming.finish(audio)
            else:
                text = self.transcriber.transcribe(audio)
            # Guarda o texto bruto (pré-pós-texto) para o histórico
            texto_bruto = text
            # Texto fiel após pós-texto (vocabulário + comandos de voz)
            texto_fiel = self._apply_postprocess(text)
            if not texto_fiel or not texto_fiel.strip():
                self.logger.info("transcrição vazia; nada a colar")
                discard_spool(spool, self.logger)
                return
            self.logger.info("transcrito: %s audio=%.1fs", describe_text(texto_fiel), seconds)

            # T47b (P2): Modo B — etapa do gerador
            usou_gerador = False
            revisar = False
            generated_text = None
            texto_final = texto_fiel

            if self.prompt_step is not None:
                # Emite estado "generating" para a UI
                self._emit("generating")
                try:
                    resultado = self.prompt_step(texto_fiel)
                except Exception as e:
                    # Exceção no prompt_step: fallback para transcrição fiel
                    self.logger.warning("prompt_step falhou: %s; fallback para transcrição fiel", type(e).__name__)
                    resultado = None

                if resultado is not None and getattr(resultado, "usou_gerador", False):
                    # Gerador funcionou: usa o texto do gerador
                    texto_final = resultado.texto
                    usou_gerador = True
                    revisar = getattr(resultado, "revisar", False)
                    generated_text = resultado.texto
                else:
                    # Fallback: usa a transcrição fiel, emite generator_fallback
                    self._emit(GENERATOR_FALLBACK)
                    # texto_final já é texto_fiel
                    usou_gerador = False
                    revisar = False
                    generated_text = None
            # else: Modo A - valores padrão já definidos

            # raw no histórico: no Modo B com gerador, SEMPRE a transcrição fiel (nunca perder a fala —
            # o texto colado é o prompt); senão, o bruto do Whisper só quando o vocabulário mudou algo
            if usou_gerador:
                raw_para_historico = texto_fiel
            else:
                raw_para_historico = self._post_meta.get("raw") if self._post_meta else None
            self._deliver(texto_final, seconds, spool, usou_gerador, revisar, generated_text, raw_para_historico)
        except Exception as e:
            # nunca logar str(e) nem traceback: podem conter o texto ditado
            self.logger.error("erro no ciclo de ditado: %s (estado=%s)", type(e).__name__, self._state)
            self.feedback.error()
            self._emit(ERROR)
        finally:
            self._post_meta = None  # revisão 022: nada de um ditado sobra para o próximo
            self._set_state(IDLE)

    def _apply_postprocess(self, text: str) -> str:
        """Dicionário + comandos de voz. Se falhar, segue com o texto como veio (nunca perde o ditado).

        O pós-texto pode devolver `str` ou um objeto com `.texto` e `.mudancas` (vocabulário, T45b):
        nesse caso o texto bruto e os pares corrigidos vão junto para o histórico (`_save`)."""
        self._post_meta = None
        if not text or self.postprocess is None:
            return text
        try:
            result = self.postprocess(text)
            if isinstance(result, str):
                return result
            texto = getattr(result, "texto", None)
            if not isinstance(texto, str):
                return text
            changes = [[m.ouvido, m.correto] for m in getattr(result, "mudancas", ()) or ()]
            if texto != text:
                self._post_meta = {"raw": text, "changes": changes}
            if changes:
                self.logger.info("vocabulário: %d correção(ões)", len(changes))  # nunca o texto
            return texto
        except Exception as e:
            self.logger.warning("pós-texto falhou: %s; usando o texto sem ajuste", type(e).__name__)
            return text

    def _deliver(
        self,
        text: str,
        seconds: float,
        spool,
        usou_gerador: bool,
        revisar: bool,
        generated_text: str | None,
        texto_fiel: str,
    ) -> None:
        """Histórico (pendente) → teclas → colar → status → spool → aviso."""
        entry_id = self._save(
            text, seconds, "pendente",
            usou_gerador=usou_gerador, revisar=revisar,
            generated_text=generated_text, raw_text=texto_fiel
        )
        in_history = entry_id is not None
        if in_history:
            discard_spool(spool, self.logger)  # o texto já está a salvo no histórico

        if self.before_paste is not None and self.before_paste() is False:
            # teclas do atalho ainda seguradas: Ctrl+V sairia errado (ex.: Ctrl+Alt+V)
            in_clipboard = self._copy_to_clipboard(text)
            self.logger.warning("teclas do atalho ainda pressionadas; não colou (%s, na área de transferência: %s)",
                                describe_text(text), in_clipboard)
            self._finish_not_pasted(entry_id, in_clipboard)
            return

        reason = self._paste_block_reason()
        if reason is not None:
            # colar aqui seria arriscado (outra janela, ex.: um chat) ou inútil (janela de admin
            # não recebe Ctrl+V de um app comum): texto fica na área de transferência + histórico
            in_clipboard = self._copy_to_clipboard(text)
            self.logger.warning("não colou: %s (%s, na área de transferência: %s)",
                                "a janela em primeiro plano mudou" if reason == WINDOW_CHANGED
                                else "janela de administrador", describe_text(text), in_clipboard)
            self._emit(reason)
            self._finish_not_pasted(entry_id, in_clipboard)
            return

        try:
            result = self.paster.paste(text)
        except Exception as e:
            self.logger.warning("falha ao colar: %s", type(e).__name__)
            self._finish_not_pasted(entry_id, self._copy_to_clipboard(text))
            return

        pasted = True if result is None else bool(getattr(result, "pasted", True))
        if not pasted:
            # o PasteResult diz se o texto ficou DE FATO na área de transferência
            # (Ctrl+V falhou: sim; área de transferência travada no set: não) — revisão 014
            reason = getattr(result, "reason", "?")
            in_clipboard = bool(getattr(result, "in_clipboard", False))
            self.logger.warning("não colou (%s; na área de transferência: %s)", reason, in_clipboard)
            self._finish_not_pasted(entry_id, in_clipboard)
            return

        self._set_status(entry_id, "colado")
        if not in_history:
            discard_spool(spool, self.logger)  # sem histórico: só agora que colou
        self.feedback.done()

    def _finish_not_pasted(self, entry_id, in_clipboard: bool) -> None:
        self._set_status(entry_id, "salvo")
        self.feedback.error()
        if entry_id is not None or in_clipboard:
            self._emit(SAVED)  # "texto salvo — Ctrl+V"
        else:
            self._emit(ERROR)

    def _copy_to_clipboard(self, text: str) -> bool:
        try:
            self.paster.clipboard.set(text)
            return True
        except Exception as e:
            self.logger.warning("falha ao copiar para a área de transferência: %s", type(e).__name__)
            return False

    def _save(
        self,
        text: str,
        seconds: float,
        status: str,
        usou_gerador: bool = False,
        revisar: bool = False,
        generated_text: str | None = None,
        raw_text: str | None = None,
    ):
        """Grava no histórico; devolve o id (None se desligado, pausado ou com erro)."""
        meta = {"duration_s": round(float(seconds), 2), "language": self.language, "status": status}
        if self._post_meta:
            meta.update(self._post_meta)
            self._post_meta = None
        # T47b (P2): campos do Modo B
        if usou_gerador and generated_text is not None:
            meta["generated"] = generated_text
        if revisar:
            meta["revisar"] = True  # só quando a verificação de fidelidade apontou falta
        # raw_text só vem quando há alterações de vocabulário (compatibilidade com comportamento anterior)
        if raw_text is not None:
            meta["raw"] = raw_text
        try:
            return self.sink.save(text, meta)
        except Exception as e:
            self.logger.warning("histórico falhou: %s", type(e).__name__)
            return None

    def _set_status(self, entry_id, status: str) -> None:
        if entry_id is None or not hasattr(self.sink, "set_status"):
            return
        try:
            self.sink.set_status(entry_id, status)
        except Exception as e:
            self.logger.warning("falha ao marcar status no histórico: %s", type(e).__name__)

    # --- recuperação de gravações que sobraram -----------------------------------------
    def recover(self, orphans, load_orphan, discard, done=None) -> bool:
        """Transcreve gravações órfãs, salva no histórico ("salvo") e põe o texto na área de
        transferência. Só com o app ocioso. Assíncrono se `run_async`. False se ocupado."""
        with self._lock:
            if self._state != IDLE:
                return False
            self._state = TRANSCRIBING
        self._notify()

        def work():
            texts, failed = [], 0
            try:
                for orphan in orphans:
                    try:
                        audio = load_orphan(orphan)
                        text = self.transcriber.transcribe(audio).strip() if len(audio) else ""
                        text = self._apply_postprocess(text).strip()
                    except Exception as e:
                        failed += 1
                        self.logger.warning("falha ao recuperar gravação: %s", type(e).__name__)
                        continue
                    if not text:
                        discard(orphan)  # só silêncio: nada a recuperar
                        continue
                    entry_id = self._save(text, len(audio) / self.samplerate, "salvo")
                    self._post_meta = None  # revisão 022
                    texts.append((text, entry_id, orphan))
                in_clipboard = bool(texts) and self._copy_to_clipboard("\n\n".join(t for t, _, _ in texts))
                for _text, entry_id, orphan in texts:
                    if entry_id is not None or in_clipboard:
                        discard(orphan)  # o texto está guardado (histórico ou área de transferência)
                self.logger.info("recuperação: %d gravação(ões) com texto, %d falha(s)", len(texts), failed)
                if texts and (in_clipboard or any(e is not None for _, e, _ in texts)):
                    self.feedback.done()
                    self._emit(SAVED)
                elif failed:
                    self.feedback.error()
                    self._emit(ERROR)
            except Exception as e:
                self.logger.error("erro na recuperação: %s", type(e).__name__)
                self.feedback.error()
                self._emit(ERROR)
            finally:
                self._set_state(IDLE)
                if done is not None:
                    try:
                        done()
                    except Exception as e:
                        self.logger.warning("callback da recuperação falhou: %s", type(e).__name__)

        if self.run_async:
            self._thread = threading.Thread(target=work, name="hear-ai-recover", daemon=True)
            self._thread.start()
        else:
            work()
        return True


def postprocessor_from_config(cfg):
    """Pós-texto com o dicionário e os comandos de voz do config (para o __main__ ligar)."""
    from hear_ai.postprocess import postprocess

    dictionary = getattr(cfg, "dictionary", None)
    voice = bool(getattr(cfg, "voice_commands", True))

    def run(text: str) -> str:
        return postprocess(text, dictionary=dictionary, voice_commands=voice)

    return run


class PostResult:
    """Resultado do pós-texto com vocabulário: o texto final e o que o vocabulário corrigiu."""

    __slots__ = ("texto", "mudancas")

    def __init__(self, texto: str, mudancas=()):
        self.texto = texto
        self.mudancas = tuple(mudancas)


def vocab_postprocessor(cfg, store=None):
    """Pós-texto do fluxo real (v1.3, T45b): vocabulário contextual (confirmados do usuário >
    `cfg.dictionary` > regras embutidas) e depois os comandos de voz (se `cfg.voice_commands`).
    `store` (VocabStore) é lido a CADA ditado: uma correção confirmada vale já no próximo."""
    from hear_ai.postprocess import apply_voice_commands
    from hear_ai.vocab import corrigir

    dictionary = getattr(cfg, "dictionary", None)
    dictionary = dictionary if isinstance(dictionary, dict) else {}
    voice = bool(getattr(cfg, "voice_commands", True))

    def run(text: str) -> PostResult:
        if not text:
            return PostResult(text)
        r = corrigir(text, extras=dictionary, confirmados=store.entries() if store is not None else None)
        out = apply_voice_commands(r.texto) if voice else r.texto
        return PostResult(out, r.mudancas)

    return run
