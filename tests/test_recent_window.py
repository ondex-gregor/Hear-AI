"""Testes da janela "Recentes" (T22). Qt offscreen; HistoryStore em tmp_path (nunca o histórico real).

Capturas: `HEAR_AI_CAPTURAS=1 pytest tests/test_recent_window.py -k captura` grava
evidencias/recentes-lista.png e recentes-busca.png (2x).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))

import pytest  # noqa: E402

from hear_ai.history import HistoryStore  # noqa: E402

AGORA = datetime(2026, 10, 9, 15, 40, 0)

EXEMPLOS = [  # (há quantos minutos, texto, duração, status) — textos sintéticos
    (2, "Pedir para a equipe revisar o relatório de vendas até sexta-feira e mandar o resumo por e-mail.",
     6.4, "colado"),
    (25, "Reunião com o time às 14 horas sobre o cronograma do projeto Ondex. Levar a apresentação "
         "atualizada, os números do último trimestre e a proposta de orçamento para o próximo semestre.",
     21.0, "colado"),
    (70, "Ação: comprar pão, café e frutas para a reunião de amanhã cedo.", 4.2, "salvo"),
    (180, "Lembrar de configurar o atalho novo do Hear AI no computador do escritório.", 5.0, "pendente"),
    (60 * 24 + 30, "Rascunho do e-mail para o cliente: agradecemos o retorno e confirmamos a entrega "
                   "da versão 2 na próxima semana, com as correções combinadas na última conversa.",
     187.0, "colado"),
    (60 * 24 * 3, "Ideia para o blog: como ditar textos longos sem perder o fim da frase.", 9.8, "colado"),
]


def preencher(store: HistoryStore, clock) -> list[str]:
    ids = []
    for minutos, texto, dur, status in sorted(EXEMPLOS, key=lambda e: -e[0]):
        clock.now = AGORA - timedelta(minutes=minutos)
        ids.append(store.save(texto, {"duration_s": dur, "language": "pt", "status": status}))
    clock.now = AGORA
    return ids


class Relogio:
    def __init__(self):
        self.now = AGORA

    def __call__(self):
        return self.now


class Clip:
    def __init__(self, falha=False):
        self.texto = None
        self.falha = falha

    def set(self, t):
        if self.falha:
            raise RuntimeError("área de transferência ocupada")
        self.texto = t


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    clock = Relogio()
    s = HistoryStore(tmp_path / "historico", clock=clock)
    ids = preencher(s, clock)
    return s, ids


def janela(qapp, store=None, **kw):
    from hear_ai.ui.recent_window import RecentWindow

    kw.setdefault("now", AGORA)
    w = RecentWindow(store, **kw)
    w.resize(480, 640)
    return w


# --- formatação ----------------------------------------------------------------------------------

def test_formatacao_de_hora_duracao_e_previa():
    from hear_ai.ui.recent_window import format_duration, format_when, preview

    assert format_when(AGORA - timedelta(minutes=5), AGORA) == "hoje 15:35"
    assert format_when(AGORA - timedelta(days=1), AGORA) == "ontem 15:40"
    assert format_when(datetime(2026, 10, 6, 9, 5), AGORA) == "ter 09:05"  # 06/10/2026 é terça
    assert format_when(datetime(2026, 9, 1, 9, 5), AGORA) == "01/09 09:05"
    assert format_when(datetime(2025, 9, 1, 9, 5), AGORA) == "01/09/2025 09:05"
    assert format_when(None, AGORA) == "—"
    assert format_duration(6.4) == "6 s" and format_duration(187) == "3 min 07 s"
    assert format_duration(3725) == "1 h 02 min" and format_duration(None) == ""
    assert preview("a\n\nb   c") == "a b c"
    longo = "palavra " * 60
    p = preview(longo, 50)
    assert p.endswith("…") and len(p) <= 51 and "palav…" not in p


# --- lista, busca, ações --------------------------------------------------------------------------

def test_lista_mais_recentes_primeiro_com_hora_duracao_e_status(qapp, store):
    s, ids = store
    w = janela(qapp, s)
    rows = w.rows()
    assert [r.entry.id for r in rows] == ids[::-1]
    primeira = rows[0]
    assert primeira.meta.text() == "hoje 15:38 · 6 s"
    assert primeira.status is None  # colado: sem ícone
    por_status = {r.entry.status: r for r in rows}
    assert por_status["salvo"].status.text() == "● salvo · não colado"
    assert por_status["pendente"].status.text() == "◌ pendente"
    longa = next(r for r in rows if "Rascunho do e-mail" in r.entry.text)
    assert longa.meta.text() == "ontem 15:10 · 3 min 07 s"
    assert w.footer.text() == "6 ditados"
    assert w.empty_label.isHidden() and not w.scroll.isHidden()


def test_busca_sem_acento_nem_maiuscula(qapp, store):
    s, _ = store
    w = janela(qapp, s)
    w.search.setText("REUNIAO")
    w.refresh()  # (na tela, o refresh vem do debounce da digitação)
    textos = [r.entry.text for r in w.rows()]
    assert len(textos) == 2 and all("eunião" in t for t in textos)
    w.search.setText("ACAO: COMPRAR PAO")  # "ação" e "pão" sem acento, em maiúsculas
    w.refresh()
    assert [r.entry.text[:4] for r in w.rows()] == ["Ação"]
    w.search.setText("apresentacao")
    w.refresh()
    assert len(w.rows()) == 1 and "apresentação" in w.rows()[0].entry.text
    w.search.setText("xyz inexistente")
    w.refresh()
    assert w.rows() == [] and "Nada encontrado" in w.empty_label.text() and not w.empty_label.isHidden()


def test_busca_dispara_sozinha_depois_de_digitar(qapp, store):
    from PySide6.QtCore import QCoreApplication, QElapsedTimer

    s, _ = store
    w = janela(qapp, s)
    w.search.setText("pão")
    t = QElapsedTimer()
    t.start()
    while len(w.rows()) != 1 and t.elapsed() < 2000:
        QCoreApplication.processEvents()
    assert len(w.rows()) == 1


def test_copiar_poe_na_area_de_transferencia_e_mostra_copiado(qapp, store):
    s, _ = store
    clip = Clip()
    w = janela(qapp, s, clipboard=clip)
    row = w.rows()[0]
    row.copy_btn.click()
    assert clip.texto == row.entry.text
    assert row.copy_btn.text() == "Copiado ✓" and "Copiado" in w.footer.text()
    row._reset_copy()
    assert row.copy_btn.text() == "Copiar"


def test_copiar_com_falha_avisa(qapp, store):
    s, _ = store
    w = janela(qapp, s, clipboard=Clip(falha=True))
    row = w.rows()[0]
    row.copy_btn.click()
    assert row.copy_btn.text() == "Falhou" and "Não foi possível copiar" in w.footer.text()


def test_apagar_pede_confirmacao_na_propria_janela(qapp, store):
    s, ids = store
    w = janela(qapp, s)
    row = w.rows()[0]
    alvo = row.entry.id
    row.delete_btn.click()
    assert not row.confirm.isHidden() and row.copy_btn.isHidden()  # confirmação na linha
    assert s.get(alvo) is not None  # nada apagado ainda
    row.cancel_btn.click()
    assert row.confirm.isHidden() and not row.copy_btn.isHidden()
    row.delete_btn.click()
    row.confirm_btn.click()
    assert s.get(alvo) is None
    assert alvo not in [r.entry.id for r in w.rows()] and len(w.rows()) == 5
    assert "apagado" in w.footer.text()


def test_apagar_que_falha_mantem_a_linha(qapp, store, monkeypatch):
    s, _ = store
    w = janela(qapp, s)
    monkeypatch.setattr(s, "delete", lambda _id: False)
    row = w.rows()[0]
    row.delete_btn.click()
    row.confirm_btn.click()
    assert len(w.rows()) == 6 and "Não foi possível apagar" in w.footer.text()


def test_apagar_tudo_com_confirmacao(qapp, store):
    s, _ = store
    w = janela(qapp, s)
    w.clear_all_btn.click()
    assert not w.clear_confirm.isHidden() and len(s.entries()) == 6
    w.clear_cancel_btn.click()
    assert w.clear_confirm.isHidden() and len(s.entries()) == 6
    w.clear_all_btn.click()
    w.clear_confirm_btn.click()
    assert s.entries() == [] and w.rows() == []
    assert "Nenhum ditado ainda" in w.empty_label.text()
    assert not w.clear_all_btn.isEnabled()


def test_pausar_e_retomar_mostra_indicador(qapp, store):
    s, _ = store
    alternou = []

    def alternar():
        s.paused = not s.paused
        alternou.append(s.paused)
        return s.paused

    w = janela(qapp, s, on_toggle_pause=alternar)
    assert w.pause_btn.text() == "Pausar histórico" and w.paused_banner.isHidden()
    w.pause_btn.click()
    assert alternou == [True] and w.pause_btn.text() == "Retomar histórico"
    assert not w.paused_banner.isHidden()
    assert s.save("não deve gravar", {}) is None  # pausado de verdade
    w.pause_btn.click()
    assert alternou == [True, False] and w.paused_banner.isHidden()


def test_sem_callback_de_pausa_o_botao_some(qapp, store):
    s, _ = store
    w = janela(qapp, s)
    assert w.pause_btn.isHidden()


def test_historico_desligado_e_vazio(qapp, tmp_path):
    w = janela(qapp, None)
    assert w.rows() == [] and "desligado" in w.empty_label.text()
    assert not w.clear_all_btn.isEnabled() and w.pause_btn.isHidden()
    vazio = janela(qapp, HistoryStore(tmp_path / "vazio"))
    assert "Nenhum ditado ainda" in vazio.empty_label.text() and vazio.footer.text() == ""


def test_atualiza_quando_chega_ditado_novo(qapp, store):
    s, _ = store
    w = janela(qapp, s)
    s.save("ditado novo agora", {"duration_s": 2.0, "status": "colado"})
    assert len(w.rows()) == 6  # não relê o disco sozinha
    w.refresh()  # o app chama quando um ditado é salvo
    assert w.rows()[0].entry.text == "ditado novo agora"


def test_limite_de_linhas_mostra_aviso(qapp, tmp_path, monkeypatch):
    from hear_ai.ui import recent_window

    monkeypatch.setattr(recent_window, "MAX_ROWS", 3)
    s = HistoryStore(tmp_path / "h")
    for i in range(5):
        s.save(f"ditado {i}", {})
    w = janela(qapp, s)
    assert len(w.rows()) == 3 and w.footer.text() == "Mostrando os 3 mais recentes de 5"


def test_janela_normal_pode_ter_foco_e_traz_para_frente(qapp, store):
    from PySide6.QtCore import Qt

    s, _ = store
    w = janela(qapp, s)
    assert not (w.windowFlags() & Qt.WindowDoesNotAcceptFocus)
    assert (w.windowFlags() & Qt.WindowType_Mask) == Qt.Window  # janela normal (não Tool)
    w.bring_to_front()
    assert w.isVisible()
    s.save("outro", {})
    w.bring_to_front()  # reabrir: mesma janela, lista atualizada
    assert w.rows()[0].entry.text == "outro"
    w.close()


def test_fonte_legivel(qapp, store):
    """Spec: fonte >= 10 pt em tudo (o QSS vale depois do polish, que a tela faz ao mostrar)."""
    from PySide6.QtWidgets import QLabel, QPushButton

    s, _ = store
    w = janela(qapp, s)
    w.ensurePolished()
    tamanhos = []
    for widget in w.findChildren(QLabel) + w.findChildren(QPushButton):
        widget.ensurePolished()
        if widget.text():
            tamanhos.append(widget.font().pointSizeF())
    assert tamanhos and min(tamanhos) >= 10


# --- capturas (B2/C1) ------------------------------------------------------------------------------

def _captura(w, path: Path) -> None:
    from PySide6.QtCore import QPoint, QCoreApplication
    from PySide6.QtGui import QPixmap

    w.show()
    for _ in range(5):
        QCoreApplication.processEvents()
    pm = QPixmap(w.width() * 2, w.height() * 2)
    pm.setDevicePixelRatio(2)
    w.render(pm, QPoint(0, 0))
    pm.save(str(path))


@pytest.mark.skipif(not os.environ.get("HEAR_AI_CAPTURAS"), reason="defina HEAR_AI_CAPTURAS=1 para gerar as PNGs")
def test_captura_recentes(qapp, tmp_path):
    out = Path(os.environ.get("HEAR_AI_CAPTURAS_DIR") or Path(__file__).resolve().parents[1] / "evidencias")
    out.mkdir(parents=True, exist_ok=True)
    clock = Relogio()
    s = HistoryStore(tmp_path / "historico", clock=clock)
    preencher(s, clock)
    w = janela(qapp, s, on_toggle_pause=lambda: None)
    _captura(w, out / "recentes-lista.png")
    w.search.setText("reunião")
    w.refresh()
    _captura(w, out / "recentes-busca.png")
    w.search.setText("")
    s.paused = True
    w.refresh()
    w.rows()[0].ask_delete()
    _captura(w, out / "recentes-pausado-confirmar.png")
    w.close()


# --- ligação com o app (QtUi): clique na bolha, menu, atualização, pausa --------------------------

class EscFalso:
    def register(self):
        return True, 0

    def unregister(self):
        pass


def _ui(qapp, tmp_path, store):
    from types import SimpleNamespace

    from hear_ai.config import Config, load_config
    from hear_ai.tray import TrayApp
    from hear_ai.ui.qt_app import QtUi

    cfg_path = tmp_path / "config.json"  # config TEMPORÁRIO (nunca o do usuário)
    cfg = load_config(cfg_path)
    controller = SimpleNamespace(state="idle", sink=store, cancel=lambda: None)
    pasta = []
    tray = TrayApp(controller, cfg, on_quit=lambda: None, config_path=cfg_path)
    ui = QtUi(controller, tray, level_source=lambda: 0.0, bubble_state_path=tmp_path / "bolha.json",
              esc_api=EscFalso(), open_history=lambda: pasta.append(1), show_bubble=False)
    return ui, tray, cfg_path, pasta


def _processar():
    from PySide6.QtCore import QCoreApplication

    for _ in range(10):
        QCoreApplication.processEvents()


def test_clique_na_bolha_abre_uma_unica_janela(qapp, tmp_path, store):
    s, _ = store
    ui, tray, _, pasta = _ui(qapp, tmp_path, s)
    ui.bubble.clicked.emit()
    w = ui.recent_window
    assert w is not None and w.isVisible() and len(w.rows()) == 6
    ui.bubble.clicked.emit()  # reabrir: mesma janela
    assert ui.recent_window is w and pasta == []
    w.close()
    tray._icon.qt.hide()


def test_item_abrir_historico_do_menu_abre_a_janela(qapp, tmp_path, store):
    s, _ = store
    ui, tray, _, _ = _ui(qapp, tmp_path, s)
    itens = {i[0]: i[1] for i in tray.menu_items(with_history=True) if i}
    itens["Abrir histórico"]()
    assert ui.recent_window is not None and ui.recent_window.isVisible()
    ui.recent_window.close()
    tray._icon.qt.hide()


def test_novo_ditado_atualiza_a_janela_aberta(qapp, tmp_path, store):
    s, _ = store
    ui, tray, _, _ = _ui(qapp, tmp_path, s)
    ui.show_recent()
    s.save("acabei de ditar isto", {"status": "colado"})
    ui.on_state("transcribing")
    _processar()
    assert len(ui.recent_window.rows()) == 6  # ainda transcrevendo: não relê
    ui.on_state("idle")  # vem da thread do Controller depois de salvar/colar
    _processar()
    assert ui.recent_window.rows()[0].entry.text == "acabei de ditar isto"
    ui.recent_window.close()
    tray._icon.qt.hide()


def test_pausar_pela_janela_grava_no_config_temporario(qapp, tmp_path, store):
    import json

    s, _ = store
    ui, tray, cfg_path, _ = _ui(qapp, tmp_path, s)
    ui.show_recent()
    w = ui.recent_window
    assert not w.pause_btn.isHidden()
    w.pause_btn.click()
    assert s.paused is True and tray.cfg.history_paused is True
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["history_paused"] is True
    assert not w.paused_banner.isHidden()
    w.pause_btn.click()
    assert s.paused is False
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["history_paused"] is False
    w.close()
    tray._icon.qt.hide()


def test_historico_desligado_janela_avisa(qapp, tmp_path):
    from hear_ai.history import NullSink

    ui, tray, _, _ = _ui(qapp, tmp_path, NullSink())
    ui.show_recent()
    w = ui.recent_window
    assert w.store is None and "desligado" in w.empty_label.text() and w.pause_btn.isHidden()
    w.close()
    tray._icon.qt.hide()


def test_falha_ao_abrir_a_janela_cai_na_pasta(qapp, tmp_path, store, monkeypatch):
    from hear_ai.ui import recent_window

    s, _ = store
    ui, tray, _, pasta = _ui(qapp, tmp_path, s)

    def quebra(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(recent_window, "RecentWindow", quebra)
    ui.show_recent()
    assert pasta == [1]
    tray._icon.qt.hide()


# --- engrenagem de configurações (T43) -------------------------------------------------------------

class Chamadas:
    def __init__(self, falha=False):
        self.n = 0
        self.falha = falha

    def __call__(self):
        self.n += 1
        if self.falha:
            raise RuntimeError("não abriu")


def test_engrenagem_existe_com_tooltip_e_nome_acessivel(qapp, store):
    s, _ = store
    w = janela(qapp, s, on_open_settings=Chamadas())
    btn = w.settings_btn
    assert btn.objectName() == "gear" and not btn.isHidden()
    assert btn.toolTip() == "Configurações"
    assert btn.accessibleName() == "Configurações"
    assert "configurações" in btn.accessibleDescription().lower()
    w.close()


def test_engrenagem_fica_no_canto_superior_direito_do_cabecalho(qapp, store):
    s, _ = store
    w = janela(qapp, s, on_toggle_pause=lambda: None, on_open_settings=Chamadas())
    w.show()
    qapp.processEvents()
    g = w.settings_btn.geometry()
    assert w.width() - g.right() <= 24  # encostada na margem direita (18 px)
    assert g.top() <= 24  # no cabeçalho, em cima
    assert g.left() > w.pause_btn.geometry().right() - 1  # à direita do "Pausar histórico"
    assert g.center().x() > w.width() // 2
    w.close()


def test_engrenagem_clique_chama_o_callback_uma_vez(qapp, store):
    s, _ = store
    cham = Chamadas()
    w = janela(qapp, s, on_open_settings=cham)
    w.settings_btn.click()
    assert cham.n == 1
    w.settings_btn.click()
    assert cham.n == 2  # um clique = uma chamada (o app decide trazer a janela única para frente)
    w.close()


def test_engrenagem_tab_enter_e_espaco(qapp, store):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    s, _ = store
    cham = Chamadas()
    w = janela(qapp, s, on_open_settings=cham)
    w.show()
    btn = w.settings_btn
    assert btn.focusPolicy() & Qt.TabFocus  # entra na ordem do Tab
    QTest.keyClick(btn, Qt.Key_Return)  # eventos de teclado só dentro do processo (nada de teclas reais)
    assert cham.n == 1
    QTest.keyClick(btn, Qt.Key_Enter)
    assert cham.n == 2
    QTest.keyClick(btn, Qt.Key_Space)
    assert cham.n == 3
    w.close()


def test_engrenagem_e_alcancavel_so_com_o_tab(qapp, store):
    from PySide6.QtCore import Qt

    s, _ = store
    w = janela(qapp, s, on_toggle_pause=lambda: None, on_open_settings=Chamadas())
    w.show()
    w.activateWindow()
    qapp.processEvents()
    visitados, atual = [], w.pause_btn
    atual.setFocus(Qt.TabFocusReason)
    for _ in range(6):
        atual = atual.nextInFocusChain()
        visitados.append(atual)
    assert w.settings_btn in visitados  # alcançável só com o Tab
    w.close()


def test_sem_callback_a_engrenagem_fica_oculta_e_nao_faz_nada(qapp, store):
    s, _ = store
    w = janela(qapp, s)  # teste isolado, sem o app
    assert w.settings_btn.isHidden()
    w.open_settings()  # não levanta
    w.on_open_settings = Chamadas()
    w.refresh()  # a visibilidade acompanha o callback
    assert not w.settings_btn.isHidden()
    w.close()


def test_callback_que_falha_nao_derruba_a_janela(qapp, store):
    s, _ = store
    w = janela(qapp, s, on_open_settings=Chamadas(falha=True))
    w.settings_btn.click()  # o erro é registrado, a janela segue de pé
    assert w.rows() and not w.settings_btn.isHidden()
    w.close()


def test_engrenagem_nao_mexe_na_busca_nem_fecha_recentes(qapp, store):
    s, _ = store
    w = janela(qapp, s, on_open_settings=Chamadas())
    w.show()
    w.search.setText("reunião")
    w.refresh()
    antes = [r.entry.id for r in w.rows()]
    w.settings_btn.click()
    assert w.isVisible() and w.search.text() == "reunião"
    assert [r.entry.id for r in w.rows()] == antes
    w.close()


def test_recentes_nunca_cria_a_janela_de_configuracoes_sozinha(qapp, store, monkeypatch):
    from hear_ai.ui import settings_window

    def proibido(*a, **k):
        raise AssertionError("Recentes não pode criar a SettingsWindow; quem abre é o QtUi")

    monkeypatch.setattr(settings_window, "SettingsWindow", proibido)
    s, _ = store
    cham = Chamadas()
    w = janela(qapp, s, on_open_settings=cham)
    w.settings_btn.click()
    assert cham.n == 1
    w.close()


def test_gear_path_e_uma_engrenagem_com_furo_e_dentes():
    from PySide6.QtCore import QPointF

    from hear_ai.ui.recent_window import gear_path

    p = gear_path(8.6, 8)
    assert not p.contains(QPointF(0, 0))  # furo no centro
    assert p.contains(QPointF(0, -8.2)) and p.contains(QPointF(8.2, 0))  # dentes (norte e leste)
    assert p.contains(QPointF(4.5, 0))  # corpo
    import math

    ang = math.radians(22.5)  # entre o dente do norte (0°) e o seguinte (45°): vão, fora da engrenagem
    assert not p.contains(QPointF(8.0 * math.sin(ang), -8.0 * math.cos(ang)))
    ang = math.radians(45)  # no eixo do segundo dente: dentro
    assert p.contains(QPointF(8.0 * math.sin(ang), -8.0 * math.cos(ang)))
    box = p.boundingRect()
    assert 16.5 <= box.width() <= 18 and 16.5 <= box.height() <= 18  # cabe no botão de 32 px com folga


def test_engrenagem_muda_de_aparencia_ao_pressionar(qapp, store):
    from PySide6.QtGui import QImage

    s, _ = store
    w = janela(qapp, s, on_open_settings=Chamadas())
    w.show()
    qapp.processEvents()
    normal = w.settings_btn.grab().toImage()
    w.settings_btn.setDown(True)
    apertado = w.settings_btn.grab().toImage()
    w.settings_btn.setDown(False)
    assert isinstance(normal, QImage) and normal != apertado
    w.close()
