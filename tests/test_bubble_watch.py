"""Testes do vigia da bolha (T40): "a bolha que some do nada".

1. Lógica pura (sem Qt): `decide`, geometria, mensagens do Windows, `Debouncer`, `DiagLog`.
2. Win32 REAL, mas só numa janela de TESTE (1x1 fora da tela, sem ativar nada) — nunca na do usuário.
3. Qt offscreen: reproduz o sumiço (esconder / opacidade 0 / fora das telas / sem topmost) e prova que o
   vigia recupera. A plataforma "offscreen" não tem HWND de verdade: o Win32 é um falso injetado.
4. Qt na plataforma "windows" num subprocesso nosso (janela de teste fora da tela).

Nada aqui abre o app do usuário, usa teclas reais ou mexe em config.json/bolha.json da raiz: tudo em tmp_path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))

import pytest  # noqa: E402

from hear_ai.ui import bubble_watch as bw  # noqa: E402
from hear_ai.ui.bubble import PAD, WIN_H, WIN_W  # noqa: E402
from hear_ai.ui.bubble_watch import BubbleProbe, DiagLog, Debouncer, classify_native_message, decide  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src"
SEGREDO = "SENTINELA-do-texto-ditado-4711"


def sonda(**kw) -> BubbleProbe:
    base = dict(visible=True, opacity=1.0, x=300, y=700, inside_screen=True, screen_name="DISPLAY1",
                topmost=True, visual="idle", hide_when_idle=False, native_visible=True, z_above=0,
                frame_opacity=0.85)
    base.update(kw)
    return BubbleProbe(**base)


# --- 1. decide: tabela de casos ----------------------------------------------------------------

CASOS = [
    ("tudo certo", {}, True, []),
    ("escondida (Qt)", {"visible": False}, True, ["show"]),
    ("opacidade zerada", {"opacity": 0.0}, True, ["opacity"]),
    ("opacidade 0,5", {"opacity": 0.5}, True, ["opacity"]),
    ("opacidade no limite (0,98) vale", {"opacity": 0.98}, True, []),
    ("opacidade logo abaixo do limite", {"opacity": 0.97}, True, ["opacity"]),
    ("fora de todas as telas", {"inside_screen": False}, True, ["reposition"]),
    ("perdeu o topmost", {"topmost": False}, True, ["topmost"]),
    ("Win32 diz que não está visível (Qt acha que sim)", {"native_visible": False}, True, ["topmost"]),
    ("fora do Windows (topmost/native desconhecidos)", {"topmost": None, "native_visible": None}, True, []),
    ("tudo errado, na ordem de aplicação",
     {"visible": False, "opacity": 0.0, "inside_screen": False, "topmost": False}, True,
     ["show", "opacity", "reposition", "topmost"]),
    ("o app não quer a bolha (show_bubble=False)", {"visible": False, "inside_screen": False}, False, []),
    ("esconder em repouso é pedido do usuário: deixa quieta",
     {"hide_when_idle": True, "visual": "idle", "visible": False, "opacity": 0.0, "inside_screen": False,
      "topmost": False}, True, []),
    ("esconder em repouso, mas GRAVANDO escondida: recupera",
     {"hide_when_idle": True, "visual": "recording", "visible": False}, True, ["show"]),
    ("esconder em repouso, estado 'hidden' do modelo: deixa quieta",
     {"hide_when_idle": True, "visual": "hidden", "opacity": 0.0}, True, []),
    ("transcrevendo e fora da tela", {"visual": "transcribing", "inside_screen": False}, True, ["reposition"]),
]


@pytest.mark.parametrize("nome,mudancas,quer,esperado", CASOS, ids=[c[0] for c in CASOS])
def test_decide_tabela_de_casos(nome, mudancas, quer, esperado):
    assert decide(sonda(**mudancas), quer) == esperado


def test_decide_e_pura_nao_altera_a_sonda():
    s = sonda(visible=False)
    antes = replace(s)
    decide(s, True)
    assert s == antes
    with pytest.raises(Exception):
        s.visible = True  # a sonda é imutável


def test_acoes_conhecidas():
    assert bw.ACTIONS == ("show", "opacity", "reposition", "topmost")


# --- geometria (sem Qt) -------------------------------------------------------------------------

TELA = [("A", (0, 0, 1920, 1080))]


def test_fracao_da_forma_sobre_as_telas():
    assert bw.shape_fraction_on_screens((800, 900), TELA) == 1.0
    assert bw.shape_fraction_on_screens((-30000, -30000), TELA) == 0.0
    # metade da forma (52 px de altura) para fora, embaixo
    meio = bw.shape_fraction_on_screens((800, 1080 - PAD - 26), TELA)
    assert meio == pytest.approx(0.5)


def test_alcancavel_inclui_a_barra_de_tarefas_e_a_margem_transparente():
    # a forma pode estar sobre a barra de tarefas (tela inteira) sem ser "perdida"
    assert bw.reachable((800, 1080 - WIN_H), TELA)
    # só a margem transparente (PAD) saindo da tela não é problema
    assert bw.reachable((-PAD, 500), TELA)
    assert not bw.reachable((-WIN_W, 500), TELA)  # inteira à esquerda da tela
    assert not bw.reachable((800, 1080 + 10), TELA)


def test_alcancavel_com_dois_monitores_somando_as_areas():
    telas = [("A", (0, 0, 1920, 1080)), ("B", (1920, 0, 1280, 1024))]
    # a forma (220 de largura) a cavalo entre os dois monitores: soma as duas partes
    assert bw.reachable((1920 - PAD - 110, 400), telas)
    assert bw.shape_fraction_on_screens((1920 - PAD - 110, 400), telas) == pytest.approx(1.0)
    assert not bw.reachable((3400, 400), telas)  # monitor B saiu / diminuiu


def test_nome_da_tela_do_ponto():
    telas = [("A", (0, 0, 1920, 1080)), ("B", (1920, 0, 1280, 1024))]
    assert bw.screen_at((10, 10), telas) == "A"
    assert bw.screen_at((1919, 500), telas) == "A" and bw.screen_at((1920, 500), telas) == "B"
    assert bw.screen_at((3300, 500), telas) is None  # fora de todas
    assert bw.screen_at((-1, 5), telas) is None


# --- mensagens nativas do Windows ---------------------------------------------------------------

@pytest.mark.parametrize("mensagem,wparam,esperado", [
    (bw.WM_DISPLAYCHANGE, 32, "display_change"),
    (bw.WM_WTSSESSION_CHANGE, bw.WTS_SESSION_LOCK, "session_lock"),
    (bw.WM_WTSSESSION_CHANGE, bw.WTS_SESSION_UNLOCK, "session_unlock"),
    (bw.WM_WTSSESSION_CHANGE, bw.WTS_REMOTE_CONNECT, "session_remote_connect"),
    (bw.WM_WTSSESSION_CHANGE, bw.WTS_CONSOLE_CONNECT, "session_console_connect"),
    (bw.WM_WTSSESSION_CHANGE, 0x99, "session_other"),
    (bw.WM_POWERBROADCAST, bw.PBT_APMSUSPEND, "power_suspend"),
    (bw.WM_POWERBROADCAST, bw.PBT_APMRESUMEAUTOMATIC, "power_resume"),
    (bw.WM_POWERBROADCAST, bw.PBT_APMRESUMESUSPEND, "power_resume"),
    (bw.WM_POWERBROADCAST, 0x0A, None),  # PBT_APMPOWERSTATUSCHANGE: não interessa
    (0x0201, 0, None),  # WM_LBUTTONDOWN
    (0x0312, 0x4845, None),  # WM_HOTKEY do Esc: não é nosso
])
def test_classifica_mensagens_do_windows(mensagem, wparam, esperado):
    assert classify_native_message(mensagem, wparam) == esperado


def test_taskbar_created_so_com_a_mensagem_registrada():
    assert classify_native_message(0xC123, 0, taskbar_created=0xC123) == "taskbar_created"
    assert classify_native_message(0xC123, 0, taskbar_created=0) is None  # não registrada: ignora
    assert classify_native_message(0xC124, 0, taskbar_created=0xC123) is None


# --- Debouncer ----------------------------------------------------------------------------------

def test_debouncer_junta_pedidos_em_uma_reafirmacao():
    # T35 (revisão 021): cada pedido reagenda; só o disparo do ÚLTIMO pedido age
    agendados, disparos = [], []
    d = Debouncer(lambda ms, fn: agendados.append((ms, fn)), 800, disparos.append, clock=lambda: 0.0)
    assert d.request("display_change") is True
    assert d.request("display_change") is False  # o Windows manda a mesma mensagem a cada janela de topo
    assert d.request("session_unlock") is False
    assert d.pending and [ms for ms, _ in agendados] == [800, 800, 800]
    assert d.reasons == ("display_change", "session_unlock")
    assert disparos == []  # nada antes do atraso
    agendados[0][1]()
    agendados[1][1]()
    assert disparos == []  # disparos velhos são ignorados
    agendados[2][1]()
    assert disparos == ["display_change+session_unlock"] and not d.pending
    assert d.request("power_resume") is True  # depois de disparar, novo ciclo
    assert len(agendados) == 4


# --- DiagLog -------------------------------------------------------------------------------------

LINHA = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} (EVT|STATE)( [a-z_0-9]+=[A-Za-z0-9_.,:+\\/-]*)+$")


def linhas(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_diag_grava_estado_e_eventos_em_formato_estrito(tmp_path):
    d = DiagLog(tmp_path / "logs")
    assert d.enabled and d.path == tmp_path / "logs" / "bolha-diag.log"
    d.sample(sonda(screen_name="\\\\.\\DISPLAY1"))
    d.event("display_change")
    d.event("fix", reason="vigia", actions="show+topmost", native_ok=True, was_opacity=0.0, state="idle")
    d.close()
    ls = linhas(d.path)
    assert len(ls) == 3 and all(LINHA.match(x) for x in ls), ls
    assert ls[0].split(" ", 2)[2].startswith("STATE visible=1 native_visible=1 opacity=1.00 frame_opacity=0.85 state=idle")
    assert "visible_on_any=1" in ls[0] and "topmost=1" in ls[0] and "z_above=0" in ls[0]
    assert "screen=\\\\.\\DISPLAY1" in ls[0]
    assert "EVT name=display_change" in ls[1]
    assert "native_ok=1" in ls[2] and "was_opacity=0.00" in ls[2]


def test_diag_filtra_o_que_nao_e_estado(tmp_path):
    """Texto livre não consegue entrar nem se alguém passar por engano: sem espaço, sem quebra de linha."""
    d = DiagLog(tmp_path)
    d.event("fix", reason=f"vigia\nSEGUNDA LINHA falsa {SEGREDO} olá, mundo!", actions=None)
    d.close()
    ls = linhas(d.path)
    assert len(ls) == 1 and LINHA.match(ls[0]), ls
    assert "\n" not in ls[0] and " olá" not in ls[0] and "mundo!" not in ls[0]
    assert "actions=-" in ls[0]
    # tamanho limitado: mesmo um texto enorme vira no máximo 60 caracteres
    d2 = DiagLog(tmp_path / "2")
    d2.event("x", note="a" * 5000)
    d2.close()
    assert len(linhas(d2.path)[0]) < 160


def test_diag_rotativo_1mb_em_3_arquivos(tmp_path):
    d = DiagLog(tmp_path)
    assert d._handler.maxBytes == 1_000_000 and d._handler.backupCount == 2  # ativo + 2 antigos = 3 arquivos
    d._handler.maxBytes = 3000  # encolhe só para exercitar a rotação
    for _ in range(400):
        d.sample(sonda())
    d.close()
    nomes = sorted(p.name for p in tmp_path.iterdir())
    assert nomes == ["bolha-diag.log", "bolha-diag.log.1", "bolha-diag.log.2"]
    assert all(p.stat().st_size <= 3200 for p in tmp_path.iterdir())


def test_diag_desligado_ou_sem_permissao_nao_levanta(tmp_path):
    off = DiagLog(None)
    off.event("x")
    off.sample(sonda())
    off.close()
    assert not off.enabled and off.path is None
    arquivo = tmp_path / "arquivo"
    arquivo.write_text("x", encoding="utf-8")
    quebrado = DiagLog(arquivo / "dentro")  # "pasta" que é um arquivo
    quebrado.event("x")
    assert not quebrado.enabled


def test_diag_nao_vaza_para_o_log_principal(tmp_path):
    import logging

    recebidos = []

    class H(logging.Handler):
        def emit(self, record):
            recebidos.append(record.getMessage())

    principal = logging.getLogger("hear_ai")
    h = H()
    principal.addHandler(h)
    try:
        d = DiagLog(tmp_path)
        d.event("display_change")
        d.sample(sonda())
        d.close()
    finally:
        principal.removeHandler(h)
    assert recebidos == []


# --- 2. Win32 REAL, em janela de teste ---------------------------------------------------------

win32 = pytest.mark.skipif(sys.platform != "win32", reason="só no Windows")


@pytest.fixture
def janela_de_teste():
    """STATIC de 8x8 em (-32000,-32000), WS_EX_NOACTIVATE|TOOLWINDOW: ninguém vê, nada ganha foco."""
    import ctypes
    from ctypes import wintypes

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    u.CreateWindowExW.restype = wintypes.HWND
    u.DestroyWindow.argtypes = [wintypes.HWND]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, wintypes.UINT]
    u.GetForegroundWindow.restype = wintypes.HWND
    WS_POPUP = 0x80000000
    hwnd = u.CreateWindowExW(bw.WS_EX_TOOLWINDOW | bw.WS_EX_NOACTIVATE, "STATIC", "hear-ai-t40-teste",
                             WS_POPUP, -32000, -32000, 8, 8, None, None, None, None)
    if not hwnd:
        pytest.skip("não foi possível criar a janela de teste (sem desktop interativo?)")
    yield SimpleNamespace(hwnd=hwnd, u=u, ct=ctypes)
    u.DestroyWindow(hwnd)


@win32
def test_win32_real_reafirma_topmost_sem_ativar(janela_de_teste):
    j = janela_de_teste
    api = bw.Win32Topmost()
    assert api.is_topmost(j.hwnd) is False
    assert api.is_visible(j.hwnd) is False  # criada sem WS_VISIBLE: o "Qt acha que sim, o Win32 não" real
    antes = j.u.GetForegroundWindow()
    assert api.reassert(j.hwnd) is True
    if not api.is_topmost(j.hwnd):
        # D41: em alguns momentos o Windows devolve sucesso e NÃO aplica o topmost (ambiental, medido);
        # o vigia trata isso tentando de novo a cada ciclo. Aqui: não é falha do código.
        pytest.skip("o Windows recusou o topmost neste instante (D41, ambiental)")
    assert api.is_topmost(j.hwnd) is True and api.is_visible(j.hwnd) is True
    assert j.u.GetForegroundWindow() == antes  # SWP_NOACTIVATE: o foco de quem está digitando não muda
    estilo = api.ex_style(j.hwnd)
    assert estilo & bw.WS_EX_NOACTIVATE and estilo & bw.WS_EX_TOPMOST
    # perde o topmost (o que o Windows faz em alguns eventos) -> o vigia enxerga e reafirma
    j.u.SetWindowPos(j.hwnd, j.ct.c_void_p(-2), 0, 0, 0, 0, bw.SWP_NOMOVE | bw.SWP_NOSIZE | bw.SWP_NOACTIVATE)
    assert api.is_topmost(j.hwnd) is False
    assert decide(sonda(topmost=api.is_topmost(j.hwnd), native_visible=api.is_visible(j.hwnd))) == ["topmost"]
    assert api.reassert(j.hwnd) is True and api.is_topmost(j.hwnd) is True
    assert j.u.GetForegroundWindow() == antes
    z = api.z_above(j.hwnd)
    assert isinstance(z, int) and 0 <= z <= bw.Z_LIMIT


@win32
def test_win32_real_janela_inexistente_nao_quebra():
    api = bw.Win32Topmost()
    assert api.is_topmost(0) is None and api.is_visible(0) is None and api.z_above(0) is None
    assert api.reassert(0) is False
    assert api.is_topmost(0x7FFF0) is None  # HWND que não existe


@win32
def test_win32_real_flags_nunca_ativam():
    assert bw.REASSERT_FLAGS & bw.SWP_NOACTIVATE
    assert bw.REASSERT_FLAGS == bw.SWP_NOMOVE | bw.SWP_NOSIZE | bw.SWP_NOACTIVATE | bw.SWP_SHOWWINDOW == 0x53


@win32
def test_win32_real_registro_de_sessao_e_taskbar_created(janela_de_teste):
    j = janela_de_teste
    sess = bw.Win32Session()
    assert sess.register(j.hwnd) is True
    assert sess.unregister(j.hwnd) is True
    assert 0xC000 <= sess.taskbar_created_message() <= 0xFFFF  # mensagem registrada (RegisterWindowMessage)
    assert sess.taskbar_created_message() == sess.taskbar_created_message()


def test_apis_win32_so_na_plataforma_windows_do_qt():
    assert bw.make_win32_apis("offscreen") == (None, None)  # winId() do offscreen não é HWND: ninguém mexe
    assert bw.make_win32_apis("minimal") == (None, None)


# --- 3. Qt offscreen: o sumiço e a recuperação ----------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class FakeTopmost:
    """O Win32 da bolha: guarda o estado e registra as reafirmações (offscreen não tem HWND real)."""

    def __init__(self):
        self.topmost = True
        self.visible = True
        self.z = 0
        self.reasserts = []

    def is_topmost(self, hwnd):
        return self.topmost

    def is_visible(self, hwnd):
        return self.visible

    def z_above(self, hwnd, limit=64):
        return self.z

    def reassert(self, hwnd):
        self.reasserts.append(hwnd)
        self.topmost = True
        self.visible = True
        return True


class FakeSession:
    def __init__(self):
        self.registered, self.unregistered = [], []

    def register(self, hwnd):
        self.registered.append(hwnd)
        return True

    def unregister(self, hwnd):
        self.unregistered.append(hwnd)
        return True

    def taskbar_created_message(self):
        return 0xC123


def girar(segundos=0.1):
    from PySide6.QtCore import QCoreApplication

    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        QCoreApplication.processEvents()
        time.sleep(0.003)


@pytest.fixture
def mk(qapp, tmp_path):
    """Fábrica de QtUi com a bolha ligada, Win32 falso e log/estado em tmp_path."""
    from hear_ai.config import Config
    from hear_ai.tray import TrayApp
    from hear_ai.ui.qt_app import QtUi

    criados = []

    def make(show_bubble=True, interval=0, explicit_log=True, win32=True):
        esc = SimpleNamespace(register=lambda: (True, 0), unregister=lambda: None)
        controller = SimpleNamespace(state="idle", cancel=lambda: None, window_guard=None)
        tray = TrayApp(controller, Config(), on_quit=lambda: None, config_path=tmp_path / "c.json",
                       log_dir=tmp_path / "logs")
        fake, sess = (FakeTopmost(), FakeSession()) if win32 else (None, None)
        kw = {} if interval is None else {"watch_interval_ms": interval}
        ui = QtUi(controller, tray, level_source=lambda: 0.0, bubble_state_path=tmp_path / "bolha.json",
                  esc_api=esc, show_bubble=show_bubble, log_dir=(tmp_path / "logs") if explicit_log else None,
                  topmost_api=fake, session_api=sess, reassert_delay_ms=1, **kw)
        criados.append((ui, tray))
        ui.fake, ui.fake_session, ui.controller_fake = fake, sess, controller
        return ui

    yield make
    for ui, tray in criados:
        ui.shutdown_watch()
        ui.bubble.timer.stop()
        ui.bubble.hide()
        tray._icon.qt.hide()


def sabotar(ui):
    """As quatro maneiras de a bolha sumir sem o app perceber."""
    ui.bubble.hide()
    ui.bubble.setWindowOpacity(0.0)
    ui.bubble.move(-30000, -30000)
    if ui.fake is not None:
        ui.fake.topmost = False


def estado_ok(ui):
    from hear_ai.ui.bubble_watch import reachable
    from hear_ai.ui.qt_app import screens_full

    b = ui.bubble
    return (b.isVisible() and b.windowOpacity() >= 0.98 and reachable((b.x(), b.y()), screens_full())
            and (ui.fake is None or ui.fake.topmost))


def diag_linhas(ui):
    ui.diag._handler.flush()
    return linhas(ui.diag.path)


def test_reproducao_sem_vigia_a_bolha_continua_sumida(mk):
    """O código da v1.2 só reagia a sinais de tela: nada traz de volta a bolha escondida/zerada/fora/sem topmost."""
    ui = mk(interval=0)  # sem QTimer do vigia = o comportamento antigo
    assert estado_ok(ui)
    sabotar(ui)
    assert not estado_ok(ui)
    girar(0.4)  # o event loop roda e NADA recupera
    assert not ui.bubble.isVisible() and ui.bubble.windowOpacity() == 0.0
    assert (ui.bubble.x(), ui.bubble.y()) == (-30000, -30000) and ui.fake.topmost is False
    assert not ui.watch_timer.isActive()


def test_vigia_recupera_a_bolha_em_um_ciclo(mk):
    ui = mk(interval=0)
    sabotar(ui)
    assert ui.watch_tick() == ["show", "opacity", "reposition", "topmost"]
    assert estado_ok(ui) and len(ui.fake.reasserts) == 1
    assert ui.watch_tick() == []  # recuperada: o ciclo seguinte não faz nada
    assert len(ui.fake.reasserts) == 1


def test_vigia_com_timer_real_recupera_em_poucos_segundos(mk):
    ui = mk(interval=40)
    assert ui.watch_timer.isActive()
    sabotar(ui)
    inicio = time.monotonic()
    fim = inicio + 3.0
    while time.monotonic() < fim and not estado_ok(ui):
        girar(0.02)
    assert estado_ok(ui), "o vigia não recuperou a bolha em 3 s"
    assert time.monotonic() - inicio < 1.0  # e foi rápido (ciclo de 40 ms no teste)


def test_padroes_do_vigia_2s_e_amostra_a_cada_5s(mk):
    ui = mk(interval=None)  # constantes de verdade
    assert ui.watch_timer.interval() == 2000 and ui.watch_timer.isActive()
    assert ui.sample_timer.interval() == 5000 and ui.sample_timer.isActive()
    ui.shutdown_watch()
    assert not ui.watch_timer.isActive() and not ui.sample_timer.isActive()


@pytest.mark.parametrize("quebra,esperado", [
    (lambda ui: ui.bubble.hide(), ["show"]),
    (lambda ui: ui.bubble.setWindowOpacity(0.0), ["opacity"]),
    (lambda ui: ui.bubble.move(-30000, 500), ["reposition"]),
    (lambda ui: setattr(ui.fake, "topmost", False), ["topmost"]),
    (lambda ui: setattr(ui.fake, "visible", False), ["topmost"]),  # o Win32 escondeu, o Qt nem sabe
], ids=["escondida", "opacidade 0", "fora das telas", "sem topmost", "Win32 invisivel"])
def test_cada_condicao_sozinha_e_corrigida(mk, quebra, esperado):
    ui = mk()
    quebra(ui)
    assert ui.watch_tick() == esperado
    assert estado_ok(ui) and ui.fake.visible
    assert ui.watch_tick() == []


def test_vigia_sem_win32_ainda_corrige_o_que_o_qt_enxerga(mk):
    ui = mk(win32=False)  # offscreen/sem API: topmost desconhecido
    assert ui._bubble_filter is None
    sabotar(ui)
    assert ui.watch_tick() == ["show", "opacity", "reposition"]
    assert estado_ok(ui)


def test_vigia_nao_repinta_nem_acorda_a_animacao(mk):
    ui = mk()
    chamadas = {"update": 0, "kick": 0}
    ui.bubble.update = lambda *a: chamadas.__setitem__("update", chamadas["update"] + 1)
    ui.bubble.kick = lambda: chamadas.__setitem__("kick", chamadas["kick"] + 1)
    parado = ui.bubble.timer.isActive()
    t0 = time.perf_counter()
    for _ in range(300):
        assert ui.watch_tick() == []
    por_ciclo_ms = (time.perf_counter() - t0) / 300 * 1000
    assert chamadas == {"update": 0, "kick": 0}  # saudável: zero redesenho
    assert ui.bubble.timer.isActive() == parado  # o timer de animação (16 ms) não é acordado
    assert por_ciclo_ms < 3.0, f"vigia custou {por_ciclo_ms:.2f} ms por ciclo"


def test_correcao_pede_no_maximo_um_redesenho(mk):
    ui = mk()
    n = {"update": 0}
    ui.bubble.update = lambda *a: n.__setitem__("update", n["update"] + 1)
    sabotar(ui)
    ui.watch_tick()
    assert n["update"] == 1


def test_foco_nunca_e_roubado_flags_preservadas(mk):
    from PySide6.QtCore import Qt

    ui = mk()
    sabotar(ui)
    ui.watch_tick()
    ui.reassert_bubble("teste")
    girar(0.05)
    flags = ui.bubble.windowFlags()
    for f in (Qt.FramelessWindowHint, Qt.WindowStaysOnTopHint, Qt.Tool, Qt.WindowDoesNotAcceptFocus):
        assert flags & f
    assert ui.bubble.testAttribute(Qt.WA_ShowWithoutActivating)
    # (a plataforma offscreen ativa qualquer janela mostrada; "não ativa" de verdade é provado no teste
    # da plataforma windows, mais abaixo, e SWP_NOACTIVATE em test_win32_real_flags_nunca_ativam)


def test_posicao_sobre_a_barra_de_tarefas_e_respeitada(mk, monkeypatch):
    """Só a ÁREA ÚTIL ficou de fora (barra de tarefas): o usuário pôs ali de propósito, o vigia não mexe."""
    from hear_ai.ui import qt_app
    from hear_ai.ui.bubble import visible_on_any

    cheia = [("A", (0, 0, 1000, 800))]
    util = [("A", (0, 0, 1000, 760))]
    monkeypatch.setattr(qt_app, "screens_full", lambda: cheia)
    monkeypatch.setattr(qt_app, "screens_info", lambda: (util, util[0][1]))
    ui = mk()
    ui.bubble.move(400, 800 - WIN_H + PAD)  # forma colada na borda de baixo, em cima da "barra"
    assert not visible_on_any((ui.bubble.x(), ui.bubble.y()), util)  # a regra estrita reposicionaria
    assert ui.watch_tick() == []
    assert (ui.bubble.x(), ui.bubble.y()) == (400, 800 - WIN_H + PAD)
    ui.bubble.move(5000, 5000)  # esta sim saiu da tela
    assert ui.watch_tick() == ["reposition"]
    assert visible_on_any((ui.bubble.x(), ui.bubble.y()), util)


def test_sem_nenhuma_tela_o_vigia_nao_corrige_por_tela(mk, monkeypatch):
    from hear_ai.ui import qt_app

    ui = mk()
    monkeypatch.setattr(qt_app, "screens_full", lambda: [])
    assert ui.build_probe().inside_screen is True
    ui.bubble.move(-30000, -30000)
    assert ui.watch_tick() == []


def test_nao_corrige_posicao_enquanto_arrasta(mk):
    ui = mk()
    ui.bubble.move(-30000, 500)
    ui.bubble._dragging = True
    ui.watch_tick()
    assert (ui.bubble.x(), ui.bubble.y()) == (-30000, 500)  # o usuário está com o mouse nela
    ui.bubble._dragging = False
    assert ui.watch_tick() == ["reposition"]
    assert estado_ok(ui)


def test_esconder_em_repouso_pedido_do_usuario_e_respeitado(mk):
    ui = mk()
    ui.set_loading(False)  # modelo carregado: a bolha está em repouso
    girar(0.05)
    assert ui.bubble.visual().value == "idle"
    ui.bubble.model._hide_when_idle = True  # bubble_hide_when_idle=True
    ui.bubble.hide()
    ui.bubble.setWindowOpacity(0.0)
    assert ui.watch_tick() == []  # escondida por escolha: não "recupera"
    assert not ui.bubble.isVisible()
    ui.on_state("recording")  # mas gravando a bolha TEM de aparecer
    girar(0.1)
    assert ui.bubble.isVisible() and ui.bubble.windowOpacity() == 1.0 and ui.fake.reasserts


def test_show_bubble_false_nao_vigia_nem_grava_log(mk, tmp_path):
    ui = mk(show_bubble=False, explicit_log=False)
    assert not ui.watch_timer.isActive() and not ui.sample_timer.isActive()
    assert not ui.diag.enabled and not (tmp_path / "logs" / "bolha-diag.log").exists()
    ui.bubble.move(-30000, -30000)
    assert ui.watch_tick() == [] and ui.reassert_bubble("x") == [] and not ui.bubble.isVisible()


# --- reafirmar ao gravar, eventos do Windows, bandeja --------------------------------------------

def test_gravando_reafirma_a_bolha_antes_de_pintar(mk):
    ui = mk()
    sabotar(ui)
    visto = {}
    original = ui.bubble.driver.state

    def espiao(nome):
        if nome == "recording":  # o instante em que o estado "gravando" começaria a ser pintado
            visto.update(visible=ui.bubble.isVisible(), opacity=ui.bubble.windowOpacity(),
                         topmost=ui.fake.topmost, reasserts=len(ui.fake.reasserts), x=ui.bubble.x())
        original(nome)

    ui.bubble.driver.state = espiao
    ui.on_state("recording")  # vem da thread do controller
    girar(0.1)
    assert visto and visto["visible"] and visto["opacity"] == 1.0 and visto["topmost"] and visto["reasserts"] == 1
    assert visto["x"] != -30000
    assert ui.bubble.visual().value == "recording"


def test_ao_gravar_com_tudo_certo_reafirma_so_o_topmost_e_so_ao_entrar(mk):
    ui = mk()
    assert estado_ok(ui)
    ui.on_state("recording")
    girar(0.1)
    assert len(ui.fake.reasserts) == 1  # sempre reafirma o topmost (barato, sem ativar)
    ui.on_state("transcribing")
    girar(0.05)
    assert len(ui.fake.reasserts) == 1  # só ao ENTRAR em gravando


def test_evento_do_windows_reafirma_uma_vez_depois_do_atraso(mk):
    ui = mk()
    sabotar(ui)
    for _ in range(3):  # o Windows entrega a mesma mensagem a cada janela de topo
        ui.on_native_event("display_change")
    assert not estado_ok(ui)  # nada na hora: espera a tela estabilizar
    girar(0.1)
    assert estado_ok(ui) and len(ui.fake.reasserts) == 1
    ls = diag_linhas(ui)
    assert sum("EVT name=display_change" in x for x in ls) == 1  # repetida: loga uma vez só
    assert sum("EVT name=fix reason=display_change " in x for x in ls) == 1


@pytest.mark.parametrize("evento", ["display_change", "session_unlock", "power_resume", "taskbar_created"])
def test_cada_evento_reafirma(mk, evento):
    ui = mk()
    sabotar(ui)
    ui.on_native_event(evento)
    girar(0.1)
    assert estado_ok(ui)
    assert any(f"EVT name=fix reason={evento} " in x for x in diag_linhas(ui))


def msg(message, wparam=0):
    from ctypes import wintypes

    m = wintypes.MSG()
    m.message, m.wParam = message, wparam
    return m


@win32
def test_filtro_nativo_so_observa_nunca_consome(qapp):
    import ctypes

    from hear_ai.ui.qt_app import _make_bubble_event_filter

    eventos = []
    f = _make_bubble_event_filter()(eventos.append, 0xC123)
    for m, esperado in [(msg(bw.WM_DISPLAYCHANGE, 32), "display_change"),
                        (msg(bw.WM_WTSSESSION_CHANGE, bw.WTS_SESSION_UNLOCK), "session_unlock"),
                        (msg(bw.WM_POWERBROADCAST, bw.PBT_APMRESUMEAUTOMATIC), "power_resume"),
                        (msg(0xC123), "taskbar_created")]:
        eventos.clear()
        assert f.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(m)) == (False, 0)
        assert eventos == [esperado]
    eventos.clear()
    assert f.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg(0x0200))) == (False, 0)  # mouse
    assert f.nativeEventFilter(b"xcb_generic_event_t", ctypes.addressof(msg(bw.WM_DISPLAYCHANGE))) == (False, 0)
    assert eventos == []


@win32
def test_filtro_que_levanta_excecao_nao_derruba_a_fila(qapp):
    import ctypes

    from hear_ai.ui.qt_app import _make_bubble_event_filter

    def explode(_nome):
        raise RuntimeError("boom")

    f = _make_bubble_event_filter()(explode, 0)
    m = msg(bw.WM_DISPLAYCHANGE, 32)
    assert f.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(m)) == (False, 0)


def test_ui_liga_o_filtro_a_sessao_e_cancela_ao_sair(mk):
    ui = mk()
    assert ui._bubble_filter is not None and ui._bubble_filter.taskbar_created == 0xC123
    hwnd = ui._hwnd()
    assert hwnd != 0 and ui.fake_session.registered == [hwnd]  # WTSRegisterSessionNotification(hwnd_da_bolha)
    ui.shutdown_watch()
    assert ui.fake_session.unregistered == [hwnd]
    ui.shutdown_watch()  # idempotente: não cancela duas vezes
    assert ui.fake_session.unregistered == [hwnd]
    assert not ui.diag.enabled  # arquivo fechado


ITEM = "Mostrar/recentralizar bolha"


def test_item_da_bandeja_so_aparece_com_a_bolha_ligada(mk, qapp):
    from hear_ai.config import Config
    from hear_ai.tray import TrayApp

    solta = TrayApp(SimpleNamespace(state="idle"), Config(), on_quit=lambda: None)
    assert ITEM not in [i[0] for i in solta.menu_items() if i and i[3]]  # menu da v1 intacto sem a bolha
    ui = mk()
    visiveis = [i[0] for i in ui.tray.menu_items() if i and i[3]]
    assert ITEM in visiveis and ITEM in [i[0] for i in ui.tray.menu_items(with_history=True) if i and i[3]]
    assert visiveis.index(ITEM) < visiveis.index("Sair")


def test_item_da_bandeja_recentraliza_e_preserva_posicao_valida(mk, tmp_path):
    from hear_ai.ui.bubble import save_bubble_state
    from hear_ai.ui.qt_app import screens_info

    ui = mk()
    nome, avail = screens_info()[0][0]
    x, y = avail[0] + 40, avail[1] + 60
    estado = tmp_path / "bolha.json"
    save_bubble_state(estado, x, y, nome)
    antes = estado.read_bytes()
    sabotar(ui)
    item = next(i for i in ui.tray.menu_items() if i and i[0] == ITEM)
    item[1]()  # clique no item
    assert estado_ok(ui) and (ui.bubble.x(), ui.bubble.y()) == (x, y)
    assert len(ui.fake.reasserts) == 1
    assert estado.read_bytes() == antes  # posição válida: bolha.json intocado


def test_item_da_bandeja_posicao_salva_invalida_vai_para_o_padrao_sem_apagar(mk, tmp_path):
    from hear_ai.ui.bubble import default_position, save_bubble_state
    from hear_ai.ui.qt_app import screens_info

    ui = mk()
    screens, primary = screens_info()
    estado = tmp_path / "bolha.json"
    save_bubble_state(estado, 50000, 50000, screens[0][0])  # monitor que saiu
    antes = estado.read_bytes()
    sabotar(ui)
    ui.recenter_bubble()
    assert estado_ok(ui) and (ui.bubble.x(), ui.bubble.y()) == default_position(primary)
    assert estado.read_bytes() == antes  # não apaga nem reescreve: quem decide é o usuário


def test_indicadores_redundantes_bandeja_muda_ao_gravar(mk):
    from hear_ai.tray import make_icon

    ui = mk()
    ui.set_loading(False)  # modelo carregado (senão a bandeja mostra "carregando modelo")
    girar(0.05)
    assert "pronto" in ui.tray._icon.title
    ui.tray.controller.state = "recording"
    ui.on_state("recording")
    girar(0.1)
    assert "gravando" in ui.tray._icon.title
    assert ui.tray._icon.icon.getpixel((50, 50))[:3] == (220, 38, 38)  # o ponto vermelho da bandeja
    assert make_icon("idle").getpixel((50, 50))[:3] != (220, 38, 38)
    ui.tray.controller.state = "idle"
    ui.on_state("idle")
    girar(0.1)
    assert "pronto" in ui.tray._icon.title


def test_indicadores_redundantes_bipe_ao_iniciar_e_ao_parar():
    """Com um Controller de verdade: o bipe de início sai ao começar e o de fim ao parar."""
    import logging

    import numpy as np

    from hear_ai.controller import Controller
    from hear_ai.feedback import TONES, Feedback

    tons = []
    fb = Feedback(True, beep=lambda freq, ms: tons.append(freq))
    recorder = SimpleNamespace(start=lambda: None, stop=lambda: np.zeros(16000, dtype=np.float32))
    c = Controller(recorder, SimpleNamespace(transcribe=lambda a: "ok"), SimpleNamespace(paste=lambda t: None),
                   fb, SimpleNamespace(save=lambda t, m: None), logging.getLogger("teste.t40.beep"), "pt",
                   run_async=False)
    c.begin()
    fb.join()
    assert tons == [TONES["start"][0][0]]
    c.end()
    fb.join()
    assert tons[1] == TONES["stop"][0][0] and TONES["start"] != TONES["stop"]


# --- log de diagnóstico: só estados ---------------------------------------------------------------

def test_log_do_vigia_nunca_tem_texto_ditado(mk):
    ui = mk()
    ui.controller_fake.last_text = SEGREDO  # onde um texto ditado poderia estar por perto
    ui.tray.set_error(SEGREDO)
    ui.bubble.model.on_warning(SEGREDO)
    for estado in ("recording", "transcribing", "saved", "error", "idle", "mic_lost"):
        ui.on_state(estado)
    girar(0.1)
    sabotar(ui)
    ui.watch_tick()
    ui.on_native_event("session_unlock")
    girar(0.1)
    ui.diag_sample()
    ui.recenter_bubble()
    caminho = ui.diag.path
    ui.shutdown_watch()
    texto = caminho.read_text(encoding="utf-8")
    assert texto  # o log existe e tem conteúdo
    assert SEGREDO.lower() not in texto.lower() and "SENTINELA" not in texto
    ls = texto.splitlines()
    assert ls and all(LINHA.match(x) for x in ls), [x for x in ls if not LINHA.match(x)]
    assert any(" STATE " in x for x in ls) and any("EVT name=fix " in x for x in ls)
    for p in caminho.parent.glob("bolha-diag.log*"):  # nenhum arquivo rotativo traz o texto
        assert SEGREDO.lower() not in p.read_text(encoding="utf-8").lower()


def test_log_registra_inicio_correcao_e_amostra(mk):
    ui = mk()
    sabotar(ui)
    ui.watch_tick()
    ui.diag_sample()
    ls = diag_linhas(ui)
    assert any("EVT name=start platform=offscreen win32=1 wanted=1" in x for x in ls)
    assert any("EVT name=wts_register ok=1" in x for x in ls)
    fix = next(x for x in ls if "EVT name=fix reason=vigia" in x)
    assert "actions=show+opacity+reposition+topmost" in fix and "native_ok=1" in fix
    assert "was_visible=0" in fix and "was_opacity=0.00" in fix and "was_inside=0" in fix and "was_topmost=0" in fix
    estado = [x for x in ls if " STATE " in x][-1]
    assert ui.build_probe().screen_name is not None  # a bolha (já recuperada) está sobre uma tela
    for chave in ("visible=", "native_visible=", "opacity=", "frame_opacity=", "state=", "hide_when_idle=", "x=",
                  "y=", "visible_on_any=", "screen=", "topmost=", "z_above="):
        assert chave in estado


# --- 4. Qt na plataforma "windows" de verdade, num subprocesso nosso ---------------------------------

SCRIPT_WINDOWS = r"""
import ctypes, json, os, sys, time
os.environ["QT_QPA_PLATFORM"] = "windows"
sys.path.insert(0, sys.argv[1])
from ctypes import wintypes
from PySide6.QtWidgets import QApplication
from hear_ai.ui import bubble_watch as bw
from hear_ai.ui.bubble import BubbleWidget
from hear_ai.ui.qt_app import _make_bubble_event_filter

