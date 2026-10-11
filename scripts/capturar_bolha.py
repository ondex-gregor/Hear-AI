"""Renderiza cada estado da bolha em PNG (2x) para conferência visual (B2).

Saída: evidencias/bolha-<estado>.png (fundo transparente) e evidencias/bolha-todas.png
(todos os estados sobre fundo escuro e claro, para julgar contraste).
Não abre janela na tela: usa a plataforma "offscreen" do Qt.

Uso: .venv\\Scripts\\python.exe scripts\\capturar_bolha.py [pasta_saida]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# a plataforma offscreen não enxerga as fontes do Windows sozinha (o texto vira quadradinhos)
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from hear_ai.ui.bubble import WIN_H, WIN_W, BubbleWidget  # noqa: E402

SCALE = 2


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def advance(widget, clock, seconds, level=None, fps=60):
    """Avança o relógio falso em passos de 1/fps, como o QTimer faria."""
    steps = max(1, int(seconds * fps))
    for _ in range(steps):
        clock.t += seconds / steps
        if level is not None:
            widget.model.on_level(level)
        widget.model.frame(clock.t)
    widget._frame = widget.model.frame(clock.t)


def render(widget) -> QPixmap:
    pm = QPixmap(WIN_W * SCALE, WIN_H * SCALE)
    pm.setDevicePixelRatio(SCALE)
    pm.fill(Qt.transparent)
    widget.render(pm, QPoint(0, 0))
    return pm


def build_states():
    """[(nome, widget)] com cada estado já no ponto certo da animação."""
    out = []

    def fresh():
        clock = FakeClock()
        w = BubbleWidget(clock=clock)
        w.timer.stop()
        return w, clock

    w, c = fresh()
    advance(w, c, 0.1)
    out.append(("idle", w))

    w, c = fresh()
    w.driver.loading(True)
    w.timer.stop()
    advance(w, c, 0.1)
    out.append(("loading", w))

    w, c = fresh()
    w.driver.state("recording")
    w.timer.stop()
    advance(w, c, 7.4, level=0.55)
    out.append(("recording", w))

    w, c = fresh()
    w.driver.state("recording")
    w.timer.stop()
    advance(w, c, 2.0, level=0.5)
    w.driver.state("transcribing")
    w.timer.stop()
    advance(w, c, 0.55)
    out.append(("transcribing", w))

    w, c = fresh()
    w.driver.state("recording")
    w.timer.stop()
    advance(w, c, 1.0, level=0.4)
    w.driver.state("transcribing")
    w.timer.stop()
    advance(w, c, 0.4)
    w.driver.pasted()
    w.timer.stop()
    advance(w, c, 0.25)
    out.append(("done", w))

    w, c = fresh()
    w.driver.state("saved")
    w.timer.stop()
    advance(w, c, 0.5)
    out.append(("saved", w))

    w, c = fresh()
    w.driver.state("error")
    w.timer.stop()
    advance(w, c, 0.3)
    out.append(("error", w))
    return out


def contact_sheet(images, path):
    label_w = 120
    cell_h = WIN_H * SCALE
    sheet = QPixmap(label_w * SCALE + WIN_W * SCALE * 2, cell_h * len(images))
    sheet.fill(QColor("#2B2F36"))
    p = QPainter(sheet)
    p.setRenderHint(QPainter.Antialiasing)
    font = QFont("Segoe UI")
    font.setPixelSize(13 * SCALE)
    p.setFont(font)
    for i, (name, pm) in enumerate(images):
        y = i * cell_h
        p.fillRect(label_w * SCALE + WIN_W * SCALE, y, WIN_W * SCALE, cell_h, QColor("#F3F4F6"))
        p.setPen(QColor("#E5E7EB"))
        p.drawText(10 * SCALE, y + cell_h // 2 + 6 * SCALE, name)
        p.drawPixmap(label_w * SCALE, y, pm)
        p.drawPixmap(label_w * SCALE + WIN_W * SCALE, y, pm)
    p.end()
    sheet.save(str(path))


def main(argv) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else ROOT / "evidencias"
    out_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv[:1])  # noqa: F841
    images = []
    for name, widget in build_states():
        pm = render(widget)
        path = out_dir / f"bolha-{name}.png"
        pm.save(str(path))
        images.append((name, pm))
        print(path)
    contact_sheet(images, out_dir / "bolha-todas.png")
    print(out_dir / "bolha-todas.png")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
