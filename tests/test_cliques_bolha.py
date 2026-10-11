"""T42b — cliques na bolha: ClickMachine ligada ao BubbleWidget e ao QtUi/Controller. Qt offscreen.

Eventos de mouse sintéticos (`QTest`), relógio simulado na bolha, config/histórico/spool TEMPORÁRIOS, Esc e Win32
falsos. Nada de teclas reais, rede, MessageBox nem `HEAR_AI_REAL_IO`. A lógica de decisão em si (estado × tempo ×
movimento) é testada em tests/test_click_machine.py; aqui se prova a LIGAÇÃO: o que o mouse faz com o widget, e o que
o app faz com cada ação.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from hear_ai.ui.bubble import DRAG_THRESHOLD, WIN_H, WIN_W  # noqa: E402

CLIQUE_S = 0.08  # tempo típico entre apertar e soltar
ENTRE_CLIQUES_S = 0.12  # soltar o 1º -> soltar o 2º de um duplo clique costuma ser ~150-250 ms
SEGREDO = "texto ditado de teste"


class Relogio:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def intervalo_de_duplo_clique(qapp):
    """O intervalo do Windows vale 500 ms nos testes (e é restaurado no fim)."""
    from PySide6.QtWidgets import QApplication

    antes = QApplication.doubleClickInterval()
    QApplication.setDoubleClickInterval(500)
    yield
    QApplication.setDoubleClickInterval(antes)


def centro():
    from PySide6.QtCore import QPoint

    return QPoint(WIN_W // 2, WIN_H // 2)


def esperar(cond, timeout: float = 3.0) -> bool:
    from PySide6.QtCore import QCoreApplication

    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.003)
    QCoreApplication.processEvents()
    return cond()


def bolha_clicar(w, relogio, depois: float = 0.0, pos=None, duracao: float = CLIQUE_S) -> None:
    """Um clique esquerdo (aperta, `duracao` depois solta) e, em seguida, `depois` s de relógio."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    pos = pos or centro()
    QTest.mousePress(w, Qt.LeftButton, Qt.NoModifier, pos)
    relogio.t += duracao
    QTest.mouseRelease(w, Qt.LeftButton, Qt.NoModifier, pos)
    relogio.t += depois


# --- a bolha sozinha: eventos de mouse -> sinal `action` ----------------------------------------------------

@pytest.fixture
def bolha(qapp):
    """BubbleWidget com estado do app controlável e todos os sinais registrados."""
    from hear_ai.ui.bubble import BubbleWidget

    relogio = Relogio()
    estado = {"v": "idle"}
    w = BubbleWidget(clock=relogio, state_source=lambda: estado["v"])
    w.move(300, 300)
    eventos = []
    w.action.connect(lambda nome: eventos.append(nome))
    w.moved.connect(lambda x, y: eventos.append(("moveu", x, y)))
    w.menu_requested.connect(lambda pos: eventos.append("menu"))
    yield SimpleNamespace(w=w, relogio=relogio, estado=estado, eventos=eventos)
    w.timer.stop()
    w.deleteLater()


def test_clique_simples_parado_gera_start(bolha):
    bolha_clicar(bolha.w, bolha.relogio)
    assert bolha.eventos == ["start"]


def test_clique_simples_gravando_gera_stop(bolha):
    bolha.estado["v"] = "recording"
    bolha_clicar(bolha.w, bolha.relogio)
    assert bolha.eventos == ["stop"]


def test_clique_simples_durante_a_transcricao_nao_faz_nada(bolha):
    bolha.estado["v"] = "transcribing"
    bolha_clicar(bolha.w, bolha.relogio)
    assert bolha.eventos == []


def test_duplo_clique_parado_descarta_e_abre_recentes(bolha):
    bolha_clicar(bolha.w, bolha.relogio, depois=ENTRE_CLIQUES_S)
    bolha.estado["v"] = "recording"  # o app já começou a gravar com o 1º clique
    bolha_clicar(bolha.w, bolha.relogio)
    assert bolha.eventos == ["start", "discard_and_open"]