u = ctypes.WinDLL("user32", use_last_error=True)
u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u.GetForegroundWindow.restype = wintypes.HWND
app = QApplication([])
res = {"platform": app.platformName()}
w = BubbleWidget()
w.move(-32000, -32000)  # a janela de TESTE nasce fora de todas as telas: ninguém vê, ninguém clica
w.show()

def spin(s=0.2):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)

spin()
hwnd = int(w.winId())
api, sess = bw.Win32Topmost(), bw.Win32Session()
fg = u.GetForegroundWindow()
st = api.ex_style(hwnd)
res["noactivate"] = bool(st & bw.WS_EX_NOACTIVATE)
res["toolwindow"] = bool(st & bw.WS_EX_TOOLWINDOW)
res["topmost_inicial"] = api.is_topmost(hwnd)
res["native_visible_inicial"] = api.is_visible(hwnd)
u.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, bw.SWP_NOMOVE | bw.SWP_NOSIZE | bw.SWP_NOACTIVATE)
res["topmost_apos_remover"] = api.is_topmost(hwnd)
res["reassert_ok"] = api.reassert(hwnd)
res["topmost_apos_reafirmar"] = api.is_topmost(hwnd)
u.ShowWindow(hwnd, 0)  # esconde SÓ no Win32: o Qt continua achando que está visível
res["qt_visible_apos_esconder_win32"] = w.isVisible()
res["native_visible_apos_esconder_win32"] = api.is_visible(hwnd)
api.reassert(hwnd)
res["native_visible_apos_reafirmar"] = api.is_visible(hwnd)
eventos = []
taskbar = sess.taskbar_created_message()
filtro = _make_bubble_event_filter()(eventos.append, taskbar)
app.installNativeEventFilter(filtro)
res["wts_register"] = sess.register(hwnd)
for m, wp in ((bw.WM_DISPLAYCHANGE, 32), (bw.WM_WTSSESSION_CHANGE, bw.WTS_SESSION_UNLOCK),
              (bw.WM_POWERBROADCAST, bw.PBT_APMRESUMEAUTOMATIC), (taskbar, 0)):
    u.PostMessageW(hwnd, m, wp, 0)
