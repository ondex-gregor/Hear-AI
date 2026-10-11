"""
VisualizadorNivel — lógica pura de onda de voz (sem Qt, sem rede).

Transforma RMS linear em barras 0..1 com:
- escala em dB
- piso de ruído adaptativo (percentil 10% dos últimos janela_piso_s)
- teto dinâmico (piso + faixa_db ou pico lento que decai)
- gate (margem_gate_db)
- suavização assimétrica (ataque/decimento)
- histórico rolante (deque)
"""

from __future__ import annotations
import math
from collections import deque
from typing import Deque


class VisualizadorNivel:
    """
    Visualizador de nível de áudio para onda de voz.

    Args:
        n_barras: Número de barras no histórico rolante.
        taxa_hz: Taxa de amostragem da onda (amostras/segundo).
        faixa_db: Faixa dinâmica mínima em dB entre piso e teto.
        ataque_s: Constante de tempo de ataque (subida rápida) em segundos.
        decaimento_s: Constante de tempo de decaimento (descida lenta) em segundos.
        janela_piso_s: Janela temporal para estimar o piso de ruído (segundos).
        margem_gate_db: Margem em dB acima do piso para ativar o gate (silêncio forçado).
    """

    def __init__(
        self,
        n_barras: int = 20,
        taxa_hz: float = 30.0,
        faixa_db: float = 38.0,
        ataque_s: float = 0.06,
        decaimento_s: float = 0.2,
        janela_piso_s: float = 2.5,
        margem_gate_db: float = 4.0,
    ) -> None:
        if n_barras <= 0:
            raise ValueError("n_barras deve ser > 0")
        if taxa_hz <= 0:
            raise ValueError("taxa_hz deve ser > 0")
        if faixa_db <= 0:
            raise ValueError("faixa_db deve ser > 0")
        if ataque_s <= 0:
            raise ValueError("ataque_s deve ser > 0")
        if decaimento_s <= 0:
            raise ValueError("decaimento_s deve ser > 0")
        if janela_piso_s <= 0:
            raise ValueError("janela_piso_s deve ser > 0")
        if margem_gate_db < 0:
            raise ValueError("margem_gate_db deve ser >= 0")

        self._n_barras = n_barras
        self._taxa_hz = taxa_hz
        self._dt_amostra = 1.0 / taxa_hz
        self._faixa_db = faixa_db
        self._ataque_s = ataque_s
        self._decaimento_s = decaimento_s
        self._janela_piso_s = janela_piso_s
        self._margem_gate_db = margem_gate_db

        # Histórico de dB para cálculo do piso (percentil 10%)
        self._db_history: Deque[tuple[float, float]] = deque()  # (timestamp, db)
        # Piso de ruído adaptativo (valor suavizado do percentil 10%)
        self._piso = -120.0
        # Pico lento que decai para o teto
        self._pico_lento = -120.0
        # Valor suavizado atual (0..1)
        self._valor_suave = 0.0
        # Último timestamp processado
        self._ultimo_t = 0.0
        # Próximo timestamp para empurrar no histórico rolante
        self._proximo_push_t = 0.0
        # Histórico rolante de valores suavizados (0..1) — mais antigo à esquerda
        self._barras: Deque[float] = deque(maxlen=n_barras)
        # Inicializa com zeros
        for _ in range(n_barras):
            self._barras.append(0.0)

        # Tau para suavizar o piso em direção ao percentil calculado
        self._piso_tau = 1.0
        # Antes da 1ª amostra não há referência: piso e pico partem do 1º dB medido (T41, líder)
        self._iniciado = False

    def _calcular_piso_candidato(self, agora: float) -> float:
        """Calcula o piso candidato como percentil 10% dos dB na janela."""
        # Remove entradas antigas
        corte = agora - self._janela_piso_s
        while self._db_history and self._db_history[0][0] < corte:
            self._db_history.popleft()

        if not self._db_history:
            return -120.0

        # Extrai apenas os valores dB
        dbs = [db for _, db in self._db_history]
        dbs.sort()
        # Percentil ~10% (índice int(len * 0.1))
        idx = max(0, int(len(dbs) * 0.1))
        return dbs[idx]

    def _atualizar_piso(self, dt: float, agora: float) -> None:
        """
        Atualiza o piso de ruído adaptativo suavemente em direção ao percentil 10%.
        O percentil 10% ignora naturalmente a fala (topo 90%), então não precisa
        de lógica condicional complexa.
        """
        if dt <= 0:
            return

        piso_candidato = self._calcular_piso_candidato(agora)

        # Suaviza o piso em direção ao candidato
        # Tau curto para resposta rápida a mudanças de ruído de fundo
        alpha = math.exp(-dt / self._piso_tau)
        self._piso = self._piso * alpha + piso_candidato * (1 - alpha)

        # Garante que o piso nunca ultrapasse -120 dB (limite teórico)
        if self._piso < -120.0:
            self._piso = -120.0

    def _atualizar_pico_lento(self, db: float, dt: float) -> None:
        """Atualiza o pico lento que decai exponencialmente (tau ~2s)."""
        if dt <= 0:
            return
        if db > self._pico_lento:
            self._pico_lento = db
        else:
            tau_pico = 2.0
            alpha = math.exp(-dt / tau_pico)
            self._pico_lento = self._pico_lento * alpha + db * (1 - alpha)

    def amostra(self, rms: float, agora: float) -> None:
        """
        Processa uma amostra de RMS.

        Args:
            rms: Valor RMS linear (0..1, pode vir NaN, negativo ou >1).
            agora: Timestamp atual em segundos (monotônico).
        """
        # Robustez: sanitiza rms
        if not isinstance(rms, (int, float)) or math.isnan(rms) or rms <= 0.0:
            rms = 1e-6
        elif rms > 1.0:
            rms = 1.0

        # Robustez: sanitiza agora
        if not isinstance(agora, (int, float)) or math.isnan(agora) or agora < self._ultimo_t:
            agora = self._ultimo_t
        dt = agora - self._ultimo_t
        if dt < 0:
            dt = 0.0
        self._ultimo_t = agora

        # Converte para dB
        db = 20.0 * math.log10(rms + 1e-6)

        if not self._iniciado:
            # sem isto o piso partiria de -120 dB e o 1º segundo de silêncio encheria as barras
            self._iniciado = True
            self._piso = db
            self._pico_lento = db
            # o relógio é monotônico do sistema (pode estar em 200 000 s): o histórico começa AGORA
            self._proximo_push_t = agora

        # Adiciona ao histórico para cálculo do piso
        self._db_history.append((agora, db))

        # Atualiza piso adaptativo (percentil 10% suavizado)
        self._atualizar_piso(dt, agora)

        # Atualiza pico lento
        self._atualizar_pico_lento(db, dt)

        # Teto = max(piso + faixa_db, pico_lento)
        teto = max(self._piso + self._faixa_db, self._pico_lento)

        # Mapeia para 0..1
        if teto <= self._piso:
            valor_bruto = 0.0
        else:
            valor_bruto = (db - self._piso) / (teto - self._piso)

        # Gate: abaixo de piso + margem_gate_db → 0
        if db < self._piso + self._margem_gate_db:
            valor_bruto = 0.0

        # Clampa
        if valor_bruto < 0.0:
            valor_bruto = 0.0
        elif valor_bruto > 1.0:
            valor_bruto = 1.0

        # Suavização assimétrica (ataque/decimento)
        if dt > 0:
            if valor_bruto > self._valor_suave:
                # Ataque: subida rápida
                tau = self._ataque_s
            else:
                # Decaimento: descida lenta
                tau = self._decaimento_s
            if tau > 0:
                alpha = math.exp(-dt / tau)
                self._valor_suave = self._valor_suave * alpha + valor_bruto * (1 - alpha)
            else:
                self._valor_suave = valor_bruto

        # Empurra no histórico rolante a cada 1/taxa_hz
        if agora >= self._proximo_push_t - 1e-9:
            # Se o relógio pulou, empurra uma vez por amostra perdida — no máximo n_barras vezes
            # (mais que isso não muda o desenho; sem o limite um salto grande travaria a interface)
            perdidas = int((agora - self._proximo_push_t) / self._dt_amostra + 1e-9) + 1
            for _ in range(min(perdidas, self._n_barras)):
                self._barras.append(self._valor_suave)
            self._proximo_push_t += perdidas * self._dt_amostra

    def barras(self) -> tuple[float, ...]:
        """
        Retorna o histórico rolante como tupla de floats 0..1.

        O valor mais antigo está no índice 0 (esquerda),
        o mais recente no índice -1 (direita).
        """
        return tuple(self._barras)

    def reset(self) -> None:
        """Reseta todo o estado interno."""
        self._db_history.clear()
        self._piso = -120.0
        self._pico_lento = -120.0
        self._valor_suave = 0.0
        self._ultimo_t = 0.0
        self._proximo_push_t = 0.0
        self._iniciado = False
        self._barras.clear()
        for _ in range(self._n_barras):
            self._barras.append(0.0)