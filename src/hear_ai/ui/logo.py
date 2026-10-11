"""Marca nas janelas do Hear AI.

Hierarquia (ver docs/MARCA.md): **Ondex** é a empresa, **Hear AI** é o produto.
- `white_logo`: isotipo da Ondex em branco (assets/icone.png) — bolha, bandeja e ícone do app. Não mudou.
- `hear_ai_logotipo` / `brand_logo` / `brand_header`: logotipo "hear ai" (assets/marca/logotipo-hear-ai.png,
  preto sobre transparente) em BRANCO, no cabeçalho das janelas Recentes e Configurações. Nunca junto do isotipo.

As partes que desenham pixmaps importam o Qt só quando chamadas (o módulo importa sem Qt).
"""

from __future__ import annotations

from functools import lru_cache

from hear_ai.config import PROJECT_ROOT
from hear_ai.ui import style

ICON_PATH = PROJECT_ROOT / "assets" / "icone.png"
WORDMARK_PATH = PROJECT_ROOT / "assets" / "marca" / "logotipo-hear-ai.png"

WORDMARK_HEIGHT = 24  # px lógicos de TINTA (do alto do "h" à base das letras) no cabeçalho
WORDMARK_COLOR = "#FFFFFF"
WORDMARK_FALLBACK_TEXT = "Hear AI"  # se o arquivo do logotipo faltar
INK_ALPHA_MIN = 8  # alfa (0-255) a partir do qual um pixel conta como "tinta" ao recortar as margens
HEADER_HEIGHT = 32  # px: altura da linha do cabeçalho das janelas (= botão de engrenagem de Recentes)
NAME_BASELINE_SHIFT = 5  # px que o nome da janela desce para alinhar a base dele com a do logotipo

# estilo do cabeçalho compartilhado (as janelas anexam isto ao `style.QSS`)
BRAND_QSS = f"""
QLabel#window_name {{
    color: {style.TEXT_MUTED};
    font-size: 12pt;
    font-weight: 600;
}}
QFrame#brand_divider {{ background: rgba(157, 176, 207, 0.35); border: none; }}
"""


