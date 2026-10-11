"""Janela "Recentes" (C1): o histórico de ditados, no estilo escuro-azul da bolha.

Lista os mais recentes primeiro (`HistoryStore.entries`), com hora, prévia de 1–2 linhas,
duração e status. Busca sem diferenciar acento/maiúscula (`entries(query=)`), Copiar,
Apagar e Apagar tudo (confirmação DENTRO da janela, nunca MessageBox) e Pausar histórico.

É uma janela normal (pode receber foco), ao contrário da bolha. Não relê o disco sozinha:
`refresh()` é chamado ao abrir, ao buscar, depois de apagar e quando o app avisa que um
ditado foi salvo.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from hear_ai.ui import logo as brand
from hear_ai.ui import style

log = logging.getLogger("hear_ai.recentes")

MAX_ROWS = 200  # o histórico pode ter milhares; a janela mostra os mais recentes
PREVIEW_CHARS = 160
COPIED_MS = 1500
SEARCH_DEBOUNCE_MS = 180


# --- formatação (sem Qt) ---------------------------------------------------------------------

def format_when(ts: datetime | None, now: datetime | None = None) -> str:
    if ts is None:
        return "—"
    now = now or datetime.now(ts.tzinfo)
    day = (now.date() - ts.date()).days
    hhmm = ts.strftime("%H:%M")
    if day == 0:
        return f"hoje {hhmm}"
    if day == 1:
        return f"ontem {hhmm}"
    if 1 < day < 7:
        nomes = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
        return f"{nomes[ts.weekday()]} {hhmm}"
    return ts.strftime("%d/%m %H:%M") if ts.year == now.year else ts.strftime("%d/%m/%Y %H:%M")


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


def preview(text: str, max_chars: int = PREVIEW_CHARS) -> str:
    """Uma linha só (quebras viram espaço), cortada com "…" sem partir palavra no meio."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= max_chars:
        return flat
    cut = flat[:max_chars].rstrip()
    if " " in cut[max_chars // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:") + "…"


STATUS_LABELS = {
    "salvo": ("● salvo · não colado", "status_salvo"),
    "pendente": ("◌ pendente", "status_pendente"),
}


class ElidedLabel(QLabel):
    """Texto em até `lines` linhas; o que não couber termina em "…" (recalcula ao redimensionar)."""

    def __init__(self, text: str, lines: int = 2):
        super().__init__()
        self._full = text
        self._lines = lines
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._apply(400)

    def full_text(self) -> str:
        return self._full

    def _fits(self, fm, width: int, text: str) -> bool:
        from PySide6.QtCore import QRect

        h = fm.boundingRect(QRect(0, 0, width, 100000), int(Qt.TextWordWrap), text).height()
        return h <= fm.lineSpacing() * self._lines + 1

    def _apply(self, width: int) -> None:
        fm = self.fontMetrics()
        width = max(40, width)
        text = self._full
        if not self._fits(fm, width, text):
            lo, hi = 0, len(text)  # maior prefixo que cabe com "…"
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if self._fits(fm, width, text[:mid].rstrip() + "…"):
                    lo = mid
                else:
                    hi = mid - 1
            cut = text[:lo].rstrip()
            if " " in cut[len(cut) // 2:]:
                cut = cut[: cut.rfind(" ")]
            text = cut.rstrip(" ,.;:") + "…"
        super().setText(text)
        self.setFixedHeight(self._height_for(fm, width, text))

    def _height_for(self, fm, width, text) -> int:
        from PySide6.QtCore import QRect

        return fm.boundingRect(QRect(0, 0, width, 100000), int(Qt.TextWordWrap), text).height() + 2

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._apply(event.size().width())

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        from PySide6.QtCore import QEvent

        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._apply(self.width() or 400)


# --- linha da lista -----------------------------------------------------------------------------

class EntryRow(QFrame):
    """Um ditado: hora · duração · status, prévia, e os botões Copiar / Apagar.
    Apagar troca a linha por uma confirmação ali mesmo (Apagar / Cancelar)."""

    copy_requested = Signal(object)
    use_requested = Signal(object, str)  # T47b: (linha, "original" | "generated") — copia, nunca cola sozinho
    delete_confirmed = Signal(object)
    learn_requested = Signal(object, object)  # (linha, [(ouvido, correto), ...]) — T45b

    def __init__(self, entry, now=None, can_learn: bool = False):
        super().__init__()
        self.entry = entry
        self.setObjectName("row")
        self.setAttribute(Qt.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 12, 10)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        meta = [format_when(entry.ts, now)]
        if entry.duration_s is not None:
            meta.append(format_duration(entry.duration_s))
        self.meta = QLabel(" · ".join(meta))
        self.meta.setObjectName("meta")
        top.addWidget(self.meta)
        if entry.status in STATUS_LABELS:
            text, name = STATUS_LABELS[entry.status]
            self.status = QLabel(text)
            self.status.setObjectName(name)
            self.status.setToolTip("O texto não foi colado; copie e cole com Ctrl+V." if entry.status == "salvo"
                                   else "Ditado salvo antes de colar; a colagem não foi confirmada.")
            top.addWidget(self.status)
        else:
            self.status = None
        self.revisar = None
        if getattr(entry, "revisar", False):  # T47b: a verificação de fidelidade apontou falta no prompt gerado
            self.revisar = QLabel("● revisar")
            self.revisar.setObjectName("status_salvo")
            self.revisar.setToolTip("A verificação automática não encontrou no prompt gerado algum número, caminho, "
                                    "URL ou nome da sua fala. Confira antes de usar (ou use o original).")
            top.addWidget(self.revisar)
        top.addStretch(1)
        outer.addLayout(top)

        self.preview = ElidedLabel(preview(entry.text, 600), lines=2)  # 1–2 linhas, "…" no fim
        self.preview.setToolTip(entry.text if len(entry.text) <= 600 else entry.text[:600] + "…")

        # ações na mesma linha da hora (lista mais compacta)
        self.copy_btn = QPushButton("Copiar")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.clicked.connect(lambda: self.copy_requested.emit(self))
        self.delete_btn = QPushButton("Apagar")
        self.delete_btn.setObjectName("ghost")
        self.delete_btn.setCursor(Qt.PointingHandCursor)
        self.delete_btn.clicked.connect(self.ask_delete)
        self.copy_btn.setObjectName("small")
        self.delete_btn.setObjectName("ghost_small")
        self._top = top
        top.addWidget(self.delete_btn)
        top.addWidget(self.copy_btn)
        outer.addWidget(self.preview)
        self._build_generated(outer)
        self._build_learning(outer, can_learn)

        # confirmação dentro da linha
        self.confirm = QFrame()
        self.confirm.setObjectName("confirm")
        self.confirm.setAttribute(Qt.WA_StyledBackground, True)
        c = QHBoxLayout(self.confirm)
        c.setContentsMargins(10, 6, 6, 6)
        c.addWidget(QLabel("Apagar este ditado?"))
        c.addStretch(1)
        self.cancel_btn = QPushButton("Cancelar")
        self.cancel_btn.setObjectName("ghost")
        self.cancel_btn.clicked.connect(self.cancel_delete)
        self.confirm_btn = QPushButton("Apagar")
        self.confirm_btn.setObjectName("danger")
        self.confirm_btn.clicked.connect(lambda: self.delete_confirmed.emit(self))
        c.addWidget(self.cancel_btn)
        c.addWidget(self.confirm_btn)
        self.confirm.hide()
        outer.addWidget(self.confirm)

        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.timeout.connect(self._reset_copy)

    # --- Modo B (T47b): "usar o original" / "usar o prompt gerado" ------------------------------------
    def _build_generated(self, outer) -> None:
        self.gen_label = self.original_btn = self.generated_btn = None
        generated = getattr(self.entry, "generated", None)
        if not generated:
            return
        line = QHBoxLayout()
        line.setSpacing(8)
        self.gen_label = QLabel("prompt gerado")
        self.gen_label.setObjectName("meta")
        self.gen_label.setToolTip("O texto colado foi o prompt gerado a partir da sua fala. A transcrição fiel "
                                  "(original) fica guardada aqui.")
        line.addWidget(self.gen_label)
        line.addStretch(1)
        self.original_btn = QPushButton("Usar o original")
        self.original_btn.setObjectName("small")
        self.original_btn.setCursor(Qt.PointingHandCursor)
        raw = getattr(self.entry, "raw", None)
        if raw:
            self.original_btn.setToolTip("Copia a transcrição fiel da sua fala (não cola sozinho).")
        else:
            self.original_btn.setEnabled(False)
            self.original_btn.setToolTip("A transcrição original deste ditado não foi guardada.")
        self.original_btn.clicked.connect(lambda: self.use_requested.emit(self, "original"))
        self.generated_btn = QPushButton("Usar o prompt gerado")
        self.generated_btn.setObjectName("small")
        self.generated_btn.setCursor(Qt.PointingHandCursor)
        self.generated_btn.setToolTip("Copia o prompt gerado (não cola sozinho).")
        self.generated_btn.clicked.connect(lambda: self.use_requested.emit(self, "generated"))
        line.addWidget(self.original_btn)
        line.addWidget(self.generated_btn)
        outer.addLayout(line)

    def version_text(self, which: str) -> str | None:
        """O texto de cada versão: "original" = transcrição fiel (raw); "generated" = o prompt gerado."""
        return getattr(self.entry, "raw" if which == "original" else "generated", None) or None

    # --- vocabulário (T45b): nada é aprendido sem clique em "Lembrar" ---------------------------
    def _build_learning(self, outer, can_learn: bool) -> None:
        self.changes = tuple(getattr(self.entry, "changes", ()) or ())
        self.fixed_label = self.learn_btn = self.teach_btn = self.editor = None
        self.pair_boxes: list[QCheckBox] = []
        if self.changes:
            shown = ", ".join(f"{h} → {c}" for h, c in self.changes[:3]) + ("…" if len(self.changes) > 3 else "")
            line = QHBoxLayout()
            line.setSpacing(8)
            self.fixed_label = QLabel(f"corrigido: {shown}")
            self.fixed_label.setObjectName("meta")
            self.fixed_label.setToolTip("O vocabulário trocou estes termos. Lembrar faz valer sempre.")
            line.addWidget(self.fixed_label, 1)
            if can_learn:
                self.learn_btn = QPushButton("Lembrar")
                self.learn_btn.setObjectName("ghost_small")
                self.learn_btn.setToolTip("Guardar estas correções no seu vocabulário (só no seu PC)")
                self.learn_btn.clicked.connect(lambda: self.learn_requested.emit(self, list(self.changes)))
                line.addWidget(self.learn_btn)
            outer.addLayout(line)
        if not can_learn:
            return
        self.teach_btn = QPushButton("Corrigir…")
        self.teach_btn.setObjectName("ghost_small")
        self.teach_btn.setToolTip("Corrigir o texto e ensinar a correção ao vocabulário")
        self.teach_btn.clicked.connect(self.open_editor)
        self._top.insertWidget(self._top.indexOf(self.delete_btn), self.teach_btn)

        self.editor = QFrame()
        self.editor.setObjectName("confirm")
        self.editor.setAttribute(Qt.WA_StyledBackground, True)
        ed = QVBoxLayout(self.editor)
        ed.setContentsMargins(10, 6, 6, 6)
        self.edit_field = QLineEdit(self.entry.text)
        self.edit_field.setAccessibleName("Texto corrigido")
        ed.addWidget(self.edit_field)
        self.pairs_box = QVBoxLayout()
        ed.addLayout(self.pairs_box)
        self.edit_hint = QLabel("")
        self.edit_hint.setObjectName("meta")
        self.edit_hint.setWordWrap(True)
        ed.addWidget(self.edit_hint)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.edit_cancel = QPushButton("Cancelar")
        self.edit_cancel.setObjectName("ghost")
        self.edit_cancel.clicked.connect(self.close_editor)
        self.edit_save = QPushButton("Salvar correção")
        self.edit_save.setObjectName("small")
        self.edit_save.clicked.connect(self.propose_pairs)
        self.edit_learn = QPushButton("Lembrar marcadas")
        self.edit_learn.setObjectName("small")
        self.edit_learn.clicked.connect(self._learn_checked)
        self.edit_learn.hide()
        buttons.addWidget(self.edit_cancel)
        buttons.addWidget(self.edit_save)
        buttons.addWidget(self.edit_learn)
        ed.addLayout(buttons)
        self.editor.hide()
        outer.addWidget(self.editor)

    def open_editor(self) -> None:
        if self.editor is None:
            return
        self.edit_field.setText(self.entry.text)
        self._clear_pairs()
        self.edit_hint.setText("Edite só o que saiu errado e clique em Salvar correção.")
        self.edit_save.show()
        self.edit_learn.hide()
        self.edit_learn.setEnabled(True)
        self.editor.show()
        self.edit_field.setFocus()

    def close_editor(self) -> None:
        if self.editor is not None:
            self._clear_pairs()
            self.editor.hide()

    def _clear_pairs(self) -> None:
        for box in self.pair_boxes:
            box.setParent(None)
            box.deleteLater()
        self.pair_boxes = []

    def propose_pairs(self) -> list:
        """Compara o texto (o bruto do Whisper, se houver) com o editado e MOSTRA os pares para marcar.
        Não guarda nada: quem guarda é "Lembrar marcadas"."""
        from hear_ai.vocab import suggest_pairs

        base = getattr(self.entry, "raw", None) or self.entry.text
        pairs = suggest_pairs(base, self.edit_field.text())
        self._clear_pairs()
        if not pairs:
            self.edit_hint.setText("Nenhuma troca curta encontrada (mude de 1 a 3 palavras; "
                                   "reescritas grandes não viram correção).")
            return []
        for heard, correct in pairs:
            box = QCheckBox(f"{heard} → {correct}")
            box.setChecked(True)
            box.setProperty("pair", [heard, correct])
            self.pairs_box.addWidget(box)
            self.pair_boxes.append(box)
        self.edit_hint.setText("Marque o que deve valer sempre e clique em Lembrar marcadas.")
        self.edit_save.hide()
        self.edit_learn.show()
        return pairs

    def _learn_checked(self) -> None:
        pairs = [tuple(b.property("pair")) for b in self.pair_boxes if b.isChecked()]
        if pairs:
            self.learn_requested.emit(self, pairs)

    def show_learned(self, count: int) -> None:
        if self.learn_btn is not None:
            self.learn_btn.setText("Guardado ✓" if count else "Não guardado")
            self.learn_btn.setEnabled(False)
        if self.editor is not None and not self.editor.isHidden():
            self.edit_hint.setText(f"{count} correção(ões) guardada(s)." if count
                                   else "Nada guardado (termo comum demais ou inválido).")
            self.edit_learn.setEnabled(False)

    def ask_delete(self) -> None:
        self.copy_btn.hide()
        self.delete_btn.hide()
        self.confirm.show()

    def cancel_delete(self) -> None:
        self.confirm.hide()
        self.copy_btn.show()
        self.delete_btn.show()

    def show_copied(self, ok: bool = True) -> None:
        self.copy_btn.setText("Copiado ✓" if ok else "Falhou")
        self.copy_btn.setObjectName("done" if ok else "danger")
        self.copy_btn.style().unpolish(self.copy_btn)
        self.copy_btn.style().polish(self.copy_btn)
        self._copied_timer.start(COPIED_MS)

    def _reset_copy(self) -> None:
        self.copy_btn.setText("Copiar")
        self.copy_btn.setObjectName("small")
        self.copy_btn.style().unpolish(self.copy_btn)
        self.copy_btn.style().polish(self.copy_btn)


# --- botão de configurações (engrenagem) -----------------------------------------------------------

def gear_path(radius: float = 8.6, teeth: int = 8) -> QPainterPath:
    """Engrenagem vetorial centrada em (0, 0): corpo redondo + `teeth` dentes arredondados + furo central."""
    body_r = radius * 0.74
    tooth_w, tooth_h = radius * 0.40, radius * 0.50
    path = QPainterPath()
    path.addEllipse(QPointF(0, 0), body_r, body_r)
    for i in range(teeth):
        tooth = QPainterPath()
        tooth.addRoundedRect(QRectF(-tooth_w / 2, -radius, tooth_w, tooth_h + (radius - body_r) * 0.6), 1.0, 1.0)
        path = path.united(QTransform().rotate(i * 360.0 / teeth).map(tooth))
    hole = QPainterPath()
    hole.addEllipse(QPointF(0, 0), radius * 0.30, radius * 0.30)
    return path.subtracted(hole)


class GearButton(QPushButton):
    """Engrenagem discreta (canto direito do cabeçalho). Vetorial, sem dependência nova: nítida em qualquer
    DPI. Tab dá foco (anel azul), Espaço e Enter acionam; hover/pressionado clareiam o fundo."""

    SIZE = 32

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("gear")
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setToolTip("Configurações")
        self.setAccessibleName("Configurações")
        self.setAccessibleDescription("Abre a janela de configurações do Hear AI")

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):  # QPushButton só aceita Enter dentro de QDialog
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        active = self.underMouse() or self.isDown() or self.hasFocus()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.isDown():
            bg = QColor(59, 130, 246, 85)
        elif self.underMouse():
            bg = QColor(255, 255, 255, 26)
        else:
            bg = QColor(0, 0, 0, 0)
        p.setBrush(bg)
        p.setPen(QPen(QColor(style.ACCENT_LIGHT), 1.4) if self.hasFocus() else Qt.NoPen)
        p.drawRoundedRect(rect, 8, 8)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(style.TEXT if active else style.TEXT_MUTED))
        p.translate(self.width() / 2, self.height() / 2)
        p.drawPath(gear_path())