def test_duplo_clique_vale_mesmo_se_o_microfone_ainda_esta_abrindo(bolha):
    """O begin() pode demorar: no 2º clique o estado visível ainda é "idle" e mesmo assim é duplo clique."""
    bolha_clicar(bolha.w, bolha.relogio, depois=ENTRE_CLIQUES_S)
    bolha_clicar(bolha.w, bolha.relogio)  # estado continua "idle"
    assert bolha.eventos == ["start", "discard_and_open"]


def test_clique_para_parar_mais_clique_extra_so_abre_recentes(bolha):
    bolha.estado["v"] = "recording"
    bolha_clicar(bolha.w, bolha.relogio, depois=ENTRE_CLIQUES_S)
    bolha.estado["v"] = "transcribing"
    bolha_clicar(bolha.w, bolha.relogio)
    assert bolha.eventos == ["stop", "open_recent"]  # nunca "discard": a transcrição segue


def test_duplo_clique_durante_a_transcricao_abre_recentes(bolha):
    bolha.estado["v"] = "transcribing"
    bolha_clicar(bolha.w, bolha.relogio, depois=ENTRE_CLIQUES_S)
    bolha_clicar(bolha.w, bolha.relogio)
    assert bolha.eventos == ["open_recent"]


def test_dois_cliques_lentos_sao_dois_cliques_simples(bolha):
    bolha_clicar(bolha.w, bolha.relogio, depois=0.8)  # passou do intervalo de duplo clique
    bolha.estado["v"] = "recording"
    bolha_clicar(bolha.w, bolha.relogio)
    assert bolha.eventos == ["start", "stop"]


def test_triplo_clique_acidental_e_engolido(bolha):
    bolha_clicar(bolha.w, bolha.relogio, depois=ENTRE_CLIQUES_S)
    bolha_clicar(bolha.w, bolha.relogio, depois=ENTRE_CLIQUES_S)
    bolha_clicar(bolha.w, bolha.relogio, depois=ENTRE_CLIQUES_S)
    assert bolha.eventos == ["start", "discard_and_open"]


def test_intervalo_de_duplo_clique_vem_do_windows(bolha):
    from PySide6.QtWidgets import QApplication

    QApplication.setDoubleClickInterval(200)  # o usuário configurou um duplo clique bem rápido
    bolha_clicar(bolha.w, bolha.relogio, depois=0.3)
    bolha.estado["v"] = "recording"
    bolha_clicar(bolha.w, bolha.relogio)
    assert bolha.eventos == ["start", "stop"]  # 300 ms > 200 ms: não é duplo
    QApplication.setDoubleClickInterval(900)
    bolha.estado["v"] = "idle"
    bolha.relogio.t += 5
    bolha.eventos.clear()
    bolha_clicar(bolha.w, bolha.relogio, depois=0.3)
    bolha.estado["v"] = "recording"
    bolha_clicar(bolha.w, bolha.relogio)
    assert bolha.eventos == ["start", "discard_and_open"]  # 300 ms < 900 ms: é duplo


def test_arrastar_move_a_janela_salva_a_posicao_e_nao_grava(bolha):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    w = bolha.w
    QTest.mousePress(w, Qt.LeftButton, Qt.NoModifier, centro())
    QTest.mouseMove(w, centro() + QPoint(40, -10))
    assert w._dragging is True  # o vigia da T40 lê isto
    antes = (w.x(), w.y())
    assert antes != (300, 300)
    QTest.mouseRelease(w, Qt.LeftButton, Qt.NoModifier, centro() + QPoint(40, -10))
    assert w._dragging is False
    assert bolha.eventos == [("moveu", antes[0], antes[1])]  # só `moved`: nenhuma ação, nada de gravar