@lru_cache(maxsize=16)
def white_logo(size: int, dpr: float = 1.0, color: str = "#FFFFFF"):
    """QPixmap do isotipo pintado de `color`, mantendo a transparência.

    `size` é em pixels lógicos; `dpr` (devicePixelRatio) deixa o desenho nítido em 125%/150%/200%.
    Sem o arquivo, devolve um pixmap transparente (o app não quebra por falta do ícone).
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

    px = max(1, round(size * dpr))
    img = QImage(str(ICON_PATH))
    if img.isNull():
        empty = QPixmap(px, px)
        empty.fill(Qt.transparent)
        empty.setDevicePixelRatio(dpr)
        return empty
    img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(img)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)  # pinta só onde há tinta
    painter.fillRect(img.rect(), QColor(color))
    painter.end()
    scaled = img.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm = QPixmap.fromImage(scaled)
    pm.setDevicePixelRatio(dpr)
    return pm


# --- logotipo do Hear AI (produto) -------------------------------------------------------------------

@lru_cache(maxsize=4)
def _wordmark_ink(path: str):
    """QImage ARGB32 do logotipo recortado na caixa da tinta (o PNG tem margens transparentes grandes:
    com elas, a "altura" do arquivo seria ~1,7x a altura visível). None se faltar/estiver ilegível/vazio."""
    import numpy as np
    from PySide6.QtGui import QImage

    img = QImage(path)
    if img.isNull() or img.width() == 0 or img.height() == 0:
        return None
    img = img.convertToFormat(QImage.Format_ARGB32)
    raw = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    alpha = raw.reshape(img.height(), img.bytesPerLine() // 4, 4)[:, : img.width(), 3]  # B,G,R,A na memória
    ys, xs = np.where(alpha > INK_ALPHA_MIN)
    if len(xs) == 0:
        return None
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    return img.copy(x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def wordmark_aspect() -> float | None:
    """largura / altura da tinta do logotipo (≈ 4,5), ou None se o arquivo não estiver disponível."""
    ink = _wordmark_ink(str(WORDMARK_PATH))
    return None if ink is None else ink.width() / ink.height()


@lru_cache(maxsize=32)
def _logotipo(path: str, height: int, dpr: float, color: str):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

    ink = _wordmark_ink(path)
    if ink is None:
        pm = QPixmap()
        pm.setDevicePixelRatio(dpr)
        return pm
    img = ink.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(img)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)  # pinta só onde há tinta; o alfa fica
    painter.fillRect(img.rect(), QColor(color))
    painter.end()
    scaled = img.scaledToHeight(max(1, round(height * dpr)), Qt.SmoothTransformation)  # mantém a proporção
    pm = QPixmap.fromImage(scaled)
    pm.setDevicePixelRatio(dpr)
    return pm


def hear_ai_logotipo(height: int = WORDMARK_HEIGHT, dpr: float = 1.0, color: str = WORDMARK_COLOR):
    """QPixmap do logotipo "hear ai" pintado de `color` (padrão: branco), com a transparência original.

    `height` é a altura da tinta em px lógicos; `dpr` deixa o desenho nítido em 100/125/150/200 %.
    Sem o arquivo devolve um QPixmap nulo (`isNull()`): quem desenha cai para o texto "Hear AI".
    O resultado é cacheado por (arquivo, altura, dpr, cor)."""
    return _logotipo(str(WORDMARK_PATH), int(height), float(dpr), color)


_BRAND_CLASS = None


def _make_brand_class():
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
    from PySide6.QtWidgets import QWidget

    class BrandLogo(QWidget):
        """O logotipo do Hear AI, desenhado com o DPR do destino (tela atual ou captura 2x), então
        continua nítido ao arrastar a janela entre monitores de densidades diferentes.
        Sem o arquivo desenha o texto "Hear AI" (`fallback` = True)."""

        def __init__(self, height: int = WORDMARK_HEIGHT, color: str = WORDMARK_COLOR, parent=None):
            super().__init__(parent)
            self.setObjectName("brand_logotipo")
            self._height = int(height)
            self._color = color
            aspect = wordmark_aspect()
            self.fallback = aspect is None
            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.setAccessibleName("Hear AI")
            self._font = QFont(style.FONT_FAMILY)
            self._font.setPixelSize(max(12, round(self._height * 0.8)))
            self._font.setWeight(QFont.DemiBold)
            if self.fallback:
                fm = QFontMetrics(self._font)
                self.setFixedSize(fm.horizontalAdvance(WORDMARK_FALLBACK_TEXT) + 2, max(self._height, fm.height()))
            else:
                self.setFixedSize(round(self._height * aspect), self._height)

        def paintEvent(self, _event) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            if self.fallback:
                p.setFont(self._font)
                p.setPen(QColor(self._color))
                p.drawText(QRectF(self.rect()), int(Qt.AlignVCenter | Qt.AlignLeft), WORDMARK_FALLBACK_TEXT)
                return
            dpr = round((p.device().devicePixelRatioF() or 1.0) * 4) / 4  # 1, 1,25, 1,5, 1,75, 2...
            pm = hear_ai_logotipo(self._height, dpr, self._color)
            ratio = pm.devicePixelRatio() or 1.0
            w, h = pm.width() / ratio, pm.height() / ratio
            p.drawPixmap(QPointF((self.width() - w) / 2, (self.height() - h) / 2), pm)

    return BrandLogo


def brand_logo(height: int = WORDMARK_HEIGHT, color: str = WORDMARK_COLOR, parent=None):
    """Widget `BrandLogo` (objectName `brand_logotipo`)."""
    global _BRAND_CLASS
    if _BRAND_CLASS is None:
        _BRAND_CLASS = _make_brand_class()
    return _BRAND_CLASS(height, color, parent)


def brand_header(window_name: str):
    """Lado esquerdo do cabeçalho das janelas: logotipo do Hear AI + divisória fina + nome da janela
    (menor, em tom apagado). Devolve `(layout, logo, name_label)`; quem chama faz `head.addLayout(layout)`.
    O isotipo da Ondex NÃO entra aqui (fica na bolha, na bandeja e no ícone do app)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QSpacerItem

    layout = QHBoxLayout()
    layout.setSpacing(12)
    logo = brand_logo()
    layout.addWidget(logo, 0, Qt.AlignVCenter)
    divider = QFrame()
    divider.setObjectName("brand_divider")
    divider.setFixedSize(1, 18)
    layout.addWidget(divider, 0, Qt.AlignVCenter)
    name = QLabel(window_name)
    name.setObjectName("window_name")
    # centralizado, o texto (altura de maiúscula ~11 px) ficaria ~6 px acima da base do logotipo (24 px de tinta);
    # a margem de cima empurra o texto 5 px para baixo e as duas linhas de base quase coincidem (<= 1 px)
    name.setContentsMargins(0, 2 * NAME_BASELINE_SHIFT, 0, 0)
    layout.addWidget(name, 0, Qt.AlignVCenter)
    # a linha do cabeçalho tem sempre a mesma altura (a engrenagem de Recentes tem 32 px): o logotipo fica no
    # mesmo lugar nas duas janelas
    layout.addSpacerItem(QSpacerItem(0, HEADER_HEIGHT, QSizePolicy.Fixed, QSizePolicy.Fixed))
    return layout, logo, name
