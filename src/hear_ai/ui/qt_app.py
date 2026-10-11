"""App Qt: bandeja (QSystemTrayIcon) + bolha, na thread principal.

O Controller, o transcritor e o paster continuam sem Qt e rodam em outras threads. Tudo que
mexe na interface passa por `MainThreadBridge.post(fn)`, que entrega `fn` na thread do Qt
(sinal com conexão enfileirada: seguro entre threads).

Esc durante a gravação (decisão T21): a bolha não recebe teclado (não pega foco), então o Esc
é registrado no Windows com RegisterHotKey SÓ enquanto grava e desregistrado ao parar. Nesse
intervalo o Esc não chega a outros programas; fora da gravação o Esc é 100% normal. A mensagem
WM_HOTKEY chega pela fila da thread do Qt e é lida por um filtro de eventos nativos.

Vigia da bolha (T40, docs/BOLHA-SOME.md): um QTimer de ~2 s confere se a bolha está visível, opaca,
sobre alguma tela e topmost, e corrige sem nunca ativar a janela; eventos do Windows (troca de tela,
bloqueio/desbloqueio, retomada de energia, Explorer reiniciado) e o início da gravação reafirmam
tudo na hora. Cada estado/correção vai para logs/bolha-diag.log (só estados, nunca texto ditado).
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from dataclasses import replace
from pathlib import Path

from hear_ai.ui.bubble_watch import (ACTION_OPACITY, ACTION_REPOSITION, ACTION_SHOW, ACTION_TOPMOST,
                                     DIAG_PERIOD_S, BubbleProbe, DiagLog, Debouncer, classify_native_message,
                                     decide, make_win32_apis, reachable, screen_at)

log = logging.getLogger("hear_ai.ui")

WATCH_INTERVAL_MS = 2000  # vigia: barato (sem repaint, sem acordar a animação)
REASSERT_DELAY_MS = 800  # depois de um evento do Windows, espera a tela estabilizar
# campos do modo de operação/gerador que religam o passo do Controller quando mudam nas Configurações
MODE_FIELDS = {"operation_mode", "generator_provider", "ollama_url", "ollama_model", "cloud_base_url", "cloud_model",
               "cloud_key_env", "cloud_consent", "generator_app_context", "generator_permanent_prefs",
               "generator_timeout_s"}
MODEL_RETRY_MS = 800  # troca de modelo adiada (ditado em andamento): olha de novo a cada tanto

# avisos curtos na bolha para eventos de robustez do controller (T31)
ROBUSTNESS_MESSAGES = {
    "mic_lost": "Microfone desconectado — reconectando…",
    "mic_reconnected": "Microfone reconectado",
    "mic_failed": "Microfone não voltou — transcrevendo o que já foi gravado",
}

ESC_HOTKEY_ID = 0x4845  # "HE"
VK_ESCAPE = 0x1B
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312


class Win32EscApi:
    """RegisterHotKey/UnregisterHotKey do Esc na thread atual (a do Qt)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._u = u

    def register(self) -> tuple[bool, int]:
        ok = bool(self._u.RegisterHotKey(None, ESC_HOTKEY_ID, MOD_NOREPEAT, VK_ESCAPE))
        return ok, (0 if ok else self._ct.get_last_error())

    def unregister(self) -> None:
        self._u.UnregisterHotKey(None, ESC_HOTKEY_ID)


class EscGuard:
    """Liga o Esc global só durante a gravação. `api` injetável (testes)."""

    def __init__(self, on_esc, api=None):
        self.on_esc = on_esc
        self.api = api
        self.active = False
        self._warned = False

    def set_recording(self, recording: bool) -> None:
        if recording and not self.active:
            if self.api is None:
                return
            try:
                ok, err = self.api.register()
            except Exception as e:
                ok, err = False, type(e).__name__
            if ok:
                self.active = True
            elif not self._warned:
                self._warned = True
                log.warning("Esc para cancelar indisponível (Esc já registrado por outro programa: %s)", err)
        elif not recording and self.active:
            try:
                self.api.unregister()
            except Exception as e:
                log.warning("falha ao liberar o Esc: %s", type(e).__name__)
            self.active = False

    def handle_message(self, message: int, wparam: int) -> bool:
        """True se a mensagem era o nosso Esc (e já foi tratada)."""
        if message == WM_HOTKEY and wparam == ESC_HOTKEY_ID and self.active:
            try:
                self.on_esc()
            except Exception as e:
                log.warning("cancelar pelo Esc falhou: %s", type(e).__name__)
            return True
        return False


def _make_qt_classes():
    from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt, Signal

    class MainThreadBridge(QObject):
        call = Signal(object)

        def __init__(self):
            super().__init__()
            self.call.connect(self._run, Qt.QueuedConnection)

        def _run(self, fn) -> None:
            try:
                fn()
            except Exception as e:
                log.warning("atualização da interface falhou: %s", type(e).__name__)

        def post(self, fn) -> None:
            """Executa `fn` na thread do Qt (pode ser chamado de qualquer thread)."""
            self.call.emit(fn)

    class EscEventFilter(QAbstractNativeEventFilter):
        def __init__(self, guard: EscGuard):
            super().__init__()
            self.guard = guard

        def nativeEventFilter(self, event_type, message):
            if bytes(event_type) != b"windows_generic_MSG":
                return False, 0
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if self.guard.handle_message(msg.message, msg.wParam):
                return True, 0
            return False, 0

    return MainThreadBridge, EscEventFilter


