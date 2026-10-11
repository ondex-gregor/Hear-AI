"""Ícone na bandeja do sistema (Qt: QSystemTrayIcon; T21 tirou o pystray)."""

from __future__ import annotations

import os
from pathlib import Path

from hear_ai.config import PROJECT_ROOT, resolve_path, save_config

ICON_PATH = PROJECT_ROOT / "assets" / "icone.png"

# cor do ponto de estado; idle não tem ponto
STATE_COLORS = {
    "recording": (220, 38, 38, 255),  # vermelho
    "transcribing": (234, 179, 8, 255),  # amarelo
    "loading": (156, 163, 175, 255),  # cinza
    "error": (17, 17, 17, 255),  # preto (falha ao carregar o modelo / atalho em conflito)
    "saved": (37, 99, 235, 255),  # azul (texto salvo — Ctrl+V)
}
# avisos vindos do Controller (on_state_change) -> texto do tooltip
NOTICES = {
    "saved": "texto salvo — Ctrl+V",
    "error": "erro no último ditado — veja o log",
    "limit_warning": "1 min para o limite de gravação",
}
TOOLTIP_MAX = 127  # limite do Windows para o texto do ícone da bandeja
MODE_LABELS = {"hold": "Segurar", "toggle": "Alternar"}


def make_icon(state: str = "idle", size: int = 64, icon_path: Path = ICON_PATH):
    """PNG original (preto, transparente) sobre fundo branco arredondado + ponto de estado."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 4, fill=(255, 255, 255, 255))
    try:
        glyph = Image.open(icon_path).convert("RGBA")
        pad = size // 8
        glyph = glyph.resize((size - 2 * pad, size - 2 * pad), Image.LANCZOS)
        img.alpha_composite(glyph, (pad, pad))
    except OSError:
        pass  # sem o arquivo, fica só o fundo
    color = STATE_COLORS.get(state)
    if color:
        r = size // 5
        draw.ellipse((size - 2 * r - 1, size - 2 * r - 1, size - 2, size - 2), fill=color, outline=(255, 255, 255, 255))
    return img


class TrayApp:
    def __init__(self, controller, cfg, on_quit, config_path=None, binder=None, log_dir=None,
                 feedback=None, logger=None, opener=None, on_recover=None, on_open_history=None):
        self.controller = controller
        self.cfg = cfg
        self.on_quit = on_quit
        self.config_path = Path(config_path) if config_path else PROJECT_ROOT / "config.json"
        self.binder = binder
        self.log_dir = Path(log_dir) if log_dir else resolve_path(cfg.log_dir)
        self.loading = True
        self.error: str | None = None
        self.feedback = feedback
        self.logger = logger
        self._opener = opener  # os.startfile por padrão; injetável em testes
        self.on_recover = on_recover  # chamado pelo item "Recuperar gravação (N)"
        self.on_open_history = on_open_history  # "Abrir histórico" (menu da bolha)
        self.on_open_settings = None  # T23: janela Configurações (se ligada, no lugar do arquivo)
        self.on_show_bubble = None  # T40: "Mostrar/recentralizar bolha" (o QtUi liga)
        self.on_set_operation = None  # T47b: "Transcrição fiel / Gerador de prompts" (o QtUi liga: modo -> None)
        self.notice: str | None = None  # aviso do último ditado (ex.: "saved")
        self.orphans = 0  # gravações que sobraram para recuperar
        self._icon = None

    # estado -> ícone ---------------------------------------------------
    def _visual_state(self) -> str:
        if self.error:
            return "error"
        if self.loading:
            return "loading"
        state = self.controller.state
        if state == "idle" and self.notice == "saved":
            return "saved"
        return state

    def refresh(self) -> None:
        if self._icon is not None:
            state = self._visual_state()
            self._icon.icon = make_icon(state)
            self._icon.title = self.title(state)
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def set_state(self, state: str = "") -> None:
        """Recebe estados (idle/recording/transcribing) e avisos (saved/error/limit_warning)."""
        if state in NOTICES:
            self.notice = state
        elif state == "recording":
            if self.notice != "limit_warning":
                self.notice = None  # novo ditado: o aviso anterior já não vale
        elif state == "transcribing" and self.notice == "limit_warning":
            self.notice = None
        self.refresh()

    def set_conflict(self, reason: str) -> None:
        """Atalho não registrado (outro programa usa a combinação): aviso visível."""
        self.set_error(f"Atalho em conflito: {reason}")

    def set_orphans(self, count: int) -> None:
        self.orphans = max(0, int(count))
        self.refresh()

    def recover(self) -> None:
        if self.on_recover is not None and self.orphans:
            self.on_recover()

    def set_loading(self, loading: bool) -> None:
        self.loading = loading
        self.refresh()

    def set_error(self, message: str = "erro — veja o log") -> None:
        """Estado de erro visível (ícone preto + tooltip); 'Sair' continua funcionando."""
        self.error = message
        self.loading = False
        self.refresh()

    def title(self, state: str | None = None) -> str:
        state = state or self._visual_state()
        names = {
            "idle": "pronto",
            "recording": "gravando",
            "transcribing": "transcrevendo",
            "loading": "carregando modelo",
            "error": self.error or "erro — veja o log",
        }
        text = names.get(state, state)
        if state != "error" and self.notice in NOTICES:
            text = f"{text} · {NOTICES[self.notice]}" if state != "saved" else NOTICES["saved"]
        if self.orphans and state != "error":
            text = f"{text} · {self.orphans} gravação(ões) para recuperar"
        if getattr(self.cfg, "operation_mode", "fiel") == "prompt":
            text = f"{text} · gerador de prompts"
        full = f"Hear AI — {text} ({MODE_LABELS[self.cfg.mode]}: {self.cfg.hotkey})"
        return full if len(full) <= TOOLTIP_MAX else full[: TOOLTIP_MAX - 1] + "…"

    # ações do menu -----------------------------------------------------
    def toggle_mode(self) -> None:
        self.cfg.mode = "toggle" if self.cfg.mode == "hold" else "hold"
        save_config(self.cfg, self.config_path)
        if self.binder is not None:
            self.binder.set_mode(self.cfg.mode)
        self.refresh()

    def _open(self, path: Path, what: str) -> None:
        opener = self._opener or os.startfile
        try:
            opener(str(path))
        except OSError as e:
            if self.logger is not None:
                self.logger.warning("não foi possível abrir %s: %s", what, type(e).__name__)
            if self.feedback is not None:
                self.feedback.error()

    def open_settings(self) -> None:
        if self.on_open_settings is not None:  # T23: abre a janela Configurações
            self.on_open_settings()
            return
        try:
            if not self.config_path.exists():
                save_config(self.cfg, self.config_path)
        except OSError as e:
            if self.logger is not None:
                self.logger.warning("não foi possível criar a configuração: %s", type(e).__name__)
            if self.feedback is not None:
                self.feedback.error()
            return
        self._open(self.config_path, "configurações")

    def show_bubble(self) -> None:
        """Reafirma a bolha e a traz para uma posição segura (a posição salva, se ainda for válida)."""
        if self.on_show_bubble is not None:
            self.on_show_bubble()

    def open_logs(self) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            if self.logger is not None:
                self.logger.warning("não foi possível criar a pasta de logs: %s", type(e).__name__)
            if self.feedback is not None:
                self.feedback.error()
            return
        self._open(self.log_dir, "pasta de logs")

    def quit(self) -> None:
        try:
            self.on_quit()
        finally:
            if self._icon is not None:
                self._icon.stop()

    # --- menu (o mesmo da v1.0; o Qt só desenha) ----------------------------------
    def menu_items(self, with_history: bool = False) -> list:
        """Itens do menu como dados: (texto, ação | None, habilitado, visível).
        `None` no lugar de um item = separador. Usado pela bandeja e pelo botão direito da bolha."""
        items = [
            (f"Modo: {MODE_LABELS[self.cfg.mode]} — {self.cfg.hotkey}", None, False, True),
            ("Mudar para Alternar" if self.cfg.mode == "hold" else "Mudar para Segurar",
             self.toggle_mode, True, True),
            (f"⚠ {self.error}", None, False, bool(self.error)),
            (f"Recuperar gravação ({self.orphans})", self.recover, True, self.orphans > 0),
            ("Abrir configurações", self.open_settings, True, True),
        ]
        if self.on_set_operation is not None:  # T47b: Modo A (fiel) / Modo B (gerador de prompts), marca o atual
            atual = getattr(self.cfg, "operation_mode", "fiel")
            items.insert(2, ("Operação do ditado:", None, False, True))
            for i, (modo, nome) in enumerate((("fiel", "Transcrição fiel"), ("prompt", "Gerador de prompts"))):
                items.insert(3 + i, (f"{'✓' if atual == modo else '    '} {nome}",
                                     lambda m=modo: self.on_set_operation(m), True, True))
        if with_history and self.on_open_history is not None:
            items.append(("Abrir histórico", self.on_open_history, True, True))
        items.append(("Mostrar/recentralizar bolha", self.show_bubble, True, self.on_show_bubble is not None))
        items += [
            ("Abrir pasta de logs", self.open_logs, True, True),
            None,
            ("Sair", self.quit, True, True),
        ]
        return items

    def attach_qt(self, app_quit=None):
        """Cria o ícone da bandeja do Qt (precisa de um QApplication). Chamar na thread do Qt."""
        self._icon = QtTrayIcon(self, app_quit=app_quit)
        self.refresh()
        return self._icon


def fill_qmenu(menu, items) -> None:
    """Desenha a lista de `menu_items` num QMenu."""
    menu.clear()
    for item in items:
        if item is None:
            menu.addSeparator()
            continue
        text, action, enabled, visible = item
        if not visible:
            continue
        act = menu.addAction(text)
        act.setEnabled(enabled and action is not None)
        if action is not None:
            act.triggered.connect(lambda _checked=False, fn=action: fn())


def pil_to_qicon(image):
    """Imagem do Pillow (RGBA) -> QIcon."""
    from PySide6.QtGui import QIcon, QImage, QPixmap

    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimg = QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format_RGBA8888).copy()
    return QIcon(QPixmap.fromImage(qimg))


class QtTrayIcon:
    """Ícone da bandeja com QSystemTrayIcon, com a mesma "cara" do ícone antigo do pystray
    (`icon`, `title`, `update_menu()`, `stop()`), para o TrayApp não mudar."""

    def __init__(self, tray: TrayApp, app_quit=None):
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon

        self.tray = tray
        self._app_quit = app_quit
        self.qt = QSystemTrayIcon()
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self.update_menu)  # textos sempre atuais ao abrir
        self.qt.setContextMenu(self.menu)
        self._icon_image = None
        self._stopped = False
        self._title = ""
        self.update_menu()  # aparece na bandeja quando recebe o 1º ícone (sem aviso "No Icon set")

    @property
    def icon(self):
        return self._icon_image

    @icon.setter
    def icon(self, image) -> None:
        self._icon_image = image
        self.qt.setIcon(pil_to_qicon(image))
        if not self.qt.isVisible() and not self._stopped:
            self.qt.show()

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, text: str) -> None:
        self._title = text
        self.qt.setToolTip(text)

    def update_menu(self) -> None:
        fill_qmenu(self.menu, self.tray.menu_items())

    def stop(self) -> None:
        self._stopped = True
        self.qt.hide()
        if self._app_quit is not None:
            self._app_quit()
