"""Janela "Configurações" (C2), no estilo escuro-azul da bolha, usando `settings_model.SettingsForm`.

Salvar valida pelo modelo (T23a), grava o config.json (aqui SIM: o usuário pediu pelo app),
liga/desliga o "iniciar com o Windows" (`startup.py`), aplica na hora o que dá (via
`on_applied`) e diz o que só vale depois de reiniciar o Hear AI.

T46 (modelo de voz): a caixa "Modelo" lista `small` e `medium` sempre. Escolher um que ainda não foi baixado NÃO
grava o config: mostra a explicação e o botão "Baixar agora" (confirmação dentro da janela, progresso, Cancelar,
"Tentar de novo"). É a única coisa que usa a internet e só com clique. O modelo escolhido é trocado sem
reiniciar o app (o `QtUi` recarrega o transcritor em segundo plano).
"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import fields
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

from hear_ai import startup
from hear_ai.config import Config, resolve_path, save_config
from hear_ai.model_download import ModelDownloader, size_label
from hear_ai.settings_model import (MODEL_HINTS, SettingsForm, download_confirm_text, download_offer_text,
                                    download_progress_text, model_options)
from hear_ai.ui import style

log = logging.getLogger("hear_ai.configuracoes")

# o que só vale depois de reiniciar o Hear AI (o resto é aplicado na hora por `on_applied`)
RESTART_FIELDS = {"hotkey", "input_device", "device", "history_enabled",
                  "bubble_hide_when_idle", "bubble_idle_opacity"}  # (T46: "model" é trocado na hora pelo QtUi)
FIELD_NAMES = {
    "hotkey": "atalho", "mode": "modo", "language": "idioma", "input_device": "microfone",
    "model": "modelo", "device": "processador", "history_enabled": "histórico ligado",
    "history_retention_days": "dias do histórico", "history_paused": "pausa do histórico",
    "bubble_hide_when_idle": "ocultar a bolha", "bubble_idle_opacity": "opacidade da bolha",
    "start_with_windows": "iniciar com o Windows",
}
MODE_ITEMS = [("hold", "Segurar o atalho enquanto fala"), ("toggle", "Apertar para começar e para parar")]
LANG_ITEMS = [("pt", "Português"), ("en", "English")]
DEVICE_ITEMS = [("auto", "Automático (placa de vídeo, se houver)"), ("cpu", "Processador (CPU)"),
                ("cuda", "Placa de vídeo NVIDIA (CUDA)")]
_PSEUDO_MICS = ("mapeador de som", "sound mapper", "driver de captura", "primary sound capture",
                "mixagem estéreo", "stereo mix")


# --- listas (sem Qt) --------------------------------------------------------------------------------

def input_devices(query_devices=None, query_hostapis=None) -> list[str]:
    """Nomes dos microfones (entradas), sem repetição. Usa o WASAPI quando existe (nomes completos;
    o MME corta em 31 letras) e tira "mapeador"/"mixagem estéreo"."""
    try:
        if query_devices is None or query_hostapis is None:
            import sounddevice as sd

            query_devices = query_devices or sd.query_devices
            query_hostapis = query_hostapis or sd.query_hostapis
        devices = list(query_devices())
        apis = list(query_hostapis())
    except Exception as e:
        log.warning("não foi possível listar os microfones: %s", type(e).__name__)
        return []
    wasapi = next((i for i, a in enumerate(apis) if "wasapi" in str(a.get("name", "")).lower()), None)
    names = []
    for d in devices:
        if d.get("max_input_channels", 0) <= 0:
            continue
        if wasapi is not None and d.get("hostapi") != wasapi:
            continue
        name = str(d.get("name", "")).strip()
        if not name or any(p in name.lower() for p in _PSEUDO_MICS) or name in names:
            continue
        names.append(name)
    return names


def downloaded_models(models_dir) -> list[str]:
    """Modelos já baixados: pastas `faster-whisper-<nome>` com `model.bin`."""
    try:
        base = Path(models_dir)
        found = [p.name[len("faster-whisper-"):] for p in base.glob("faster-whisper-*")
                 if (p / "model.bin").exists()]
    except OSError:
        return []
    order = ["tiny", "base", "small", "medium", "large-v2", "large-v3"]
    return sorted(found, key=lambda n: (order.index(n) if n in order else len(order), n))


def pretty_hotkey(hotkey: str) -> str:
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "space": "Espaço",
             "enter": "Enter", "esc": "Esc", "tab": "Tab", "page up": "Page Up", "page down": "Page Down"}
    return " + ".join(names.get(p, p.upper() if len(p) <= 3 else p.title()) for p in hotkey.split("+"))


class _RecordSignals(QObject):
    rejected = Signal(str)
    finished = Signal(object)


class _DownloadSignals(QObject):
    """O download roda em outra thread; estes sinais levam o que ele conta de volta para a thread do Qt."""
    progress = Signal(float, int)
    done = Signal()
    error = Signal(str)
    cancelled = Signal()


class SettingsWindow(QWidget):
    """`cfg` é o Config VIVO do app (atualizado ao salvar). `on_applied(cfg, mudou, reiniciar)`
    aplica na hora o que dá; `on_reset_bubble()` volta a bolha à posição padrão."""

    def __init__(self, cfg: Config, config_path, *, startup_dir="default", startup_target=None,
                 startup_runner=None, models_dir=None, list_mics=None, record_hotkey_fn=None,
                 on_applied=None, on_reset_bubble=None, parent=None, downloader_factory=None, loaded_model=None,
                 provider_status_fn=None, provider_async=True):
        """`downloader_factory(models_dir, nome, on_progress=, on_done=, on_error=, on_cancelled=)` devolve um
        objeto com `start()`/`cancel()` (padrão: `ModelDownloader`, que usa a internet; os testes passam um falso).
        `loaded_model()` diz qual modelo o app tem carregado agora (para aplicar um modelo do config que ainda
        não foi carregado, p. ex. depois de baixar)."""
        super().__init__(parent, Qt.Window)
        self.cfg = cfg
        self.config_path = Path(config_path)
        self.startup_dir = startup.default_startup_dir() if startup_dir == "default" else startup_dir
        self.startup_target = startup_target
        self.startup_runner = startup_runner
        self.models_dir = Path(models_dir) if models_dir else resolve_path(cfg.models_dir)
        self.list_mics = list_mics or input_devices
        self.record_hotkey_fn = record_hotkey_fn
        self.on_applied = on_applied
        self.on_reset_bubble = on_reset_bubble
        self._recording = False
        self._sig = _RecordSignals()
        self._sig.rejected.connect(self._on_record_rejected)
        self._sig.finished.connect(self._on_record_finished)
        self.provider_status_fn = provider_status_fn  # T47b: estado do gerador (injetável nos testes)
        self.provider_async = provider_async
        self.loaded_model = loaded_model
        self.downloader_factory = downloader_factory or self._default_downloader
        self._dl = None  # download em andamento (ModelDownloader ou falso)
        self._dl_name: str | None = None  # modelo que está sendo baixado / falhou
        self._dl_phase = "idle"  # idle | confirm | downloading | failed
        self._dl_error = ""
        self._dl_sig = _DownloadSignals()
        self._dl_sig.progress.connect(self._on_dl_progress)
        self._dl_sig.done.connect(self._on_dl_done)
        self._dl_sig.error.connect(self._on_dl_error)
        self._dl_sig.cancelled.connect(self._on_dl_cancelled)
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is not None:  # sair do app com um download rodando: cancela (sem filho órfão)
            QApplication.instance().aboutToQuit.connect(self._cancel_download_on_quit)

        self.setWindowTitle("Hear AI — Configurações")
        from hear_ai.ui.logo import BRAND_QSS

        self.setStyleSheet(style.QSS + BRAND_QSS + _extra_qss())
        self.setFont(style.app_font())
        self.resize(540, 760)
        self.setMinimumSize(440, 420)
        try:
            from PySide6.QtGui import QIcon

            from hear_ai.ui.logo import ICON_PATH

            self.setWindowIcon(QIcon(str(ICON_PATH)))
        except Exception:
            pass
        self.errors: dict[str, QLabel] = {}
        self._build()
        self.load_from_config()

    # --- montagem ---------------------------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        style.paint_background(self)

    def _section(self, title: str, subtitle: str | None = None):
        card = QFrame()
        card.setObjectName("card")
        card.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("section")
        lay.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setObjectName("muted")
            s.setWordWrap(True)
            lay.addWidget(s)
        self._content.addWidget(card)
        return lay

    def _row(self, lay, label: str, widget, field: str | None = None) -> None:
        row = QHBoxLayout()
        row.setSpacing(12)
        lab = QLabel(label)
        lab.setMinimumWidth(130)
        row.addWidget(lab)
        row.addWidget(widget, 1)
        lay.addLayout(row)
        if field:
            self._error_label(lay, field)

    def _error_label(self, lay, field: str) -> None:
        err = QLabel()
        err.setObjectName("error")
        err.setWordWrap(True)
        err.hide()
        lay.addWidget(err)
        self.errors[field] = err

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        # cabeçalho (T43): logotipo do Hear AI + nome da janela; o isotipo da Ondex não entra aqui
        head = QHBoxLayout()
        head.setSpacing(10)
        from hear_ai.ui.logo import brand_header

        left, self.logo, self.window_name = brand_header("Configurações")
        head.addLayout(left)
        head.addStretch(1)
        root.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("list_host")
        self._content = QVBoxLayout(host)
        self._content.setContentsMargins(0, 0, 6, 0)
        self._content.setSpacing(12)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        # Atalho e modo
        lay = self._section("Atalho")
        hk = QHBoxLayout()
        self.hotkey_label = QLabel()
        self.hotkey_label.setObjectName("keycap")
        hk.addWidget(self.hotkey_label)
        hk.addStretch(1)
        self.record_btn = QPushButton("Gravar novo atalho")
        self.record_btn.setCursor(Qt.PointingHandCursor)
        self.record_btn.clicked.connect(self.start_record)
        hk.addWidget(self.record_btn)
        lay.addLayout(hk)
        self.hotkey_hint = QLabel()
        self.hotkey_hint.setObjectName("muted")
        self.hotkey_hint.setWordWrap(True)
        self.hotkey_hint.hide()
        lay.addWidget(self.hotkey_hint)
        self._error_label(lay, "hotkey")
        self.mode_combo = self._combo(MODE_ITEMS)
        self._row(lay, "Modo", self.mode_combo, "mode")

        # Modo de operação (T47b P4): A fiel / B gerador de prompts
        from hear_ai.ui.modo_panel import ModoPanel

        lay = self._section("Modo de operação")
        self.modo = ModoPanel(status_fn=self.provider_status_fn, run_async=self.provider_async)
        lay.addWidget(self.modo)
        self.errors.update(self.modo.errors)

        # Voz
        lay = self._section("Voz")
        self.lang_combo = self._combo(LANG_ITEMS)
        self._row(lay, "Idioma", self.lang_combo, "language")
        self.mic_combo = QComboBox()
        self._row(lay, "Microfone", self.mic_combo, "input_device")
        self.model_combo = QComboBox()
        self._row(lay, "Modelo", self.model_combo, "model")
        self._build_model_panel(lay)
        self.model_combo.currentIndexChanged.connect(lambda _i: self._refresh_model_panel())
        self.device_combo = self._combo(DEVICE_ITEMS)
        self._row(lay, "Processar no", self.device_combo, "device")

        # Histórico
        lay = self._section("Histórico", "Os ditados ficam só neste computador.")
        self.hist_enabled = QCheckBox("Guardar o histórico de ditados")
        lay.addWidget(self.hist_enabled)
        self._error_label(lay, "history_enabled")
        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 3650)
        self.days_spin.setSuffix(" dias")
        self.days_spin.lineEdit().setTextMargins(9, 0, 0, 0)  # alinha com o texto das listas
        self._row(lay, "Guardar por", self.days_spin, "history_retention_days")
        self.hist_paused = QCheckBox("Pausar (não guardar os próximos ditados)")
        lay.addWidget(self.hist_paused)
        self._error_label(lay, "history_paused")
        self.hist_enabled.toggled.connect(self._sync_history_controls)

        # Bolha
        lay = self._section("Bolha")
        self.bubble_hide = QCheckBox("Ocultar quando ocioso")
        lay.addWidget(self.bubble_hide)
        self._error_label(lay, "bubble_hide_when_idle")
        op = QHBoxLayout()
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(30, 100)
        self.opacity_slider.setSingleStep(5)
        self.opacity_value = QLabel()
        self.opacity_value.setMinimumWidth(44)
        self.opacity_slider.valueChanged.connect(lambda v: self.opacity_value.setText(f"{v}%"))
        op.addWidget(self.opacity_slider, 1)
        op.addWidget(self.opacity_value)
        holder = QWidget()
        holder.setLayout(op)
        op.setContentsMargins(0, 0, 0, 0)
        self._row(lay, "Opacidade em repouso", holder, "bubble_idle_opacity")
        pos = QHBoxLayout()
        pos.addStretch(1)
        self.reset_pos_btn = QPushButton("Voltar a bolha à posição padrão")
        self.reset_pos_btn.setObjectName("ghost")
        self.reset_pos_btn.clicked.connect(self.reset_bubble_position)
        self.reset_pos_btn.setEnabled(self.on_reset_bubble is not None)
        pos.addWidget(self.reset_pos_btn)
        lay.addLayout(pos)

        # Sistema
        lay = self._section("Sistema")
        self.startup_check = QCheckBox("Iniciar o Hear AI com o Windows")
        self.startup_check.setEnabled(self.startup_dir is not None)
        lay.addWidget(self.startup_check)
        self._error_label(lay, "start_with_windows")
        self._content.addStretch(1)

        # rodapé
        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.status = QLabel()
        self.status.setObjectName("footer")
        self.status.setWordWrap(True)
        foot.addWidget(self.status, 1)
        self.cancel_btn = QPushButton("Fechar")
        self.cancel_btn.setObjectName("ghost")
        self.cancel_btn.clicked.connect(self.close)
        self.save_btn = QPushButton("Salvar")
        self.save_btn.setObjectName("primary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self.save)
        foot.addWidget(self.cancel_btn)
        foot.addWidget(self.save_btn)
        root.addLayout(foot)

    def _build_model_panel(self, lay) -> None:
        """Dica do modelo + caixa de download (oculta quando o modelo escolhido já está baixado)."""
        self.model_hint = QLabel()
        self.model_hint.setObjectName("muted")
        self.model_hint.setWordWrap(True)
        self.model_hint.hide()
        lay.addWidget(self.model_hint)

        self.model_box = QFrame()
        self.model_box.setObjectName("model_box")
        self.model_box.setAttribute(Qt.WA_StyledBackground, True)
        box = QVBoxLayout(self.model_box)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(8)
        self.model_info = QLabel()
        self.model_info.setWordWrap(True)
        box.addWidget(self.model_info)

        self.model_download_btn = QPushButton()
        self.model_download_btn.setObjectName("primary")
        self.model_download_btn.setCursor(Qt.PointingHandCursor)
        self.model_download_btn.clicked.connect(self._ask_download)
        box.addWidget(self.model_download_btn, 0, Qt.AlignLeft)

        self.model_confirm = QFrame()  # confirmação DENTRO da janela (nunca MessageBox)
        confirm = QVBoxLayout(self.model_confirm)
        confirm.setContentsMargins(0, 0, 0, 0)
        confirm.setSpacing(8)
        self.model_confirm_label = QLabel()
        self.model_confirm_label.setWordWrap(True)
        confirm.addWidget(self.model_confirm_label)
        row = QHBoxLayout()
        self.model_confirm_yes = QPushButton("Sim, baixar")
        self.model_confirm_yes.setObjectName("primary")
        self.model_confirm_yes.setCursor(Qt.PointingHandCursor)
        self.model_confirm_yes.clicked.connect(self._confirm_download)
        self.model_confirm_no = QPushButton("Agora não")
        self.model_confirm_no.setObjectName("ghost")
        self.model_confirm_no.clicked.connect(self._cancel_confirm)
        row.addWidget(self.model_confirm_yes)
        row.addWidget(self.model_confirm_no)
        row.addStretch(1)
        confirm.addLayout(row)
        box.addWidget(self.model_confirm)

        self.model_progress_box = QFrame()
        prog = QVBoxLayout(self.model_progress_box)
        prog.setContentsMargins(0, 0, 0, 0)
        prog.setSpacing(6)
        self.model_progress = QProgressBar()
        self.model_progress.setRange(0, 1000)
        self.model_progress.setTextVisible(False)
        self.model_progress.setFixedHeight(8)
        prog.addWidget(self.model_progress)
        self.model_progress_label = QLabel()
        self.model_progress_label.setObjectName("muted")
        prog.addWidget(self.model_progress_label)
        self.model_cancel_btn = QPushButton("Cancelar download")
        self.model_cancel_btn.setObjectName("ghost")
        self.model_cancel_btn.setCursor(Qt.PointingHandCursor)
        self.model_cancel_btn.clicked.connect(self._cancel_download)
        prog.addWidget(self.model_cancel_btn, 0, Qt.AlignLeft)
        box.addWidget(self.model_progress_box)

        self.model_error = QLabel()
        self.model_error.setObjectName("error")
        self.model_error.setWordWrap(True)
        box.addWidget(self.model_error)
        self.model_retry_btn = QPushButton("Tentar de novo")
        self.model_retry_btn.setCursor(Qt.PointingHandCursor)
        self.model_retry_btn.clicked.connect(self._retry_download)
        box.addWidget(self.model_retry_btn, 0, Qt.AlignLeft)

        lay.addWidget(self.model_box)
        self.model_box.hide()

    @staticmethod
    def _combo(items) -> QComboBox:
        c = QComboBox()
        for value, text in items:
            c.addItem(text, value)
        return c

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        i = combo.findData(value)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _sync_history_controls(self) -> None:
        on = self.hist_enabled.isChecked()
        self.days_spin.setEnabled(on)
        self.hist_paused.setEnabled(on)

    # --- carregar / coletar -----------------------------------------------------------------------
    def load_from_config(self) -> None:
        cfg = self.cfg
        form = SettingsForm.from_config(cfg)
        self._hotkey = form.hotkey
        self.hotkey_label.setText(pretty_hotkey(form.hotkey))
        self._select(self.mode_combo, form.mode)
        self._select(self.lang_combo, form.language)

        self.mic_combo.clear()
        self.mic_combo.addItem("Padrão do Windows", None)
        for name in self.list_mics():
            self.mic_combo.addItem(name, name)
        saved_mic = getattr(cfg, "input_device", None)
        if saved_mic and self.mic_combo.findData(saved_mic) < 0:
            self.mic_combo.addItem(f"{saved_mic} (não encontrado agora)", saved_mic)
        self._select(self.mic_combo, saved_mic)

        # um download em andamento (ou que falhou) continua selecionado: a janela pode ter sido fechada e reaberta
        self.modo.load(cfg)
        keep = self._dl_name if self._dl_phase in ("downloading", "failed") else None
        self._fill_model_combo(select=keep or form.model)
        self._select(self.device_combo, form.device)

        self.hist_enabled.setChecked(form.history_enabled)
        self.days_spin.setValue(form.history_retention_days)
        self.hist_paused.setChecked(form.history_paused)
        self._sync_history_controls()
        self.bubble_hide.setChecked(bool(getattr(cfg, "bubble_hide_when_idle", False)))
        self.opacity_slider.setValue(round(float(getattr(cfg, "bubble_idle_opacity", 0.85)) * 100))
        self.opacity_value.setText(f"{self.opacity_slider.value()}%")
        self.startup_check.setChecked(startup.is_enabled(self.startup_dir))  # estado REAL do atalho
        self._clear_errors()

    # --- modelos de voz (T46) ------------------------------------------------------------------------------
    def _fill_model_combo(self, select: str | None = None) -> None:
        """`small` e `medium` sempre; o não baixado aparece como "medium — não baixado (~1,5 GB)"."""
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for opt in model_options(downloaded_models(self.models_dir), self.cfg.model):
            self.model_combo.addItem(opt.label, opt.name)
        self._select(self.model_combo, select or self.cfg.model)
        self.model_combo.blockSignals(False)
        self._refresh_model_panel()

    def refresh_model_choices(self, select: str | None = None) -> None:
        """Relê a pasta de modelos (depois de um download, ou se o app voltou o modelo atrás)."""
        self._fill_model_combo(select=select or self.model_combo.currentData())

    def set_model_status(self, text: str, kind: str = "footer") -> None:
        """Mensagem no rodapé sobre o modelo (o app avisa aqui como terminou a troca)."""
        self._set_status(text, kind)

    def _is_downloaded(self, name) -> bool:
        return bool(name) and (self.models_dir / f"faster-whisper-{name}" / "model.bin").exists()

    def _refresh_model_panel(self) -> None:
        name = self.model_combo.currentData()
        downloading = self._dl_phase == "downloading"
        shown = self._dl_name if (downloading or (self._dl_phase == "failed" and self._dl_name == name)) else name
        hint = MODEL_HINTS.get(name or "")
        self.model_hint.setText(hint or "")
        self.model_hint.setVisible(bool(hint))
        missing = bool(shown) and not self._is_downloaded(shown)
        phase = self._dl_phase if (self._dl_name == shown) else "idle"
        if not (missing or downloading):
            self.model_box.hide()
            return
        offer = phase == "idle" and not downloading
        self.model_info.setText(download_offer_text(shown))
        self.model_info.setVisible(not downloading)
        self.model_download_btn.setText(f"Baixar agora ({size_label(shown)})" if size_label(shown)
                                        else "Baixar agora")
        self.model_download_btn.setVisible(offer)
        self.model_confirm_label.setText(download_confirm_text(shown))
        self.model_confirm.setVisible(phase == "confirm")
        self.model_progress_box.setVisible(downloading)
        self.model_error.setText(self._dl_error)
        self.model_error.setVisible(phase == "failed")
        self.model_retry_btn.setVisible(phase == "failed")
        self.model_box.show()

    def _default_downloader(self, models_dir, name, *, on_progress, on_done, on_error, on_cancelled):
        return ModelDownloader(models_dir, name, on_progress=on_progress, on_done=on_done, on_error=on_error,
                               on_cancelled=on_cancelled)

    def _ask_download(self) -> None:
        name = self.model_combo.currentData()
        if not name or self._is_downloaded(name) or self._dl_phase == "downloading":
            return
        self._dl_name, self._dl_phase = name, "confirm"
        self._refresh_model_panel()

    def _cancel_confirm(self) -> None:
        self._dl_phase = "idle"
        self._refresh_model_panel()

    def _confirm_download(self) -> None:
        if self._dl_phase == "confirm":
            self._start_download(self._dl_name)

    def _retry_download(self) -> None:
        if self._dl_phase == "failed" and self._dl_name:
            self._start_download(self._dl_name)  # continua de onde parou (o huggingface_hub retoma)

    def _start_download(self, name: str) -> None:
        """ÚNICO ponto que liga a internet, e só depois do clique de confirmação."""
        self._dl_name, self._dl_phase, self._dl_error = name, "downloading", ""
        self.model_progress.setValue(0)
        self.model_progress_label.setText(download_progress_text(name, 0.0, 0))
        self.model_cancel_btn.setEnabled(True)
        try:
            self._dl = self.downloader_factory(
                self.models_dir, name, on_progress=self._dl_sig.progress.emit, on_done=self._dl_sig.done.emit,
                on_error=self._dl_sig.error.emit, on_cancelled=self._dl_sig.cancelled.emit)
            started = self._dl.start()
        except Exception as e:  # noqa: BLE001
            log.warning("não foi possível iniciar o download: %s", type(e).__name__)
            self._dl_phase = "failed"
            self._dl_error = "Não foi possível iniciar o download. Clique em “Tentar de novo”."
            self._refresh_model_panel()
            return
        if started is False:  # já havia um download rodando
            log.info("download do modelo já em andamento")
        self._refresh_model_panel()

    def _cancel_download(self) -> None:
        if self._dl is not None and self._dl_phase == "downloading":
            self.model_cancel_btn.setEnabled(False)
            self.model_progress_label.setText("Cancelando…")
            self._dl.cancel()

    def _cancel_download_on_quit(self) -> None:
        """O app vai fechar: encerra o download que NÓS abrimos (o que já foi baixado fica para a retomada)."""
        dl = self._dl
        if dl is None or self._dl_phase != "downloading":
            return
        try:
            dl.cancel()
            join = getattr(dl, "join", None)
            if join is not None:
                join(3)
        except Exception as e:  # noqa: BLE001 - na saída, nada pode travar o fechamento
            log.warning("não foi possível cancelar o download ao sair: %s", type(e).__name__)

    def _on_dl_progress(self, frac: float, done_bytes: int) -> None:
        if self._dl_phase != "downloading":
            return
        self.model_progress.setValue(round(max(0.0, min(1.0, frac)) * 1000))
        self.model_progress_label.setText(download_progress_text(self._dl_name or "", frac, done_bytes))

    def _on_dl_done(self) -> None:
        name = self._dl_name
        self._dl_phase, self._dl_error = "idle", ""
        self.refresh_model_choices(select=name)  # o rótulo deixa de dizer "não baixado"
        self._set_status(f"Modelo ‘{name}’ baixado. Clique em Salvar para usá-lo.", "ok")

    def _on_dl_error(self, message: str) -> None:
        self._dl_phase, self._dl_error = "failed", message
        self._refresh_model_panel()
        self._set_status("O download do modelo não terminou.", "error")

    def _on_dl_cancelled(self) -> None:
        self._dl_phase = "failed"
        self._dl_error = "Download cancelado. Clique em “Tentar de novo” para continuar de onde parou."
        self._refresh_model_panel()
        self._set_status("Download cancelado.", "warn")

    def collect_form(self) -> SettingsForm:
        return SettingsForm(
            hotkey=self._hotkey,
            mode=self.mode_combo.currentData(),
            language=self.lang_combo.currentData(),
            input_device=self.mic_combo.currentData(),
            model=self.model_combo.currentData(),
            device=self.device_combo.currentData(),
            history_enabled=self.hist_enabled.isChecked(),
            history_retention_days=self.days_spin.value(),
            history_paused=self.hist_paused.isChecked(),
            bubble_hide_when_idle=self.bubble_hide.isChecked(),
            bubble_idle_opacity=round(self.opacity_slider.value() / 100, 2),
            start_with_windows=self.startup_check.isChecked(),
            **self.modo.values(),
        )

    # --- gravar novo atalho --------------------------------------------------------------------------
    def start_record(self) -> None:
        if self._recording:
            return
        self._recording = True
        self.record_btn.setEnabled(False)
        self.record_btn.setText("Pressione a combinação…")
        self.hotkey_hint.setObjectName("muted")
        self._repolish(self.hotkey_hint)
        self.hotkey_hint.setText("Ex.: Ctrl + Alt + H. Esc cancela. Combinações do Windows são recusadas.")
        self.hotkey_hint.show()
        fn = self.record_hotkey_fn
        if fn is None:
            from hear_ai.hotkey import record_hotkey as fn

        def work():
            try:
                result = fn(timeout_s=10, on_rejected=self._sig.rejected.emit)
            except Exception as e:
                log.warning("gravar atalho falhou: %s", type(e).__name__)
                result = None
            self._sig.finished.emit(result)

        threading.Thread(target=work, name="hear-ai-gravar-atalho", daemon=True).start()

    def _on_record_rejected(self, reason: str) -> None:
        self.hotkey_hint.setObjectName("error")
        self._repolish(self.hotkey_hint)
        self.hotkey_hint.setText(f"Não dá para usar: {reason}. Tente outra combinação.")

    def _on_record_finished(self, hotkey) -> None:
        self._recording = False
        self.record_btn.setEnabled(True)
        self.record_btn.setText("Gravar novo atalho")
        if hotkey:
            self._hotkey = hotkey
            self.hotkey_label.setText(pretty_hotkey(hotkey))
            self.hotkey_hint.setObjectName("ok")
            self._repolish(self.hotkey_hint)
            self.hotkey_hint.setText("Novo atalho escolhido. Clique em Salvar para usar.")
        else:
            self.hotkey_hint.setObjectName("muted")
            self._repolish(self.hotkey_hint)
            self.hotkey_hint.setText("Nenhum atalho novo (cancelado ou tempo esgotado).")
        self.hotkey_hint.show()

    @staticmethod
    def _repolish(w) -> None:
        w.style().unpolish(w)
        w.style().polish(w)

    # --- salvar ----------------------------------------------------------------------------------------
    def _clear_errors(self) -> None:
        for lab in self.errors.values():
            lab.clear()
            lab.hide()

    def _set_status(self, text: str, kind: str = "footer") -> None:
        self.status.setObjectName(kind)
        self._repolish(self.status)
        self.status.setText(text)

    def save(self) -> bool:
        """Valida, grava o config, liga/desliga o iniciar com o Windows e aplica. True se salvou."""
        self._clear_errors()
        form = self.collect_form()
        errors = form.validate(downloaded=set(downloaded_models(self.models_dir)), current_model=self.cfg.model)
        if self.modo.needs_consent():  # Modo B na nuvem só com o aceite dado AQUI (nunca em silêncio)
            errors["cloud_consent"] = "aceite o envio do texto da sua fala (botão “Ler e aceitar…”)"
        if errors:
            for field, msg in errors.items():
                lab = self.errors.get(field)
                if lab is not None:
                    lab.setText(msg[:1].upper() + msg[1:])
                    lab.show()
            self._set_status("Corrija os campos marcados em vermelho.", "error")
            return False

        new = copy.deepcopy(self.cfg)
        new, changed, _ = form.apply_to(new)
        if self.loaded_model is not None and "model" not in changed:
            try:  # o config já aponta para o modelo escolhido, mas o app ainda não o carregou (ex.: acabou de baixar)
                loaded = self.loaded_model()
            except Exception:  # noqa: BLE001
                loaded = None
            if loaded and loaded != form.model and self._is_downloaded(form.model):
                changed.add("model")  # (modelo ausente no disco: nada a carregar; o config não é tocado)
        if getattr(new, "input_device", None) != form.input_device:
            new.input_device = form.input_device
            changed.add("input_device")

        # iniciar com o Windows: o estado real é o atalho na pasta Inicializar
        want = form.start_with_windows
        problems = []
        if want != startup.is_enabled(self.startup_dir):
            kwargs = {}
            if self.startup_target is not None:
                kwargs["target"] = self.startup_target
            if self.startup_runner is not None:
                kwargs["runner"] = self.startup_runner
            real = startup.set_enabled(self.startup_dir, want, **kwargs)
            if real != want:
                problems.append("não foi possível " + ("ligar" if want else "desligar") + " o iniciar com o Windows")
            want = real
        if new.start_with_windows != want:
            new.start_with_windows = want
            changed.add("start_with_windows")
        self.startup_check.setChecked(want)

        try:
            save_config(new, self.config_path)
        except OSError as e:
            log.warning("não foi possível salvar as configurações: %s", type(e).__name__)
            self._set_status("Não foi possível salvar o arquivo de configurações.", "error")
            return False
        for f in fields(Config):  # o app inteiro usa o mesmo objeto de config
            setattr(self.cfg, f.name, getattr(new, f.name))

        restart = changed & RESTART_FIELDS
        if self.on_applied is not None:
            try:
                self.on_applied(self.cfg, set(changed), set(restart))
            except Exception as e:
                log.warning("aplicar configurações falhou: %s", type(e).__name__)
        log.info("configurações salvas (%d campo(s) mudaram)", len(changed))

        note = ""
        if "model" in changed and self.on_applied is not None:  # a troca é assíncrona: o app avisa o resultado depois
            note = f"O modelo ‘{new.model}’ está sendo carregado agora."
        if problems:
            self._set_status("Salvo, mas " + "; ".join(problems) + ". " + note, "error")
        elif not changed:
            self._set_status("Nada mudou.")
        elif restart:
            names = ", ".join(FIELD_NAMES.get(f, f) for f in sorted(restart, key=list(FIELD_NAMES).index))
            self._set_status(f"Salvo. Reinicie o Hear AI para aplicar: {names}. {note}".strip(), "warn")
        elif note:
            self._set_status(f"Salvo. {note}", "ok")
        else:
            self._set_status("Salvo e aplicado.", "ok")
        return True

    def reset_bubble_position(self) -> None:
        if self.on_reset_bubble is None:
            return
        try:
            self.on_reset_bubble()
            self._set_status("A bolha voltou para o centro, embaixo.", "ok")
        except Exception as e:
            log.warning("voltar a bolha à posição padrão falhou: %s", type(e).__name__)

    def refresh_operation_mode(self) -> None:
        """O modo foi trocado pela bandeja/atalho: esta janela (se aberta) acompanha."""
        self.modo.set_mode(self.cfg.operation_mode)

    def bring_to_front(self) -> None:
        if not self.isVisible():
            self.load_from_config()  # abre sempre com o que está salvo
            self.status.clear()
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()


def _extra_qss() -> str:
    check = style.icon_path("check")
    chevron = style.icon_path("chevron")
    return _EXTRA_QSS.replace("@CHECK@", check).replace("@CHEVRON@", chevron)


_EXTRA_QSS = f"""
QFrame#card {{
    background: {style.SURFACE};
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 14px;
}}
QLabel#section {{ font-size: 12pt; font-weight: 600; color: {style.TEXT}; }}
QLabel#keycap {{
    background: rgba(59, 130, 246, 0.18);
    border: 1px solid rgba(96, 165, 250, 0.55);
    border-radius: 8px;
    padding: 5px 12px;
    font-weight: 600;
}}
QFrame#model_box {{
    background: rgba(59, 130, 246, 0.08);
    border: 1px solid rgba(96, 165, 250, 0.30);
    border-radius: 10px;
}}
QProgressBar {{ background: rgba(255, 255, 255, 0.12); border: none; border-radius: 4px; }}
QProgressBar::chunk {{ background: {style.ACCENT}; border-radius: 4px; }}
QLabel#error {{ color: #FCA5A5; font-size: 10pt; }}
QLabel#ok {{ color: {style.OK}; font-size: 10pt; }}
QLabel#warn {{ color: #FDE68A; font-size: 10pt; }}
QComboBox, QSpinBox {{
    background: rgba(255, 255, 255, 0.07);
    border: 1px solid {style.BORDER};
    border-radius: 8px;
    padding: 5px 10px;
    min-height: 22px;
}}
QComboBox:focus, QSpinBox:focus {{ border: 1px solid {style.ACCENT_LIGHT}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{ image: url("@CHEVRON@"); width: 14px; height: 14px; }}
QSpinBox QLineEdit {{ background: transparent; border: none; padding: 0; margin: 0; }}
QComboBox QAbstractItemView {{
    background: #0B1530;
    border: 1px solid {style.BORDER};
    selection-background-color: {style.ACCENT};
    color: {style.TEXT};
    padding: 4px;
}}
/* setas escondidas por largura 0 (NoButtons + QSS deixa o campo com 1 px no Qt 6.12) */
QSpinBox::up-button, QSpinBox::down-button {{ width: 0px; border: none; }}
QCheckBox {{ spacing: 10px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border-radius: 5px;
    border: 1px solid rgba(96, 165, 250, 0.6);
    background: rgba(255, 255, 255, 0.06);
}}
QCheckBox::indicator:checked {{
    background: {style.ACCENT};
    border: 1px solid {style.ACCENT_LIGHT};
    image: url("@CHECK@");
}}
QCheckBox:disabled, QLabel:disabled, QSpinBox:disabled {{ color: rgba(232, 238, 249, 0.40); }}
QSlider::groove:horizontal {{ height: 6px; border-radius: 3px; background: rgba(255, 255, 255, 0.12); }}
QSlider::sub-page:horizontal {{ border-radius: 3px; background: {style.ACCENT}; }}
QSlider::handle:horizontal {{
    width: 16px; height: 16px; margin: -6px 0;
    border-radius: 8px; background: {style.ACCENT_LIGHT}; border: 2px solid #0B2A66;
}}
"""