def _make_bubble_event_filter():
    """Filtro de eventos nativos do vigia: só OBSERVA (nunca consome a mensagem)."""
    from ctypes import wintypes

    from PySide6.QtCore import QAbstractNativeEventFilter

    class BubbleEventFilter(QAbstractNativeEventFilter):
        def __init__(self, on_event, taskbar_created: int = 0):
            super().__init__()
            self.on_event = on_event
            self.taskbar_created = taskbar_created

        def nativeEventFilter(self, event_type, message):
            try:
                if bytes(event_type) == b"windows_generic_MSG":
                    msg = wintypes.MSG.from_address(int(message))
                    name = classify_native_message(msg.message, msg.wParam, self.taskbar_created)
                    if name is not None:
                        self.on_event(name)
            except Exception as e:  # noqa: BLE001 - o vigia nunca atrapalha a fila de mensagens
                log.warning("filtro de eventos da bolha falhou: %s", type(e).__name__)
            return False, 0

    return BubbleEventFilter


def screens_info():
    """[(nome, (x, y, w, h) da área útil)], tela principal, em coordenadas lógicas (DPI)."""
    from PySide6.QtGui import QGuiApplication

    def geo(s):
        g = s.availableGeometry()
        return (g.x(), g.y(), g.width(), g.height())

    screens = [(s.name(), geo(s)) for s in QGuiApplication.screens()]
    primary = QGuiApplication.primaryScreen()
    return screens, (geo(primary) if primary else (0, 0, 1920, 1040))


def screens_full():
    """[(nome, (x, y, w, h) da tela INTEIRA, com a barra de tarefas)] — o vigia só quer saber se a
    bolha ainda está sobre alguma tela, não se cabe na área útil."""
    from PySide6.QtGui import QGuiApplication

    out = []
    for s in QGuiApplication.screens():
        g = s.geometry()
        out.append((s.name(), (g.x(), g.y(), g.width(), g.height())))
    return out


class SerialWorker:
    """Uma thread que roda, UMA DE CADA VEZ e na ordem, as ações da bolha sobre o Controller (T42b).

    Por quê: `Controller.begin()` marca "recording" ANTES de abrir o microfone (`recorder.start()` demora) e só
    avisa a interface no fim. Um duplo clique cancela a gravação ~100-300 ms depois do 1º clique; se o
    `cancel()` rodasse em outra thread enquanto o `begin()` ainda abre o microfone, o `begin()` terminaria
    por cima e a gravação "cancelada" continuaria. Em fila, o cancel só roda depois que o begin terminou.
    """

    def __init__(self, name: str = "hear-ai-bubble"):
        self._q: queue.Queue = queue.Queue()
        self._name = name
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, fn) -> None:
        """Enfileira `fn` (a thread, daemon, nasce no 1º uso). Seguro de chamar da thread do Qt."""
        self._q.put(fn)
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            fn = self._q.get()
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - uma ação com erro não pode matar a fila
                log.warning("ação da bolha falhou: %s", type(e).__name__)
            finally:
                self._q.task_done()

    def join(self, timeout: float = 5.0) -> bool:
        """Espera a fila esvaziar e a última ação terminar (testes). True se deu tempo."""
        import time

        fim = time.monotonic() + timeout
        while time.monotonic() < fim:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.003)
        return False