# --- a janela -----------------------------------------------------------------------------------

class RecentWindow(QWidget):
    """`store`: HistoryStore (ou None se o histórico estiver desligado).
    `clipboard`: objeto com `.set(texto)` (padrão: área de transferência do Qt).
    `on_toggle_pause()`: alterna `history_paused` no config e devolve o novo valor.
    `on_open_settings()`: abre a janela Configurações (a MESMA do app; injetada pelo `QtUi`). Sem ela o botão
    de engrenagem fica oculto; esta janela nunca cria a SettingsWindow por conta própria."""

    def __init__(self, store=None, clipboard=None, on_toggle_pause=None, now=None, parent=None,
                 on_open_settings=None, on_learn=None):
        super().__init__(parent, Qt.Window)
        self.store = store
        self.on_learn = on_learn  # T45b: callable([(ouvido, correto)]) -> quantos guardou
        self.clipboard = clipboard
        self.on_toggle_pause = on_toggle_pause
        self.on_open_settings = on_open_settings
        self._now = now  # relógio fixo para testes e capturas
        self.setWindowTitle("Hear AI — Recentes")
        self.setObjectName("recentes")
        self.setStyleSheet(style.QSS + brand.BRAND_QSS)
        self.setFont(style.app_font())
        self.resize(480, 600)
        self.setMinimumSize(380, 360)
        try:
            from PySide6.QtGui import QIcon

            from hear_ai.ui.logo import ICON_PATH

            self.setWindowIcon(QIcon(str(ICON_PATH)))
        except Exception:
            pass

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        # cabeçalho: logotipo do Hear AI + nome da janela | pausar + engrenagem (T43; sem o isotipo da Ondex)
        head = QHBoxLayout()
        head.setSpacing(10)
        left, self.logo, self.window_name = brand.brand_header("Recentes")
        head.addLayout(left)
        head.addStretch(1)
        self.pause_btn = QPushButton()
        self.pause_btn.setObjectName("ghost")
        self.pause_btn.setCursor(Qt.PointingHandCursor)
        self.pause_btn.clicked.connect(self.toggle_pause)
        head.addWidget(self.pause_btn)
        self.settings_btn = GearButton()
        self.settings_btn.clicked.connect(self.open_settings)
        self.settings_btn.setVisible(self.on_open_settings is not None)
        head.addWidget(self.settings_btn)
        root.addLayout(head)

        self.paused_banner = QLabel("Histórico pausado — os próximos ditados não serão guardados.")
        self.paused_banner.setObjectName("banner")
        self.paused_banner.setWordWrap(True)
        root.addWidget(self.paused_banner)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar nos ditados…")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda _t: self._search_timer.start(SEARCH_DEBOUNCE_MS))
        root.addWidget(self.search)

        # lista
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_host = QWidget()
        self.list_host.setObjectName("list_host")
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 4, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        root.addWidget(self.scroll, 1)

        self.empty_label = QLabel()
        self.empty_label.setObjectName("empty")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        root.addWidget(self.empty_label, 1)

        # rodapé: contagem/aviso + apagar tudo (com confirmação na própria janela)
        foot = QHBoxLayout()
        self.footer = QLabel()
        self.footer.setObjectName("footer")
        self.footer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        foot.addWidget(self.footer)
        self.clear_all_btn = QPushButton("Apagar tudo")
        self.clear_all_btn.setObjectName("ghost")
        self.clear_all_btn.clicked.connect(self.ask_clear_all)
        foot.addWidget(self.clear_all_btn)
        root.addLayout(foot)

        self.clear_confirm = QFrame()
        self.clear_confirm.setObjectName("confirm")
        self.clear_confirm.setAttribute(Qt.WA_StyledBackground, True)
        cc = QHBoxLayout(self.clear_confirm)
        cc.setContentsMargins(12, 8, 8, 8)
        msg = QLabel("Apagar TODO o histórico? Não dá para desfazer.")
        msg.setWordWrap(True)
        cc.addWidget(msg, 1)
        self.clear_cancel_btn = QPushButton("Cancelar")
        self.clear_cancel_btn.setObjectName("ghost")
        self.clear_cancel_btn.clicked.connect(self.cancel_clear_all)
        self.clear_confirm_btn = QPushButton("Apagar tudo")
        self.clear_confirm_btn.setObjectName("danger")
        self.clear_confirm_btn.clicked.connect(self.confirm_clear_all)
        cc.addWidget(self.clear_cancel_btn)
        cc.addWidget(self.clear_confirm_btn)
        self.clear_confirm.hide()
        root.addWidget(self.clear_confirm)

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._update_footer)
        self._rows: list[EntryRow] = []
        self._total = 0
        self.refresh()

    # --- aparência -------------------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        style.paint_background(self)

    # --- dados -------------------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return bool(getattr(self.store, "paused", False))

    def rows(self) -> list[EntryRow]:
        return list(self._rows)

    def now(self) -> datetime:
        return self._now() if callable(self._now) else (self._now or datetime.now())

    def refresh(self) -> None:
        """Relê o histórico (ao abrir, buscar, apagar ou quando chega um ditado novo)."""
        query = self.search.text().strip() or None
        entries, error = [], None
        if self.store is not None:
            try:
                entries = self.store.entries(query=query)
            except Exception as e:  # o HistoryStore já não levanta; proteção extra
                error = type(e).__name__
                log.warning("falha ao ler o histórico: %s", error)
        self._total = len(entries)
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        now = self.now()
        for entry in entries[:MAX_ROWS]:
            row = EntryRow(entry, now, can_learn=self.on_learn is not None)
            row.learn_requested.connect(self.learn_from_row)
            row.copy_requested.connect(self.copy_entry)
            row.use_requested.connect(self.use_version)
            row.delete_confirmed.connect(self.delete_entry)
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)
            self._rows.append(row)

        has_rows = bool(self._rows)
        self.scroll.setVisible(has_rows)
        self.empty_label.setVisible(not has_rows)
        if self.store is None:
            self.empty_label.setText("O histórico está desligado nas configurações.")
        elif error:
            self.empty_label.setText("Não foi possível ler o histórico agora.")
        elif query:
            self.empty_label.setText(f"Nada encontrado para “{query}”.")
        else:
            self.empty_label.setText("Nenhum ditado ainda.\nSegure o atalho e fale — o texto aparece aqui.")
        self.clear_all_btn.setEnabled(self.store is not None and (has_rows or query is not None))
        self._update_pause()
        self.settings_btn.setVisible(self.on_open_settings is not None)
        self._update_footer()

    def learn_from_row(self, row, pairs) -> int:
        """Só chamado por clique do usuário ("Lembrar" / "Lembrar marcadas")."""
        if self.on_learn is None or not pairs:
            return 0
        try:
            count = int(self.on_learn(list(pairs)) or 0)
        except Exception as e:
            log.warning("falha ao guardar correção: %s", type(e).__name__)
            count = 0
        row.show_learned(count)
        return count

    def _update_pause(self) -> None:
        enabled = self.store is not None and self.on_toggle_pause is not None
        self.pause_btn.setVisible(enabled)
        self.pause_btn.setText("Retomar histórico" if self.paused else "Pausar histórico")
        self.paused_banner.setVisible(self.store is not None and self.paused)

    def _update_footer(self, text: str | None = None) -> None:
        if text is None:
            if self._total > MAX_ROWS:
                text = f"Mostrando os {MAX_ROWS} mais recentes de {self._total}"
            elif self._total == 1:
                text = "1 ditado"
            else:
                text = f"{self._total} ditados" if self._total else ""
        self.footer.setText(text)

    def _flash(self, text: str) -> None:
        self._update_footer(text)
        self._status_timer.start(2500)

    # --- ações ---------------------------------------------------------------------------------
    def use_version(self, row: EntryRow, which: str) -> None:
        """T47b: copia a versão pedida (original ou prompt gerado) para a área de transferência. NÃO cola."""
        text = row.version_text(which)
        if not text:
            self._flash("Essa versão não está guardada")
            return
        ok = True
        try:
            if self.clipboard is not None:
                self.clipboard.set(text)
            else:
                from PySide6.QtGui import QGuiApplication

                QGuiApplication.clipboard().setText(text)
        except Exception as e:
            ok = False
            log.warning("falha ao copiar: %s", type(e).__name__)
        nome = "Original" if which == "original" else "Prompt gerado"
        self._flash(f"{nome} copiado — cole com Ctrl+V" if ok else "Não foi possível copiar")

    def copy_entry(self, row: EntryRow) -> None:
        ok = True
        try:
            if self.clipboard is not None:
                self.clipboard.set(row.entry.text)
            else:
                from PySide6.QtGui import QGuiApplication

                QGuiApplication.clipboard().setText(row.entry.text)
        except Exception as e:
            ok = False
            log.warning("falha ao copiar: %s", type(e).__name__)
        row.show_copied(ok)
        self._flash("Copiado — cole com Ctrl+V" if ok else "Não foi possível copiar")

    def delete_entry(self, row: EntryRow) -> None:
        ok = False
        try:
            ok = bool(self.store.delete(row.entry.id))
        except Exception as e:
            log.warning("falha ao apagar: %s", type(e).__name__)
        if not ok:
            row.cancel_delete()
            self._flash("Não foi possível apagar (arquivo em uso?)")
            return
        self.refresh()
        self._flash("Ditado apagado")

    def ask_clear_all(self) -> None:
        self.clear_all_btn.hide()
        self.clear_confirm.show()

    def cancel_clear_all(self) -> None:
        self.clear_confirm.hide()
        self.clear_all_btn.show()

    def confirm_clear_all(self) -> None:
        try:
            self.store.clear_all()
        except Exception as e:
            log.warning("falha ao apagar tudo: %s", type(e).__name__)
        self.cancel_clear_all()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.refresh()
        self._flash("Histórico apagado")

    def toggle_pause(self) -> None:
        if self.on_toggle_pause is None:
            return
        try:
            self.on_toggle_pause()
        except Exception as e:
            log.warning("falha ao pausar/retomar o histórico: %s", type(e).__name__)
        self._update_pause()
        self._flash("Histórico pausado" if self.paused else "Histórico retomado")

    def open_settings(self) -> None:
        """Engrenagem: pede ao app para abrir/trazer a janela Configurações. Não mexe em mais nada aqui:
        a busca digitada, a lista, a gravação em andamento e esta própria janela ficam como estão."""
        if self.on_open_settings is None:
            return
        try:
            self.on_open_settings()
        except Exception as e:
            log.warning("falha ao abrir as configurações: %s", type(e).__name__)

    def bring_to_front(self) -> None:
        """Mostra (ou traz para frente, se já aberta) e relê a lista."""
        self.refresh()
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
