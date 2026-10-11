"""T41b — onda de voz reativa na bolha, ponta a ponta: BubbleModel + VisualizadorNivel + pintura.

Sinais sintéticos entram por `on_level` (RMS linear, como o Recorder.level) com relógio simulado; nada de
microfone, teclas ou rede. Qt só em modo offscreen (parte da pintura).

Também gera as evidências visuais:  .venv\\Scripts\\python.exe tests\\test_onda_bolha.py [pasta]
(padrão: evidencias/onda) -> silencio.png, fala-baixa.png, fala-normal.png, fala-forte.png (pílula, 2x DPI).
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402

import hear_ai.ui.bubble as bubble_mod  # noqa: E402
from hear_ai.ui.bubble import (BAR_MIN_H, BAR_RIGHT_SPACE, BAR_X_GAP, WIN_H, WIN_W, bar_height,  # noqa: E402
                               bar_max_height)
from hear_ai.ui.bubble_model import AppState, BubbleModel  # noqa: E402

HZ = 60.0  # o QTimer da bolha bate a ~60 Hz; a onda amostra a 30 Hz sozinha
RUIDO = 0.0008  # ruído ambiente típico de sala (~ -62 dBFS)
MAX_H = bar_max_height(BubbleModel.CIRCLE_H)  # altura máxima de barra na pílula (px)
ESCALA = 2  # 2x DPI nas capturas


class Relogio:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def fala(pico: float):
    """Fala sintética: palavras de 1 a 4 sílabas (0,17 s), vales de ~-15 dB entre sílabas, pausa de 0,22 s
    entre palavras. Devolve f(t) -> RMS linear; `pico` é o RMS da sílaba mais forte."""
    palavras = [[0.8, 1.0, 0.6], [0.9, 0.7], [1.0, 0.55, 0.85, 0.6], [0.75], [0.95, 0.65, 1.0]] * 4
    seg, t = [], 0.0
    for p in palavras:
        for a in p:
            seg.append((t, t + 0.17, a * pico))
            t += 0.17
        t += 0.22

    def f(tt: float) -> float:
        for ini, fim, amp in seg:
            if ini <= tt < fim:
                nucleo = math.sin(math.pi * (tt - ini) / (fim - ini)) ** 2
                return RUIDO + 0.17 * amp + (amp - 0.17 * amp) * nucleo
        return RUIDO

    return f


class Cena:
    """Modelo + relógio simulado. `tocar` injeta o sinal a 60 Hz e avança o tempo como o QTimer faria."""

    def __init__(self, t0: float = 0.0):
        self.clock = Relogio(t0)
        self.model = BubbleModel(clock=self.clock)
        self.model.on_state(AppState.RECORDING)
        self.model.tick(self.clock())

    def tocar(self, sinal, segundos: float) -> None:
        t_ini = self.clock()
        for i in range(max(1, round(segundos * HZ))):
            self.clock.t += 1.0 / HZ
            self.model.on_level(sinal(self.clock.t - t_ini) if callable(sinal) else sinal)
            self.model.tick(self.clock())

    def estado(self, st: AppState) -> None:
        self.model.on_state(st)
        self.model.tick(self.clock())

    def barras(self) -> tuple[float, ...]:
        return self.model.frame(self.clock()).bars

    def px(self) -> list[float]:
        return [bar_height(b, MAX_H) for b in self.barras()]


# --- lógica (sem Qt) -----------------------------------------------------------------------------

def test_silencio_todas_as_barras_no_minimo():
    c = Cena()
    c.tocar(RUIDO, 3.0)
    assert all(h <= BAR_MIN_H + 0.01 for h in c.px())
    assert all(b == 0.0 for b in c.barras())


def test_silencio_com_tremida_leve_do_ruido_continua_nos_pontinhos():
    c = Cena()
    c.tocar(lambda t: RUIDO * (1.0 + 0.10 * math.sin(t * 37.0)), 3.0)  # +-0,8 dB, abaixo do gate
    assert all(h <= BAR_MIN_H + 0.01 for h in c.px())


def test_silencio_absoluto_zero_digital_tambem():
    c = Cena()
    c.tocar(0.0, 2.0)
    assert all(h <= BAR_MIN_H + 0.01 for h in c.px())


@pytest.mark.parametrize("rms", [0.005, 0.02, 0.1])
def test_seno_enche_a_barra_proporcional_ao_volume(rms):
    c = Cena()
    c.tocar(RUIDO, 1.5)
    c.tocar(rms, 1.0)
    assert max(c.px()) > BAR_MIN_H + 8  # fala de verdade nunca fica em pontinho


def test_seno_rms_005_020_100_alturas_estritamente_crescentes_em_px():
    maximos = []
    for rms in (0.005, 0.02, 0.1):
        c = Cena()
        c.tocar(RUIDO, 1.5)
        c.tocar(rms, 1.0)
        maximos.append(max(c.px()))
    assert maximos[0] < maximos[1] < maximos[2]
    assert maximos[1] - maximos[0] >= 3.0 and maximos[2] - maximos[1] >= 3.0, maximos


def test_rajada_sobe_e_depois_decai():
    c = Cena()
    c.tocar(RUIDO, 1.5)
    c.tocar(0.1, 0.4)
    pico = max(c.px())
    assert pico >= MAX_H - 0.5
    c.tocar(RUIDO, 0.05)
    nova = c.px()[-1]
    assert 0.5 * pico < nova < pico + 0.01  # a barra mais nova desce devagar, não some de uma vez
    ultimas = []
    for _ in range(12):
        c.tocar(RUIDO, 0.05)
        ultimas.append(c.px()[-1])
    assert all(b <= a + 1e-9 for a, b in zip(ultimas, ultimas[1:]))  # só desce
    assert ultimas[-1] <= BAR_MIN_H + 2.0  # 0,65 s depois: cauda exponencial (tau 0,2 s) quase no chão
    c.tocar(RUIDO, 1.0)  # o histórico (0,6 s) rola para fora: sobra só a cauda sub-pixel
    assert max(c.px()) <= BAR_MIN_H + 0.5


def test_silabas_geram_barras_diferentes_entre_si():
    c = Cena()
    c.tocar(RUIDO, 1.5)
    c.tocar(fala(0.05), 2.0)
    px = c.px()
    assert len({round(h) for h in px}) >= 4  # não é uma fileira de barras iguais
    assert max(px) - min(px) >= 4  # sílabas contínuas ondulam pouco; os vales fundos são as pausas (teste abaixo)


def test_onda_segue_o_sinal_pausa_vira_vale_e_nao_uma_forma_fixa():
    def com_pausa(t):  # 0,25 s de fala, 0,30 s de pausa, 0,25 s de fala (cabe nos 0,6 s do histórico)
        return 0.06 if (t < 0.25 or 0.55 <= t < 0.80) else RUIDO

    c = Cena()
    c.tocar(RUIDO, 1.5)
    c.tocar(com_pausa, 0.80)
    px = c.px()
    meio = min(px[4:13])  # a pausa fica no meio da janela
    assert meio < min(px[0], px[1]) - 10 or meio < max(px) - 15
    assert px[-1] > meio + 10 and max(px[:6]) > meio + 10

    # sinal espelhado => forma espelhada (uma forma fixa por barra daria o mesmo desenho nos dois)
    def espelhado(t):
        return 0.06 if (0.30 <= t < 0.55 or t >= 0.55 + 0.30 + 0.05) else RUIDO

    d = Cena()
    d.tocar(RUIDO, 1.5)
    d.tocar(espelhado, 0.92)
    assert [round(h) for h in d.px()] != [round(h) for h in px]


def test_a_onda_anda_da_direita_para_a_esquerda():
    c = Cena()
    c.tocar(RUIDO, 1.5)
    c.tocar(0.1, 0.12)  # rajada curta
    b0 = c.barras()
    i0 = max(range(len(b0)), key=lambda i: b0[i])
    assert i0 >= len(b0) - 4  # acabou de entrar pelo lado direito
    c.tocar(RUIDO, 0.30)  # 0,3 s a 30 Hz = ~9 barras
    b1 = c.barras()
    i1 = max(range(len(b1)), key=lambda i: b1[i])
    assert 6 <= i0 - i1 <= 12, (i0, i1)


def test_ao_parar_de_gravar_as_barras_vao_a_zero():
    c = Cena()
    c.tocar(RUIDO, 1.0)
    c.tocar(fala(0.05), 1.5)
    assert max(c.barras()) > 0.5
    c.estado(AppState.TRANSCRIBING)
    antes = max(c.barras())
    c.tocar(0.1, 0.1)  # nível que chegar depois de parar é ignorado
    assert max(c.barras()) <= antes
    c.tocar(RUIDO, 1.0)
    assert all(b == 0.0 for b in c.barras())
    c.estado(AppState.IDLE)
    c.tocar(0.1, 0.5)
    assert all(b == 0.0 for b in c.barras())


def test_nova_gravacao_comeca_limpa():
    c = Cena()
    c.tocar(RUIDO, 1.0)
    c.tocar(fala(0.05), 1.5)
    c.estado(AppState.TRANSCRIBING)
    c.estado(AppState.IDLE)
    c.tocar(RUIDO, 0.1)  # menos que o tempo de queda: sobras seriam visíveis se não houvesse reset
    c.estado(AppState.RECORDING)
    assert all(b == 0.0 for b in c.barras())  # nada da gravação anterior
    # outra sala, bem mais barulhenta: o piso é reaprendido do zero, então ruído constante não enche barras
    c.tocar(0.02, 3.0)
    assert all(h <= BAR_MIN_H + 0.01 for h in c.px())


def test_ruido_de_fundo_alto_a_onda_se_adapta():
    c = Cena()
    c.tocar(RUIDO, 1.5)
    c.tocar(0.05, 0.3)  # a sala fica barulhenta de repente
    assert max(c.px()) > MAX_H * 0.8
    c.tocar(0.05, 9.0)
    assert max(c.px()) <= BAR_MIN_H + 3.0  # voltou a ficar baixa sozinha


def test_zeros_do_aquecimento_do_microfone_nao_fixam_o_piso():
    # Recorder.level() vale 0.0 até o 1º bloco chegar; se isso virasse "silêncio medido" (-114 dB), o ruído
    # normal da sala (-60 dB) encheria as barras no começo de toda gravação
    c = Cena()
    c.tocar(0.0, 0.10)
    for _ in range(30):
        c.tocar(0.001, 0.1)
        assert max(c.px()) <= BAR_MIN_H + 3.0
    # silêncio digital de verdade (muito além do aquecimento) continua sendo silêncio medido
    d = Cena()
    d.tocar(0.0, 0.6)
    d.tocar(0.05, 0.2)
    assert max(d.px()) > MAX_H * 0.8


def test_relogio_monotonico_grande_nao_trava_a_interface():
    # time.monotonic() pode valer milhões de segundos; o visualizador recebe tempo relativo à gravação
    c = Cena(t0=5_000_000.0)
    ini = time.perf_counter()
    c.tocar(RUIDO, 1.5)
    c.tocar(fala(0.05), 2.0)
    assert time.perf_counter() - ini < 2.0
    assert max(c.px()) > 15


@pytest.mark.parametrize("ruim", [float("nan"), -1.0, 5.0, None, "x", float("inf")])
def test_nivel_invalido_nao_quebra_e_barras_ficam_em_0_1(ruim):
    c = Cena()
    c.tocar(RUIDO, 0.5)
    c.model.on_level(ruim)
    c.tocar(RUIDO, 0.3)
    assert all(0.0 <= b <= 1.0 for b in c.barras())
    assert len(c.barras()) == BubbleModel.N_BARS


def test_nao_ha_mais_forma_fixa_nem_tremor_falso():
    assert not hasattr(bubble_mod, "BAR_SHAPE")
    codigo = Path(bubble_mod.__file__).read_text(encoding="utf-8")
    assert "wobble" not in codigo
    assert bar_height(0.0, MAX_H) == BAR_MIN_H and bar_height(1.0, MAX_H) == MAX_H
    assert bar_height(float("nan"), MAX_H) == BAR_MIN_H and bar_height(7.0, MAX_H) == MAX_H


# --- pintura (Qt offscreen) ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def montar_widget(sinal, segundos: float, t_gravando: float = 0.0):
    """Widget real (Qt offscreen) gravando; `sinal(t)` é o RMS em função do tempo desde que começou a gravar."""
    from hear_ai.ui.bubble import BubbleWidget

    clock = Relogio(100.0)
    w = BubbleWidget(clock=clock, level_source=lambda: sinal(clock.t - t0))
    t0 = clock.t
    w.driver.state("recording")
    for _ in range(round(segundos * HZ)):
        clock.t += 1.0 / HZ
        w._tick()
    w.timer.stop()
    return w, clock


def renderizar(w):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPixmap

    pm = QPixmap(WIN_W * ESCALA, WIN_H * ESCALA)
    pm.setDevicePixelRatio(ESCALA)
    pm.fill(Qt.transparent)
    w.render(pm, QPoint(0, 0))
    return pm


def alturas_pintadas(pm) -> list[float]:
    """Altura visível (px lógicos) de cada barra, medida nos pixels da imagem (coluna central da barra)."""
    img = pm.toImage()
    n = BubbleModel.N_BARS
    left = (WIN_W - BubbleModel.PILL_W) / 2
    x0 = left + BubbleModel.PILL_H + BAR_X_GAP
    x1 = left + BubbleModel.PILL_W - BAR_RIGHT_SPACE
    step = (x1 - x0) / n
    out = []
    for i in range(n):
        x = int((x0 + (i + 0.5) * step) * ESCALA)
        ys = [y for y in range(img.height())
              if (c := img.pixelColor(x, y)).alpha() > 200 and c.green() > 90 and c.blue() > 150]
        out.append((max(ys) - min(ys) + 1) / ESCALA if ys else 0.0)
    return out


def test_pintura_reflete_o_historico_em_pixels(qapp):
    w, clock = montar_widget(lambda t: RUIDO if t < 1.3 else fala(0.05)(t - 1.3), 3.3)
    esperado = [bar_height(b, MAX_H) for b in w.frame().bars]
    medido = alturas_pintadas(renderizar(w))
    assert len(medido) == BubbleModel.N_BARS
    assert all(abs(m - e) <= 1.5 for m, e in zip(medido, esperado)), (medido, esperado)
    assert max(medido) - min(medido) >= 4  # barras diferentes entre si na tela, não só no modelo
    w.deleteLater()


def test_pintura_silencio_pontinhos_e_fala_enche_a_pilula(qapp):
    w, _ = montar_widget(lambda t: RUIDO, 3.0)
    assert all(m <= BAR_MIN_H + 1.0 for m in alturas_pintadas(renderizar(w)))
    w.deleteLater()
    w, _ = montar_widget(lambda t: RUIDO if t < 1.3 else 0.08, 2.3)
    assert max(alturas_pintadas(renderizar(w))) >= 0.6 * BubbleModel.PILL_H  # ocupa bem a pílula
    w.deleteLater()


def test_repaint_no_maximo_30_por_segundo_gravando(qapp):
    from hear_ai.ui.bubble import BubbleWidget

    clock = Relogio(100.0)
    t0 = clock.t
    fala_f = fala(0.05)
    w = BubbleWidget(clock=clock, level_source=lambda: RUIDO if clock.t - t0 < 1.3 else fala_f(clock.t - t0 - 1.3))
    w.driver.state("recording")
    for _ in range(round(1.0 * HZ)):  # deixa a pílula abrir e a onda começar
        clock.t += 1.0 / HZ
        w._tick()
    chamadas = {"n": 0}
    w.update = lambda *a: chamadas.__setitem__("n", chamadas["n"] + 1)  # conta os pedidos de repaint
    ticks = round(3.0 * HZ)
    for _ in range(ticks):
        clock.t += 1.0 / HZ
        w._tick()
    w.timer.stop()
    por_segundo = chamadas["n"] / 3.0
    assert por_segundo <= 32.0, por_segundo  # 180 ticks (60 Hz) -> no máximo ~30 repaints/s
    assert por_segundo >= 10.0, por_segundo  # e a onda realmente se mexe
    w.deleteLater()


def test_estado_visual_diferente_repinta_mesmo_com_frame_igual(qapp):
    from hear_ai.ui.bubble import BubbleWidget

    clock = Relogio(100.0)
    w = BubbleWidget(clock=clock)
    w.kick()
    for _ in range(30):
        clock.t += 1.0 / HZ
        w._tick()
    chamadas = {"n": 0}
    w.update = lambda *a: chamadas.__setitem__("n", chamadas["n"] + 1)
    w.driver.loading(True)  # só o estado visual muda (IDLE -> LOADING): precisa repintar
    assert chamadas["n"] >= 1
    w.timer.stop()
    w.deleteLater()


# --- evidência visual ----------------------------------------------------------------------------

def silabas_continuas(pico: float, vale_db: float = 20.0, silaba: float = 0.20):
    """Sílabas seguidas, sem pausa (pior caso para a ondulação): vale `vale_db` abaixo do pico a cada sílaba."""
    vale = 10 ** (-vale_db / 20)

    def f(t: float) -> float:
        env = vale + (1 - vale) * math.sin(math.pi * ((t % silaba) / silaba)) ** 2
        return RUIDO + pico * env

    return f


FALA_DESDE = 1.3  # s de ruído ambiente antes da fala nas cenas
# A captura cai a 2,75 s de fala: a janela de 0,6 s da onda pega o fim de uma pausa, uma palavra de 1 sílaba,
# outra pausa e o começo da palavra seguinte — o desenho típico de fala com pausas entre palavras.
INSTANTE = FALA_DESDE + 2.75
CENAS = {  # nome -> (sinal(t desde que começou a gravar), instante da captura em s de gravação)
    "silencio": (lambda t: RUIDO, 3.2),
    "fala-baixa": (lambda t: RUIDO if t < FALA_DESDE else fala(0.01)(t - FALA_DESDE), INSTANTE),
    "fala-normal": (lambda t: RUIDO if t < FALA_DESDE else fala(0.05)(t - FALA_DESDE), INSTANTE),
    "fala-forte": (lambda t: RUIDO if t < FALA_DESDE else fala(0.25)(t - FALA_DESDE), INSTANTE),
    # extra: sílabas contínuas, sem pausa — mostra que a ondulação entre sílabas é discreta (ver docs/ONDA.md)
    "fala-normal-continua": (lambda t: RUIDO if t < FALA_DESDE else silabas_continuas(0.05)(t - FALA_DESDE), 3.3),
}
CENAS_PEDIDAS = ("silencio", "fala-baixa", "fala-normal", "fala-forte")


def gerar_evidencias(pasta) -> dict[str, list[float]]:
    """Grava os PNGs (pílula gravando, 2x DPI) em `pasta` e devolve as alturas pintadas de cada um."""
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    resultado = {}
    for nome, (sinal, ate) in CENAS.items():
        w, _ = montar_widget(sinal, ate)
        pm = renderizar(w)
        assert pm.save(str(pasta / f"{nome}.png"))
        resultado[nome] = alturas_pintadas(pm)
        w.deleteLater()
    return resultado


def test_evidencia_cenas_claramente_diferentes(qapp, tmp_path):
    res = gerar_evidencias(tmp_path)
    assert sorted(p.name for p in tmp_path.glob("*.png")) == sorted(f"{n}.png" for n in CENAS)
    pico = {nome: max(alt) for nome, alt in res.items()}
    assert pico["silencio"] <= BAR_MIN_H + 1.0
    assert pico["silencio"] + 8 < pico["fala-baixa"] < pico["fala-normal"] - 5
    assert pico["fala-normal"] >= 0.6 * BubbleModel.PILL_H  # a fala normal ocupa a pílula
    assert pico["fala-forte"] >= pico["fala-normal"]
    # a captura "típica" tem pausa na janela: vale bem abaixo do pico (a onda anda com as palavras)
    assert max(res["fala-normal"]) - min(res["fala-normal"]) >= 8
    assert len({(tmp_path / f"{n}.png").read_bytes() for n in CENAS}) == len(CENAS)  # imagens distintas


if __name__ == "__main__":
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv[:1])
    destino = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "evidencias" / "onda"
    for nome, alt in gerar_evidencias(destino).items():
        print(f"{nome:12s} max={max(alt):5.1f}px min={min(alt):5.1f}px  {[round(a) for a in alt]}")
    print("salvo em", destino)
