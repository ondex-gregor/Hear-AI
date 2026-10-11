"""Paleta e folha de estilo (QSS) comuns das janelas do Hear AI: escuro-azul, como a bolha."""

from __future__ import annotations

from pathlib import Path

BG_FROM = "#05070D"
BG_TO = "#0B2A66"
SURFACE = "rgba(255, 255, 255, 0.06)"  # cartões sobre o gradiente
SURFACE_HOVER = "rgba(255, 255, 255, 0.10)"
BORDER = "rgba(96, 165, 250, 0.28)"
TEXT = "#E8EEF9"
TEXT_MUTED = "#9DB0CF"
ACCENT = "#3B82F6"
ACCENT_LIGHT = "#60A5FA"
WARN = "#FBBF24"  # "salvo — não colado"
DANGER = "#F87171"
OK = "#34D399"

FONT_FAMILY = "Segoe UI"
FONT_PT = 11  # spec: fonte legível (>= 10 pt); textos secundários com 10 pt

QSS = f"""
QWidget {{
    color: {TEXT};
    font-family: "{FONT_FAMILY}";
    font-size: {FONT_PT}pt;
}}
QLabel#title {{
    font-size: 15pt;
    font-weight: 600;
}}
QLabel#muted, QLabel#meta {{
    color: {TEXT_MUTED};
    font-size: 10pt;
}}
QLabel#status_salvo {{ color: {WARN}; font-size: 10pt; }}
QLabel#status_pendente {{ color: {TEXT_MUTED}; font-size: 10pt; }}
QLabel#empty {{ color: {TEXT_MUTED}; font-size: 11pt; }}
QLabel#banner {{
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(251, 191, 36, 0.45);
    border-radius: 10px;
    padding: 8px 12px;
    color: #FDE68A;
}}
QLabel#footer {{ color: {TEXT_MUTED}; font-size: 10pt; }}
QLineEdit {{
    background: rgba(255, 255, 255, 0.07);
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 8px 12px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{ border: 1px solid {ACCENT_LIGHT}; }}
QFrame#row {{
    background: {SURFACE};
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
}}
QFrame#row:hover {{ background: {SURFACE_HOVER}; border: 1px solid {BORDER}; }}
QFrame#confirm {{
    background: rgba(248, 113, 113, 0.10);
    border: 1px solid rgba(248, 113, 113, 0.45);
    border-radius: 10px;
}}
QPushButton {{
    background: rgba(59, 130, 246, 0.16);
    border: 1px solid rgba(96, 165, 250, 0.45);
    border-radius: 8px;
    padding: 5px 12px;
    color: {TEXT};
}}
QPushButton:hover {{ background: rgba(59, 130, 246, 0.30); }}
QPushButton:pressed {{ background: rgba(59, 130, 246, 0.45); }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT_LIGHT}; }}
QPushButton#primary:hover {{ background: {ACCENT_LIGHT}; }}
QPushButton#danger {{
    background: rgba(248, 113, 113, 0.14);
    border: 1px solid rgba(248, 113, 113, 0.55);
    color: #FECACA;
}}
QPushButton#danger:hover {{ background: rgba(248, 113, 113, 0.28); }}
QPushButton#ghost {{
    background: transparent;
    border: 1px solid transparent;
    color: {TEXT_MUTED};
}}
QPushButton#ghost:hover {{ color: {TEXT}; border: 1px solid {BORDER}; }}
QPushButton#small {{ padding: 3px 12px; font-size: 10pt; }}
QPushButton#ghost_small {{
    background: transparent;
    border: 1px solid transparent;
    color: {TEXT_MUTED};
    padding: 3px 8px;
    font-size: 10pt;
}}
QPushButton#ghost_small:hover {{ color: #FECACA; border: 1px solid rgba(248, 113, 113, 0.45); }}
QPushButton#done {{ padding: 3px 12px; font-size: 10pt; background: rgba(52, 211, 153, 0.18); border: 1px solid rgba(52, 211, 153, 0.6); }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(255, 255, 255, 0.18); border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(96, 165, 250, 0.5); }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


_ICON_CACHE: dict[str, str] = {}


def icon_path(name: str) -> str:
    """PNG pequeno desenhado na hora (QSS não desenha "check" nem "seta" sozinho).
    Gravado na pasta temporária do usuário; devolve o caminho com barras "/" para usar em url()."""
    if name in _ICON_CACHE:
        return _ICON_CACHE[name]
    import tempfile

    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

    size = 36  # desenhado grande; o Qt reduz com suavidade
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    if name == "check":
        path.moveTo(QPointF(size * 0.22, size * 0.52))
        path.lineTo(QPointF(size * 0.42, size * 0.72))
        path.lineTo(QPointF(size * 0.78, size * 0.30))
        p.setPen(QPen(QColor("#FFFFFF"), size * 0.13, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    else:  # "chevron" (seta para baixo)
        path.moveTo(QPointF(size * 0.25, size * 0.38))
        path.lineTo(QPointF(size * 0.50, size * 0.63))
        path.lineTo(QPointF(size * 0.75, size * 0.38))
        p.setPen(QPen(QColor(ACCENT_LIGHT), size * 0.11, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(path)
    p.end()
    folder = Path(tempfile.gettempdir()) / "hear-ai-ui"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{name}.png"
    img.save(str(out))
    _ICON_CACHE[name] = out.as_posix()
    return _ICON_CACHE[name]


def app_font():
    from PySide6.QtGui import QFont

    font = QFont(FONT_FAMILY)
    font.setPointSizeF(FONT_PT)
    return font


def paint_background(widget, painter=None) -> None:
    """Gradiente #05070D -> #0B2A66 cobrindo o widget (chamar no paintEvent)."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QLinearGradient, QPainter

    p = painter or QPainter(widget)
    r = widget.rect()
    g = QLinearGradient(QPointF(0, 0), QPointF(r.width(), r.height()))
    g.setColorAt(0.0, QColor(BG_FROM))
    g.setColorAt(1.0, QColor(BG_TO))
    p.fillRect(r, g)
    if painter is None:
        p.end()