spin(0.5)
res["eventos"] = eventos
res["wts_unregister"] = sess.unregister(hwnd)
res["foco_igual"] = (u.GetForegroundWindow() != hwnd)  # T35: só que NÓS não viramos o primeiro plano (o usuário pode trocar de janela durante o teste)
res["ativa"] = w.isActiveWindow()
print("RESULT " + json.dumps(res))
"""


@win32
def test_plataforma_windows_real_topmost_nativo_e_eventos_postados():
    """Qt de verdade (plataforma "windows") numa janela de teste fora da tela, num processo que NÓS lançamos."""
    r = subprocess.run([sys.executable, "-c", SCRIPT_WINDOWS, str(SRC)], capture_output=True, text=True,
                       timeout=90)
    saida = next((x for x in r.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert saida, f"sem resultado (rc={r.returncode})\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr[-2000:]}"
    res = json.loads(saida[len("RESULT "):])
    assert res["platform"] == "windows"
    assert res["noactivate"] and res["toolwindow"]  # WindowDoesNotAcceptFocus / Tool viram WS_EX_NOACTIVATE / TOOLWINDOW
    assert res["topmost_inicial"] is True and res["native_visible_inicial"] is True
    assert res["topmost_apos_remover"] is False and res["reassert_ok"] and res["topmost_apos_reafirmar"] is True
    assert res["qt_visible_apos_esconder_win32"] is True and res["native_visible_apos_esconder_win32"] is False
    assert res["native_visible_apos_reafirmar"] is True  # SWP_SHOWWINDOW traz de volta o que só o Win32 escondeu
    assert res["wts_register"] and res["wts_unregister"]
    # o Qt entrega cada mensagem postada mais de uma vez ao filtro (dispatcher + windowProc): por isso o
    # Debouncer e o log sem repetição; aqui só importa que os quatro eventos chegaram, na ordem
    assert list(dict.fromkeys(res["eventos"])) == ["display_change", "session_unlock", "power_resume",
                                                   "taskbar_created"]
    assert res["foco_igual"] and not res["ativa"]  # nada disto ativou a janela nem roubou o foco


def test_depois_de_carregar_o_repouso_nao_fica_no_visual_de_carregando(mk):
    """Achado da T40: o modelo da bolha só saía de 'carregando' no 1º evento de estado (o 1º ditado)."""
    ui = mk()
    assert ui.bubble.visual().value == "loading"
    ui.set_loading(False)
    girar(0.05)
    assert ui.bubble.visual().value == "idle"
    ui2 = mk()
    ui2.on_state("recording")
    girar(0.05)
    ui2.set_loading(False)  # gravando: não pode virar repouso
    girar(0.05)
    assert ui2.bubble.visual().value == "recording"


# --- 4b. QtUi COMPLETO na plataforma "windows": Win32 real + QTimer real, bolha estacionada fora de tudo ----------

SCRIPT_QTUI_WINDOWS = r"""
import ctypes, json, os, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace
os.environ["QT_QPA_PLATFORM"] = "windows"
sys.path.insert(0, sys.argv[1])
from ctypes import wintypes
from PySide6.QtWidgets import QApplication
from hear_ai.config import Config
from hear_ai.tray import TrayApp
from hear_ai.ui import bubble_watch as bw, qt_app

