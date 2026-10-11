"""T31 (D2): robustez — microfone que cai, janela de administrador, troca de janela, monitores/DPI.

Tudo com streams, janelas e telas FALSAS: nenhuma tecla real, nenhum dispositivo desconectado.
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

from hear_ai.audio import Recorder  # noqa: E402

SR = 16000


# --- 1. microfone ----------------------------------------------------------------------------------

class Relogio:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class StreamFalso:
    def __init__(self, nome, callback, device):
        self.nome = nome
        self.callback = callback
        self.device = device
        self.active = False
        self.fechado = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.fechado = True

    def push(self, segundos, valor):
        n = int(segundos * SR)
        self.callback(np.full((n, 1), valor, np.float32), n, None, None)


class Fabrica:
    """1º stream = microfone original; os seguintes seguem o roteiro (stream novo ou erro)."""

    def __init__(self, roteiro=()):
        self.roteiro = list(roteiro)
        self.streams: list[StreamFalso] = []
        self.devices = []

    def __call__(self, **kw):
        self.devices.append(kw["device"])
        if self.streams and self.roteiro:
            passo = self.roteiro.pop(0)
            if isinstance(passo, Exception):
                raise passo
        s = StreamFalso(f"s{len(self.streams)}", kw["callback"], kw["device"])
        self.streams.append(s)
        return s


def esperar(cond, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def gravador(tmp_path, fabrica, eventos, relogio=None, **kw):
    relogio = relogio or Relogio()
    kw.setdefault("watchdog_s", 0.5)
    kw.setdefault("reconnect_delay", 0.01)
    r = Recorder(stream_factory=fabrica, spool_dir=tmp_path, on_device_event=eventos.append,
                 clock=relogio, **kw)
    return r, relogio


def test_microfone_cai_e_volta_sem_perder_nada(tmp_path):
    fab, eventos, recarregou = Fabrica(), [], []
    r, rel = gravador(tmp_path, fab, eventos, device="Microfone USB",
                      refresh_devices=lambda: recarregou.append(1))
    r.start()
    fab.streams[0].push(2.0, 0.1)  # fala antes de cair
    fab.streams[0].active = False  # o USB saiu: o stream morre
    assert esperar(lambda: eventos == ["lost", "reconnected"])
    assert fab.streams[0].fechado  # o stream morto foi fechado
    assert fab.devices == ["Microfone USB", None]  # reabre no dispositivo PADRÃO atual
    assert recarregou == [1]  # recarregou a lista do PortAudio antes de reabrir
    fab.streams[1].push(1.0, 0.3)  # fala depois de reconectar
    audio = r.stop()
    assert len(audio) == 3 * SR
    assert np.allclose(audio[:2 * SR], 0.1) and np.allclose(audio[2 * SR:], 0.3)
    disco = np.frombuffer(r.last_spool_path.read_bytes(), dtype="<i2")
    assert len(disco) == 3 * SR  # o arquivo em disco também tem tudo
    assert r.reconnects == 1


def test_microfone_mudo_por_tempo_demais_tambem_e_queda(tmp_path):
    fab, eventos = Fabrica(), []
    r, rel = gravador(tmp_path, fab, eventos)
    r.start()
    fab.streams[0].push(1.0, 0.2)
    assert r.stream_alive()
    rel.t += 0.6  # sem nenhum bloco de áudio por mais que o vigia aceita (o driver travou)
    assert esperar(lambda: "reconnected" in eventos)
    assert eventos[0] == "lost"
    r.stop()


def test_tres_tentativas_e_desiste_mantendo_o_audio(tmp_path):
    erro = OSError("dispositivo não encontrado")
    fab, eventos = Fabrica([erro, erro, erro]), []
    r, _ = gravador(tmp_path, fab, eventos)
    r.start()
    fab.streams[0].push(1.5, 0.4)
    fab.streams[0].active = False
    assert esperar(lambda: eventos == ["lost", "failed"])
    assert len(fab.devices) == 4  # original + 3 tentativas
    audio = r.stop()
    assert len(audio) == int(1.5 * SR) and np.allclose(audio, 0.4)
    assert r.last_spool_path.exists()


def test_segunda_tentativa_funciona(tmp_path):
    fab, eventos = Fabrica([OSError("ainda não"), "ok"]), []
    r, _ = gravador(tmp_path, fab, eventos)
    r.start()
    fab.streams[0].active = False
    assert esperar(lambda: "reconnected" in eventos)
    assert len(fab.devices) == 3 and eventos == ["lost", "reconnected"]
    r.stop()


def test_parar_durante_a_reconexao_nao_trava_nem_reabre(tmp_path):
    fab, eventos = Fabrica(), []
    r, _ = gravador(tmp_path, fab, eventos, reconnect_delay=0.5)
    r.start()
    fab.streams[0].push(0.5, 0.2)
    fab.streams[0].active = False
    assert esperar(lambda: "lost" in eventos)
    t0 = time.monotonic()
    audio = r.stop()  # o usuário soltou o atalho enquanto o vigia esperava
    assert time.monotonic() - t0 < 2.0
    assert len(audio) == int(0.5 * SR) and "reconnected" not in eventos
    assert len(fab.streams) == 1


def test_vigia_desligado_com_stream_falso_por_padrao(tmp_path):
    """Os streams falsos dos testes antigos entregam tudo de uma vez e param: o vigia
    automático só liga com o microfone real, para não "reconectar" à toa."""
    r = Recorder(stream_factory=Fabrica())
    assert r.watchdog_s is None
    assert Recorder().watchdog_s == 1.0


# --- controller: avisos do microfone, janela de destino -----------------------------------------------

def _controller(guard=None, recorder=None, **kw):
    from hear_ai.controller import Controller

    estados = []
    recorder = recorder or SimpleNamespace(on_device_event=None, start=lambda: None,
                                           stop=lambda: np.zeros(SR, np.float32))
    clip = []
    paster = SimpleNamespace(colado=[], clipboard=SimpleNamespace(set=clip.append))
    paster.paste = lambda t: (paster.colado.append(t),
                              SimpleNamespace(pasted=True, restored=True, reason="ok", in_clipboard=False))[1]
    salvos = []
    sink = SimpleNamespace(save=lambda t, m: (salvos.append((t, m["status"])), "id1")[1],
                           set_status=lambda i, s: salvos.append(("status", s)))
    fb = SimpleNamespace(start=lambda: None, stop=lambda: None, done=lambda: None,
                         error=lambda: estados.append("bipe-erro"))
    log = logging.getLogger(f"t31.{id(object())}")
    linhas = []
    h = logging.Handler()
    h.emit = lambda r: linhas.append(r.getMessage())
    log.addHandler(h)
    log.setLevel(logging.DEBUG)
    tr = SimpleNamespace(transcribe=lambda a: "texto secreto ditado")
    c = Controller(recorder, tr, paster, fb, sink, log, "pt", run_async=False, on_state_change=estados.append,
                   window_guard=guard, postprocess=None, **kw)
    return c, SimpleNamespace(estados=estados, paster=paster, clip=clip, salvos=salvos, linhas=linhas,
                              recorder=recorder)


class Janelas:
    def __init__(self, sequencia, elevadas=(), eu_admin=False):
        self.sequencia = list(sequencia)
        self.elevadas = set(elevadas)
        self.eu_admin = eu_admin

    def foreground(self):
        return self.sequencia.pop(0) if len(self.sequencia) > 1 else self.sequencia[0]

    def is_elevated(self, hwnd):
        return hwnd in self.elevadas

    def self_elevated(self):
        return self.eu_admin


def test_controller_se_liga_aos_eventos_do_microfone():
    c, x = _controller()
    assert x.recorder.on_device_event == c.on_device_event
    c.begin()
    x.recorder.on_device_event("lost")
    x.recorder.on_device_event("reconnected")
    assert x.estados[-2:] == ["mic_lost", "mic_reconnected"]


def test_microfone_que_nao_volta_encerra_e_transcreve():
    c, x = _controller()
    c.begin()
    c.on_device_event("failed")
    assert esperar(lambda: x.paster.colado == ["texto secreto ditado"])  # nunca descarta
    assert "mic_failed" in x.estados and "bipe-erro" in x.estados


def test_mesma_janela_cola_normalmente():
    c, x = _controller(Janelas([111, 111]))
    c.begin()
    c.end()
    assert x.paster.colado == ["texto secreto ditado"]
    assert ("status", "colado") in x.salvos


def test_janela_mudou_durante_a_transcricao_nao_cola():
    c, x = _controller(Janelas([111, 222]))  # começou no editor, terminou num chat
    c.begin()
    c.end()
    assert x.paster.colado == []
    assert x.clip == ["texto secreto ditado"]  # fica na área de transferência
    assert x.salvos[0] == ("texto secreto ditado", "pendente") and ("status", "salvo") in x.salvos
    assert [e for e in x.estados if e != "bipe-erro"][-3:] == ["window_changed", "saved", "idle"]
    assert not any("secreto" in l for l in x.linhas)


def test_janela_de_administrador_nao_tenta_ctrl_v():
    c, x = _controller(Janelas([333, 333], elevadas={333}))
    c.begin()
    c.end()
    assert x.paster.colado == [] and x.clip == ["texto secreto ditado"]
    assert "admin_window" in x.estados and "saved" in x.estados


def test_se_o_hear_ai_tambem_e_admin_cola():
    c, x = _controller(Janelas([333, 333], elevadas={333}, eu_admin=True))
    c.begin()
    c.end()
    assert x.paster.colado == ["texto secreto ditado"]


def test_janela_desconhecida_ou_api_quebrada_cola():
    class Quebrada(Janelas):
        def is_elevated(self, hwnd):
            raise OSError("x")

    for guard in (Janelas([None, None]), Janelas([111, None]), Quebrada([5, 5])):
        c, x = _controller(guard)
        c.begin()
        c.end()
        assert x.paster.colado == ["texto secreto ditado"], guard


def test_sem_guarda_de_janela_comportamento_antigo():
    c, x = _controller(None)
    c.begin()
    c.end()
    assert x.paster.colado == ["texto secreto ditado"]


# --- detecção de admin (API Win32 falsa por dentro) ---------------------------------------------------

def test_deteccao_de_admin_com_api_falsa():
    from hear_ai import paster

    w = paster.Win32Windows.__new__(paster.Win32Windows)
    w._self_elevated = None
    pids = {10: 100, 20: 200, 30: 300, 40: 0}
    w.process_id = lambda hwnd: pids[hwnd]

    class K:
        erro = 0

        def OpenProcess(self, acc, inh, pid):
            if pid == 200:
                K.erro = paster.ERROR_ACCESS_DENIED
                return 0
            if pid == 300:
                K.erro = 87  # processo sumiu
                return 0
            return 7

        def CloseHandle(self, h):
            pass

        def GetCurrentProcess(self):
            return 9

    w._k = K()
    w._ct = SimpleNamespace(get_last_error=lambda: K.erro)
    w._token_elevated = lambda h: h == 9 and False
    assert w.is_elevated(10) is False  # processo comum
    assert w.is_elevated(20) is True  # acesso negado = trata como elevado
    assert w.is_elevated(30) is False  # não existe mais
    assert w.is_elevated(40) is False  # sem processo


@pytest.mark.skipif(sys.platform != "win32", reason="só Windows")
def test_deteccao_de_admin_de_verdade_so_leitura():
    import subprocess

    from hear_ai.paster import Win32Windows

    w = Win32Windows()
    assert w.is_pid_elevated(os.getpid()) is w.self_elevated()
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq csrss.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    if "csrss" in out and not w.self_elevated():
        pid = int(out.split(",")[1].strip('"'))
        assert w.is_pid_elevated(pid) is True  # processo do sistema: acesso negado = elevado
    assert w.is_pid_elevated(0) is False


# --- 4. monitores / DPI -------------------------------------------------------------------------------

def test_visivel_em_alguma_tela():
    from hear_ai.ui.bubble import PAD, visible_on_any

    telas = [("A", (0, 0, 1920, 1040)), ("B", (1920, 0, 1280, 984))]
    assert visible_on_any((800, 900), telas)
    assert visible_on_any((2500, 800), telas)
    assert not visible_on_any((3400, 800), telas)  # monitor B diminuiu/saiu
    assert not visible_on_any((-500, 900), telas)
    # só a forma precisa caber: a margem transparente (PAD) pode sair da tela
    assert visible_on_any((-PAD, 900 - PAD), [("A", (0, 900, 1920, 140))])


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _qtui(tmp_path):
    from hear_ai.config import Config
    from hear_ai.tray import TrayApp
    from hear_ai.ui.qt_app import QtUi

    esc = SimpleNamespace(register=lambda: (True, 0), unregister=lambda: None)
    controller = SimpleNamespace(state="idle", cancel=lambda: None, window_guard=None)
    tray = TrayApp(controller, Config(), on_quit=lambda: None, config_path=tmp_path / "c.json")
    ui = QtUi(controller, tray, level_source=lambda: 0.0, bubble_state_path=tmp_path / "bolha.json",
              esc_api=esc, show_bubble=False)
    return ui, tray, controller


def test_bolha_fora_das_telas_volta_para_uma_visivel(qapp, tmp_path, monkeypatch):
    from hear_ai.ui import qt_app

    ui, tray, _ = _qtui(tmp_path)
    telas = [("A", (0, 0, 1920, 1040))]
    monkeypatch.setattr(qt_app, "screens_info", lambda: (telas, telas[0][1]))
    ui.bubble.move(800, 900)
    assert ui.ensure_bubble_visible() is False  # visível: não mexe
    assert (ui.bubble.x(), ui.bubble.y()) == (800, 900)
    ui.bubble.move(2600, 700)  # estava no monitor que saiu
    assert ui.ensure_bubble_visible() is True
    from hear_ai.ui.bubble import default_position, visible_on_any

    assert (ui.bubble.x(), ui.bubble.y()) == default_position(telas[0][1])
    assert visible_on_any((ui.bubble.x(), ui.bubble.y()), telas)
    tray._icon.qt.hide()


def test_sinais_de_tela_e_dpi_estao_ligados(qapp, tmp_path, monkeypatch):
    from hear_ai.ui import qt_app

    chamadas = []
    monkeypatch.setattr(qt_app.QtUi, "ensure_bubble_visible", lambda self: chamadas.append(1) or False)
    ui, tray, _ = _qtui(tmp_path)
    tela = qapp.screens()[0]
    tela.logicalDotsPerInchChanged.emit(144.0)  # DPI mudou (ex.: 100% -> 150%)
    tela.geometryChanged.emit(tela.geometry())
    tela.availableGeometryChanged.emit(tela.availableGeometry())
    assert len(chamadas) >= 3
    tray._icon.qt.hide()


def test_qtui_liga_a_guarda_de_janela_e_mostra_avisos_do_microfone(qapp, tmp_path):
    from PySide6.QtCore import QCoreApplication

    ui, tray, controller = _qtui(tmp_path)
    if sys.platform == "win32":
        from hear_ai.paster import Win32Windows

        assert isinstance(controller.window_guard, Win32Windows)
    ui.on_state("recording")
    ui.on_state("mic_lost")
    for _ in range(10):
        QCoreApplication.processEvents()
    assert ui.bubble.visual().value == "recording"  # continua gravando
    assert ui.bubble.model.frame(time.monotonic()).message == "Microfone desconectado — reconectando…"
    ui.on_state("window_changed")
    for _ in range(10):
        QCoreApplication.processEvents()
    assert ui.bubble.visual().value == "recording"  # o "saved" que vem depois é que avisa
    ui.bubble.timer.stop()
    tray._icon.qt.hide()
