"""Janela da bolha (PySide6). Só PINTA o `Frame` de `ui.bubble_model` (T20).

Partes sem Qt (testáveis sem tela): `default_position`, `restore_position`,
`load_bubble_state`/`save_bubble_state` e `BubbleDriver` (liga os eventos do app ao modelo).
`BubbleWidget` é a janela: sem borda, transparente, sempre por cima, não rouba foco, fora do
Alt+Tab. Tamanho fixo (cabe a pílula + o brilho do pulso); a forma é desenhada centralizada,
então a bolha cresce para os dois lados sem a janela "pular".
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

from hear_ai.ui.bubble_model import AppState, BubbleModel, Frame, VisualState
from hear_ai.ui.click_machine import Action, ClickMachine

log = logging.getLogger("hear_ai.bubble")

PAD = 12  # margem transparente em volta da forma (espaço do pulso)
WIN_W = BubbleModel.PILL_W + 2 * PAD
WIN_H = BubbleModel.PILL_H + 2 * PAD
BOTTOM_MARGIN = 48  # distância da forma até a barra de tarefas
LOGO_SIZE = 26
DRAG_THRESHOLD = 4  # px: menos que isso é clique
RING_SPAN = 110.0  # graus do arco que gira em "transcrevendo"
RING_WIDTH = 2.8  # px

# cores (spec B1)
BG_FROM = "#05070D"
BG_TO = "#0B2A66"
BORDER = (59, 130, 246, 110)
WAVE_LOW = "#3B82F6"
WAVE_HIGH = "#60A5FA"
CHECK = "#34D399"
SOFT_RED = (248, 113, 113)
BADGE_PROMPT = "#A78BFA"  # T47b: selo do Modo B (violeta); na nuvem fica âmbar
BADGE_CLOUD = "#FBBF24"
GENERATING_TEXT = "Gerando prompt…"
GENERATOR_FALLBACK_TEXT = "gerador indisponível — colei a transcrição"
# T40: aro claro do repouso — o disco escuro some sobre fundo escuro; o aro (fino + brilho suave) o mantém
# perceptível em fundo claro E escuro (sobre o claro quem se destaca é o próprio disco). Alfa 0 = sem aro.
HALO_EDGE_ALPHA = 125
HALO_GLOW_ALPHA = 30

MESSAGES = {
    VisualState.SAVED: "Texto salvo — Ctrl+V",
    VisualState.ERROR: "Erro no ditado — veja o log",
}
# T41b: onda reativa. Cada barra mostra UMA amostra do histórico do modelo (sem forma fixa nem tremor falso).
BAR_MIN_H = 3.0  # px: silêncio = pontinhos
BAR_V_MARGIN = 12.0  # px de folga (soma de cima + baixo) entre a barra mais alta e a borda da pílula
BAR_MAX_W = 4.0  # px
BAR_X_GAP = 4.0  # px entre o isotipo e a primeira barra
BAR_RIGHT_SPACE = 62.0  # px reservados à direita para o cronômetro


def bar_max_height(shape_h: float) -> float:
    return max(BAR_MIN_H, shape_h - BAR_V_MARGIN)


def bar_height(level: float, max_h: float) -> float:
    """Altura em px de uma barra de nível 0..1 (3 px em silêncio, `max_h` no nível máximo)."""
    v = 0.0 if not (level > 0.0) else min(1.0, level)  # NaN/negativo -> 0
    return BAR_MIN_H + v * (max_h - BAR_MIN_H)


# --- posição (sem Qt) --------------------------------------------------------------------

def default_position(avail, win_w: int = WIN_W, win_h: int = WIN_H, margin: int = BOTTOM_MARGIN):
    """Canto superior esquerdo da janela: centro-baixo da área útil, forma `margin` px acima da barra."""
    x, y, w, h = avail
    shape_h = win_h - 2 * PAD
    return (int(x + (w - win_w) / 2), int(y + h - margin - shape_h - PAD))


def _shape_inside(pos, avail, win_w: int, win_h: int) -> bool:
    px, py = pos
    ax, ay, aw, ah = avail
    # a forma (área visível) precisa caber na tela; a margem transparente pode sobrar
    sx, sy = px + PAD, py + PAD
    sw, sh = win_w - 2 * PAD, win_h - 2 * PAD
    return ax <= sx and ay <= sy and sx + sw <= ax + aw and sy + sh <= ay + ah


def visible_on_any(pos, screens, win_w: int = WIN_W, win_h: int = WIN_H) -> bool:
    """A bolha (a forma, não a margem transparente) cabe inteira em alguma tela? (T31)"""
    return any(_shape_inside(pos, avail, win_w, win_h) for _name, avail in screens)


def restore_position(saved, screens, primary_avail, win_w: int = WIN_W, win_h: int = WIN_H):
    """(x, y, nome_da_tela). `screens`: [(nome, (x, y, w, h) da área útil)].

    Usa a posição salva se a tela dela ainda existe e a bolha cabe nela; senão volta ao padrão
    na tela principal (monitor desligado, resolução/DPI mudou...)."""
    if isinstance(saved, dict):
        name, x, y = saved.get("screen"), saved.get("x"), saved.get("y")
        if isinstance(x, int) and isinstance(y, int) and not isinstance(x, bool) and not isinstance(y, bool):
            for sname, avail in screens:
                if sname == name and _shape_inside((x, y), avail, win_w, win_h):
                    return x, y, sname
    primary_name = screens[0][0] if screens else None
    for sname, avail in screens:
        if avail == primary_avail:
            primary_name = sname
            break
    x, y = default_position(primary_avail, win_w, win_h)
    return x, y, primary_name


def load_bubble_state(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_bubble_state(path, x: int, y: int, screen: str | None) -> bool:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"x": int(x), "y": int(y), "screen": screen}), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as e:
        log.warning("não foi possível salvar a posição da bolha: %s", type(e).__name__)
        return False


# --- eventos do app -> modelo (sem Qt) ---------------------------------------------------

class BubbleDriver:
    """Traduz eventos do Controller para o `BubbleModel`.

    O Controller manda "saved"/"error" e logo em seguida "idle". Se o idle fosse direto ao
    modelo, a pílula fecharia antes de dar para ler a frase; então o idle é ADIADO até o aviso
    (ou o check de "colado") terminar. Um novo "recording" cancela o adiamento.
    `schedule(atraso_s, fn)` é injetável (QTimer.singleShot na janela; imediato/manual em testes).
    """

    def __init__(self, model: BubbleModel, clock=time.monotonic, schedule=None, on_change=None):
        self.model = model
        self.clock = clock
        self.schedule = schedule
        self.on_change = on_change
        self._hold_until = 0.0
        self._token = 0

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _hold(self, seconds: float) -> None:
        self._hold_until = max(self._hold_until, self.clock() + seconds)

    def state(self, name: str) -> None:
        if name == "recording":
            self._token += 1
            self._hold_until = 0.0
            self.model.on_state(AppState.RECORDING)
        elif name == "transcribing":
            self.model.on_state(AppState.TRANSCRIBING)
        elif name == "saved":
            self.model.on_state(AppState.SAVED)
            self._hold(BubbleModel.SAVED_DURATION)
        elif name == "error":
            self.model.on_state(AppState.ERROR)
            self._hold(BubbleModel.ERROR_DURATION)
        elif name == "generating":  # T47b: o Controller avisa que está gerando o prompt (Modo B)
            self.model.on_state(AppState.GENERATING)
        elif name == "generator_fallback":  # o gerador falhou: a transcrição fiel foi colada; o aviso precisa ser lido
            self.model.on_warning(GENERATOR_FALLBACK_TEXT)
            self._hold(BubbleModel.SAVED_DURATION)
        elif name == "limit_warning":
            self.model.on_warning("1 min para o limite")
        elif name == "idle":
            remaining = self._hold_until - self.clock()
            if remaining > 0 and self.schedule is not None:
                self._token += 1
                token = self._token
                self.schedule(remaining, lambda: self._deferred_idle(token))
            else:
                self.model.on_state(AppState.IDLE)
        else:
            return
        self._changed()

    def _deferred_idle(self, token: int) -> None:
        if token != self._token:
            return  # chegou outro evento (ex.: nova gravação) depois
        self._hold_until = 0.0
        self.model.on_state(AppState.IDLE)
        self._changed()

    def set_operation(self, prompt: bool, cloud: bool = False) -> None:
        """Selo do Modo B (e da nuvem) em repouso e gravando."""
        self.model.set_operation(prompt, cloud)
        self._changed()

    def notice(self, text: str, duration: float = 1.5) -> None:
        """Aviso curto até em repouso (ex.: "Modo: Gerador de prompts")."""
        self.model.on_notice(text, duration)
        self._changed()

    def pasted(self) -> None:
        self.model.on_pasted()
        self._hold(BubbleModel.DONE_DURATION)
        self._changed()

    def loading(self, value: bool) -> None:
        self.model.on_loading(value)
        self._changed()

    def cancelled(self) -> None:
        self._token += 1
        self._hold_until = 0.0
        self.model.on_cancel()
        self._changed()

    def visual(self) -> VisualState:
        return getattr(self.model, "_visual", VisualState.IDLE)  # leitura; o modelo é da T20


# --- a janela (Qt) -----------------------------------------------------------------------

def _make_widget_class():
    from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QFont, QLinearGradient, QPainter,
                               QPainterPath, QPen)
    from PySide6.QtWidgets import QWidget

    from hear_ai.ui.logo import white_logo

    class BubbleWidget(QWidget):
        # T42b: o mouse passa pela ClickMachine; `action` leva o nome da Action ("start", "stop",
        # "discard_and_open", "open_recent"). O que fazer com cada uma é do QtUi.
        action = Signal(str)
        clicked = Signal()  # LEGADO (T22): não é mais emitido pelo mouse; o QtUi ainda o liga a "abrir Recentes"
        menu_requested = Signal(object)  # botão direito: posição global (QPoint)
        moved = Signal(int, int)  # arrastou para (x, y)

        FLAGS = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                 | Qt.WindowDoesNotAcceptFocus)

        def __init__(self, model: BubbleModel | None = None, level_source=None, clock=time.monotonic,
                     interval_ms: int = 16, state_source=None):
            """`state_source()` -> estado do app ("idle"/"recording"/"transcribing"...), o do controller;
            sem ele vale o estado visual da bolha. Serve para a ClickMachine decidir o que um clique faz."""
            super().__init__(None, self.FLAGS)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self.setFixedSize(WIN_W, WIN_H)
            self.setWindowTitle("Hear AI")
            self.clock = clock
            self.model = model or BubbleModel(clock=clock)
            self.level_source = level_source
            self.driver = BubbleDriver(self.model, clock=clock, schedule=self._schedule, on_change=self.kick)
            self.timer = QTimer(self)
            self.timer.setInterval(interval_ms)
            self.timer.timeout.connect(self._tick)
            self._frame: Frame = self.model.frame(clock())
            self._painted = None  # (frame, visual) do último repaint pedido (T41b: sem repaint repetido)
            self.state_source = state_source
            self._machine = ClickMachine(double_click_ms=self._double_click_ms(), drag_px=DRAG_THRESHOLD)
            self._press = None  # (posição global do clique, posição da janela)
            self._dragging = False  # o vigia da T40 lê isto (QtUi._dragging)

        # --- animação ---------------------------------------------------------------
        def _schedule(self, delay_s: float, fn) -> None:
            QTimer.singleShot(max(0, int(delay_s * 1000) + 1), fn)

        def visual(self) -> VisualState:
            return self.driver.visual()

        def kick(self) -> None:
            """Algo mudou: redesenha e liga o timer se houver animação."""
            self._tick()

        def _tick(self) -> None:
            recording = self.visual() == VisualState.RECORDING
            if recording and self.level_source is not None:
                try:
                    self.model.on_level(float(self.level_source()))
                except Exception:
                    self.model.on_level(0.0)
            self._frame = self.model.frame(self.clock())
            # o timer roda a 60 Hz, mas a onda só muda a 30 Hz: sem mudança no frame/estado não há o que repintar
            key = (self._frame, self.visual())
            if key != self._painted:
                self._painted = key
                self.update()
            # durante a gravação o cronômetro e o volume mudam sempre; fora disso, só se animando
            if self._frame.needs_animation or recording:
                if not self.timer.isActive():
                    self.timer.start()
            elif self.timer.isActive():
                self.timer.stop()  # repouso: CPU ~0

        def frame(self) -> Frame:
            return self._frame

        def shape_rect(self) -> QRectF:
            f = self._frame
            w, h = max(f.height, f.width), f.height
            return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

        # --- desenho ----------------------------------------------------------------
        def paintEvent(self, _event) -> None:
            f = self._frame
            visual = self.visual()
            # opacidade reduzida só em repouso (spec B1); ativo = opaco
            opacity = f.opacity if visual in (VisualState.IDLE, VisualState.HIDDEN) else 1.0
            if opacity <= 0.001:
                return
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.setOpacity(opacity)
            r = self.shape_rect()
            radius = r.height() / 2
            alert = visual in (VisualState.SAVED, VisualState.ERROR)

            self._paint_pulse(p, r, f, alert)
            if visual in (VisualState.IDLE, VisualState.LOADING):
                self._paint_idle_halo(p, r)

            # fundo: gradiente preto -> azul-marinho
            g = QLinearGradient(r.topLeft(), QPointF(r.right(), r.bottom()))
            g.setColorAt(0.0, QColor(BG_FROM))
            g.setColorAt(1.0, QColor(BG_TO))
            p.setBrush(QBrush(g))
            border = QColor(*SOFT_RED, 150) if alert else QColor(*BORDER)
            p.setPen(QPen(border, 1.2))
            p.drawRoundedRect(r.adjusted(0.6, 0.6, -0.6, -0.6), radius, radius)

            center = QPointF(r.left() + r.height() / 2, r.center().y())
            if visual == VisualState.DONE and f.pulse_color == "check":
                self._paint_check(p, center, f.pulse)
            else:
                self._paint_logo(p, center, dim=visual == VisualState.LOADING)
            if visual in (VisualState.TRANSCRIBING, VisualState.LOADING, VisualState.GENERATING):
                self._paint_ring(p, center, f.ring_angle, spinning=visual != VisualState.LOADING)
            if f.badge and visual in (VisualState.IDLE, VisualState.LOADING, VisualState.RECORDING,
                                      VisualState.TRANSCRIBING, VisualState.GENERATING):
                self._paint_badge(p, center, f)

            reveal = max(0.0, min(1.0, (f.shape_progress - 0.55) / 0.45))  # conteúdo entra no fim da expansão
            if reveal > 0:
                p.setOpacity(opacity * reveal)
                text = MESSAGES.get(visual) or f.message
                if visual == VisualState.GENERATING:
                    text = GENERATING_TEXT + (" · nuvem" if f.cloud else "")
                if visual == VisualState.RECORDING and not f.message:
                    self._paint_bars(p, r, f)
                    self._paint_timer(p, r, f.timer_text)
                elif visual == VisualState.RECORDING:
                    self._paint_text(p, r, f.message, right_space=58)
                    self._paint_timer(p, r, f.timer_text)
                elif visual == VisualState.DONE:
                    self._paint_text(p, r, f.message or "Colado")  # com aviso (ex.: gerador indisponível), o aviso
                elif text:
                    self._paint_text(p, r, text)
            p.end()

        def _paint_pulse(self, p, r, f, alert) -> None:
            if f.pulse_color == "check" and 0 < f.pulse < 1:
                color, strength = QColor(CHECK), 1.0
            elif (f.pulse_color == "red" and 0 < f.pulse < 1) or alert:
                color, strength = QColor(*SOFT_RED), 0.7
            else:
                return
            t = f.pulse if f.pulse_color else 0.5  # aviso parado: brilho fixo e suave
            grow = 2 + 6 * t
            color.setAlphaF(max(0.0, (1.0 - t) * 0.40 * strength + (0.10 if alert else 0)))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color, 2.4))
            rr = r.adjusted(-grow, -grow, grow, grow)
            p.drawRoundedRect(rr, rr.height() / 2, rr.height() / 2)

        def _paint_idle_halo(self, p, r) -> None:
            """Brilho suave + aro claro logo fora do disco (desenhado ANTES do fundo, que cobre a metade de dentro)."""
            p.setBrush(Qt.NoBrush)
            for grow, width, alpha in ((1.8, 3.2, HALO_GLOW_ALPHA), (0.5, 1.4, HALO_EDGE_ALPHA)):
                if alpha <= 0:
                    continue
                ring = r.adjusted(-grow, -grow, grow, grow)
                p.setPen(QPen(QColor(255, 255, 255, alpha), width))
                p.drawRoundedRect(ring, ring.height() / 2, ring.height() / 2)

        def _paint_logo(self, p, center, dim=False) -> None:
            dpr = p.device().devicePixelRatioF() or 1.0  # do destino (tela ou captura 2x)
            pm = white_logo(LOGO_SIZE, round(dpr * 4) / 4)
            if dim:
                p.save()
                p.setOpacity(p.opacity() * 0.45)
            ratio = pm.devicePixelRatio() or 1.0
            lw, lh = pm.width() / ratio, pm.height() / ratio  # o ícone não é quadrado (672x692)
            p.drawPixmap(QPointF(center.x() - lw / 2, center.y() - lh / 2), pm)
            if dim:
                p.restore()

        def _paint_check(self, p, center, pulse) -> None:
            s = 1.0 + 0.12 * math.sin(math.pi * min(1.0, pulse))  # "pulo" curto
            rad = LOGO_SIZE / 2 * s
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(CHECK))
            p.drawEllipse(center, rad, rad)
            path = QPainterPath(QPointF(center.x() - rad * 0.45, center.y() + rad * 0.02))
            path.lineTo(center.x() - rad * 0.1, center.y() + rad * 0.36)
            path.lineTo(center.x() + rad * 0.5, center.y() - rad * 0.32)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#FFFFFF"), 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)

        def _paint_badge(self, p, center, f) -> None:
            """Selo discreto do Modo B no canto superior direito do isotipo: disco violeta com um brilho de 4 pontas
            (âmbar quando o gerador é de nuvem)."""
            cx, cy, rad = center.x() + 12.5, center.y() - 12.5, 6.2
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(BG_FROM))
            p.drawEllipse(QPointF(cx, cy), rad + 1.7, rad + 1.7)  # contorno escuro: destaca do disco
            p.setBrush(QColor(BADGE_CLOUD if f.cloud else BADGE_PROMPT))
            p.drawEllipse(QPointF(cx, cy), rad, rad)
            star = QPainterPath()
            for i in range(8):
                ang = math.pi / 4 * i - math.pi / 2
                rr = 3.6 if i % 2 == 0 else 1.3
                pt = QPointF(cx + rr * math.cos(ang), cy + rr * math.sin(ang))
                star.moveTo(pt) if i == 0 else star.lineTo(pt)
            star.closeSubpath()
            p.setBrush(QColor("#0B1530"))
            p.drawPath(star)

        def _paint_ring(self, p, center, angle, spinning=True) -> None:
            """Anel ao redor do isotipo. Transcrevendo: arco de ~110°, traço 2,8 px, cabeça #60A5FA
            -> cauda #3B82F6 (que esmaece só até 35%), girando no sentido horário."""
            rad = LOGO_SIZE / 2 + 5.0  # 18 px: entre o isotipo (13) e a borda do círculo (26)
            box = QRectF(center.x() - rad, center.y() - rad, 2 * rad, 2 * rad)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(96, 165, 250, 60), 2.0))
            p.drawEllipse(box)  # trilho discreto
            if not spinning:
                return
            head_c, tail_c = QColor(WAVE_HIGH), QColor(WAVE_LOW)
            head = -angle  # Qt: ângulo positivo = anti-horário; o anel gira no sentido horário
            span = RING_SPAN
            segments = 18
            step = span / segments
            for k in range(segments):
                frac = k / (segments - 1)
                color = QColor(
                    round(head_c.red() + (tail_c.red() - head_c.red()) * frac),
                    round(head_c.green() + (tail_c.green() - head_c.green()) * frac),
                    round(head_c.blue() + (tail_c.blue() - head_c.blue()) * frac),
                )
                color.setAlphaF(1.0 - 0.65 * frac)
                cap = Qt.RoundCap if k in (0, segments - 1) else Qt.FlatCap
                p.setPen(QPen(color, RING_WIDTH, Qt.SolidLine, cap))
                # cada pedaço encosta no próximo (+0,5°) para o arco sair contínuo
                p.drawArc(box, int((head + k * step) * 16), int((step + 0.5) * 16))

        def _paint_bars(self, p, r, f) -> None:
            """Onda que anda da direita para a esquerda: f.bars[0] é a amostra mais antiga (esquerda)."""
            n = len(f.bars) or 1
            x0 = r.left() + r.height() + BAR_X_GAP
            x1 = r.right() - BAR_RIGHT_SPACE
            if x1 - x0 < n * 3:
                return
            step = (x1 - x0) / n
            bw = min(BAR_MAX_W, step * 0.55)
            max_h = bar_max_height(r.height())
            g = QLinearGradient(QPointF(0, r.center().y() + max_h / 2), QPointF(0, r.center().y() - max_h / 2))
            g.setColorAt(0.0, QColor(WAVE_LOW))
            g.setColorAt(1.0, QColor(WAVE_HIGH))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(g))
            for i, level in enumerate(f.bars):
                h = bar_height(level, max_h)
                x = x0 + i * step + (step - bw) / 2
                p.drawRoundedRect(QRectF(x, r.center().y() - h / 2, bw, h), bw / 2, bw / 2)

        def _font(self, px: int, bold: bool = False) -> QFont:
            font = QFont("Segoe UI")
            font.setPixelSize(px)
            font.setWeight(QFont.DemiBold if bold else QFont.Normal)
            return font

        def _paint_timer(self, p, r, text) -> None:
            p.setFont(self._font(12, bold=True))
            p.setPen(QColor(255, 255, 255, 190))
            box = QRectF(r.right() - 60, r.top(), 44, r.height())
            p.drawText(box, Qt.AlignVCenter | Qt.AlignRight, text)

        def _paint_text(self, p, r, text, muted=False, right_space=18) -> None:
            p.setFont(self._font(12, bold=not muted))
            p.setPen(QColor(255, 255, 255, 170 if muted else 235))
            box = QRectF(r.left() + r.height() + 2, r.top(), r.width() - r.height() - 2 - right_space, r.height())
            if p.fontMetrics().horizontalAdvance(text) > box.width():
                # T47b: mensagem longa (ex.: "gerador indisponível — colei a transcrição") quebra em até 2 linhas,
                # numa fonte menor, em vez de ser cortada; só o que passar de 2 linhas leva "…"
                p.setFont(self._font(11, bold=False))
                fm = p.fontMetrics()
                text = fm.elidedText(text, Qt.ElideRight, int(box.width() * 1.85))
                p.drawText(box, int(Qt.AlignVCenter | Qt.AlignLeft | Qt.TextWordWrap), text)
                return
            elided = p.fontMetrics().elidedText(text, Qt.ElideRight, int(box.width()))
            p.drawText(box, Qt.AlignVCenter | Qt.AlignLeft, elided)

        # --- mouse ------------------------------------------------------------------
        def _hit(self, pos) -> bool:
            r = self.shape_rect()
            path = QPainterPath()
            path.addRoundedRect(r, r.height() / 2, r.height() / 2)
            return path.contains(QPointF(pos))

        @staticmethod
        def _double_click_ms() -> int:
            """Intervalo de duplo clique do Windows (o usuário pode mudá-lo a qualquer momento)."""
            try:
                from PySide6.QtWidgets import QApplication

                return int(QApplication.doubleClickInterval())
            except Exception:  # noqa: BLE001
                return 500

        def app_state(self) -> str:
            """Estado do app para a ClickMachine: o do controller (`state_source`) ou, sem ele, o visual."""
            if self.state_source is not None:
                try:
                    state = self.state_source()
                    if isinstance(state, str):
                        return state
                except Exception:  # noqa: BLE001 - estado ilegível: cai no visual
                    pass
            return {VisualState.RECORDING: "recording", VisualState.TRANSCRIBING: "transcribing"}.get(
                self.visual(), "idle")

        @staticmethod
        def _gpos(e) -> tuple[int, int]:
            p = e.globalPosition().toPoint()  # global: a janela anda durante o arraste, a posição local não serve
            return p.x(), p.y()

        def _abort_press(self) -> None:
            """Um press anterior nunca teve release (janela perdeu o mouse): recomeça do zero."""
            self._press = None
            self._dragging = False
            self._machine.reset()

        def mousePressEvent(self, e) -> None:
            if not self._hit(e.position()):
                e.ignore()
                return
            if e.button() == Qt.LeftButton:
                if self._press is not None:
                    self._abort_press()
                self._press = (e.globalPosition().toPoint(), self.pos())
                self._dragging = False
                self._machine.double_click_ms = self._double_click_ms()
                self._machine.press(self._gpos(e), self.clock(), "left")
            elif e.button() == Qt.RightButton:  # o menu não passa pela máquina: não atrapalha um duplo clique
                self.menu_requested.emit(e.globalPosition().toPoint())

        def mouseMoveEvent(self, e) -> None:
            if self._press is None:
                return
            start, win = self._press
            if self._machine.move(self._gpos(e), self.clock()) == Action.DRAG_START:
                self._dragging = True
            if self._dragging:
                self.move(win + (e.globalPosition().toPoint() - start))

        def mouseReleaseEvent(self, e) -> None:
            if self._press is None or e.button() != Qt.LeftButton:
                return
            start, win = self._press
            self._press = None
            was_dragging = self._dragging
            act = self._machine.release(self._gpos(e), self.clock(), self.app_state(), "left")
            self._dragging = False
            if act == Action.DRAG_END or was_dragging:
                if not was_dragging:  # soltou longe sem nenhum movimento antes: leva a janela até lá
                    self.move(win + (e.globalPosition().toPoint() - start))
                self.moved.emit(self.x(), self.y())  # a posição é salva pelo QtUi, como antes
            elif act in (Action.START, Action.STOP, Action.DISCARD_AND_OPEN, Action.OPEN_RECENT):
                self.action.emit(act.value)

    return BubbleWidget


_WIDGET_CLASS = None


def BubbleWidget(*args, **kwargs):  # noqa: N802 - fábrica com cara de classe (Qt só ao instanciar)
    global _WIDGET_CLASS
    if _WIDGET_CLASS is None:
        _WIDGET_CLASS = _make_widget_class()
    return _WIDGET_CLASS(*args, **kwargs)