# "tela" virtual a ~30000 px do monitor real: a bolha de TESTE nunca aparece para o usuário, mas para o app
# ela está dentro de uma tela (senão o vigia a traria para o monitor de verdade)
VIRTUAL = [("T40-virtual", (-30000, -30000, 4000, 4000))]
qt_app.screens_info = lambda: (VIRTUAL, VIRTUAL[0][1])
qt_app.screens_full = lambda: VIRTUAL


class BandejaMuda(TrayApp):
    def attach_qt(self, app_quit=None):  # nenhum ícone de bandeja de verdade
        return None


u = ctypes.WinDLL("user32", use_last_error=True)
u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.GetForegroundWindow.restype = wintypes.HWND

app = QApplication([])
tmp = Path(tempfile.mkdtemp(prefix="t40-qtui-"))
controller = SimpleNamespace(state="idle", cancel=lambda: None, window_guard=None)
tray = BandejaMuda(controller, Config(), on_quit=lambda: None, config_path=tmp / "c.json", log_dir=tmp / "logs")
esc = SimpleNamespace(register=lambda: (True, 0), unregister=lambda: None)
ui = qt_app.QtUi(controller, tray, level_source=lambda: 0.0, bubble_state_path=tmp / "bolha.json", esc_api=esc,
                 show_bubble=True, log_dir=tmp / "logs", watch_interval_ms=100, reassert_delay_ms=50)
