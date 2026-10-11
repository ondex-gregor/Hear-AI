"""T43 (parte 2) — logotipo do Hear AI nos cabeçalhos de Recentes e Configurações. Qt offscreen.

Config, histórico e pasta Inicializar são TEMPORÁRIOS (nunca os do usuário). Nada de rede nem de teclas reais.

Também gera as evidências visuais:  .venv\\Scripts\\python.exe tests\\test_marca.py [pasta]
(padrão: evidencias) -> marca-recentes[-1x].png, marca-config[-1x].png, marca-cabecalhos.png,
marca-engrenagem.png, marca-logotipo-escalas.png.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from hear_ai.config import load_config  # noqa: E402
from hear_ai.history import HistoryStore  # noqa: E402
from hear_ai.ui import logo  # noqa: E402

AGORA = datetime(2026, 10, 9, 15, 40, 0)
EXEMPLOS = [  # (minutos atrás, texto sintético, duração, status)
    (2, "Pedir para a equipe revisar o relatório de vendas até sexta-feira e mandar o resumo por e-mail.", 6.4, "colado"),
    (25, "Reunião com o time às 14 horas sobre o cronograma do projeto. Levar a apresentação atualizada e os números "
         "do último trimestre.", 21.0, "colado"),
    (70, "Ação: comprar pão, café e frutas para a reunião de amanhã cedo.", 4.2, "salvo"),
    (180, "Lembrar de configurar o atalho novo no computador do escritório.", 5.0, "pendente"),
]
MICS = ["Microfone (exemplo A)", "Microfone (exemplo B)"]


# --- utilidades -------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _argb(pm):
    """(alfa, r, g, b) como arrays 2-D do pixmap (ARGB32 não pré-multiplicado)."""
    from PySide6.QtGui import QImage

    img = pm.toImage().convertToFormat(QImage.Format_ARGB32)
    raw = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    # .copy(): o buffer pertence à QImage, que morre ao sair daqui (sem cópia o array vira lixo)
    px = raw.reshape(img.height(), img.bytesPerLine() // 4, 4)[:, : img.width(), :].copy()
    return px[..., 3], px[..., 2], px[..., 1], px[..., 0]


def _tinta_do_arquivo():
    """Caixa da tinta do PNG original, calculada de forma independente do código de produção."""
    from PySide6.QtGui import QImage

    img = QImage(str(logo.WORDMARK_PATH)).convertToFormat(QImage.Format_ARGB32)
    raw = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    alfa = raw.reshape(img.height(), img.bytesPerLine() // 4, 4)[:, : img.width(), 3].copy()  # copia: ver _argb
    ys, xs = np.where(alfa > logo.INK_ALPHA_MIN)
    return img.width(), img.height(), int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max()), alfa


def _recorte_do_logotipo(w, escala: int = 1):
    """Renderiza a JANELA inteira (com o gradiente escuro dela) e recorta a área do logotipo.
    (`widget.grab()` do filho pinta o fundo claro padrão da paleta: não é o que o usuário vê.)"""
    from PySide6.QtCore import QCoreApplication, QPoint, QRect
    from PySide6.QtGui import QPixmap

    w.show()
    for _ in range(3):
        QCoreApplication.processEvents()
    pm = QPixmap(w.width() * escala, w.height() * escala)
    pm.setDevicePixelRatio(escala)
    w.render(pm, QPoint(0, 0))
    topo = w.logo.mapTo(w, QPoint(0, 0))
    return pm.copy(QRect(topo.x() * escala, topo.y() * escala, w.logo.width() * escala, w.logo.height() * escala))


def _historico(tmp: Path) -> HistoryStore:
    relogio = SimpleNamespace(now=AGORA)
    store = HistoryStore(tmp / "historico", clock=lambda: relogio.now)
    for minutos, texto, dur, status in sorted(EXEMPLOS, key=lambda e: -e[0]):
        relogio.now = AGORA - timedelta(minutes=minutos)
        store.save(texto, {"duration_s": dur, "language": "pt", "status": status})
    relogio.now = AGORA
    return store


def _recentes(store=None, **kw):
    from hear_ai.ui.recent_window import RecentWindow

    kw.setdefault("now", AGORA)
    w = RecentWindow(store, **kw)
    w.resize(480, 640)
    return w


def _config(tmp: Path):
    """SettingsWindow isolada: config, modelos e pasta Inicializar temporários; sem microfone real."""
    from hear_ai.ui.settings_window import SettingsWindow

    cfg_path = tmp / "config.json"
    cfg = load_config(cfg_path)
    models = tmp / "models"
    for nome in ("small", "base"):
        (models / f"faster-whisper-{nome}").mkdir(parents=True, exist_ok=True)
        (models / f"faster-whisper-{nome}" / "model.bin").write_bytes(b"x")
    return SettingsWindow(cfg, cfg_path, startup_dir=tmp / "Inicializar", startup_target=tmp / "iniciar.bat",
                          startup_runner=lambda *a, **k: SimpleNamespace(returncode=0), models_dir=models,
                          list_mics=lambda: list(MICS))


# --- o pixmap do logotipo ---------------------------------------------------------------------------

def test_logotipo_branco_preserva_o_alfa_e_nao_tem_pixel_escuro(qapp):
    pm = logo.hear_ai_logotipo(24, 2.0)
    alfa, r, g, b = _argb(pm)
    assert alfa.min() == 0 and alfa.max() >= 250  # há fundo transparente e tinta cheia
    tinta = alfa >= 16
    assert tinta.sum() > 300
    assert min(r[tinta].min(), g[tinta].min(), b[tinta].min()) >= 245  # branco: nenhum pixel escuro
    # a cobertura (quanto da imagem é tinta) bate com a do arquivo original: o alfa não foi perdido
    larg, alt, x0, x1, y0, y1, alfa_orig = _tinta_do_arquivo()
    cobertura_orig = alfa_orig[y0:y1 + 1, x0:x1 + 1].mean() / 255
    cobertura = alfa.mean() / 255
    assert cobertura == pytest.approx(cobertura_orig, rel=0.04)


def test_logotipo_aceita_outra_cor_sem_perder_o_alfa(qapp):
    alfa, r, g, b = _argb(logo.hear_ai_logotipo(24, 1.0, "#60A5FA"))
    tinta = alfa >= 64
    assert abs(int(r[tinta].mean()) - 0x60) <= 3 and abs(int(g[tinta].mean()) - 0xA5) <= 3
    assert abs(int(b[tinta].mean()) - 0xFA) <= 3


def test_logotipo_mantem_a_proporcao_da_tinta(qapp):
    larg, alt, x0, x1, y0, y1, _ = _tinta_do_arquivo()
    aspecto = (x1 - x0 + 1) / (y1 - y0 + 1)  # ~4,5:1 (o arquivo inteiro é ~3:1, com margens vazias)
    assert 4.0 < aspecto < 5.0
    assert logo.wordmark_aspect() == pytest.approx(aspecto, rel=1e-6)
    for altura in (22, 24, 26):
        pm = logo.hear_ai_logotipo(altura, 1.0)
        assert pm.height() == altura
        assert pm.width() / pm.height() == pytest.approx(aspecto, rel=0.03)


def test_logotipo_recorta_as_margens_vazias(qapp):
    """Sem o recorte a tinta ocuparia só ~59% da altura pedida; com ele a tinta encosta nas bordas."""
    alfa, *_ = _argb(logo.hear_ai_logotipo(24, 1.0))
    linhas = np.where((alfa > 40).any(axis=1))[0]
    colunas = np.where((alfa > 40).any(axis=0))[0]
    assert linhas.min() <= 1 and linhas.max() >= 22
    assert colunas.min() <= 1 and colunas.max() >= alfa.shape[1] - 3


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
def test_logotipo_nitido_em_100_125_150_e_200_por_cento(qapp, dpr):
    pm = logo.hear_ai_logotipo(24, dpr)
    assert pm.devicePixelRatio() == dpr
    assert pm.height() == round(24 * dpr)  # pixels FÍSICOS na densidade da tela (não é ampliação do 1x)
    assert pm.height() / dpr == pytest.approx(24, abs=0.5)  # tamanho lógico igual em todas as densidades
    ref = logo.hear_ai_logotipo(24, 1.0)
    assert pm.width() / dpr == pytest.approx(ref.width(), abs=1.5)
    alfa, *_ = _argb(pm)
    assert (alfa > 40).any(axis=1).sum() >= pm.height() - 2  # a tinta usa a altura toda (sem borrão de margem)


def test_logotipo_e_cacheado(qapp):
    assert logo.hear_ai_logotipo(24, 2.0) is logo.hear_ai_logotipo(24, 2.0)
    assert logo.hear_ai_logotipo(24, 2.0) is not logo.hear_ai_logotipo(24, 1.0)


def test_isotipo_da_ondex_continua_igual(qapp):
    """O isotipo (bolha, bandeja, ícone do app) não mudou e não é o logotipo."""
    pm = logo.white_logo(26, 2.0)
    assert not pm.isNull() and pm.height() == 52
    assert logo.ICON_PATH.name == "icone.png" and logo.WORDMARK_PATH.name == "logotipo-hear-ai.png"
    assert logo.WORDMARK_PATH.parent.name == "marca"


# --- sem o arquivo / arquivo ruim: cai para o texto "Hear AI" ----------------------------------------

def _sem_arquivo(monkeypatch, tmp_path, modo):
    if modo == "ausente":
        alvo = tmp_path / "nao_existe.png"
    elif modo == "corrompido":
        alvo = tmp_path / "ruim.png"
        alvo.write_bytes(b"isto nao e um png")
    else:  # "vazio": PNG válido, 100% transparente
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage

        img = QImage(120, 40, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        alvo = tmp_path / "vazio.png"
        assert img.save(str(alvo))
    monkeypatch.setattr(logo, "WORDMARK_PATH", alvo)


@pytest.mark.parametrize("modo", ["ausente", "corrompido", "vazio"])
def test_sem_logotipo_o_cabecalho_cai_para_o_texto_sem_quebrar(qapp, tmp_path, monkeypatch, modo):
    from PySide6.QtWidgets import QWidget

    _sem_arquivo(monkeypatch, tmp_path, modo)
    assert logo.hear_ai_logotipo(24, 1.0).isNull() and logo.wordmark_aspect() is None
    for w in (_recentes(_historico(tmp_path)), _config(tmp_path)):
        marca = w.findChildren(QWidget, "brand_logotipo")
        assert len(marca) == 1 and marca[0].fallback is True
        _, r, g, b = _argb(_recorte_do_logotipo(w))  # pinta sem levantar exceção
        assert (r > 128).sum() > 40  # o texto "Hear AI" apareceu (pixels claros sobre o fundo escuro)
        assert w.window_name.text() in ("Recentes", "Configurações")
        w.close()


# --- cabeçalhos das duas janelas ---------------------------------------------------------------------

def _cabecalhos(qapp, tmp_path):
    return {"Recentes": _recentes(_historico(tmp_path)), "Configurações": _config(tmp_path)}


def test_cabecalhos_usam_o_logotipo_e_nao_o_isotipo(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QLabel, QWidget

    def proibido(*a, **k):
        raise AssertionError("o cabeçalho não pode usar o isotipo da Ondex (white_logo)")

    monkeypatch.setattr(logo, "white_logo", proibido)
    for nome, w in _cabecalhos(qapp, tmp_path).items():
        marca = w.findChildren(QWidget, "brand_logotipo")
        assert len(marca) == 1, nome  # exatamente um logotipo
        assert w.logo is marca[0] and marca[0].fallback is False
        assert w.window_name.text() == nome
        assert w.findChildren(QWidget, "isotipo") == []
        # o antigo isotipo era um QLabel com pixmap: não pode sobrar nenhum
        com_pixmap = [lab for lab in w.findChildren(QLabel) if lab.pixmap() is not None and not lab.pixmap().isNull()]
        assert com_pixmap == [], nome
        w.close()


def test_ordem_do_cabecalho_logotipo_divisoria_nome(qapp, tmp_path):
    from PySide6.QtWidgets import QFrame

    for nome, w in _cabecalhos(qapp, tmp_path).items():
        w.show()
        w.grab()
        div = w.findChildren(QFrame, "brand_divider")[0]
        assert w.logo.geometry().right() < div.geometry().left() < w.window_name.geometry().left(), nome
        w.close()


def _linhas_de_base(w, escala: int = 2):
    """(base da tinta do logotipo, base do texto do nome) em px lógicos, medidas na janela renderizada.
    O nome usado nos testes não tem letras com cauda (Recentes), então a base é o fundo da tinta."""
    from PySide6.QtCore import QCoreApplication, QPoint
    from PySide6.QtGui import QPixmap

    w.show()
    for _ in range(3):
        QCoreApplication.processEvents()
    pm = QPixmap(w.width() * escala, w.height() * escala)
    pm.setDevicePixelRatio(escala)
    w.render(pm, QPoint(0, 0))
    _, r, g, b = _argb(pm)

    def fundo_da_tinta(widget, limiar):
        topo = widget.mapTo(w, QPoint(0, 0))
        y0, x0 = topo.y() * escala, topo.x() * escala
        regiao = r[y0:y0 + widget.height() * escala, x0:x0 + widget.width() * escala]
        linhas = np.where((regiao > limiar).any(axis=1))[0]
        return (y0 + linhas.max() + 1) / escala

    return fundo_da_tinta(w.logo, 150), fundo_da_tinta(w.window_name, 120)


def test_nome_da_janela_alinhado_pela_base_do_logotipo(qapp, tmp_path):
    w = _recentes(_historico(tmp_path))  # "Recentes": sem cauda, base nítida
    base_logo, base_texto = _linhas_de_base(w)
    assert abs(base_logo - base_texto) <= 2.0, (base_logo, base_texto)
    w.close()


def test_logotipo_no_mesmo_lugar_nas_duas_janelas(qapp, tmp_path):
    """A linha do cabeçalho tem altura fixa (32 px, a da engrenagem): com ou sem engrenagem o logotipo não pula."""
    com = _recentes(_historico(tmp_path), on_open_settings=lambda: None)
    sem = _recentes(_historico(tmp_path))
    cfg = _config(tmp_path)
    for w in (com, sem, cfg):
        w.show()
        qapp.processEvents()
    pos = {(w.logo.geometry().x(), w.logo.geometry().y()) for w in (com, sem, cfg)}
    assert len(pos) == 1, pos
    for w in (com, sem, cfg):
        w.close()


def test_logotipo_do_cabecalho_tem_tinta_branca_e_altura_certa(qapp, tmp_path):
    for nome, w in _cabecalhos(qapp, tmp_path).items():
        _, r, g, b = _argb(_recorte_do_logotipo(w))  # como o usuário vê: tinta branca sobre o gradiente escuro
        clara = r > 200
        assert clara.sum() > 300, nome  # o logotipo está lá, em branco
        assert r.max() >= 250 and g.max() >= 250 and b.max() >= 250, nome
        assert abs(int(r[clara].mean()) - int(b[clara].mean())) <= 4, nome  # branco, não azulado
        assert 0.1 < clara.mean() < 0.5, nome  # o resto é o fundo escuro
        assert 22 <= w.logo.height() <= 26 and 100 <= w.logo.width() <= 120, nome
        w.close()


def test_icone_do_aplicativo_continua_sendo_o_isotipo(qapp, tmp_path):
    for w in _cabecalhos(qapp, tmp_path).values():
        assert not w.windowIcon().isNull()  # ícone da janela/barra de tarefas: isotipo da Ondex (assets/icone.png)
        w.close()


def test_logotipo_nao_captura_mouse(qapp, tmp_path):
    from PySide6.QtCore import Qt

    w = _recentes(_historico(tmp_path))
    assert w.logo.testAttribute(Qt.WA_TransparentForMouseEvents)
    w.close()


# --- evidências ---------------------------------------------------------------------------------------

def _renderizar(w, escala: int):
    from PySide6.QtCore import QCoreApplication, QPoint
    from PySide6.QtGui import QPixmap

    w.show()
    for _ in range(5):
        QCoreApplication.processEvents()
    pm = QPixmap(w.width() * escala, w.height() * escala)
    pm.setDevicePixelRatio(escala)
    w.render(pm, QPoint(0, 0))
    return pm


def gerar_evidencias(pasta) -> dict[str, Path]:
    """Grava os PNGs em `pasta` e devolve {nome: caminho}. Usa só dados sintéticos e pastas temporárias."""
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    saidas = {}
    with tempfile.TemporaryDirectory(prefix="hear-ai-marca-") as tmp_s:
        tmp = Path(tmp_s)
        rec = _recentes(_historico(tmp), on_toggle_pause=lambda: None, on_open_settings=lambda: None)
        cfg = _config(tmp)
        faixas = {}
        for nome, w in (("recentes", rec), ("config", cfg)):
            for escala, sufixo in ((1, "-1x"), (2, "")):
                pm = _renderizar(w, escala)
                caminho = pasta / f"marca-{nome}{sufixo}.png"
                assert pm.save(str(caminho))
                saidas[caminho.name] = caminho
                faixas[(nome, escala)] = pm.copy(0, 0, pm.width(), 66 * escala)  # só o cabeçalho
        # folha comparativa em PIXELS FÍSICOS: cada cabeçalho como sairia em tela 100% (ampliado 2x sem suavizar,
        # para ver os pixels) e em tela 200% (960 px reais), um sobre o outro
        fisicas = []
        for (nome, escala), pm in faixas.items():
            pm = pm.copy()
            pm.setDevicePixelRatio(1.0)
            if escala == 1:
                pm = pm.scaled(pm.width() * 2, pm.height() * 2, Qt.IgnoreAspectRatio, Qt.FastTransformation)
            fisicas.append(pm)
        folha = QPixmap(max(p.width() for p in fisicas), sum(p.height() + 6 for p in fisicas))
        folha.fill(QColor("#2B2F36"))
        pintor = QPainter(folha)
        y = 0
        for pm in fisicas:
            pintor.drawPixmap(0, y, pm)
            y += pm.height() + 6
        pintor.end()
        caminho = pasta / "marca-cabecalhos.png"
        assert folha.save(str(caminho))
        saidas[caminho.name] = caminho

        # a engrenagem em repouso, com foco de teclado (Tab) e pressionada, ampliada 4x (pixels físicos)
        from PySide6.QtCore import QCoreApplication, QPoint

        ampliacao = 4
        rec.activateWindow()
        botao = rec.settings_btn
        celulas = []
        for rotulo in ("repouso", "foco", "pressionada"):
            if rotulo == "foco":
                botao.setFocus(Qt.TabFocusReason)
            else:
                botao.clearFocus()  # perder o foco solta o botão: por isso vem antes do setDown
            botao.setDown(rotulo == "pressionada")
            for _ in range(3):
                QCoreApplication.processEvents()
            pm = QPixmap(rec.width() * ampliacao, rec.height() * ampliacao)
            pm.setDevicePixelRatio(ampliacao)
            rec.render(pm, QPoint(0, 0))
            g = botao.geometry()
            recorte = pm.copy((g.x() - 6) * ampliacao, (g.y() - 6) * ampliacao,
                              (g.width() + 12) * ampliacao, (g.height() + 12) * ampliacao)
            recorte.setDevicePixelRatio(1.0)
            celulas.append(recorte)
        botao.setDown(False)
        folha = QPixmap(sum(c.width() + 24 for c in celulas) + 24, celulas[0].height() + 24)
        folha.fill(QColor("#07122B"))
        pintor = QPainter(folha)
        x = 24
        for c in celulas:
            pintor.drawPixmap(x, 12, c)
            x += c.width() + 24
        pintor.end()
        caminho = pasta / "marca-engrenagem.png"
        assert folha.save(str(caminho))
        saidas[caminho.name] = caminho
        for w in (rec, cfg):
            w.close()

    # o logotipo nas 4 densidades em PIXELS FÍSICOS (1 px da imagem = 1 px da tela): é o que cada tela mostra
    dprs = (1.0, 1.25, 1.5, 2.0)
    linha = 64
    folha = QPixmap(380, len(dprs) * linha)
    folha.fill(QColor("#07122B"))
    pintor = QPainter(folha)
    fonte = QFont("Segoe UI")
    fonte.setPixelSize(13)
    pintor.setFont(fonte)
    for i, dpr in enumerate(dprs):
        pm = logo.hear_ai_logotipo(24, dpr).copy()
        pm.setDevicePixelRatio(1.0)  # desenha pixel a pixel, sem reamostrar
        pintor.setPen(QColor("#9DB0CF"))
        pintor.drawText(QRect(12, i * linha, 120, linha), int(Qt.AlignVCenter), f"{int(dpr * 100)}% ({pm.height()} px)")
        pintor.drawPixmap(140, i * linha + (linha - pm.height()) // 2, pm)
    pintor.end()
    caminho = pasta / "marca-logotipo-escalas.png"
    assert folha.save(str(caminho))
    saidas[caminho.name] = caminho
    return saidas


def test_evidencias_sao_geradas_e_diferentes(qapp, tmp_path):
    saidas = gerar_evidencias(tmp_path)
    esperados = {"marca-recentes.png", "marca-recentes-1x.png", "marca-config.png", "marca-config-1x.png",
                 "marca-cabecalhos.png", "marca-engrenagem.png", "marca-logotipo-escalas.png"}
    assert set(saidas) == esperados
    assert all(p.stat().st_size > 2000 for p in saidas.values())
    assert len({p.read_bytes() for p in saidas.values()}) == len(esperados)


if __name__ == "__main__":
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv[:1])
    destino = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "evidencias"
    for nome, caminho in gerar_evidencias(destino).items():
        print(caminho)
