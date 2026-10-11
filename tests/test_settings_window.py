"""Testes da janela "Configurações" (T23). Qt offscreen; config TEMPORÁRIO, pasta Inicializar
FALSA e PowerShell falso (nunca o config.json nem a pasta Inicializar reais do usuário).

Capturas: `HEAR_AI_CAPTURAS=1 pytest tests/test_settings_window.py -k captura` grava
evidencias/config-*.png (2x).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))

import pytest  # noqa: E402

from hear_ai.config import Config, load_config  # noqa: E402

MICS = ["Microfone (fifine Microphone)", "Microfone (Realtek HD Audio Mic input)"]


class Runner:
    """PowerShell falso: cria o .lnk pedido (na pasta falsa)."""

    def __init__(self, ok=True):
        self.ok = ok
        self.chamadas = 0

    def __call__(self, cmd, env=None, **kw):
        self.chamadas += 1
        if self.ok:
            Path(env["HEARAI_LNK"]).write_bytes(b"lnk")
        return SimpleNamespace(returncode=0 if self.ok else 1)


@pytest.fixture(autouse=True)
def pasta_inicializar_falsa(tmp_path, monkeypatch):
    """Proteção: em TODOS os testes deste arquivo a pasta Inicializar "padrão" é falsa (tmp_path).
    Assim nem a janela criada pelo app (QtUi) chega perto da pasta Inicializar real do usuário."""
    from hear_ai import startup

    falsa = tmp_path / "Inicializar-padrao-falsa"
    monkeypatch.setattr(startup, "default_startup_dir", lambda env=None: falsa)
    return falsa


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def ambiente(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg = load_config(cfg_path)  # cria o config temporário com os padrões
    models = tmp_path / "models"
    for nome in ("small", "base"):
        (models / f"faster-whisper-{nome}").mkdir(parents=True)
        (models / f"faster-whisper-{nome}" / "model.bin").write_bytes(b"x")
    (models / "faster-whisper-medium").mkdir()  # sem model.bin: download incompleto
    return SimpleNamespace(cfg=cfg, cfg_path=cfg_path, models=models, startup=tmp_path / "Inicializar",
                           alvo=tmp_path / "iniciar.bat", tmp=tmp_path)


def janela(qapp, amb, **kw):
    from hear_ai.ui.settings_window import SettingsWindow

    kw.setdefault("startup_dir", amb.startup)
    kw.setdefault("startup_target", amb.alvo)
    kw.setdefault("startup_runner", Runner())
    kw.setdefault("models_dir", amb.models)
    kw.setdefault("list_mics", lambda: list(MICS))
    w = SettingsWindow(amb.cfg, amb.cfg_path, **kw)
    w.resize(540, 900)
    return w


def gravado(amb) -> dict:
    return json.loads(amb.cfg_path.read_text(encoding="utf-8"))


# --- config: campos novos ---------------------------------------------------------------------------

def test_campos_novos_do_config_e_validacao(tmp_path):
    c = Config()
    assert c.input_device is None and c.bubble_hide_when_idle is False
    assert c.bubble_idle_opacity == 0.85 and c.start_with_windows is False
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"config_version": 2, "input_device": "Microfone X", "bubble_idle_opacity": 0.5,
                             "bubble_hide_when_idle": True, "start_with_windows": True}), encoding="utf-8")
    c = load_config(p)
    assert (c.input_device, c.bubble_idle_opacity, c.bubble_hide_when_idle, c.start_with_windows) == \
        ("Microfone X", 0.5, True, True)
    for ruim in ({"bubble_idle_opacity": 0.1}, {"bubble_idle_opacity": 1.5}, {"bubble_idle_opacity": True},
                 {"bubble_idle_opacity": "0.5"}, {"input_device": ""}, {"input_device": 3},
                 {"start_with_windows": "sim"}):
        p.write_text(json.dumps({"config_version": 2, **ruim}), encoding="utf-8")
        c = load_config(p)
        campo = next(iter(ruim))
        assert getattr(c, campo) == getattr(Config(), campo), ruim
    p.write_text(json.dumps({"config_version": 2, "bubble_idle_opacity": 1}), encoding="utf-8")
    assert load_config(p).bubble_idle_opacity == 1  # inteiro dentro da faixa vale


# --- listas -------------------------------------------------------------------------------------------

def test_lista_de_microfones_sem_repeticao_nem_pseudo_dispositivos():
    from hear_ai.ui.settings_window import input_devices

    apis = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}]
    devs = [
        {"name": "Mapeador de som da Microsoft - Input", "max_input_channels": 2, "hostapi": 0},
        {"name": "Microfone (3- fifine Microphone", "max_input_channels": 1, "hostapi": 0},  # MME corta o nome
        {"name": "Microfone (3- fifine Microphone)", "max_input_channels": 1, "hostapi": 2},
        {"name": "Microfone (Realtek HD Audio Mic input)", "max_input_channels": 2, "hostapi": 2},
        {"name": "Mixagem estéreo (Realtek HD Audio Stereo input)", "max_input_channels": 2, "hostapi": 2},
        {"name": "Alto-falantes (Realtek)", "max_input_channels": 0, "hostapi": 2},
    ]
    assert input_devices(lambda: devs, lambda: apis) == ["Microfone (3- fifine Microphone)",
                                                        "Microfone (Realtek HD Audio Mic input)"]
    sem_wasapi = input_devices(lambda: devs[:2], lambda: apis[:1])
    assert sem_wasapi == ["Microfone (3- fifine Microphone"]

    def quebra():
        raise OSError("PortAudio")

    assert input_devices(quebra, lambda: apis) == []


def test_modelos_baixados_e_atalho_bonito(tmp_path):
    from hear_ai.ui.settings_window import downloaded_models, pretty_hotkey

    for nome in ("small", "tiny", "large-v3", "base"):
        (tmp_path / f"faster-whisper-{nome}").mkdir()
        (tmp_path / f"faster-whisper-{nome}" / "model.bin").write_bytes(b"x")
    (tmp_path / "faster-whisper-medium").mkdir()
    assert downloaded_models(tmp_path) == ["tiny", "base", "small", "large-v3"]
    assert downloaded_models(tmp_path / "nao_existe") == []
    assert pretty_hotkey("ctrl+alt+h") == "Ctrl + Alt + H"
    assert pretty_hotkey("ctrl+shift+space") == "Ctrl + Shift + Espaço"
    assert pretty_hotkey("ctrl+alt+f9") == "Ctrl + Alt + F9"


# --- carregar --------------------------------------------------------------------------------------------

def test_abre_com_os_valores_do_config(qapp, ambiente):
    amb = ambiente
    amb.cfg.mode = "toggle"
    amb.cfg.input_device = MICS[1]
    amb.cfg.bubble_idle_opacity = 0.6
    w = janela(qapp, amb)
    assert w.hotkey_label.text() == "Ctrl + Alt + H"
    assert w.mode_combo.currentData() == "toggle"
    assert w.mic_combo.currentData() == MICS[1] and w.mic_combo.itemData(0) is None
    # T46: `small` e `medium` aparecem sempre (o `medium` do ambiente de teste não está baixado) + os já baixados
    assert [w.model_combo.itemData(i) for i in range(w.model_combo.count())] == ["base", "small", "medium"]
    assert w.model_combo.currentData() == "small"
    assert w.opacity_slider.value() == 60 and w.opacity_value.text() == "60%"
    assert w.startup_check.isChecked() is False  # estado real: sem atalho na pasta falsa


def test_microfone_salvo_que_sumiu_continua_na_lista(qapp, ambiente):
    ambiente.cfg.input_device = "Headset antigo"
    w = janela(qapp, ambiente)
    assert w.mic_combo.currentData() == "Headset antigo"
    assert "não encontrado" in w.mic_combo.currentText()


def test_historico_desligado_desabilita_dias_e_pausa(qapp, ambiente):
    w = janela(qapp, ambiente)
    w.hist_enabled.setChecked(False)
    assert not w.days_spin.isEnabled() and not w.hist_paused.isEnabled()


# --- salvar ----------------------------------------------------------------------------------------------

def test_salvar_grava_config_e_diz_o_que_precisa_reiniciar(qapp, ambiente):
    amb = ambiente
    aplicados = []
    w = janela(qapp, amb, on_applied=lambda cfg, mudou, reiniciar: aplicados.append((mudou, reiniciar)))
    w.mode_combo.setCurrentIndex(w.mode_combo.findData("toggle"))
    w.model_combo.setCurrentIndex(w.model_combo.findData("base"))
    w.days_spin.setValue(90)
    w.opacity_slider.setValue(70)
    assert w.save() is True
    raw = gravado(amb)
    assert raw["mode"] == "toggle" and raw["model"] == "base"
    assert raw["history_retention_days"] == 90 and raw["bubble_idle_opacity"] == 0.7
    assert amb.cfg.mode == "toggle" and amb.cfg.model == "base"  # o objeto vivo do app também
    (mudou, reiniciar), = aplicados
    assert mudou == {"mode", "model", "history_retention_days", "bubble_idle_opacity"}
    assert reiniciar == {"bubble_idle_opacity"}  # T46: o modelo é trocado na hora (não exige mais reiniciar)
    assert "Reinicie o Hear AI" in w.status.text() and "modelo" in w.status.text()


def test_salvar_sem_mudancas(qapp, ambiente):
    w = janela(qapp, ambiente)
    assert w.save() is True and w.status.text() == "Nada mudou."


def test_mudanca_que_aplica_na_hora(qapp, ambiente):
    w = janela(qapp, ambiente)
    w.hist_paused.setChecked(True)
    assert w.save() is True and w.status.text() == "Salvo e aplicado."


def test_erro_de_validacao_mostra_no_campo_e_nao_grava(qapp, ambiente):
    amb = ambiente
    antes = amb.cfg_path.read_text(encoding="utf-8")
    w = janela(qapp, amb)
    w._hotkey = "alt+tab"  # combinação reservada (só dá para chegar aqui por código)
    assert w.save() is False
    assert not w.errors["hotkey"].isHidden() and "reservad" in w.errors["hotkey"].text()
    assert "Corrija" in w.status.text()
    assert amb.cfg_path.read_text(encoding="utf-8") == antes and amb.cfg.hotkey == "ctrl+alt+h"


def test_falha_ao_gravar_o_arquivo_avisa(qapp, ambiente, monkeypatch):
    from hear_ai.ui import settings_window

    def quebra(*a):
        raise PermissionError("arquivo em uso")

    monkeypatch.setattr(settings_window, "save_config", quebra)
    w = janela(qapp, ambiente)
    w.mode_combo.setCurrentIndex(w.mode_combo.findData("toggle"))
    assert w.save() is False and "Não foi possível salvar" in w.status.text()
    assert ambiente.cfg.mode == "hold"  # o app não ficou com valor que não foi gravado


def test_microfone_escolhido_vai_para_o_config(qapp, ambiente):
    w = janela(qapp, ambiente)
    w.mic_combo.setCurrentIndex(w.mic_combo.findData(MICS[0]))
    assert w.save()
    assert gravado(ambiente)["input_device"] == MICS[0] and "microfone" in w.status.text()
    w.mic_combo.setCurrentIndex(0)
    assert w.save() and gravado(ambiente)["input_device"] is None


# --- iniciar com o Windows (pasta falsa) ------------------------------------------------------------

def test_iniciar_com_o_windows_cria_e_remove_o_atalho_na_pasta_falsa(qapp, ambiente):
    amb = ambiente
    runner = Runner()
    w = janela(qapp, amb, startup_runner=runner)
    w.startup_check.setChecked(True)
    assert w.save()
    assert (amb.startup / "Hear AI.lnk").exists() and runner.chamadas == 1
    assert gravado(amb)["start_with_windows"] is True and "Salvo e aplicado" in w.status.text()
    w.startup_check.setChecked(False)
    assert w.save()
    assert not (amb.startup / "Hear AI.lnk").exists() and gravado(amb)["start_with_windows"] is False


def test_iniciar_com_o_windows_que_falha_avisa_e_desmarca(qapp, ambiente):
    w = janela(qapp, ambiente, startup_runner=Runner(ok=False))
    w.startup_check.setChecked(True)
    assert w.save()
    assert not w.startup_check.isChecked()
    assert "não foi possível ligar o iniciar com o Windows" in w.status.text()


def test_sem_pasta_inicializar_a_opcao_fica_desabilitada(qapp, ambiente):
    w = janela(qapp, ambiente, startup_dir=None)
    assert not w.startup_check.isEnabled()


# --- gravar novo atalho ----------------------------------------------------------------------------------

def _esperar(cond, timeout=3.0):
    from PySide6.QtCore import QCoreApplication, QElapsedTimer

    t = QElapsedTimer()
    t.start()
    while not cond() and t.elapsed() < timeout * 1000:
        QCoreApplication.processEvents()
    return cond()


def test_gravar_novo_atalho_mostra_instrucao_recusa_e_aceita(qapp, ambiente):
    liberar = threading.Event()

    def gravador(timeout_s, on_rejected):
        on_rejected("alt+f4 é reservado: fecha a janela")
        liberar.wait(3)
        return "ctrl+shift+f9"

    w = janela(qapp, ambiente, record_hotkey_fn=gravador)
    w.record_btn.click()
    assert not w.record_btn.isEnabled() and "Pressione a combinação" in w.record_btn.text()
    assert _esperar(lambda: "reservado" in w.hotkey_hint.text())
    assert w.hotkey_hint.objectName() == "error"
    liberar.set()
    assert _esperar(lambda: w.record_btn.isEnabled())
    assert w.hotkey_label.text() == "Ctrl + Shift + F9" and "Salvar" in w.hotkey_hint.text()
    assert w.save() and gravado(ambiente)["hotkey"] == "ctrl+shift+f9" and "atalho" in w.status.text()


def test_gravar_cancelado_mantem_o_atalho(qapp, ambiente):
    w = janela(qapp, ambiente, record_hotkey_fn=lambda timeout_s, on_rejected: None)
    w.record_btn.click()
    assert _esperar(lambda: w.record_btn.isEnabled())
    assert w.hotkey_label.text() == "Ctrl + Alt + H" and "cancelado" in w.hotkey_hint.text()


def test_gravar_com_erro_nao_trava_o_botao(qapp, ambiente):
    def quebra(timeout_s, on_rejected):
        raise OSError("sem teclado")

    w = janela(qapp, ambiente, record_hotkey_fn=quebra)
    w.record_btn.click()
    assert _esperar(lambda: w.record_btn.isEnabled())


# --- bolha e janela --------------------------------------------------------------------------------------

def test_voltar_bolha_ao_padrao_chama_o_app(qapp, ambiente):
    chamadas = []
    w = janela(qapp, ambiente, on_reset_bubble=lambda: chamadas.append(1))
    w.reset_pos_btn.click()
    assert chamadas == [1] and "centro" in w.status.text()
    assert not janela(qapp, ambiente).reset_pos_btn.isEnabled()  # sem app ligado: desabilitado


def test_reabrir_relê_o_config_e_fonte_legivel(qapp, ambiente):
    from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

    w = janela(qapp, ambiente)
    w.bring_to_front()
    w.mode_combo.setCurrentIndex(w.mode_combo.findData("toggle"))  # mexeu e fechou sem salvar
    w.close()
    w.bring_to_front()
    assert w.mode_combo.currentData() == "hold"
    w.ensurePolished()
    tamanhos = []
    for widget in w.findChildren(QLabel) + w.findChildren(QPushButton) + w.findChildren(QCheckBox):
        widget.ensurePolished()
        if widget.text():
            tamanhos.append(widget.font().pointSizeF())
    assert min(tamanhos) >= 10
    w.close()


# --- capturas -----------------------------------------------------------------------------------------

def _captura(w, path: Path, scroll_to=None) -> None:
    from PySide6.QtCore import QCoreApplication, QPoint
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QScrollArea

    w.show()
    for _ in range(5):
        QCoreApplication.processEvents()
    if scroll_to is not None:
        sa = w.findChild(QScrollArea)
        sa.verticalScrollBar().setValue(scroll_to if scroll_to >= 0 else sa.verticalScrollBar().maximum())
        for _ in range(5):
            QCoreApplication.processEvents()
    pm = QPixmap(w.width() * 2, w.height() * 2)
    pm.setDevicePixelRatio(2)
    w.render(pm, QPoint(0, 0))
    pm.save(str(path))


@pytest.mark.skipif(not os.environ.get("HEAR_AI_CAPTURAS"), reason="defina HEAR_AI_CAPTURAS=1 para gerar as PNGs")
def test_captura_configuracoes(qapp, ambiente):
    out = Path(os.environ.get("HEAR_AI_CAPTURAS_DIR") or Path(__file__).resolve().parents[1] / "evidencias")
    out.mkdir(parents=True, exist_ok=True)
    w = janela(qapp, ambiente, on_reset_bubble=lambda: None)
    w.resize(540, 760)
    _captura(w, out / "config-topo.png", scroll_to=0)
    _captura(w, out / "config-fim.png", scroll_to=-1)
    w.model_combo.setCurrentIndex(w.model_combo.findData("base"))
    w.save()
    w.hotkey_hint.setObjectName("error")
    w._repolish(w.hotkey_hint)
    w.hotkey_hint.setText("Não dá para usar: alt+f4 é reservado: fecha a janela. Tente outra combinação.")
    w.hotkey_hint.show()
    w.record_btn.setText("Pressione a combinação…")
    w.record_btn.setEnabled(False)
    _captura(w, out / "config-gravando-atalho.png", scroll_to=0)
    w.close()


# --- ligação com o app (QtUi): menu "Abrir configurações", aplicar na hora, posição da bolha --------

class EscFalso:
    def register(self):
        return True, 0

    def unregister(self):
        pass


def _ui(qapp, amb, store=None):
    from hear_ai.history import HistoryStore
    from hear_ai.tray import TrayApp
    from hear_ai.ui.qt_app import QtUi

    store = store if store is not None else HistoryStore(amb.tmp / "hist")
    binder = SimpleNamespace(mode=amb.cfg.mode, set_mode=lambda m: setattr(binder, "mode", m))
    transcriber = SimpleNamespace(language="pt")
    controller = SimpleNamespace(state="idle", sink=store, cancel=lambda: None, language="pt",
                                 transcriber=transcriber)
    tray = TrayApp(controller, amb.cfg, on_quit=lambda: None, config_path=amb.cfg_path, binder=binder)
    ui = QtUi(controller, tray, level_source=lambda: 0.0, bubble_state_path=amb.tmp / "bolha.json",
              esc_api=EscFalso(), show_bubble=False)
    return ui, tray, controller, binder, store


def _abrir_pelo_menu(tray):
    itens = {i[0]: i[1] for i in tray.menu_items(with_history=True) if i}
    itens["Abrir configurações"]()


def test_menu_abre_a_janela_de_configuracoes_unica(qapp, ambiente, pasta_inicializar_falsa):
    ui, tray, *_ = _ui(qapp, ambiente)
    _abrir_pelo_menu(tray)
    w = ui.settings_window
    assert w is not None and w.isVisible() and w.cfg is ambiente.cfg
    assert w.startup_dir == pasta_inicializar_falsa  # nunca a pasta Inicializar real
    _abrir_pelo_menu(tray)
    assert ui.settings_window is w  # reabrir só traz para frente
    w.close()
    tray._icon.qt.hide()


def test_salvar_aplica_modo_idioma_e_historico_na_hora(qapp, ambiente):
    ui, tray, controller, binder, store = _ui(qapp, ambiente)
    ui.show_settings()
    w = ui.settings_window
    w.mode_combo.setCurrentIndex(w.mode_combo.findData("toggle"))
    w.lang_combo.setCurrentIndex(w.lang_combo.findData("en"))
    w.hist_paused.setChecked(True)
    w.days_spin.setValue(7)
    assert w.save()
    assert binder.mode == "toggle"  # atalho passa a alternar sem reiniciar
    assert controller.language == "en" and controller.transcriber.language == "en"
    assert store.paused is True and store.retention_days == 7
    assert "Alternar" in tray.title()
    assert gravado(ambiente)["mode"] == "toggle"
    assert w.status.text() == "Salvo e aplicado."
    w.close()
    tray._icon.qt.hide()


def test_voltar_bolha_ao_padrao_apaga_a_posicao_salva(qapp, ambiente):
    ui, tray, *_ = _ui(qapp, ambiente)
    padrao = (ui.bubble.x(), ui.bubble.y())
    ui.bubble.move(padrao[0] - 200, padrao[1] - 100)
    ui._on_bubble_moved(padrao[0] - 200, padrao[1] - 100)
    assert (ambiente.tmp / "bolha.json").exists()
    ui.show_settings()
    ui.settings_window.reset_pos_btn.click()
    assert not (ambiente.tmp / "bolha.json").exists()
    assert (ui.bubble.x(), ui.bubble.y()) == padrao
    ui.settings_window.close()
    tray._icon.qt.hide()


def test_falha_ao_abrir_a_janela_cai_no_arquivo(qapp, ambiente, monkeypatch):
    from hear_ai.ui import settings_window

    ui, tray, *_ = _ui(qapp, ambiente)
    abertos = []
    tray._opener = abertos.append

    def quebra(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(settings_window, "SettingsWindow", quebra)
    _abrir_pelo_menu(tray)
    assert abertos == [str(ambiente.cfg_path)]
    assert tray.on_open_settings == ui.show_settings  # religado para a próxima vez
    tray._icon.qt.hide()


def test_bandeja_sem_janela_continua_abrindo_o_arquivo(ambiente):
    from hear_ai.tray import TrayApp

    abertos = []
    tray = TrayApp(SimpleNamespace(state="idle"), ambiente.cfg, on_quit=lambda: None,
                   config_path=ambiente.cfg_path, opener=abertos.append)
    tray.open_settings()
    assert abertos == [str(ambiente.cfg_path)]


# --- engrenagem de Recentes -> Configurações (T43) ----------------------------------------------------

def _contando_settings(monkeypatch):
    """Conta quantas SettingsWindow o app chega a criar (tem que ser UMA)."""
    from hear_ai.ui import settings_window as sw

    criadas = []
    original = sw.SettingsWindow

    class Contando(original):
        def __init__(self, *a, **k):
            criadas.append(self)
            super().__init__(*a, **k)

    monkeypatch.setattr(sw, "SettingsWindow", Contando)
    return criadas


def test_engrenagem_de_recentes_abre_a_mesma_janela_de_configuracoes(qapp, ambiente, monkeypatch):
    criadas = _contando_settings(monkeypatch)
    ui, tray, *_ = _ui(qapp, ambiente)
    ui.show_recent()
    engrenagem = ui.recent_window.settings_btn
    assert not engrenagem.isHidden() and ui.recent_window.on_open_settings == ui.show_settings
    engrenagem.click()
    w = ui.settings_window
    assert w is not None and w.isVisible() and len(criadas) == 1
    engrenagem.click()  # de novo: só traz para frente
    _abrir_pelo_menu(tray)  # e a bandeja continua abrindo a MESMA janela
    assert ui.settings_window is w and len(criadas) == 1
    assert ui.recent_window.isVisible()  # Recentes continua aberta
    w.close()
    ui.recent_window.close()
    tray._icon.qt.hide()


def test_engrenagem_traz_a_janela_ja_aberta_para_frente_sem_recarregar(qapp, ambiente):
    ui, tray, *_ = _ui(qapp, ambiente)
    ui.show_recent()
    ui.show_settings()
    w = ui.settings_window
    w.hist_paused.setChecked(not w.hist_paused.isChecked())  # edição ainda não salva
    editado = w.hist_paused.isChecked()
    ui.recent_window.settings_btn.click()  # janela já visível: não recarrega do disco (não perde o que se editou)
    assert ui.settings_window is w and w.hist_paused.isChecked() == editado
    w.close()
    ui.recent_window.close()
    tray._icon.qt.hide()


def test_engrenagem_nao_interrompe_gravacao_nem_perde_a_busca(qapp, ambiente):
    ui, tray, controller, binder, store = _ui(qapp, ambiente)
    store.save("reunião com o time", {"duration_s": 3.0, "language": "pt", "status": "colado"})
    chamadas = []
    controller.state = "recording"
    controller.cancel = lambda: chamadas.append("cancel")
    controller.stop = lambda *a, **k: chamadas.append("stop")  # nada disso pode ser chamado
    ui.bubble.driver.state("recording")
    ui.show_recent()
    rec = ui.recent_window
    rec.search.setText("reunião")
    rec.refresh()
    linhas = [r.entry.id for r in rec.rows()]
    rec.settings_btn.click()
    assert chamadas == [] and controller.state == "recording"
    assert ui.bubble.visual().value == "recording"  # a bolha segue gravando
    assert rec.isVisible() and rec.search.text() == "reunião" and [r.entry.id for r in rec.rows()] == linhas
    assert ui.settings_window is not None and ui.settings_window.isVisible()
    ui.settings_window.close()
    rec.close()
    tray._icon.qt.hide()


def test_engrenagem_com_falha_ao_abrir_cai_no_arquivo_e_recentes_continua(qapp, ambiente, monkeypatch):
    from hear_ai.ui import settings_window

    ui, tray, *_ = _ui(qapp, ambiente)
    abertos = []
    tray._opener = abertos.append

    def quebra(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(settings_window, "SettingsWindow", quebra)
    ui.show_recent()
    ui.recent_window.settings_btn.click()  # mesmo plano B de antes: abre o config.json, sem derrubar nada
    assert abertos == [str(ambiente.cfg_path)] and ui.recent_window.isVisible()
    assert tray.on_open_settings == ui.show_settings
    ui.recent_window.close()
    tray._icon.qt.hide()