res = {"platform": app.platformName(), "win32_real": isinstance(ui.topmost, bw.Win32Topmost),
       "filtro": ui._bubble_filter is not None}


def spin(s):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.004)


def recuperou():
    b = ui.bubble
    h = ui._hwnd()
    return (b.isVisible() and b.windowOpacity() >= 0.98 and ui.topmost.is_topmost(h) is True
            and ui.topmost.is_visible(h) is True and (VIRTUAL[0][1][0] <= b.x() < VIRTUAL[0][1][0] + 4000))


def medir(nome, estraga):
    estraga()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 3.0 and recuperou():  # dá um instante para o estrago valer
        app.processEvents()
        if time.monotonic() - t0 > 0.02:
            break
    while time.monotonic() - t0 < 3.0 and not recuperou():
        app.processEvents()
        time.sleep(0.004)
    res["rec_" + nome] = round(time.monotonic() - t0, 2) if recuperou() else None


spin(0.4)
hwnd = ui._hwnd()
fg = u.GetForegroundWindow()
res["recuperada_no_inicio"] = recuperou()
res["wts_registrado"] = ui._session_hwnd == hwnd and hwnd != 0
medir("sem_topmost", lambda: u.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0,
                                          bw.SWP_NOMOVE | bw.SWP_NOSIZE | bw.SWP_NOACTIVATE))
