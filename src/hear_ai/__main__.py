"""Ponto de entrada: python -m hear_ai

Variáveis de ambiente (para testes de fumaça sem tocar no app do usuário):
- HEAR_AI_INSTANCE_NAME: nome da instância (mutex). Padrão: a instância normal.
- HEAR_AI_NO_DIALOGS=1: nenhuma janela de mensagem (MessageBox); tudo vai para o log.
- HEAR_AI_CONFIG: caminho alternativo do config.json.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path

from hear_ai.config import PROJECT_ROOT, load_config, resolve_path

DEFAULT_INSTANCE = "HearAI_SingleInstance"
MUTEX_NAME = f"Local\\{DEFAULT_INSTANCE}"
SPOOL_DIR = PROJECT_ROOT / "spool"
ORPHAN_KEEP_DAYS = 7  # D15
_mutex_handle = None  # mantém o mutex vivo até o processo terminar


def instance_mutex_name(env=None) -> str:
    env = os.environ if env is None else env
    name = (env.get("HEAR_AI_INSTANCE_NAME") or "").strip()
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:100]
    return f"Local\\{name or DEFAULT_INSTANCE}"


def dialogs_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get("HEAR_AI_NO_DIALOGS", "").strip() not in ("1", "true", "yes")


def config_path_from_env(env=None) -> Path:
    env = os.environ if env is None else env
    custom = (env.get("HEAR_AI_CONFIG") or "").strip()
    return Path(custom) if custom else PROJECT_ROOT / "config.json"


def acquire_single_instance(name: str = MUTEX_NAME) -> bool:
    """True se esta é a única instância (mutex nomeado do Windows)."""
    global _mutex_handle
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = kernel32.CreateMutexW(None, False, name)
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def _message_box(text: str, error: bool = False) -> None:
    if not dialogs_enabled():
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "Hear AI", 0x10 if error else 0x40)
    except Exception:
        pass


def describe_load_error(exc: BaseException) -> str:
    """Texto curto para o usuário quando o modelo não carrega (sem conteúdo ditado)."""
    from hear_ai.transcriber import ModelNotFoundError

    if isinstance(exc, ModelNotFoundError):
        return f"{exc}\n\nDepois de baixar o modelo, abra o Hear AI de novo."
    return (
        f"Não foi possível carregar o modelo de voz ({type(exc).__name__}).\n\n"
        "Veja a pasta de logs (menu da bandeja > Abrir pasta de logs)."
    )


def main() -> int:
    if not acquire_single_instance(instance_mutex_name()):
        _message_box("O Hear AI já está em execução (veja o ícone na bandeja).")
        return 0

    from hear_ai import recovery
    from hear_ai.audio import Recorder
    from hear_ai.controller import Controller
    from hear_ai.feedback import Feedback
    from hear_ai.history import make_sink
    from hear_ai.hotkey import HotkeyBinder
    from hear_ai.logs import setup_logging
    from hear_ai.modos import ModeHotkey, is_prompt_mode, make_prompt_step
    from hear_ai.model_download import resolve_startup_model
    from hear_ai.paster import Paster, SystemClipboard, send_ctrl_v
    from hear_ai.streaming import StreamingTranscriber
    from hear_ai.transcriber import Transcriber
    from hear_ai.tray import TrayApp
    from hear_ai.ui.qt_app import QtUi

    config_path = config_path_from_env()
    cfg = load_config(config_path)
    logger = setup_logging(resolve_path(cfg.log_dir))
    logger.info("iniciando Hear AI (modo=%s modelo=%s instância=%s)", cfg.mode, cfg.model, instance_mutex_name())

    feedback = Feedback(cfg.beep)
    # T46: o modelo do config sem arquivo em disco (ex.: `medium` apagado) não derruba o app: cai para o `small`
    # se ele existir. O config.json NÃO é reescrito; o aviso vai para o log, a bolha, a bandeja e as Configurações.
    model_name, fell_back = resolve_startup_model(resolve_path(cfg.models_dir), cfg.model)
    if fell_back:
        logger.warning("modelo '%s' não está no disco; usando '%s' (o config.json não foi alterado)",
                       cfg.model, model_name)
    # v1.3 (T45b): vocabulário ligado de verdade — correções confirmadas (vocabulario.json ao lado do
    # config), o dicionário do config e as regras embutidas; os termos vão como "dica" ao Whisper
    from hear_ai.controller import vocab_postprocessor
    from hear_ai.vocab import VocabStore, prompt_terms

    vocab_store = VocabStore(config_path.with_name("vocabulario.json"))
    initial_prompt = prompt_terms(vocab_store, cfg.dictionary) if cfg.use_initial_prompt else None
    transcriber = Transcriber(model_name, cfg.models_dir, cfg.device, cfg.compute_type, cfg.language,
                              initial_prompt=initial_prompt)
    recorder = Recorder(max_seconds=cfg.max_record_minutes * 60, spool_dir=SPOOL_DIR, warn_before_s=60)
    paster = Paster(
        SystemClipboard(),
        send_ctrl_v,
        base_s=cfg.paste_base_ms / 1000,
        chars_per_s=cfg.paste_chars_per_s,
        max_s=cfg.paste_max_ms / 1000,
        restore=cfg.restore_clipboard,
    )
    sink = make_sink(cfg, PROJECT_ROOT)
    streaming = StreamingTranscriber(recorder, transcriber, logger)
    # T47b P4: o gerador de prompts (Modo B) só é montado com operation_mode == "prompt"; o Modo A (padrão) não
    # tem passo nenhum e nada de rede. Trocar de modo depois (bandeja/atalho/Configurações) liga/desliga em tempo
    # real via `modos.apply_operation_mode` (o QtUi cuida).
    prompt_step = (make_prompt_step(cfg, config_dir=config_path.parent, vocab_store=vocab_store)
                   if is_prompt_mode(cfg) else None)
    controller = Controller(recorder, transcriber, paster, feedback, sink, logger, cfg.language,
                            streaming=streaming, postprocess=vocab_postprocessor(cfg, vocab_store),
                            prompt_step=prompt_step)
    recorder.on_limit_warning = controller.on_limit_warning
    recorder.on_limit_reached = controller.on_limit_reached

    ui_holder = {}

    def on_conflict(reason: str) -> None:
        feedback.error()
        if "ui" in ui_holder:
            ui_holder["ui"].set_conflict(reason)

    binder = HotkeyBinder(cfg.hotkey, cfg.mode, controller, on_conflict=on_conflict)
    controller.before_paste = binder.wait_released
    # T47b P4: atalho que alterna o modo (A ⇄ B). Conflito (igual ao do ditado, ou já em uso) → não registra, avisa
    # e o resto funciona.
    mode_binder = ModeHotkey(
        cfg.mode_hotkey, on_toggle=lambda: ui_holder["ui"].toggle_operation_mode(), main_hotkey=cfg.hotkey,
        on_conflict=lambda reason: ui_holder["ui"].show_notice(f"Atalho do modo não registrado: {reason}", "warn"))
    ui_holder["mode_binder"] = mode_binder

    # --- gravações que sobraram (app fechado/travado no meio de um ditado) -------------
    def count_orphans() -> int:
        try:
            return len(recovery.find_orphans(SPOOL_DIR, exclude=recorder.current_spool_path))
        except Exception as e:
            logger.warning("falha ao procurar gravações para recuperar: %s", type(e).__name__)
            return 0

    def on_recover() -> None:
        orphans = recovery.find_orphans(SPOOL_DIR, exclude=recorder.current_spool_path)
        if not orphans:
            ui.set_orphans(0)
            return
        started = controller.recover(orphans, recovery.load_orphan, recovery.discard,
                                     done=lambda: ui.set_orphans(count_orphans()))
        if not started:
            feedback.error()  # ocupado (gravando/transcrevendo): tente de novo depois

    history_dir = Path(PROJECT_ROOT) / cfg.history_dir

    def open_history() -> None:
        try:
            history_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(history_dir))  # Fase C troca por uma janela "Recentes"
        except OSError as e:
            logger.warning("não foi possível abrir o histórico: %s", type(e).__name__)
            feedback.error()

    def on_quit() -> None:
        binder.uninstall()  # nenhuma ditada nova
        mode_binder.uninstall()
        controller.join(timeout=5)  # deixa terminar a transcrição/colagem em andamento
        logger.info("encerrando Hear AI")

    tray = TrayApp(
        controller, cfg, on_quit, config_path=config_path, binder=binder,
        feedback=feedback, logger=logger, on_recover=on_recover, on_open_history=open_history,
    )
    # interface Qt na thread principal; o resto conversa com ela só por métodos thread-safe
    ui = QtUi(controller, tray, logger=logger, level_source=recorder.level,
              bubble_state_path=config_path.with_name("bolha.json"), open_history=open_history,
              vocab_store=vocab_store)
    ui_holder["ui"] = ui
    ui.mode_hotkey = mode_binder  # as Configurações religam o atalho do modo quando ele muda
    if fell_back:
        ui.show_notice(f"O modelo ‘{cfg.model}’ não está no disco; estou usando o ‘{model_name}’. "
                       "Baixe-o em Configurações para voltar a usá-lo.", "warn", remember=True)
    controller.on_state_change = ui.on_state
    controller.feedback = UiFeedback(feedback, on_done=ui.on_pasted)  # check "colado" na bolha

    try:
        removed = recovery.discard_older_than(SPOOL_DIR, ORPHAN_KEEP_DAYS)
        if removed:
            logger.info("%d gravação(ões) antigas apagadas (mais de %d dias)", removed, ORPHAN_KEEP_DAYS)
    except Exception as e:
        logger.warning("falha ao limpar gravações antigas: %s", type(e).__name__)
    orphans_at_start = count_orphans()
    ui.set_orphans(orphans_at_start)

    def preload() -> None:
        try:
            transcriber.load()
        except Exception as e:
            logger.error("falha ao carregar o modelo: %s", type(e).__name__)
            if isinstance(e, FileNotFoundError):
                logger.error("modelo ausente: %s", e)  # mensagem de caminho, sem texto ditado
            ui.set_error("erro — veja o log")
            feedback.error()
            _message_box(describe_load_error(e), error=True)
            return
        if not binder.install():
            if not binder.conflict:  # conflito já avisado por on_conflict
                feedback.error()
                ui.set_error(f"atalho não instalado: {binder.conflict_reason}")
            logger.warning("modelo carregado (%s), mas sem atalho", transcriber.device_used)
            return
        if not mode_binder.install() and not mode_binder.conflict:  # conflito já avisado por on_conflict
            logger.warning("atalho do modo não instalado: %s", mode_binder.conflict_reason)
        ui.set_loading(False)
        logger.info("modelo carregado (%s); atalho %s ativo", transcriber.device_used, binder.hotkey)
        if orphans_at_start:
            logger.info("%d gravação(ões) para recuperar (menu da bandeja)", orphans_at_start)
            feedback.error()  # chama a atenção para o item "Recuperar gravação"
        else:
            feedback.done()

    threading.Thread(target=preload, daemon=True).start()
    return ui.run() or 0


class UiFeedback:
    """Repassa os sons e avisa a bolha quando o ditado foi colado (`done`)."""

    def __init__(self, feedback, on_done):
        self._feedback = feedback
        self._on_done = on_done

    def start(self) -> None:
        self._feedback.start()

    def stop(self) -> None:
        self._feedback.stop()

    def error(self) -> None:
        self._feedback.error()

    def done(self) -> None:
        self._feedback.done()
        try:
            self._on_done()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