def test_movimento_abaixo_do_limite_ainda_e_clique(bolha):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    w = bolha.w
    QTest.mousePress(w, Qt.LeftButton, Qt.NoModifier, centro())
    QTest.mouseMove(w, centro() + QPoint(DRAG_THRESHOLD // 2, 1))  # 3 px < 4 px
    QTest.mouseRelease(w, Qt.LeftButton, Qt.NoModifier, centro() + QPoint(DRAG_THRESHOLD // 2, 1))
    assert bolha.eventos == ["start"] and (w.x(), w.y()) == (300, 300)


def test_arrastar_logo_apos_um_clique_nao_vira_duplo_clique(bolha):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    w = bolha.w
    bolha_clicar(w, bolha.relogio, depois=ENTRE_CLIQUES_S)  # começou a gravar
    bolha.estado["v"] = "recording"
    QTest.mousePress(w, Qt.LeftButton, Qt.NoModifier, centro())
    QTest.mouseMove(w, centro() + QPoint(60, 0))
    QTest.mouseRelease(w, Qt.LeftButton, Qt.NoModifier, centro() + QPoint(60, 0))
    assert bolha.eventos[0] == "start" and bolha.eventos[1][0] == "moveu" and len(bolha.eventos) == 2


def test_botao_direito_so_pede_o_menu_e_nao_atrapalha_o_duplo_clique(bolha):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    w = bolha.w
    bolha_clicar(w, bolha.relogio, depois=0.03)
    QTest.mouseClick(w, Qt.RightButton, Qt.NoModifier, centro())
    bolha.relogio.t += 0.03
    bolha_clicar(w, bolha.relogio)
    assert bolha.eventos == ["start", "menu", "discard_and_open"]


def test_clique_fora_da_forma_nao_chega_na_maquina(bolha):
    from PySide6.QtCore import QPoint

    bolha_clicar(bolha.w, bolha.relogio, pos=QPoint(2, 2))  # canto transparente da janela
    bolha_clicar(bolha.w, bolha.relogio, depois=ENTRE_CLIQUES_S, pos=QPoint(2, 2))
    assert bolha.eventos == []
    bolha_clicar(bolha.w, bolha.relogio)  # e um clique de verdade logo depois continua sendo o 1º
    assert bolha.eventos == ["start"]


def test_press_sem_release_nao_deixa_a_maquina_presa_em_arrastar(bolha):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    w = bolha.w
    QTest.mousePress(w, Qt.LeftButton, Qt.NoModifier, centro())
    QTest.mouseMove(w, centro() + QPoint(40, 0))
    assert w._dragging is True
    QTest.mousePress(w, Qt.LeftButton, Qt.NoModifier, centro())  # o release do arraste nunca chegou
    assert w._dragging is False
    bolha.relogio.t += CLIQUE_S
    QTest.mouseRelease(w, Qt.LeftButton, Qt.NoModifier, centro())
    assert bolha.eventos == ["start"]  # recuperou: este é um clique normal


# --- foco: a janela transparente recebe o mouse sem ser ativada ----------------------------------------------

def test_flags_de_foco_preservadas_o_mouse_chega_e_o_clique_nunca_ativa_a_bolha(bolha):
    """LIMITE DO OFFSCREEN (medido): o plugin `offscreen` ativa a janela já no `show()`, com ou sem
    `WindowDoesNotAcceptFocus`/`WA_ShowWithoutActivating`, então ele não prova que o Windows não rouba o foco.
    O que dá para provar aqui: as flags continuam, o mouse chega COM elas, e o tratamento do clique/arraste nunca
    pede ativação, foco ou `raise_` para a bolha. A prova no Windows real é o teste manual de docs/CLIQUES.md."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    w = bolha.w
    for flag in (Qt.FramelessWindowHint, Qt.WindowStaysOnTopHint, Qt.Tool, Qt.WindowDoesNotAcceptFocus):
        assert w.windowFlags() & flag
    assert w.testAttribute(Qt.WA_ShowWithoutActivating) and w.testAttribute(Qt.WA_TranslucentBackground)
    w.show()
    for _ in range(5):
        QApplication.processEvents()
    pedidos = []
    for nome in ("activateWindow", "setFocus", "raise_", "grabKeyboard"):  # qualquer um deles roubaria o foco
        setattr(w, nome, lambda *a, _n=nome, **k: pedidos.append(_n))
    bolha_clicar(w, bolha.relogio, depois=0.8)  # clique simples
    bolha.estado["v"] = "recording"
    bolha_clicar(w, bolha.relogio, depois=ENTRE_CLIQUES_S)  # parar + clique extra
    bolha_clicar(w, bolha.relogio, depois=1.0)
    QTest.mousePress(w, Qt.LeftButton, Qt.NoModifier, centro())  # arrastar
    QTest.mouseMove(w, centro() + QPoint(50, 0))
    QTest.mouseRelease(w, Qt.LeftButton, Qt.NoModifier, centro() + QPoint(50, 0))
    QTest.mouseClick(w, Qt.RightButton, Qt.NoModifier, centro())  # menu
    assert bolha.eventos[:3] == ["start", "stop", "open_recent"]  # o mouse chegou, com as flags de não-foco
    assert "menu" in bolha.eventos and any(isinstance(e, tuple) for e in bolha.eventos)
    assert pedidos == []  # nenhum pedido de ativação/foco/primeiro plano durante todos esses eventos
    assert w.windowFlags() & Qt.WindowDoesNotAcceptFocus
    w.hide()


def test_o_codigo_do_mouse_nao_chama_ativacao_nem_foco():
    """Rede de segurança estática: nada em bubble.py (eventos de mouse) pede ativação/foco da janela."""
    import inspect
    import re

    from hear_ai.ui import bubble

    fonte = inspect.getsource(bubble)
    trecho = fonte[fonte.index("        def mousePressEvent"):fonte.index("    return BubbleWidget")]
    assert not re.search(r"activateWindow|setFocus|raise_\(|grabMouse|grabKeyboard|requestActivate", trecho)


# --- QtUi + Controller falso: o que cada ação faz --------------------------------------------------------------

class ControleFalso:
    """Registra as chamadas e imita as transições do Controller real (begin/end/cancel guardados por estado)."""

    def __init__(self, store, estado="idle"):
        self.state = estado
        self.sink = store
        self.chamadas: list[str] = []

    def begin(self):
        self.chamadas.append("begin")
        if self.state == "idle":
            self.state = "recording"

    def end(self):
        self.chamadas.append("end")
        if self.state == "recording":
            self.state = "transcribing"

    def toggle(self):
        self.chamadas.append("toggle")

    def cancel(self):
        self.chamadas.append("cancel")
        if self.state == "recording":
            self.state = "idle"
            return True
        return False


class EscFalso:
    def __init__(self):
        self.registrado = 0

    def register(self):
        self.registrado += 1
        return True, 0

    def unregister(self):
        self.registrado -= 1


@pytest.fixture
def app_ui(qapp, tmp_path):
    """Fábrica de QtUi (config, histórico e posição TEMPORÁRIOS; Esc falso; bolha sem vigia)."""
    from hear_ai.config import load_config
    from hear_ai.history import HistoryStore
    from hear_ai.tray import TrayApp
    from hear_ai.ui.qt_app import QtUi

    criados = []

    def fazer(controller=None, show_bubble=False, sincrono=True):
        store = HistoryStore(tmp_path / "historico")
        controller = controller if controller is not None else ControleFalso(store)
        if getattr(controller, "sink", None) is None:
            controller.sink = store
        cfg_path = tmp_path / "config.json"
        tray = TrayApp(controller, load_config(cfg_path), on_quit=lambda: None, config_path=cfg_path,
                       log_dir=tmp_path / "logs")
        ui = QtUi(controller, tray, level_source=lambda: 0.0, bubble_state_path=tmp_path / "bolha.json",
                  esc_api=EscFalso(), show_bubble=show_bubble, log_dir=(tmp_path / "logs") if show_bubble else None,
                  watch_interval_ms=0, reassert_delay_ms=1)
        relogio = Relogio()
        ui.bubble.clock = relogio  # só o mouse usa este relógio (o modelo/driver ficam com o deles)
        if sincrono:
            ui._run_controller_action = lambda fn: fn()
        controller.on_state_change = ui.on_state
        ui.relogio, ui.controle = relogio, controller
        criados.append((ui, tray))
        return ui

    yield fazer
    for ui, tray in criados:
        ui._bubble_worker.join(3)
        ui.shutdown_watch()
        ui.bubble.timer.stop()
        ui.bubble.hide()
        if ui.recent_window is not None:
            ui.recent_window.close()
        tray._icon.qt.hide()


def test_clique_simples_parado_chama_begin_uma_vez_e_reafirma_a_bolha(app_ui):
    ui = app_ui()
    razoes = []
    ui.reassert_bubble = lambda razao: razoes.append(razao) or []
    bolha_clicar(ui.bubble, ui.relogio)
    assert ui.controle.chamadas == ["begin"] and ui.controle.state == "recording"
    assert razoes == ["click"]  # T40: bolha à vista e topmost ao começar a gravar
    assert ui.recent_window is None  # o clique simples não abre mais Recentes


def test_clique_gravando_para_pelo_mesmo_caminho_do_atalho(app_ui):
    ui = app_ui()
    ui.controle.state = "recording"  # o `app_state` do clique é o estado do controller, não o da bolha
    bolha_clicar(ui.bubble, ui.relogio)
    assert ui.controle.chamadas == ["end"] and ui.controle.state == "transcribing"


def test_clique_durante_a_transcricao_nao_chama_nada(app_ui):
    ui = app_ui()
    ui.controle.state = "transcribing"
    bolha_clicar(ui.bubble, ui.relogio)
    assert ui.controle.chamadas == [] and ui.recent_window is None


def test_duplo_clique_parado_cancela_e_abre_recentes(app_ui):
    ui = app_ui()
    bolha_clicar(ui.bubble, ui.relogio, depois=ENTRE_CLIQUES_S)
    bolha_clicar(ui.bubble, ui.relogio)
    assert ui.controle.chamadas == ["begin", "cancel"] and ui.controle.state == "idle"
    assert ui.recent_window is not None and ui.recent_window.isVisible()
    assert ui.bubble.visual().value == "idle"
    esperar(lambda: False, 0.05)  # entrega o post de volta à thread do Qt (_after_discard)
    assert ui.bubble.visual().value == "idle" and ui.esc.active is False


def test_clique_para_parar_mais_extra_nao_cancela_e_abre_recentes(app_ui):
    ui = app_ui()
    ui.controle.state = "recording"
    bolha_clicar(ui.bubble, ui.relogio, depois=ENTRE_CLIQUES_S)
    bolha_clicar(ui.bubble, ui.relogio)
    assert ui.controle.chamadas == ["end"]  # nada de cancel: a transcrição que começou segue
    assert ui.recent_window is not None and ui.recent_window.isVisible()


def test_duplo_clique_durante_a_transcricao_so_abre_recentes(app_ui):
    ui = app_ui()
    ui.controle.state = "transcribing"
    bolha_clicar(ui.bubble, ui.relogio, depois=ENTRE_CLIQUES_S)
    bolha_clicar(ui.bubble, ui.relogio)
    assert ui.controle.chamadas == [] and ui.recent_window.isVisible()


def test_arrastar_move_salva_a_posicao_e_nao_toca_no_controller(app_ui, tmp_path):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from hear_ai.ui.bubble import load_bubble_state

    ui = app_ui()
    b = ui.bubble
    antes = (b.x(), b.y())
    QTest.mousePress(b, Qt.LeftButton, Qt.NoModifier, centro())
    QTest.mouseMove(b, centro() + QPoint(30, -20))
    assert ui._dragging() is True
    QTest.mouseRelease(b, Qt.LeftButton, Qt.NoModifier, centro() + QPoint(30, -20))
    assert ui._dragging() is False
    assert (b.x(), b.y()) != antes
    salvo = load_bubble_state(tmp_path / "bolha.json")  # arquivo TEMPORÁRIO (nunca o bolha.json do usuário)
    assert salvo is not None and (salvo["x"], salvo["y"]) == (b.x(), b.y())
    assert ui.controle.chamadas == [] and ui.recent_window is None


def test_botao_direito_abre_o_menu_e_nao_grava(app_ui):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    ui = app_ui()
    menus = []
    ui._fill_qmenu = lambda menu, itens: None
    ui._menu.popup = lambda pos: menus.append(pos)
    QTest.mouseClick(ui.bubble, Qt.RightButton, Qt.NoModifier, centro())
    assert len(menus) == 1 and ui.controle.chamadas == [] and ui.recent_window is None


def test_vigia_da_t40_ignora_a_bolha_durante_o_arraste_real(app_ui):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from hear_ai.ui.bubble import visible_on_any
    from hear_ai.ui.qt_app import screens_info

    ui = app_ui(show_bubble=True)
    b = ui.bubble
    b.move(-30000, 500)  # fora de qualquer tela: o vigia normalmente traria de volta
    QTest.mousePress(b, Qt.LeftButton, Qt.NoModifier, centro())
    QTest.mouseMove(b, centro() + QPoint(40, 0))
    posicao = (b.x(), b.y())
    assert ui._dragging()
    ui.watch_tick()  # decide "reposition", mas o _apply não executa enquanto há arraste
    assert (b.x(), b.y()) == posicao  # o usuário está com o mouse nela: o vigia não mexe
    QTest.mouseRelease(b, Qt.LeftButton, Qt.NoModifier, centro() + QPoint(40, 0))
    assert not ui._dragging()
    ui.watch_tick()  # solto: o vigia volta ao normal e traz a bolha de volta para uma tela
    assert (b.x(), b.y()) != posicao and visible_on_any((b.x(), b.y()), screens_info()[0])


def test_cada_acao_vai_para_o_log_de_diagnostico_so_com_estados(app_ui, tmp_path):
    ui = app_ui(show_bubble=True)
    ui.controle.state = "recording"
    bolha_clicar(ui.bubble, ui.relogio, depois=1.0)  # stop
    ui.controle.state = "idle"
    bolha_clicar(ui.bubble, ui.relogio, depois=ENTRE_CLIQUES_S)  # start
    bolha_clicar(ui.bubble, ui.relogio)  # discard_and_open
    ui.diag._handler.flush()
    texto = (tmp_path / "logs" / "bolha-diag.log").read_text(encoding="utf-8")
    cliques = [linha.split("EVT ", 1)[1] for linha in texto.splitlines() if "EVT name=click" in linha]
    assert cliques == ["name=click action=stop state=recording", "name=click action=start state=idle",
                       "name=click action=discard_and_open state=recording"]


def test_atalho_e_bolha_dirigem_o_mesmo_controller(app_ui):
    """Começar pela bolha e terminar pelo atalho (e vice-versa) funciona: os dois usam begin/end."""
    from hear_ai.config import Config

    assert Config().hotkey == "ctrl+alt+h" and "space" not in Config().hotkey  # atalho padrão intacto
    ui = app_ui()
    bolha_clicar(ui.bubble, ui.relogio, depois=0.8)  # a bolha começa
    ui.controle.end()  # o atalho (soltar, no modo segurar) termina
    assert ui.controle.chamadas == ["begin", "end"] and ui.controle.state == "transcribing"
    ui.controle.state = "idle"
    ui.controle.begin()  # o atalho começa
    bolha_clicar(ui.bubble, ui.relogio)  # a bolha termina
    assert ui.controle.chamadas[-1] == "end" and ui.controle.state == "transcribing"


# --- a fila serial das ações ----------------------------------------------------------------------------------

def test_fila_serial_roda_em_ordem_fora_da_thread_do_chamador_e_sobrevive_a_erro():
    from hear_ai.ui.qt_app import SerialWorker

    w = SerialWorker("teste-fila")
    ordem, threads = [], set()

    def tarefa(n):
        def f():
            threads.add(threading.current_thread().name)
            time.sleep(0.01 * (3 - n))  # a 1ª é a mais lenta: sem fila a 3ª terminaria antes
            ordem.append(n)
        return f

    def quebra():
        raise RuntimeError("falhou")

    for n in (1, 2):
        w.submit(tarefa(n))
    w.submit(quebra)
    w.submit(tarefa(3))
    assert w.join(3)
    assert ordem == [1, 2, 3] and threads == {"teste-fila"} and threading.current_thread().name not in threads


# --- ponta a ponta com o Controller REAL ---------------------------------------------------------------------

class GravadorFalso:
    """Imita o Recorder: `start()` pode demorar (microfone abrindo) e cria a cópia em disco (spool)."""

    def __init__(self, pasta, abrir=None):
        self.pasta = pasta
        self.abrir = abrir  # Event: enquanto não for liberado, start() fica "abrindo o microfone"
        self.abrindo = threading.Event()
        self.started = 0
        self.stopped = 0
        self.last_spool_path = None

    def start(self):
        self.abrindo.set()
        if self.abrir is not None:
            self.abrir.wait(5)
        pcm = self.pasta / "gravacao.pcm"
        pcm.write_bytes(b"audio")
        pcm.with_suffix(".json").write_text("{}")
        self.last_spool_path = pcm
        self.started += 1

    def stop(self):
        self.stopped += 1
        return np.zeros(16000, dtype=np.float32)


class TranscritorFalso:
    def __init__(self, gate=None):
        self.gate = gate
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        return SEGREDO


class ColadorFalso:
    def __init__(self):
        self.colado = []

    def paste(self, text):
        self.colado.append(text)


class FeedbackFalso:
    def start(self):
        pass

    def stop(self):
        pass

    def done(self):
        pass

    def error(self):
        pass


def _controller_real(tmp_path, abrir=None, gate=None):
    from hear_ai.controller import Controller
    from hear_ai.history import HistoryStore

    pasta = tmp_path / "spool"
    pasta.mkdir(exist_ok=True)
    gravador = GravadorFalso(pasta, abrir)
    transcritor = TranscritorFalso(gate)
    colador = ColadorFalso()
    store = HistoryStore(tmp_path / "historico")
    ctl = Controller(gravador, transcritor, colador, FeedbackFalso(), store, logging.getLogger("teste.t42b"), "pt",
                     run_async=True)
    ctl.partes = SimpleNamespace(gravador=gravador, transcritor=transcritor, colador=colador, store=store,
                                 pcm=pasta / "gravacao.pcm")
    return ctl


def test_ponta_a_ponta_duplo_clique_descarta_sem_colar_nem_salvar(app_ui, tmp_path):
    ctl = _controller_real(tmp_path)
    ui = app_ui(ctl, sincrono=False)  # a fila serial REAL
    p = ctl.partes
    bolha_clicar(ui.bubble, ui.relogio, depois=0.0)  # 1º clique: começa a gravar na hora
    assert esperar(lambda: ctl.state == "recording" and p.gravador.started == 1)
    assert p.pcm.exists()  # a cópia em disco existe enquanto grava
    ui.relogio.t += ENTRE_CLIQUES_S
    bolha_clicar(ui.bubble, ui.relogio)  # 2º clique logo depois: duplo clique
    assert esperar(lambda: ctl.state == "idle" and p.gravador.stopped == 1)
    assert ui._bubble_worker.join(3)
    assert p.colador.colado == [] and p.transcritor.calls == 0  # nada colado nem transcrito
    assert p.store.entries() == []  # nada no histórico
    assert not p.pcm.exists() and not p.pcm.with_suffix(".json").exists()  # spool apagado
    assert ui.recent_window is not None and ui.recent_window.isVisible()
    assert esperar(lambda: ui.bubble.visual().value == "idle")


def test_ponta_a_ponta_duplo_clique_com_o_microfone_ainda_abrindo(app_ui, tmp_path):
    """O caso que a fila serial resolve: o cancel chega enquanto o begin() ainda abre o microfone."""
    abrir = threading.Event()
    ctl = _controller_real(tmp_path, abrir=abrir)
    ui = app_ui(ctl, sincrono=False)
    p = ctl.partes
    bolha_clicar(ui.bubble, ui.relogio, depois=ENTRE_CLIQUES_S)
    assert p.gravador.abrindo.wait(3)  # begin() está dentro de recorder.start()
    bolha_clicar(ui.bubble, ui.relogio)  # 2º clique, com o microfone ainda abrindo
    assert ui.recent_window is not None and ui.recent_window.isVisible()  # Recentes abre na hora
    esperar(lambda: False, 0.15)
    assert p.gravador.stopped == 0  # o cancel NÃO atropelou o begin: está na fila esperando
    abrir.set()  # o microfone termina de abrir
    assert esperar(lambda: p.gravador.started == 1 and p.gravador.stopped == 1)
    assert ui._bubble_worker.join(3)
    assert ctl.state == "idle"  # nada de "gravação cancelada que continua gravando"
    assert p.colador.colado == [] and p.store.entries() == [] and not p.pcm.exists()
    assert esperar(lambda: ui.bubble.visual().value == "idle")


def test_ponta_a_ponta_clique_para_parar_mais_extra_processa_normalmente(app_ui, tmp_path):
    gate = threading.Event()
    ctl = _controller_real(tmp_path, gate=gate)
    cancelamentos = []
    original = ctl.cancel
    ctl.cancel = lambda: cancelamentos.append(1) or original()
    ui = app_ui(ctl, sincrono=False)
    p = ctl.partes
    ctl.begin()  # começou pelo atalho
    assert esperar(lambda: ctl.state == "recording")
    bolha_clicar(ui.bubble, ui.relogio, depois=ENTRE_CLIQUES_S)  # clique para parar
    assert esperar(lambda: ctl.state == "transcribing" and p.gravador.stopped == 1)
    bolha_clicar(ui.bubble, ui.relogio)  # clique extra, ainda transcrevendo (o gate segura)
    assert ui.recent_window is not None and ui.recent_window.isVisible()
    assert cancelamentos == [] and ctl.state == "transcribing"  # a transcrição NÃO foi tocada
    gate.set()
    ctl.join(5)
    assert esperar(lambda: ctl.state == "idle")
    assert p.colador.colado == [SEGREDO]  # processou normalmente: colou
    assert [e.text for e in p.store.entries()] == [SEGREDO] and cancelamentos == []


def test_ponta_a_ponta_dois_cliques_lentos_gravam_e_param(app_ui, tmp_path):
    ctl = _controller_real(tmp_path)
    ui = app_ui(ctl, sincrono=False)
    p = ctl.partes
    bolha_clicar(ui.bubble, ui.relogio, depois=1.0)  # 1º: grava
    assert esperar(lambda: ctl.state == "recording")
    bolha_clicar(ui.bubble, ui.relogio)  # 2º, bem depois: para (não é duplo clique)
    ctl.join(5)
    assert esperar(lambda: ctl.state == "idle" and p.colador.colado == [SEGREDO])
    assert ui.recent_window is None  # nenhum duplo clique: Recentes não abriu
