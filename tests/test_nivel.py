"""
Testes para VisualizadorNivel — sinais sintéticos, relógio simulado.
"""

from __future__ import annotations
import math
import pytest
from hear_ai.ui.nivel import VisualizadorNivel


def _rms_from_amplitude(amplitude: float) -> float:
    """RMS de uma senoide de amplitude A é A/sqrt(2)."""
    return amplitude / math.sqrt(2)


class TestVisualizadorNivel:
    """Testes do VisualizadorNivel."""

    def test_silencio_todas_barras_zero(self) -> None:
        """Silêncio (rms 1e-5 por 3 s) → todas as barras ≈ 0."""
        viz = VisualizadorNivel(n_barras=20, taxa_hz=30.0)
        t = 0.0
        dt = 1.0 / 30.0
        for _ in range(int(3.0 / dt)):  # 3 segundos
            viz.amostra(1e-5, t)
            t += dt
        barras = viz.barras()
        # Todas as barras devem ser muito próximas de 0
        for v in barras:
            assert v < 0.05, f"Barra {v} não está próxima de 0 no silêncio"

    def test_seno_amplitudes_crescentes(self) -> None:
        """
        Seno de amplitude 0.005 / 0.02 / 0.1 (rms = A/√2),
        cada um depois de 2 s de ruído baixo (rms 0.001)
        → valor máximo das barras estritamente crescente;
        converte para pixels com h = 3 + v*(30-3) e exige diferença ≥ 3 px.
        """
        viz = VisualizadorNivel(n_barras=30, taxa_hz=30.0)
        amplitudes = [0.005, 0.02, 0.1]
        maximos = []
        t = 0.0
        dt = 1.0 / 30.0

        for amp in amplitudes:
            # 2s de ruído baixo para estabilizar o piso
            for _ in range(int(2.0 / dt)):
                viz.amostra(0.001, t)
                t += dt
            # 2s do sinal de teste
            rms = _rms_from_amplitude(amp)
            for _ in range(int(2.0 / dt)):
                viz.amostra(rms, t)
                t += dt
            # Pega o máximo das barras após o sinal
            barras = viz.barras()
            maximos.append(max(barras))

        # Converte para pixels: h = 3 + v * (30 - 3) = 3 + v * 27
        pixels = [3 + v * 27 for v in maximos]
        print(f"Máximos (0-1): {maximos}")
        print(f"Pixels: {pixels}")

        # Estritamente crescente
        assert maximos[0] < maximos[1] < maximos[2], "Máximos não são estritamente crescentes"

        # Diferença ≥ 3 px entre níveis vizinhos
        for i in range(len(pixels) - 1):
            diff = pixels[i + 1] - pixels[i]
            assert diff >= 3.0, f"Diferença de pixels entre nível {i} e {i+1} é {diff:.1f} < 3"

    def test_rajada_sobe_e_decaí(self) -> None:
        """Rajada (0.5 s de 0.1) seguida de silêncio → sobe e depois decai a < 0.1 em ≤ 1 s."""
        viz = VisualizadorNivel(n_barras=40, taxa_hz=30.0)
        t = 0.0
        dt = 1.0 / 30.0

        # 1s de ruído baixo para estabilizar
        for _ in range(int(1.0 / dt)):
            viz.amostra(0.001, t)
            t += dt

        # 0.5s de rajada (rms 0.1)
        for _ in range(int(0.5 / dt)):
            viz.amostra(0.1, t)
            t += dt

        # Verifica que subiu (última barra reflete o nível atual)
        barras_durante = viz.barras()
        assert barras_durante[-1] > 0.5, f"Não subiu durante a rajada: última barra={barras_durante[-1]:.3f}"

        # Silêncio por 1s
        for _ in range(int(1.0 / dt)):
            viz.amostra(1e-5, t)
            t += dt

        # Verifica que decaiu a < 0.1 (última barra = nível atual)
        barras_depois = viz.barras()
        assert barras_depois[-1] < 0.1, f"Não decaiu a < 0.1 em 1s: última barra={barras_depois[-1]:.3f}"
        
        # Também verifica que as barras recentes (últimas 10) estão baixas
        recentes = barras_depois[-10:]
        assert max(recentes) < 0.15, f"Barras recentes não decaíram: max={max(recentes):.3f}"

    def test_ruido_fundo_alto_adapta(self) -> None:
        """Ruído de fundo constante alto (rms 0.05 por 4 s) → onda se adapta e barras voltam a ficar baixas (< 0.2)."""
        viz = VisualizadorNivel(n_barras=30, taxa_hz=30.0)
        t = 0.0
        dt = 1.0 / 30.0

        # 4s de ruído alto constante
        for _ in range(int(4.0 / dt)):
            viz.amostra(0.05, t)
            t += dt

        barras = viz.barras()
        max_barra = max(barras)
        media = sum(barras) / len(barras)
        print(f"Ruído alto 4s: max={max_barra:.3f}, média={media:.3f}")

        # Após adaptação, as barras devem ficar baixas (< 0.2)
        assert max_barra < 0.2, f"Não adaptou ao ruído alto: max={max_barra:.3f}"
        assert media < 0.15, f"Média não baixou: media={media:.3f}"

    def test_silabas_barras_diferentes(self) -> None:
        """
        "Sílabas" (envelope alternando 0.08 / 0.005 a cada ~150 ms)
        → barras diferentes entre si (desvio padrão > 0.1), não todas iguais.
        """
        viz = VisualizadorNivel(n_barras=40, taxa_hz=30.0)
        t = 0.0
        dt = 1.0 / 30.0

        # 1s de ruído baixo para estabilizar
        for _ in range(int(1.0 / dt)):
            viz.amostra(0.001, t)
            t += dt

        # Alterna entre 0.08 e 0.005 a cada ~150ms (4.5 amostras a 30Hz)
        # Faz por 3 segundos = 90 amostras
        for i in range(90):
            if (i // 5) % 2 == 0:
                rms = 0.08  # "sílaba"
            else:
                rms = 0.005  # pausa
            viz.amostra(rms, t)
            t += dt

        barras = viz.barras()
        # Calcula desvio padrão
        media = sum(barras) / len(barras)
        var = sum((v - media) ** 2 for v in barras) / len(barras)
        std = math.sqrt(var)
        print(f"Sílabas: barras={list(barras)}, média={media:.3f}, std={std:.3f}")

        # Desvio padrão > 0.1 (barras variam)
        assert std > 0.1, f"Barras muito uniformes (std={std:.3f}), esperado > 0.1"

        # Não todas iguais (verifica que há variação real)
        assert max(barras) - min(barras) > 0.2, "Barras quase todas iguais"

    def test_entradas_invalidas_nao_levantam_excecao(self) -> None:
        """Entradas inválidas não levantam exceção."""
        viz = VisualizadorNivel(n_barras=10, taxa_hz=30.0)
        t = 0.0
        dt = 1.0 / 30.0

        # NaN
        viz.amostra(float('nan'), t)
        t += dt
        # Negativo
        viz.amostra(-0.5, t)
        t += dt
        # > 1
        viz.amostra(5.0, t)
        t += dt
        # None (será tratado como inválido)
        try:
            viz.amostra(None, t)  # type: ignore
        except TypeError:
            pytest.fail("Não deve levantar TypeError para None")
        t += dt
        # agora não avança
        viz.amostra(0.1, t)
        # agora volta no tempo
        viz.amostra(0.1, t - 1.0)

        # Deve chegar aqui sem exceção
        barras = viz.barras()
        assert len(barras) == 10
        for v in barras:
            assert 0.0 <= v <= 1.0, f"Valor fora de range: {v}"

    def test_reset_limpa_estado(self) -> None:
        """Reset limpa todo o estado."""
        viz = VisualizadorNivel(n_barras=10, taxa_hz=30.0)
        t = 0.0
        dt = 1.0 / 30.0

        # silêncio e depois sinal forte
        for i in range(30):
            viz.amostra(0.001 if i < 15 else 0.5, t)
            t += dt

        barras_antes = viz.barras()
        assert max(barras_antes) > 0.5

        viz.reset()

        barras_depois = viz.barras()
        assert all(v == 0.0 for v in barras_depois), "Reset não zerou as barras"

    def test_parametros_invalidos_levantam_excecao(self) -> None:
        """Parâmetros inválidos no construtor levantam ValueError."""
        with pytest.raises(ValueError):
            VisualizadorNivel(n_barras=0)
        with pytest.raises(ValueError):
            VisualizadorNivel(taxa_hz=0)
        with pytest.raises(ValueError):
            VisualizadorNivel(faixa_db=0)
        with pytest.raises(ValueError):
            VisualizadorNivel(ataque_s=0)
        with pytest.raises(ValueError):
            VisualizadorNivel(decaimento_s=0)
        with pytest.raises(ValueError):
            VisualizadorNivel(janela_piso_s=0)
        with pytest.raises(ValueError):
            VisualizadorNivel(margem_gate_db=-1)


    def test_partida_a_frio_silencio_nao_enche_as_barras(self) -> None:
        """Sem aquecimento: silêncio real de microfone (rms 0,001) desde o 1º instante fica baixo."""
        viz = VisualizadorNivel(n_barras=20, taxa_hz=30.0)
        t = 0.0
        for _ in range(30):  # 1 s
            viz.amostra(0.001, t)
            t += 1.0 / 30.0
        assert max(viz.barras()) < 0.05

    def test_partida_a_frio_fala_logo_depois_do_silencio_sobe(self) -> None:
        viz = VisualizadorNivel(n_barras=20, taxa_hz=30.0)
        t = 0.0
        for _ in range(15):
            viz.amostra(0.001, t)
            t += 1.0 / 30.0
        for _ in range(15):
            viz.amostra(0.05, t)
            t += 1.0 / 30.0
        assert max(viz.barras()) > 0.6

    def test_relogio_do_sistema_alto_nao_trava(self) -> None:
        """monotonic() de um PC ligado há dias (~200 000 s): a 1ª amostra é instantânea."""
        import time as _time
        viz = VisualizadorNivel(n_barras=20, taxa_hz=30.0)
        t = 200_000.0
        t0 = _time.perf_counter()
        for _ in range(60):
            viz.amostra(0.001, t)
            t += 1.0 / 30.0
        for _ in range(15):
            viz.amostra(0.05, t)
            t += 1.0 / 30.0
        assert _time.perf_counter() - t0 < 0.05
        assert max(viz.barras()) > 0.6

    def test_salto_grande_de_relogio_limitado(self) -> None:
        """Salto de 1 h entre amostras: no máximo n_barras empurrões, e a cadência continua."""
        viz = VisualizadorNivel(n_barras=20, taxa_hz=30.0)
        viz.amostra(0.001, 10.0)
        viz.amostra(0.001, 3610.0)
        assert len(viz.barras()) == 20
        antes = viz.barras()
        viz.amostra(0.05, 3610.0 + 1.0 / 30.0)
        assert viz.barras() != antes  # a próxima amostra de 1/30 s depois ainda empurra