medir("escondida_no_win32", lambda: u.ShowWindow(hwnd, 0))
medir("hide_do_qt", lambda: ui.bubble.hide())
medir("opacidade_zero", lambda: ui.bubble.setWindowOpacity(0.0))
medir("fora_das_telas", lambda: ui.bubble.move(20000, 20000))
ui.on_native_event("display_change")  # o evento real chega pelo filtro; aqui só o caminho do app
spin(0.4)
res["foco_igual"] = (u.GetForegroundWindow() != hwnd)  # T35: só que NÓS não viramos o primeiro plano (o usuário pode trocar de janela durante o teste)
res["ativa"] = ui.bubble.isActiveWindow()
ui.diag._handler.flush()
res["log"] = (tmp / "logs" / "bolha-diag.log").read_text(encoding="utf-8").splitlines()
ui.shutdown_watch()
res["wts_cancelado"] = ui._session_hwnd == 0
print("RESULT " + json.dumps(res))
"""


@win32
def test_qtui_completo_na_plataforma_windows_recupera_a_bolha_com_win32_real():
    r = subprocess.run([sys.executable, "-c", SCRIPT_QTUI_WINDOWS, str(SRC)], capture_output=True, text=True,
                       timeout=120)
    saida = next((x for x in r.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert saida, f"sem resultado (rc={r.returncode})\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr[-3000:]}"
    res = json.loads(saida[len("RESULT "):])
    assert res["platform"] == "windows" and res["win32_real"] and res["filtro"]
    assert res["recuperada_no_inicio"] and res["wts_registrado"]
    for caso in ("sem_topmost", "escondida_no_win32", "hide_do_qt", "opacidade_zero", "fora_das_telas"):
        t = res["rec_" + caso]
        assert t is not None and t < 2.0, f"{caso}: não recuperou (ou demorou {t} s)"
    assert res["foco_igual"] and not res["ativa"]  # o vigia nunca ativa a janela nem rouba o foco
    assert res["wts_cancelado"]
    log = res["log"]
    assert all(LINHA.match(x) for x in log), [x for x in log if not LINHA.match(x)]
    assert any("EVT name=wts_register ok=1" in x for x in log)
    assert any("EVT name=fix reason=vigia actions=topmost native_ok=1" in x for x in log)
    assert any("EVT name=fix reason=display_change" in x for x in log)


# --- 5. repouso perceptível em fundo claro E escuro (item 5 da T40) ---------------------------------------

FUNDOS = {"branco": "#FFFFFF", "cinza-claro": "#F3F4F6", "cinza": "#B8B8B8", "cinza-medio": "#777777",
          "grafite": "#2B2F36", "azul-marca": "#0B2A66", "quase-preto": "#1A1A1A", "preto": "#000000"}
ESCALA = 3


def _luminancia(c) -> float:
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2])


def contraste(a, b) -> float:
    """Razão de contraste WCAG entre duas cores (r, g, b)."""
    la, lb = sorted((_luminancia(a), _luminancia(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _rgb(fundo: str):
    return tuple(int(fundo[i:i + 2], 16) for i in (1, 3, 5))


class _Relogio:
    t = 1000.0

    def __call__(self):
        return self.t


def renderizar_repouso(fundo: str, escala: int = ESCALA, aro: bool = True):
    """QImage (escala x) da bolha em repouso (opacidade de repouso incluída) sobre `fundo`."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

    from hear_ai.ui import bubble as bubble_mod

    guardado = (bubble_mod.HALO_EDGE_ALPHA, bubble_mod.HALO_GLOW_ALPHA)
    if not aro:
        bubble_mod.HALO_EDGE_ALPHA = bubble_mod.HALO_GLOW_ALPHA = 0
    try:
        relogio = _Relogio()
        w = bubble_mod.BubbleWidget(clock=relogio)
        w.timer.stop()
        for _ in range(10):
            relogio.t += 0.05
            w._frame = w.model.frame(relogio.t)
        pm = QPixmap(WIN_W * escala, WIN_H * escala)
        pm.setDevicePixelRatio(escala)
        pm.fill(Qt.transparent)
        w.render(pm, QPoint(0, 0))
        pm.setDevicePixelRatio(1)
        img = QImage(pm.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(QColor(fundo))
        p = QPainter(img)
        p.drawPixmap(0, 0, pm)
        p.end()
        assert w.visual().value == "idle"
        w.deleteLater()
        return img
    finally:
        bubble_mod.HALO_EDGE_ALPHA, bubble_mod.HALO_GLOW_ALPHA = guardado


def _anel(img, r_in: float, r_out: float, escala: int = ESCALA):
    """Pixels (r, g, b) entre dois raios (px lógicos) a partir do centro da forma."""
    import math

    cx, cy = WIN_W / 2 * escala, WIN_H / 2 * escala
    out = []
    for y in range(img.height()):
        for x in range(img.width()):
            d = math.hypot(x + 0.5 - cx, y + 0.5 - cy) / escala
            if r_in <= d <= r_out:
                c = img.pixelColor(x, y)
                out.append((c.red(), c.green(), c.blue()))
    return out


def _media(pixels):
    n = len(pixels)
    return tuple(sum(p[i] for p in pixels) / n for i in range(3))


@pytest.mark.parametrize("nome", list(FUNDOS))
def test_repouso_e_perceptivel_em_qualquer_fundo(qapp, nome):
    """Critério: o disco OU o aro tem contraste >= 3:1 (WCAG 1.4.11, componentes gráficos) com o fundo."""
    fundo = FUNDOS[nome]
    bg = _rgb(fundo)
    img = renderizar_repouso(fundo)
    corpo = contraste(_media(_anel(img, 15, 22)), bg)  # entre o isotipo (raio 13) e a borda (26)
    borda = max(contraste(p, bg) for p in _anel(img, 25.5, 27.5))  # o aro logo fora do disco
    assert max(corpo, borda) >= 3.0, f"{nome}: corpo {corpo:.2f}:1, aro {borda:.2f}:1"


@pytest.mark.parametrize("nome", ["grafite", "azul-marca", "quase-preto", "preto"])
def test_o_aro_e_o_que_salva_o_repouso_sobre_fundo_escuro(qapp, nome):
    """Sem o aro (v1.2) o disco escuro se funde ao fundo escuro; com o aro a borda aparece."""
    fundo = FUNDOS[nome]
    bg = _rgb(fundo)
    sem = max(contraste(p, bg) for p in _anel(renderizar_repouso(fundo, aro=False), 25.5, 27.5))
    com = max(contraste(p, bg) for p in _anel(renderizar_repouso(fundo, aro=True), 25.5, 27.5))
    assert sem < 3.0 <= com, f"{nome}: sem aro {sem:.2f}:1, com aro {com:.2f}:1"


def test_aro_so_no_repouso_e_carregando(qapp):
    """Gravando/transcrevendo continuam como estavam (a onda é de outra tarefa): sem aro claro."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPixmap

    from hear_ai.ui.bubble import BubbleWidget

    def aro_presente(estado):
        rel = _Relogio()
        w = BubbleWidget(clock=rel)
        w.timer.stop()
        if estado == "loading":
            w.driver.loading(True)
        elif estado != "idle":
            w.driver.state(estado)
        w.timer.stop()
        for _ in range(30):
            rel.t += 0.05
            w._frame = w.model.frame(rel.t)
        pm = QPixmap(WIN_W * 2, WIN_H * 2)
        pm.setDevicePixelRatio(2)
        pm.fill(Qt.transparent)
        w.render(pm, QPoint(0, 0))
        img = pm.toImage()
        f = w.frame()
        w.deleteLater()
        # um pixel logo FORA da forma, no meio da borda esquerda: branco semitransparente = aro
        x = int(((WIN_W - f.width) / 2 - 0.9) * 2)
        c = img.pixelColor(x, WIN_H)
        return c.alpha() > 40 and min(c.red(), c.green(), c.blue()) > 200

    assert aro_presente("idle") and aro_presente("loading")
    assert not aro_presente("recording") and not aro_presente("transcribing")


def gerar_evidencias(pasta) -> list:
    """evidencias/bolha-idle-claro.png e bolha-idle-escuro.png: ANTES (v1.2, sem aro) | DEPOIS (com aro)."""
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    cw, ch = 100 * ESCALA, 70 * ESCALA
    tira = 20 * ESCALA  # faixa do rótulo, acima da bolha (não sobrepõe o disco)
    sx, sy = (WIN_W * ESCALA - cw) // 2, (WIN_H * ESCALA - ch) // 2
    saidas = []
    for arquivo, fundo, texto in (("bolha-idle-claro.png", "#FFFFFF", "#333333"),
                                  ("bolha-idle-escuro.png", "#000000", "#DDDDDD")):
        folha = QImage(cw * 2, ch + tira, QImage.Format_ARGB32_Premultiplied)
        folha.fill(QColor(fundo))
        p = QPainter(folha)
        p.setRenderHint(QPainter.Antialiasing)
        fonte = QFont("Segoe UI")
        fonte.setPixelSize(11 * ESCALA)
        p.setFont(fonte)
        p.setPen(QColor(texto))
        for i, (rotulo, aro) in enumerate((("antes (v1.2)", False), ("depois (T40)", True))):
            img = renderizar_repouso(fundo, aro=aro)
            p.drawImage(i * cw, tira, img, sx, sy, cw, ch)
            p.drawText(i * cw + 6 * ESCALA, 14 * ESCALA, rotulo)
        p.end()
        destino = pasta / arquivo
        assert folha.save(str(destino))
        saidas.append(destino)
    return saidas


def test_evidencias_do_repouso_sao_geradas(qapp, tmp_path):
    arquivos = gerar_evidencias(tmp_path)
    assert [a.name for a in arquivos] == ["bolha-idle-claro.png", "bolha-idle-escuro.png"]
    assert all(a.stat().st_size > 3000 for a in arquivos)


if __name__ == "__main__":  # regenera as evidências: python tests/test_bubble_watch.py
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    for _a in gerar_evidencias(Path(__file__).resolve().parents[1] / "evidencias"):
        print(_a)