class QtUi:
    """Bandeja + bolha. Métodos `on_*`/`set_*` são seguros de chamar de qualquer thread."""

    def __init__(self, controller, tray, logger=None, level_source=None, bubble_state_path=None,
                 esc_api=None, open_history=None, show_bubble=True, log_dir=None,
                 topmost_api=None, session_api=None, watch_interval_ms=WATCH_INTERVAL_MS,
                 reassert_delay_ms=REASSERT_DELAY_MS, vocab_store=None):
        """`show_bubble=False` (testes): a bolha não é mostrada nem vigiada e nada é gravado em log.
        `log_dir`: pasta de `bolha-diag.log` (padrão: a pasta de logs da bandeja, só com a bolha ligada).
        `topmost_api`/`session_api`: adaptadores Win32 (injetáveis; o padrão é o real, só na plataforma
        "windows" do Qt — no `offscreen` o `winId()` não é um HWND e ninguém mexe nele)."""
        from PySide6.QtWidgets import QApplication, QMenu

        from hear_ai.tray import fill_qmenu
        from hear_ai.ui.bubble import BubbleWidget

        self.app = QApplication.instance() or QApplication(sys.argv[:1])
        self.app.setQuitOnLastWindowClosed(False)  # a bolha não é "a janela principal"
        Bridge, EscEventFilter = _make_qt_classes()
        self.bridge = Bridge()
        self.controller = controller
        self.tray = tray
        self.logger = logger or log
        self.bubble_state_path = bubble_state_path
        self.vocab_store = vocab_store  # T45b: correções confirmadas (VocabStore) ou None
        self._fill_qmenu = fill_qmenu
        self._menu = QMenu()
        self._bubble_worker = SerialWorker()  # T42b: ações da bolha sobre o controller, uma por vez, em ordem
        self.recent_window = None  # janela "Recentes" (T22), criada no 1º uso
        self.settings_window = None  # janela "Configurações" (T23), criada no 1º uso

        # T22: clique na bolha e "Abrir histórico" (bolha e bandeja) abrem a janela Recentes;
        # `open_history` (pasta do histórico) fica só como plano B se a janela falhar
        self._open_history_folder = open_history
        self.open_history = self.show_recent
        tray.on_open_history = self.show_recent
        tray.on_open_settings = self.show_settings  # T23: "Abrir configurações" (bolha e bandeja)
        tray.on_show_bubble = self.recenter_bubble  # T40: "Mostrar/recentralizar bolha"
        tray.attach_qt(app_quit=self.app.quit)

        # T31: o controller confere a janela de destino antes de colar (mudou? é de admin?)
        if sys.platform == "win32" and getattr(controller, "window_guard", "x") is None:
            try:
                from hear_ai.paster import Win32Windows

                controller.window_guard = Win32Windows()
            except Exception as e:
                self.logger.warning("checagem da janela de destino indisponível: %s", type(e).__name__)

        self.bubble = BubbleWidget(level_source=level_source, state_source=self._controller_state)
        self.bubble.action.connect(self._on_bubble_action)  # T42b: ClickMachine (clique, duplo clique)
        self.bubble.clicked.connect(self._on_bubble_click)  # legado: o mouse não emite mais; abre Recentes
        self.bubble.menu_requested.connect(self._on_bubble_menu)
        self.bubble.moved.connect(self._on_bubble_moved)
        self.place_bubble()
        # T31: monitor sai/entra ou muda de DPI/resolução -> a bolha volta para uma tela visível
        self.app.screenRemoved.connect(lambda _s: self.ensure_bubble_visible())
        self.app.screenAdded.connect(self._watch_screen)
        self.app.screenAdded.connect(lambda _s: self.ensure_bubble_visible())
        for screen in self.app.screens():
            self._watch_screen(screen)
        if show_bubble:
            self.bubble.show()
        self.bubble.driver.loading(True)

        if esc_api is None and sys.platform == "win32":
            try:
                esc_api = Win32EscApi()
            except Exception as e:
                self.logger.warning("Esc para cancelar indisponível: %s", type(e).__name__)
        self.esc = EscGuard(self._on_esc, esc_api)
        self._esc_filter = EscEventFilter(self.esc)
        self.app.installNativeEventFilter(self._esc_filter)

        # T46: troca de modelo de voz sem reiniciar
        self._model_switching = False  # há um carregamento em segundo plano?
        self._wanted_model: str | None = None  # modelo pedido que ainda não foi aplicado (ditado em andamento)
        self._model_retry_pending = False
        self._model_thread: threading.Thread | None = None
        self._startup_notice: str | None = None  # aviso de inicialização (ex.: caiu para o small)
        # T47b P4: modo de operação (A fiel / B gerador de prompts)
        self.mode_hotkey = None  # ModeHotkey (o __main__ liga); None = sem atalho do modo
        self.provider_status_fn = None  # estado do gerador (None = modos.provider_status; os testes injetam um falso)
        self._mode_thread: threading.Thread | None = None
        tray.on_set_operation = self.set_operation_mode
        self._apply_operation_visuals()

        self._init_watch(show_bubble, log_dir, topmost_api, session_api, watch_interval_ms, reassert_delay_ms)

    # --- vigia da bolha (T40) --------------------------------------------------------
    def _init_watch(self, show_bubble, log_dir, topmost_api, session_api, watch_interval_ms,
                    reassert_delay_ms) -> None:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QGuiApplication

        platform_name = QGuiApplication.platformName()
        self._bubble_wanted = bool(show_bubble)
        if topmost_api is None and session_api is None:
            topmost_api, session_api = make_win32_apis(platform_name)
        self.topmost = topmost_api
        self.session = session_api
        self._session_hwnd = 0
        diag_dir = log_dir if log_dir is not None else (getattr(self.tray, "log_dir", None) if show_bubble else None)
        self.diag = DiagLog(diag_dir)
        self.debounce = Debouncer(lambda ms, fn: QTimer.singleShot(ms, fn), reassert_delay_ms,
                                  self._reassert_after_event)
        self.watch_timer = QTimer()
        self.watch_timer.setInterval(max(1, int(watch_interval_ms)))
        self.watch_timer.timeout.connect(self.watch_tick)
        self.sample_timer = QTimer()
        self.sample_timer.setInterval(int(DIAG_PERIOD_S * 1000))
        self.sample_timer.timeout.connect(self.diag_sample)
        taskbar_msg = 0
        if self.session is not None:
            try:
                taskbar_msg = self.session.taskbar_created_message()
            except Exception as e:  # noqa: BLE001
                self.logger.warning("mensagem TaskbarCreated indisponível: %s", type(e).__name__)
        self._bubble_filter = None
        if self.topmost is not None or self.session is not None:
            self._bubble_filter = _make_bubble_event_filter()(self.on_native_event, taskbar_msg)
            self.app.installNativeEventFilter(self._bubble_filter)
        self.app.aboutToQuit.connect(self.shutdown_watch)
        self.diag.event("start", platform=platform_name, win32=self.topmost is not None,
                        wanted=self._bubble_wanted, screens=len(screens_full()))
        if self._bubble_wanted:
            self._register_session()
            self.diag.sample(self.build_probe())
            if watch_interval_ms > 0:
                self.watch_timer.start()
                self.sample_timer.start()

    def _hwnd(self) -> int:
        """HWND da bolha (0 se não há API Win32 ou a janela ainda não existe)."""
        if self.topmost is None and self.session is None:
            return 0
        try:
            return int(self.bubble.winId())
        except Exception:  # noqa: BLE001
            return 0

    def _hide_when_idle(self) -> bool:
        return bool(getattr(self.bubble.model, "_hide_when_idle", False))

    def _dragging(self) -> bool:
        return bool(getattr(self.bubble, "_dragging", False))

    def build_probe(self) -> BubbleProbe:
        """Retrato da bolha agora (leituras baratas: nada de repaint)."""
        b = self.bubble
        screens = screens_full()
        pos = (b.x(), b.y())
        topmost = native_visible = z_above = hwnd_valid = None
        hwnd = self._hwnd() if self.topmost is not None else 0
        if hwnd:
            try:
                topmost = self.topmost.is_topmost(hwnd)
                native_visible = self.topmost.is_visible(hwnd)
                z_above = self.topmost.z_above(hwnd)
                # T35: com Win32 ativo, "não dá para saber" = IsWindow falso (janela nativa destruída)
                hwnd_valid = native_visible is not None
            except Exception as e:  # noqa: BLE001
                self.logger.warning("leitura Win32 da bolha falhou: %s", type(e).__name__)
        return BubbleProbe(
            visible=b.isVisible(), opacity=b.windowOpacity(), x=pos[0], y=pos[1],
            # sem nenhuma tela (RDP/monitores todos desligados) não há onde reposicionar: não "corrige"
            inside_screen=reachable(pos, screens) if screens else True,
            screen_name=screen_at((pos[0] + b.width() // 2, pos[1] + b.height() // 2), screens),
            topmost=topmost, visual=b.visual().value, hide_when_idle=self._hide_when_idle(),
            native_visible=native_visible, z_above=z_above, frame_opacity=b.frame().opacity,
            hwnd_valid=hwnd_valid)

    def watch_tick(self) -> list[str]:
        """Um ciclo do vigia: lê, decide e corrige SÓ o que estiver errado. Devolve as correções."""
        try:
            self._ensure_session()
            probe = self.build_probe()
            actions = decide(probe, self._bubble_wanted)
            if actions:
                self._apply(actions, "vigia", probe)
            return actions
        except Exception as e:  # noqa: BLE001 - o vigia nunca derruba o app
            self.logger.warning("vigia da bolha falhou: %s", type(e).__name__)
            return []

    def reassert_bubble(self, reason: str) -> list[str]:
        """Reafirma a bolha AGORA (início da gravação, evento do Windows, item da bandeja):
        o que estiver errado é corrigido e o topmost é sempre reafirmado (sem ativar a janela)."""
        if not self._bubble_wanted:
            return []
        try:
            probe = self.build_probe()
            # reafirmar vale também para "esconder em repouso": quem chama quer a bolha à vista
            actions = decide(replace(probe, hide_when_idle=False), True)
            if ACTION_TOPMOST not in actions:
                actions.append(ACTION_TOPMOST)
            self._apply(actions, reason, probe)
            return actions
        except Exception as e:  # noqa: BLE001
            self.logger.warning("reafirmar a bolha falhou: %s", type(e).__name__)
            return []

    def _apply(self, actions: list[str], reason: str, probe: BubbleProbe) -> None:
        b = self.bubble
        done = []
        if ACTION_SHOW in actions:
            b.show()  # WA_ShowWithoutActivating: não ativa
            done.append(ACTION_SHOW)
        if ACTION_OPACITY in actions:
            b.setWindowOpacity(1.0)
            done.append(ACTION_OPACITY)
        if ACTION_REPOSITION in actions and not self._dragging():
            self.ensure_bubble_visible()
            done.append(ACTION_REPOSITION)
        native = topmost_after = None
        if ACTION_TOPMOST in actions and self.topmost is not None:
            hwnd = self._hwnd()
            native = bool(hwnd and self.topmost.reassert(hwnd))
            # T35: o SetWindowPos pode devolver sucesso e o Windows NÃO aplicar o topmost (medido em
            # 2026-10-10, D41): registra o estado REAL depois; o próximo ciclo do vigia tenta de novo
            if hwnd:
                try:
                    topmost_after = self.topmost.is_topmost(hwnd)
                except Exception:  # noqa: BLE001
                    topmost_after = None
            done.append(ACTION_TOPMOST)
        if set(done) & {ACTION_SHOW, ACTION_OPACITY, ACTION_REPOSITION}:
            b.update()  # um pedido de redesenho (a janela pode ter voltado vazia); o vigia em si não repinta
        self.diag.event("fix", reason=reason, actions="+".join(done) or "-", native_ok=native,
                        topmost_after=topmost_after,
                        was_visible=probe.visible, was_native_visible=probe.native_visible,
                        was_opacity=probe.opacity, was_inside=probe.inside_screen,
                        was_topmost=probe.topmost, state=probe.visual)

    def on_native_event(self, name: str) -> None:
        """Evento do Windows que pode ter derrubado a bolha: registra e reafirma em ~800 ms."""
        # o Windows (e o próprio Qt) entregam a mesma mensagem mais de uma vez: loga e agenda uma só
        if name not in self.debounce.reasons:
            self.diag.event(name)
        self.debounce.request(name)

    def _reassert_after_event(self, reasons: str) -> None:
        self.reassert_bubble(reasons)

    def _ensure_session(self) -> None:
        """T35 (revisão 021): se o Qt recriou a janela (HWND novo), troca o registro WTS para ela."""
        if self.session is None or not self._bubble_wanted:
            return
        hwnd = self._hwnd()
        if not hwnd or hwnd == self._session_hwnd:
            return
        if self._session_hwnd:
            try:
                self.session.unregister(self._session_hwnd)
            except Exception:  # noqa: BLE001
                pass
            self.diag.event("wts_rehwnd")
        self._register_session()

    def _register_session(self) -> None:
        if self.session is None:
            return
        hwnd = self._hwnd()
        try:
            ok = bool(hwnd and self.session.register(hwnd))
        except Exception as e:  # noqa: BLE001
            ok = False
            self.logger.warning("registro de sessão do Windows falhou: %s", type(e).__name__)
        self._session_hwnd = hwnd if ok else 0
        self.diag.event("wts_register", ok=ok)

    def diag_sample(self) -> None:
        if not self.diag.enabled:
            return
        try:
            self.diag.sample(self.build_probe())
        except Exception as e:  # noqa: BLE001
            self.logger.warning("amostra de diagnóstico da bolha falhou: %s", type(e).__name__)

    def recenter_bubble(self) -> None:
        """Item "Mostrar/recentralizar bolha": volta para a posição salva se ela ainda é válida (e sem
        apagar bolha.json); senão para a padrão da tela principal. Depois reafirma tudo."""
        self.diag.event("menu_recenter")
        self.place_bubble()
        self.reassert_bubble("menu")

    def shutdown_watch(self) -> None:
        """Ao sair: para os timers, cancela o registro de sessão e fecha o log."""
        self.watch_timer.stop()
        self.sample_timer.stop()
        if self.session is not None and self._session_hwnd:
            try:
                self.session.unregister(self._session_hwnd)
            except Exception:  # noqa: BLE001
                pass
            self._session_hwnd = 0
        self.diag.event("shutdown")
        self.diag.close()

    # --- posição da bolha ----------------------------------------------------------
    def _watch_screen(self, screen) -> None:
        for signal in (screen.logicalDotsPerInchChanged, screen.geometryChanged,
                       screen.availableGeometryChanged):
            signal.connect(lambda *_a: self.ensure_bubble_visible())

    def ensure_bubble_visible(self) -> bool:
        """Se a bolha ficou fora de todas as telas, volta para a posição salva (ou a padrão).
        Devolve True se precisou mover."""
        from hear_ai.ui.bubble import visible_on_any

        screens, _primary = screens_info()
        if screens and visible_on_any((self.bubble.x(), self.bubble.y()), screens):
            return False
        self.place_bubble()
        self.logger.info("bolha reposicionada (tela/DPI mudou)")
        return True

    def place_bubble(self) -> None:
        from hear_ai.ui.bubble import load_bubble_state, restore_position

        screens, primary = screens_info()
        saved = load_bubble_state(self.bubble_state_path) if self.bubble_state_path else None
        x, y, _name = restore_position(saved, screens, primary)
        self.bubble.move(x, y)

    def _on_bubble_moved(self, x: int, y: int) -> None:
        from PySide6.QtGui import QGuiApplication

        from hear_ai.ui.bubble import save_bubble_state

        screen = QGuiApplication.screenAt(self.bubble.geometry().center())
        name = screen.name() if screen is not None else None
        if self.bubble_state_path:
            save_bubble_state(self.bubble_state_path, x, y, name)

    # --- interações da bolha -------------------------------------------------------
    def _on_bubble_click(self) -> None:
        self.show_recent()

    def _controller_state(self) -> str:
        """Estado atual do controller (o `app_state` que a ClickMachine usa em cada clique)."""
        state = getattr(self.controller, "state", "idle")
        return state if isinstance(state, str) else "idle"

    def _run_controller_action(self, fn) -> None:
        """Roda `fn` (begin/end/cancel) FORA da thread do Qt, em fila (ver SerialWorker). Os testes trocam
        este método por uma chamada direta."""
        self._bubble_worker.submit(fn)

    def _on_bubble_action(self, name: str) -> None:
        """Ação decidida pela ClickMachine (T42b, docs/CLIQUES.md). Roda na thread do Qt e volta logo."""
        self.diag.event("click", action=name, state=self._controller_state())  # só estados, nunca texto
        if name == "start":
            self._bubble_start()
        elif name == "stop":
            self._bubble_stop()
        elif name == "discard_and_open":
            self._bubble_discard_and_open()
        elif name == "open_recent":
            self.show_recent()  # a transcrição em andamento NÃO é tocada

    def _bubble_start(self) -> None:
        """1º clique com o app parado: grava NA HORA, pelo mesmo caminho do atalho (Controller.begin)."""
        self.reassert_bubble("click")  # T40: a bolha precisa estar à vista (e topmost) ao começar
        self._run_controller_action(self.controller.begin)

    def _bubble_stop(self) -> None:
        """Clique durante a gravação: para e segue para a transcrição, como o atalho (Controller.end)."""
        self._run_controller_action(self.controller.end)

    def _bubble_discard_and_open(self) -> None:
        """2º clique logo após o 1º (que começou a gravar): descarta a gravação e abre Recentes.
        Mesmo caminho do Esc (`_on_esc`): bolha volta a repouso, Esc global liberado e `Controller.cancel()`
        (para, apaga o spool, não salva nem cola). Fora da thread do Qt e DEPOIS do begin() que pode ainda
        estar abrindo o microfone."""
        self.bubble.driver.cancelled()
        self.esc.set_recording(False)

        def cancelar() -> None:
            try:
                self.controller.cancel()
            finally:  # se o begin() terminou depois do 1º cancelled(), a bolha chegou a mostrar "gravando"
                self.bridge.post(self._after_discard)

        self._run_controller_action(cancelar)
        self.show_recent()

    def _after_discard(self) -> None:
        if self._controller_state() == "recording":
            return  # já há outra gravação (clique novo depois do descarte): não mexe nela
        self.bubble.driver.cancelled()
        self.esc.set_recording(False)

    # --- janela Recentes (T22) -----------------------------------------------------
    def _history_store(self):
        """O HistoryStore do Controller (None se o histórico estiver desligado)."""
        sink = getattr(self.controller, "sink", None)
        return sink if hasattr(sink, "entries") else None

    def _toggle_history_pause(self) -> bool:
        """Pausa/retoma o histórico agora e grava `history_paused` no config."""
        from hear_ai.config import save_config

        store = self._history_store()
        if store is None:
            return False
        store.paused = not store.paused
        cfg = getattr(self.tray, "cfg", None)
        path = getattr(self.tray, "config_path", None)
        if cfg is not None and hasattr(cfg, "history_paused"):
            cfg.history_paused = store.paused
            if path is not None:
                try:
                    save_config(cfg, path)
                except OSError as e:
                    self.logger.warning("não foi possível salvar a pausa do histórico: %s", type(e).__name__)
        self.logger.info("histórico %s", "pausado" if store.paused else "retomado")
        return store.paused

    def show_recent(self) -> None:
        """Abre a janela Recentes (uma só: se já existe, só traz para frente)."""
        try:
            if self.recent_window is None:
                from hear_ai.ui.recent_window import RecentWindow

                self.recent_window = RecentWindow(self._history_store(),
                                                  on_toggle_pause=self._toggle_history_pause,
                                                  on_open_settings=self.show_settings,  # T43: engrenagem
                                                  on_learn=self.learn_corrections if self.vocab_store else None)
            self.recent_window.bring_to_front()
        except Exception as e:
            self.logger.warning("não foi possível abrir Recentes: %s", type(e).__name__)
            if self._open_history_folder is not None:
                self._open_history_folder()

    # --- vocabulário: aprender só por ação explícita em Recentes (T45b) ------------
    def learn_corrections(self, pairs) -> int:
        """Confirma pares ouvido→correto escolhidos pelo usuário. Devolve quantos foram aceitos.
        Atualiza na hora a "dica" do Whisper (initial_prompt) se ela estiver ligada."""
        store = self.vocab_store
        if store is None:
            return 0
        ok = 0
        for heard, correct in pairs:
            try:
                ok += bool(store.confirm(heard, correct))
            except Exception as e:
                self.logger.warning("não foi possível guardar a correção: %s", type(e).__name__)
        cfg = getattr(self.tray, "cfg", None)
        transcriber = getattr(self.controller, "transcriber", None)
        if ok and cfg is not None and getattr(cfg, "use_initial_prompt", False) and transcriber is not None:
            from hear_ai.vocab import prompt_terms

            transcriber.initial_prompt = prompt_terms(store, getattr(cfg, "dictionary", None))
        self.logger.info("vocabulário: %d correção(ões) confirmada(s)", ok)  # só a contagem
        return ok

    # --- janela Configurações (T23) ------------------------------------------------
    def show_settings(self) -> None:
        """Abre a janela Configurações (uma só). Se não der, abre o arquivo, como antes."""
        try:
            if self.settings_window is None:
                from hear_ai.ui.settings_window import SettingsWindow

                self.settings_window = SettingsWindow(
                    self.tray.cfg, self.tray.config_path,
                    on_applied=self._apply_settings, on_reset_bubble=self._reset_bubble_position,
                    loaded_model=self._loaded_model_name, provider_status_fn=self.provider_status_fn,
                )
            self.settings_window.bring_to_front()
            if self._startup_notice:  # ex.: "o modelo medium não está no disco; usando small"
                self.settings_window.set_model_status(self._startup_notice, "warn")
        except Exception as e:
            self.logger.warning("não foi possível abrir Configurações: %s", type(e).__name__)
            self.tray.on_open_settings = None
            try:
                self.tray.open_settings()  # plano B: o arquivo config.json
            finally:
                self.tray.on_open_settings = self.show_settings

    def _apply_settings(self, cfg, changed: set, restart: set) -> None:
        """Aplica na hora o que não precisa reiniciar (o resto a janela avisa)."""
        binder = getattr(self.tray, "binder", None)
        if "mode" in changed and binder is not None:
            binder.set_mode(cfg.mode)
        if "language" in changed:
            self.controller.language = cfg.language
            transcriber = getattr(self.controller, "transcriber", None)
            if transcriber is not None and hasattr(transcriber, "language"):
                transcriber.language = cfg.language
        store = self._history_store()
        if store is not None:
            store.paused = cfg.history_paused
            store.retention_days = cfg.history_retention_days
        if "model" in changed:
            self._request_model_switch(cfg.model)
        if changed & MODE_FIELDS:  # T47b: o passo do gerador lê o config ao vivo, mas o selo/ligação mudam
            self._apply_operation_mode()
        if "mode_hotkey" in changed or "hotkey" in changed:
            self._rebind_mode_hotkey()
        self.tray.refresh()
        if self.recent_window is not None and self.recent_window.isVisible():
            self.recent_window.refresh()

    # --- modo de operação: A (fiel) / B (gerador de prompts) (T47b P4, docs/MODOS.md) -----------------
    def _apply_operation_visuals(self) -> None:
        """Selo do Modo B na bolha (âmbar quando o gerador é de nuvem)."""
        from hear_ai.modos import is_prompt_mode, uses_cloud

        cfg = self.tray.cfg
        self.bubble.driver.set_operation(is_prompt_mode(cfg), uses_cloud(cfg))

    def _apply_operation_mode(self) -> None:
        """Liga/desliga o `prompt_step` do Controller conforme `operation_mode` e atualiza selo e bandeja."""
        from hear_ai.modos import apply_operation_mode

        apply_operation_mode(self.controller, self.tray.cfg, self.tray.config_path, vocab_store=self.vocab_store)
        self._apply_operation_visuals()
        self.tray.refresh()

    def set_operation_mode(self, mode: str, announce: bool = True) -> None:
        """Bandeja/atalho: escolhe o modo. Seguro de qualquer thread (o atalho chega por outra)."""
        self.bridge.post(lambda: self._set_operation_mode(mode, announce))

    def toggle_operation_mode(self) -> None:
        """Atalho do modo (`mode_hotkey`): alterna A ⇄ B."""
        self.bridge.post(lambda: self._set_operation_mode(
            "fiel" if self.tray.cfg.operation_mode == "prompt" else "prompt", True))

    def _set_operation_mode(self, mode: str, announce: bool = True) -> None:
        from hear_ai.config import save_config
        from hear_ai.modos import MODE_FIEL, MODE_LABELS, MODE_PROMPT

        cfg = self.tray.cfg
        if mode not in (MODE_FIEL, MODE_PROMPT) or mode == cfg.operation_mode:
            return
        cfg.operation_mode = mode
        try:
            save_config(cfg, self.tray.config_path)  # escolha explícita do usuário: fica salva
        except OSError as e:
            self.logger.warning("não foi possível salvar o modo: %s", type(e).__name__)
        self.logger.info("modo de operação: %s", mode)
        self._apply_operation_mode()
        if announce:
            self.bubble.driver.notice(f"Modo: {MODE_LABELS[mode]}", 1.5)
        if self.settings_window is not None and self.settings_window.isVisible():
            self.settings_window.refresh_operation_mode()
        if mode == MODE_PROMPT:
            self._warn_if_generator_unavailable()

    def _warn_if_generator_unavailable(self) -> None:
        """Modo B ligado sem gerador disponível é permitido, mas a bolha diz claramente que vai colar a
        transcrição. A consulta ao Ollama (127.0.0.1) roda numa thread: nunca bloqueia a interface."""
        from hear_ai.modos import provider_status

        cfg = self.tray.cfg
        consultar = self.provider_status_fn or provider_status

        def work() -> None:
            status = consultar(cfg)
            if not status.ok:
                self.bridge.post(lambda: self.bubble.driver.notice(
                    "Gerador indisponível — vou colar a transcrição", 3.0))

        self._mode_thread = threading.Thread(target=work, name="hear-ai-gerador-status", daemon=True)
        self._mode_thread.start()

    def _rebind_mode_hotkey(self) -> None:
        hk = self.mode_hotkey
        if hk is None:
            return
        cfg = self.tray.cfg

        def work() -> None:
            ok = hk.rebind(cfg.mode_hotkey, main_hotkey=cfg.hotkey)
            if not ok and hk.conflict_reason:
                self.show_notice(f"Atalho do modo não registrado: {hk.conflict_reason}", "warn", window=True)

        threading.Thread(target=work, name="hear-ai-atalho-modo", daemon=True).start()

    # --- troca do modelo de voz sem reiniciar (T46, docs/MODELOS.md) -------------------------------
    def _transcriber(self):
        transcriber = getattr(self.controller, "transcriber", None)
        return transcriber if hasattr(transcriber, "reload") else None

    def _loaded_model_name(self) -> str | None:
        """Modelo que o app tem carregado AGORA (pode diferir do config: caiu para o small na inicialização)."""
        return getattr(getattr(self.controller, "transcriber", None), "model_name", None)

    def _model_busy(self) -> bool:
        return getattr(self.controller, "state", "idle") != "idle"  # gravando ou transcrevendo

    def show_notice(self, text: str, kind: str = "warn", balloon: bool = True, remember: bool = False,
                    window: bool = False) -> None:
        """Aviso curto ao usuário: log, mensagem na bolha (quando ela está em forma de pílula), balão da bandeja
        e, se `window`, o rodapé da janela Configurações. `remember`: a janela mostra o aviso quando abrir.
        Seguro de chamar de qualquer thread. Nunca leva texto ditado."""
        self.logger.info("aviso: %s", text)
        if remember:
            self._startup_notice = text

        def apply():
            try:
                self.bubble.model.on_warning(text)
                self.bubble.kick()
            except Exception as e:  # noqa: BLE001
                self.logger.warning("aviso na bolha falhou: %s", type(e).__name__)
            if balloon:
                try:
                    from PySide6.QtWidgets import QSystemTrayIcon

                    icon = getattr(getattr(self.tray, "_icon", None), "qt", None)
                    if icon is not None and QSystemTrayIcon.isSystemTrayAvailable():
                        icon.showMessage("Hear AI", text, QSystemTrayIcon.Information, 7000)
                except Exception as e:  # noqa: BLE001 - o balão é só um reforço
                    self.logger.warning("balão da bandeja falhou: %s", type(e).__name__)
            if window and self.settings_window is not None:
                self.settings_window.set_model_status(text, kind)

        self.bridge.post(apply)

    def _request_model_switch(self, name: str) -> None:
        """Pedido de trocar para `name` (Configurações salvou). Não troca durante gravação/transcrição: adia."""
        transcriber = self._transcriber()
        if transcriber is None:
            return
        self._wanted_model = name
        if self._model_switching:
            return  # a troca em andamento olha `_wanted_model` quando terminar
        if transcriber.model_name == name:
            self._wanted_model = None
            return
        if self._model_busy():
            self.show_notice(f"O modelo ‘{name}’ será carregado assim que o ditado terminar.", "warn", window=True)
            self._schedule_model_retry()
            return
        self._start_model_switch(name)

    def _schedule_model_retry(self) -> None:
        if self._model_retry_pending:
            return
        from PySide6.QtCore import QTimer

        self._model_retry_pending = True

        def retry() -> None:
            self._model_retry_pending = False
            self._try_pending_model_switch()

        QTimer.singleShot(MODEL_RETRY_MS, retry)

    def _try_pending_model_switch(self) -> None:
        name, transcriber = self._wanted_model, self._transcriber()
        if not name or transcriber is None or self._model_switching:
            return
        if transcriber.model_name == name:
            self._wanted_model = None
        elif self._model_busy():
            self._schedule_model_retry()
        else:
            self._start_model_switch(name)

    def _start_model_switch(self, name: str) -> None:
        """Carrega `name` numa thread de fundo (a bolha mostra "carregando"); o ditado segue usando o modelo
        atual até o novo estar pronto. Falhou? O atual continua e o config volta atrás."""
        transcriber = self._transcriber()
        previous = transcriber.model_name
        self._model_switching = True
        self._wanted_model = None
        self.logger.info("trocando o modelo de voz: %s -> %s", previous, name)
        self.set_loading(True)

        def work() -> None:
            device, error = None, None
            try:
                device = transcriber.reload(name)
            except Exception as e:  # noqa: BLE001 - qualquer falha de carga: o modelo anterior segue valendo
                error = e
            self.bridge.post(lambda: self._model_switch_done(name, previous, device, error))

        self._model_thread = threading.Thread(target=work, name="hear-ai-modelo", daemon=True)
        self._model_thread.start()

    def _model_switch_done(self, name: str, previous: str, device, error) -> None:
        from hear_ai.config import save_config
        from hear_ai.settings_model import describe_model_failure

        self._model_switching = False
        self.set_loading(False)
        if error is None:
            self.logger.info("modelo de voz '%s' carregado (%s)", name, device)
            note = ""
            if device == "cpu" and getattr(self.tray.cfg, "device", "auto") != "cpu":
                note = " Está rodando no processador (mais lento): a placa de vídeo não foi usada."
            self.show_notice(f"Modelo ‘{name}’ carregado.{note}", "ok" if not note else "warn", balloon=False,
                             window=True)
        else:
            self.logger.error("falha ao carregar o modelo '%s': %s (continuo com '%s')", name,
                              type(error).__name__, previous)
            cfg = self.tray.cfg
            if cfg.model == name:  # o config não pode ficar apontando para um modelo que não carrega
                cfg.model = previous
                try:
                    save_config(cfg, self.tray.config_path)
                except OSError as e:
                    self.logger.warning("não foi possível desfazer o modelo no config: %s", type(e).__name__)
            self.show_notice(f"Não consegui carregar o modelo ‘{name}’ ({describe_model_failure(error)}). "
                             f"Continuo com o ‘{previous}’.", "error", window=True)
            if self.settings_window is not None:
                self.settings_window.refresh_model_choices(select=previous)
        self.tray.refresh()
        self._try_pending_model_switch()  # pediram outro modelo no meio da troca?

    def _reset_bubble_position(self) -> None:
        """Esquece a posição salva e volta a bolha para o centro, embaixo."""
        if self.bubble_state_path:
            try:
                Path(self.bubble_state_path).unlink(missing_ok=True)
            except OSError as e:
                self.logger.warning("não foi possível apagar a posição da bolha: %s", type(e).__name__)
        self.place_bubble()

    def _refresh_recent(self) -> None:
        w = self.recent_window
        if w is not None and w.isVisible():
            w.refresh()

    def _on_bubble_menu(self, global_pos) -> None:
        self._fill_qmenu(self._menu, self.tray.menu_items(with_history=True))
        self._menu.popup(global_pos)

    def _on_esc(self) -> None:
        self.bubble.driver.cancelled()
        self.esc.set_recording(False)
        threading.Thread(target=self.controller.cancel, name="hear-ai-esc", daemon=True).start()

    # --- eventos vindos de outras threads -------------------------------------------
    def on_state(self, state: str) -> None:
        self.bridge.post(lambda: self._on_state(state))

    def _on_state(self, state: str) -> None:
        if state in ROBUSTNESS_MESSAGES:
            # aviso curto na bolha; o estado continua o mesmo (gravando/transcrevendo)
            self.bubble.model.on_warning(ROBUSTNESS_MESSAGES[state])
            self.bubble.kick()
            return
        if state in ("window_changed", "admin_window"):
            return  # o controller manda "saved" logo depois ("texto salvo — Ctrl+V")
        if state == "recording":
            # T40: a bolha precisa estar à vista ANTES de pintar "gravando" (atalho, qualquer via)
            self.reassert_bubble("recording")
        self.tray.set_state(state)
        self.bubble.driver.state(state)
        if state in ("recording", "transcribing", "idle"):
            self.esc.set_recording(state == "recording")
        if state in ("idle", "saved", "error"):
            # um ditado acabou de ser salvo/atualizado: a janela aberta relê a lista (só nesse momento)
            self._refresh_recent()

    def on_pasted(self) -> None:
        self.bridge.post(self.bubble.driver.pasted)

    def set_loading(self, loading: bool) -> None:
        from hear_ai.ui.bubble_model import VisualState

        def apply():
            self.tray.set_loading(loading)
            self.bubble.driver.loading(loading)
            if not loading and self.bubble.visual() == VisualState.LOADING:
                # T40: o modelo da bolha só sai de "carregando" no 1º evento de estado; sem isto o repouso
                # da sessão inteira (até o 1º ditado) ficava com o isotipo a 45% e o anel de carregamento
                self.bubble.driver.state("idle")
        self.bridge.post(apply)

    def set_error(self, message: str) -> None:
        def apply():
            self.tray.set_error(message)
            self.bubble.driver.loading(False)
            self.bubble.driver.state("error")
            self.bubble.driver.state("idle")  # some depois de dar para ler; a bandeja mantém o erro
        self.bridge.post(apply)

    def set_conflict(self, reason: str) -> None:
        def apply():
            self.tray.set_conflict(reason)
            self.bubble.driver.loading(False)
            self.bubble.driver.state("error")
            self.bubble.driver.state("idle")
        self.bridge.post(apply)

    def set_orphans(self, count: int) -> None:
        self.bridge.post(lambda: self.tray.set_orphans(count))

    def run(self) -> int:
        return self.app.exec()
