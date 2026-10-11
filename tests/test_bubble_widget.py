"""Testes da janela da bolha (T21). Qt em modo offscreen: nada aparece na tela."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))

import pytest  # noqa: E402

from hear_ai.ui.bubble import (BOTTOM_MARGIN, PAD, WIN_H, WIN_W, BubbleDriver, default_position,  # noqa: E402
                               load_bubble_state, restore_position, save_bubble_state)
from hear_ai.ui.bubble_model import AppState, BubbleModel, VisualState  # noqa: E402


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


# --- posição (sem Qt) ----------------------------------------------------------------------

def test_posicao_padrao_centro_baixo_48px_acima_da_barra():
    x, y = default_position((0, 0, 1920, 1040))  # 1080 - barra de 40
    assert x == (1920 - WIN_W) // 2
    forma_baixo = y + PAD + 52
    assert forma_baixo == 1040 - BOTTOM_MARGIN


def test_posicao_padrao_em_dpi_150_e_monitor_a_esquerda():
    # 150%: o Qt entrega coordenadas lógicas (2560x1440 físico -> 1707x928 lógico)
    x, y = default_position((0, 0, 1707, 928))
    assert 0 <= x and x + WIN_W <= 1707 and y + PAD + 52 == 928 - BOTTOM_MARGIN
    x2, y2 = default_position((-1920, 0, 1920, 1040))  # monitor secundário à esquerda
    assert -1920 <= x2 < 0


TELAS = [("\\\\.\\DISPLAY1", (0, 0, 1920, 1040)), ("\\\\.\\DISPLAY2", (1920, 0, 2560, 1400))]


def test_restaura_posicao_salva_na_tela_que_existe():
    salvo = {"x": 2500, "y": 600, "screen": "\\\\.\\DISPLAY2"}
    assert restore_position(salvo, TELAS, TELAS[0][1]) == (2500, 600, "\\\\.\\DISPLAY2")


@pytest.mark.parametrize("salvo", [
    {"x": 2500, "y": 600, "screen": "\\\\.\\DISPLAY3"},  # monitor desligado
    {"x": 1900, "y": 600, "screen": "\\\\.\\DISPLAY1"},  # forma não cabe mais (resolução/DPI mudou)
    {"x": "a", "y": 1, "screen": "\\\\.\\DISPLAY1"},
    {"x": True, "y": 1, "screen": "\\\\.\\DISPLAY1"},
    None,
    [],
])
def test_tela_sumida_ou_dado_ruim_volta_ao_padrao_na_principal(salvo):
    x, y, nome = restore_position(salvo, TELAS, TELAS[0][1])
    assert (x, y) == default_position(TELAS[0][1]) and nome == "\\\\.\\DISPLAY1"


def test_salvar_e_carregar_posicao(tmp_path):
    arq = tmp_path / "sub" / "bolha.json"
    assert load_bubble_state(arq) is None
    assert save_bubble_state(arq, 10, 20, "\\\\.\\DISPLAY1")
    assert load_bubble_state(arq) == {"x": 10, "y": 20, "screen": "\\\\.\\DISPLAY1"}
    arq.write_text("{quebrado", encoding="utf-8")
    assert load_bubble_state(arq) is None


# --- driver de eventos (sem Qt) --------------------------------------------------------------

class Agenda:
    def __init__(self):
        self.itens = []

    def __call__(self, delay, fn):
        self.itens.append((delay, fn))

    def rodar(self):
        itens, self.itens = self.itens, []
        for _d, fn in itens:
            fn()


def driver(clock=None):
    clock = clock or Clock()
    model = BubbleModel(clock=clock)
    agenda = Agenda()
    mudou = []
    d = BubbleDriver(model, clock=clock, schedule=agenda, on_change=lambda: mudou.append(1))
    return d, model, agenda, clock, mudou


def test_aviso_saved_segura_o_idle_ate_dar_para_ler():
    d, model, agenda, clock, mudou = driver()
    d.state("saved")
    d.state("idle")  # o Controller manda idle logo depois do aviso
    assert d.visual() == VisualState.SAVED  # ainda mostrando "Texto salvo — Ctrl+V"
    (delay, _fn), = agenda.itens
    assert delay == pytest.approx(BubbleModel.SAVED_DURATION)
    clock.t += delay
    agenda.rodar()
    assert d.visual() == VisualState.IDLE and mudou


def test_nova_gravacao_cancela_o_idle_adiado():
    d, model, agenda, clock, _ = driver()
    d.state("error")
    d.state("idle")
    d.state("recording")
    agenda.rodar()  # o idle antigo chega atrasado: é ignorado
    assert d.visual() == VisualState.RECORDING


def test_colado_mostra_check_e_depois_volta_ao_repouso():
    d, model, agenda, clock, _ = driver()
    d.state("recording")
    d.state("transcribing")
    d.pasted()
    d.state("idle")
    assert d.visual() == VisualState.DONE
    assert agenda.itens and agenda.itens[0][0] == pytest.approx(BubbleModel.DONE_DURATION)
    agenda.rodar()
    assert d.visual() == VisualState.IDLE


def test_idle_sem_aviso_e_imediato_e_eventos_desconhecidos_sao_ignorados():
    d, model, agenda, clock, mudou = driver()
    d.state("recording")
    d.state("idle")
    assert d.visual() == VisualState.IDLE and agenda.itens == []
    n = len(mudou)
    d.state("qualquer")
    assert len(mudou) == n


def test_cancelar_e_carregando_e_aviso_de_limite():
    d, model, agenda, clock, _ = driver()
    d.loading(True)
    assert d.visual() == VisualState.LOADING
    d.loading(False)
    d.state("recording")
    d.state("limit_warning")
    assert model.frame(clock.t).message == "1 min para o limite"
    d.cancelled()
    assert d.visual() == VisualState.IDLE


# --- a janela (Qt offscreen) --------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def bolha(qapp):
    from hear_ai.ui.bubble import BubbleWidget

    clock = Clock()
    nivel = {"v": 0.0}
    w = BubbleWidget(clock=clock, level_source=lambda: nivel["v"])
    yield w, clock, nivel
    w.timer.stop()
    w.deleteLater()


def avancar(w, clock, segundos, passos=30):
    for _ in range(passos):
        clock.t += segundos / passos
        w._tick()


def test_flags_da_janela_sem_foco_sem_borda_fora_do_alt_tab(bolha):
    from PySide6.QtCore import Qt

    w, _, _ = bolha
    flags = w.windowFlags()
    for f in (Qt.FramelessWindowHint, Qt.WindowStaysOnTopHint, Qt.Tool, Qt.WindowDoesNotAcceptFocus):
        assert flags & f
    assert w.testAttribute(Qt.WA_TranslucentBackground)
    assert w.testAttribute(Qt.WA_ShowWithoutActivating)
    assert (w.width(), w.height()) == (WIN_W, WIN_H)


def test_frames_definem_a_forma_circulo_e_pilula(bolha):
    w, clock, _ = bolha
    w.kick()
    r = w.shape_rect()
    assert (r.width(), r.height()) == (52, 52)
    assert r.center().x() == pytest.approx(WIN_W / 2)
    w.driver.state("recording")
    avancar(w, clock, 0.3)
    r = w.shape_rect()
    assert r.width() == pytest.approx(220) and r.height() == 52
    assert r.center().x() == pytest.approx(WIN_W / 2)  # cresce para os dois lados


def test_timer_so_roda_enquanto_anima_ou_grava(bolha):
    w, clock, nivel = bolha
    w.kick()
    avancar(w, clock, 0.1)
    assert not w.timer.isActive()  # repouso: CPU ~0
    w.driver.state("recording")
    assert w.timer.isActive()
    avancar(w, clock, 2.05)  # silêncio, mas o cronômetro precisa andar
    assert w.timer.isActive() and w.frame().timer_text == "00:02"
    w.driver.state("idle")
    avancar(w, clock, 1.0)
    assert not w.timer.isActive()


def test_volume_do_microfone_move_as_barras(bolha):
    w, clock, nivel = bolha
    w.driver.state("recording")
    avancar(w, clock, 0.3)
    assert max(w.frame().bars) < 0.01
    nivel["v"] = 0.8
    avancar(w, clock, 0.3)
    assert max(w.frame().bars) > 0.5


def test_fonte_de_volume_com_erro_nao_quebra(qapp):
    from hear_ai.ui.bubble import BubbleWidget

    def quebra():
        raise RuntimeError("microfone sumiu")

    clock = Clock()
    w = BubbleWidget(clock=clock, level_source=quebra)
    w.driver.state("recording")
    avancar(w, clock, 0.2)
    assert max(w.frame().bars) == 0
    w.timer.stop()


def test_desenha_todos_os_estados_e_cantos_ficam_transparentes(bolha):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPixmap

    w, clock, nivel = bolha
    passos = [lambda: None, lambda: w.driver.loading(True), lambda: w.driver.loading(False),
              lambda: w.driver.state("recording"), lambda: nivel.update(v=0.6),
              lambda: w.driver.state("transcribing"), w.driver.pasted, lambda: w.driver.state("saved"),
              lambda: w.driver.state("error"), lambda: w.driver.state("limit_warning")]
    for passo in passos:
        passo()
        avancar(w, clock, 0.25, passos=8)
        pm = QPixmap(w.size())
        pm.fill(Qt.transparent)
        w.render(pm, QPoint(0, 0))
        img = pm.toImage()
        assert img.pixelColor(1, 1).alpha() == 0  # fora da forma: transparente (clique passa)
        c = img.pixelColor(int(w.shape_rect().left()) + 6, WIN_H // 2)
        assert c.alpha() > 0  # a forma foi desenhada


def test_clique_arrasto_e_botao_direito(bolha):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    w, clock, _ = bolha
    w.kick()
    w.move(300, 300)
    eventos = []
    # T42b: o clique passou pela ClickMachine; o sinal `clicked` (legado) não é mais emitido pelo mouse e o
    # clique simples com o app parado vira a ação "start" (as ações em si: tests/test_cliques_bolha.py)
    w.action.connect(lambda nome: eventos.append(nome))
    w.moved.connect(lambda x, y: eventos.append(("moveu", x, y)))
    w.menu_requested.connect(lambda pos: eventos.append("menu"))
    centro = QPoint(WIN_W // 2, WIN_H // 2)
    QTest.mouseClick(w, Qt.LeftButton, Qt.NoModifier, centro)
    assert eventos == ["start"]
    QTest.mousePress(w, Qt.LeftButton, Qt.NoModifier, centro)
    QTest.mouseMove(w, centro + QPoint(40, -10))
    QTest.mouseRelease(w, Qt.LeftButton, Qt.NoModifier, centro + QPoint(40, -10))
    assert eventos[-1][0] == "moveu" and eventos[1:] == [eventos[-1]]  # arrastar não gera ação
    QTest.mouseClick(w, Qt.RightButton, Qt.NoModifier, centro)
    assert eventos[-1] == "menu"
    n = len(eventos)
    QTest.mouseClick(w, Qt.LeftButton, Qt.NoModifier, QPoint(2, 2))  # fora da forma
    assert len(eventos) == n


def test_logo_branco_tem_tinta_e_sem_arquivo_nao_quebra(qapp, monkeypatch):
    from hear_ai.ui import logo

    pm = logo.white_logo(26, 2.0)
    img = pm.toImage()
    assert max(img.width(), img.height()) == 52 and min(img.width(), img.height()) >= 48  # ícone 672x692
    cores = {img.pixelColor(x, y).name() for x in range(0, img.width(), 3) for y in range(0, img.height(), 3)
             if img.pixelColor(x, y).alpha() > 200}
    assert cores == {"#ffffff"}  # só branco
    logo.white_logo.cache_clear()
    monkeypatch.setattr(logo, "ICON_PATH", logo.ICON_PATH.with_name("nao_existe.png"))
    vazio = logo.white_logo(26, 1.0)
    assert vazio.width() == 26 and vazio.toImage().pixelColor(5, 5).alpha() == 0
    logo.white_logo.cache_clear()


def test_modelo_da_t20_continua_intacto():
    """A janela não muda o modelo: o estado do Controller chega por AppState."""
    model = BubbleModel(clock=Clock())
    model.on_state(AppState.RECORDING)
    assert model._visual == VisualState.RECORDING


# --- Esc só durante a gravação (RegisterHotKey) ----------------------------------------------

class FakeEscApi:
    def __init__(self, ok=True):
        self.ok = ok
        self.eventos = []

    def register(self):
        self.eventos.append("reg")
        return (True, 0) if self.ok else (False, 1409)

    def unregister(self):
        self.eventos.append("unreg")


def test_esc_registrado_so_enquanto_grava():
    from hear_ai.ui.qt_app import ESC_HOTKEY_ID, WM_HOTKEY, EscGuard

    cancelou = []
    api = FakeEscApi()
    g = EscGuard(lambda: cancelou.append(1), api)
    assert g.handle_message(WM_HOTKEY, ESC_HOTKEY_ID) is False  # fora da gravação: não é nosso
    g.set_recording(True)
    g.set_recording(True)  # idempotente
    assert api.eventos == ["reg"] and g.active
    assert g.handle_message(WM_HOTKEY, 999) is False  # outro atalho (ex.: o de ditar)
    assert g.handle_message(0x0400, ESC_HOTKEY_ID) is False
    assert g.handle_message(WM_HOTKEY, ESC_HOTKEY_ID) is True and cancelou == [1]
    g.set_recording(False)
    assert api.eventos == ["reg", "unreg"] and not g.active


def test_esc_ocupado_por_outro_programa_avisa_uma_vez(caplog):
    from hear_ai.ui.qt_app import EscGuard

    g = EscGuard(lambda: None, FakeEscApi(ok=False))
    with caplog.at_level("WARNING", logger="hear_ai.ui"):
        g.set_recording(True)
        g.set_recording(False)
        g.set_recording(True)
    assert not g.active and caplog.text.count("Esc para cancelar indisponível") == 1


def test_esc_sem_api_nao_faz_nada():
    from hear_ai.ui.qt_app import EscGuard

    g = EscGuard(lambda: None, None)
    g.set_recording(True)
    assert not g.active


# --- ponte entre threads, bandeja Qt, app Qt ------------------------------------------------

def test_ponte_executa_na_thread_do_qt(qapp):
    import threading

    from PySide6.QtCore import QCoreApplication

    from hear_ai.ui.qt_app import _make_qt_classes

    Bridge, _ = _make_qt_classes()
    b = Bridge()
    onde = []
    t = threading.Thread(target=lambda: b.post(lambda: onde.append(threading.current_thread())))
    t.start()
    t.join()
    for _ in range(20):
        QCoreApplication.processEvents()
        if onde:
            break
    assert onde == [threading.main_thread()]


def _tray(**kw):
    from types import SimpleNamespace

    from hear_ai.config import Config
    from hear_ai.tray import TrayApp

    return TrayApp(SimpleNamespace(state="idle"), Config(), on_quit=lambda: None, **kw)


def test_itens_do_menu_iguais_aos_da_v1_e_historico_na_bolha():
    t = _tray(on_open_history=lambda: None)
    textos = [i[0] if i else "---" for i in t.menu_items() if i is None or i[3]]
    assert textos == ["Modo: Segurar — ctrl+alt+h", "Mudar para Alternar", "Abrir configurações",
                      "Abrir pasta de logs", "---", "Sair"]
    t.set_orphans(2)
    t.set_error("x")
    textos = [i[0] for i in t.menu_items(with_history=True) if i and i[3]]
    assert "⚠ x" in textos and "Recuperar gravação (2)" in textos and "Abrir histórico" in textos


def test_bandeja_qt_icone_dica_e_menu(qapp):
    from hear_ai.tray import QtTrayIcon

    sairam = []
    t = _tray()
    icon = t.attach_qt(app_quit=lambda: sairam.append(1))
    assert isinstance(icon, QtTrayIcon)
    assert icon.qt.toolTip() == t.title() and not icon.qt.icon().isNull()
    acoes = [a.text() for a in icon.menu.actions() if not a.isSeparator()]
    assert acoes[0].startswith("Modo:") and acoes[-1] == "Sair"
    t.set_orphans(1)
    assert any(a.text() == "Recuperar gravação (1)" for a in icon.menu.actions())
    mudar = next(a for a in icon.menu.actions() if a.text() == "Mudar para Alternar")
    t.config_path = None  # não grava config em lugar nenhum neste teste
    t.toggle_mode = lambda: sairam.append("modo")
    icon.update_menu()
    next(a for a in icon.menu.actions() if a.text() == "Mudar para Alternar").trigger()
    assert "modo" in sairam
    next(a for a in icon.menu.actions() if a.text() == "Sair").trigger()
    assert 1 in sairam and not icon.qt.isVisible()
    assert mudar is not None


def test_qtui_liga_bolha_bandeja_e_esc(qapp, tmp_path):
    import threading
    from types import SimpleNamespace

    from PySide6.QtCore import QCoreApplication

    from hear_ai.ui.qt_app import QtUi

    cancelados = []
    controller = SimpleNamespace(state="idle", cancel=lambda: cancelados.append(1))
    t = _tray()
    api = FakeEscApi()
    estado_arq = tmp_path / "bolha.json"
    ui = QtUi(controller, t, level_source=lambda: 0.5, bubble_state_path=estado_arq, esc_api=api,
              show_bubble=False)

    def processar():
        for _ in range(10):
            QCoreApplication.processEvents()

    # posição padrão na tela (offscreen) e lembrada depois de arrastar
    x0, y0 = ui.bubble.x(), ui.bubble.y()
    ui.bubble.move(x0 - 30, y0 - 20)
    ui._on_bubble_moved(x0 - 30, y0 - 20)
    assert estado_arq.exists()
    ui.bubble.move(0, 0)
    ui.place_bubble()
    assert (ui.bubble.x(), ui.bubble.y()) == (x0 - 30, y0 - 20)

    th = threading.Thread(target=lambda: ui.on_state("recording"))  # vem da thread do controller
    th.start()
    th.join()
    processar()
    assert ui.bubble.visual().value == "recording" and api.eventos == ["reg"]
    assert t.notice is None
    ui.esc.handle_message(0x0312, 0x4845)  # Esc
    processar()
    for _ in range(50):
        if cancelados:
            break
        threading.Event().wait(0.01)
    assert cancelados == [1] and api.eventos == ["reg", "unreg"]
    assert ui.bubble.visual().value == "idle"

    ui.on_state("saved")
    ui.set_orphans(3)
    processar()
    assert t.orphans == 3 and t.notice == "saved"
    ui.set_conflict("ctrl+alt+h já está em uso")
    processar()
    assert "Atalho em conflito" in t.title()
    ui.bubble.timer.stop()
    t._icon.qt.hide()


def test_ui_feedback_avisa_colado():
    from hear_ai.__main__ import UiFeedback

    eventos = []
    base = type("F", (), {k: (lambda self, k=k: eventos.append(k)) for k in ("start", "stop", "error", "done")})()
    fb = UiFeedback(base, on_done=lambda: eventos.append("bolha"))
    fb.start()
    fb.stop()
    fb.error()
    fb.done()
    assert eventos == ["start", "stop", "error", "done", "bolha"]

    def quebra():
        raise RuntimeError("ui fechada")

    UiFeedback(base, on_done=quebra).done()  # não derruba o controller


# --- rodada 1 (revisão 016): transcrevendo = círculo com anel, sem texto ---------------------

def test_transcrevendo_e_circulo_sem_texto_e_com_anel(bolha):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPixmap

    w, clock, nivel = bolha
    w.driver.state("recording")
    avancar(w, clock, 0.3)
    assert w.shape_rect().width() == pytest.approx(220)
    w.driver.state("transcribing")
    avancar(w, clock, 0.3)
    r = w.shape_rect()
    assert (r.width(), r.height()) == (52, 52)  # voltou a círculo

    textos, aneis = [], []
    w._paint_text = lambda *a, **k: textos.append(a)
    original_ring = w._paint_ring
    w._paint_ring = lambda *a, **k: (aneis.append(k.get("spinning", a[-1] if len(a) > 3 else True)),
                                     original_ring(*a, **k))
    pm = QPixmap(w.size())
    pm.fill(Qt.transparent)
    w.render(pm, QPoint(0, 0))
    assert textos == []  # nenhum texto em "transcrevendo"
    assert aneis == [True]  # anel girando desenhado

    # o arco azul aparece dentro do círculo (pixels azuis vivos ao redor do isotipo)
    img = pm.toImage()
    cx, cy = r.center().x(), r.center().y()
    azuis = 0
    import math as _m
    for k in range(72):
        a = _m.radians(k * 5)
        c = img.pixelColor(int(cx + 18 * _m.cos(a)), int(cy + 18 * _m.sin(a)))
        if c.blue() > 200 and c.red() < 120 and c.alpha() > 200:
            azuis += 1
    assert 12 <= azuis <= 30  # ~110° de 360° (72 amostras) com folga para a ponta arredondada


def test_anel_gira_suave():
    from hear_ai.ui.bubble import RING_SPAN, RING_WIDTH

    assert 90 <= RING_SPAN <= 120 and 2.5 <= RING_WIDTH <= 3.0
    clock = Clock()
    model = BubbleModel(clock=clock)
    model.on_state(AppState.TRANSCRIBING)
    angulos = []
    for _ in range(30):  # 0,5 s a 60 Hz
        clock.t += 1 / 60
        angulos.append(model.frame(clock.t).ring_angle)
    passos = [b - a for a, b in zip(angulos, angulos[1:]) if b > a]
    assert passos and max(passos) <= 4.0  # ≤ 4° por quadro: sem saltos
