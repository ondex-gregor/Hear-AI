"""Testes para ClickMachine (relógio simulado, Python puro)."""

import pytest
from hear_ai.ui.click_machine import ClickMachine, Action


class TestClickMachine:
    """Testes da máquina de estados de cliques."""

    def setup_method(self):
        """Configuração antes de cada teste."""
        self.machine = ClickMachine(double_click_ms=500, drag_px=4)

    def _click(self, now: float, app_state: str = "idle", pos: tuple[int, int] = (100, 100)) -> Action:
        """Helper: simula clique completo (press + release)."""
        self.machine.press(pos, now, "left")
        return self.machine.release(pos, now + 0.01, app_state, "left")

    def _drag(self, start_pos: tuple[int, int], end_pos: tuple[int, int], now: float) -> list[Action]:
        """Helper: simula arraste (press + move + release)."""
        actions = []
        actions.append(self.machine.press(start_pos, now, "left"))
        actions.append(self.machine.move(end_pos, now + 0.01))
        actions.append(self.machine.release(end_pos, now + 0.02, "idle", "left"))
        return actions

    # --- Cliques simples ---

    def test_clique_simples_parado_start(self):
        """Clique simples com app parado → START."""
        action = self._click(0.0, "idle")
        assert action == Action.START

    def test_clique_simples_gravando_stop(self):
        """Clique simples com app gravando → STOP."""
        action = self._click(0.0, "recording")
        assert action == Action.STOP

    def test_clique_em_transcribing_none(self):
        """Clique em transcribing → NONE."""
        action = self._click(0.0, "transcribing")
        assert action == Action.NONE

    def test_clique_em_outro_estado_none(self):
        """Clique em estado desconhecido → NONE."""
        action = self._click(0.0, "paused")
        assert action == Action.NONE

    # --- Duplo clique ---

    def test_duplo_clique_idle_start_discard_and_open(self):
        """
        Duplo clique a partir de idle:
        1º clique → START, 2º dentro do intervalo → DISCARD_AND_OPEN.
        """
        # 1º clique
        a1 = self._click(0.0, "idle")
        assert a1 == Action.START

        # 2º clique dentro de 500ms
        a2 = self._click(0.3, "idle")  # 300ms depois
        assert a2 == Action.DISCARD_AND_OPEN

    def test_duplo_clique_stop_open_recent(self):
        """
        Clique para parar (STOP) + clique extra dentro do intervalo → OPEN_RECENT.
        Não descarta a transcrição.
        """
        # 1º clique: app está gravando → STOP
        a1 = self._click(0.0, "recording")
        assert a1 == Action.STOP

        # 2º clique dentro de 500ms → OPEN_RECENT (sem descarte)
        a2 = self._click(0.3, "transcribing")
        assert a2 == Action.OPEN_RECENT

    def test_duplo_clique_primeiro_none_open_recent(self):
        """1º clique foi NONE (ex: app em transcribing) → 2º clique → OPEN_RECENT."""
        a1 = self._click(0.0, "transcribing")
        assert a1 == Action.NONE

        a2 = self._click(0.3, "transcribing")
        assert a2 == Action.OPEN_RECENT

    def test_segundo_clique_fora_intervalo_novo_primeiro(self):
        """2º clique FORA do intervalo de duplo clique → é um novo 1º clique."""
        # 1º clique
        a1 = self._click(0.0, "idle")
        assert a1 == Action.START

        # 2º clique DEPOIS de 500ms (ex: 600ms)
        a2 = self._click(0.6, "idle")
        # Como passou do intervalo, é um novo 1º clique → START novamente
        assert a2 == Action.START

    def test_triplo_clique_terceiro_engolido(self):
        """Triplo clique: 3º clique é engolido (NONE)."""
        # 1º clique
        a1 = self._click(0.0, "idle")
        assert a1 == Action.START

        # 2º clique dentro do intervalo → DISCARD_AND_OPEN
        a2 = self._click(0.2, "idle")
        assert a2 == Action.DISCARD_AND_OPEN

        # 3º clique dentro de mais um intervalo → engolido (NONE)
        a3 = self._click(0.4, "idle")
        assert a3 == Action.NONE

    # --- Arrastar ---

    def test_arrastar_4px_drag_start_drag_end_sem_start(self):
        """Arrastar ≥4px (Manhattan) → DRAG_START/DRAG_END, nenhum START."""
        actions = self._drag((100, 100), (104, 100), 0.0)  # dx=4, dy=0, manhattan=4
        assert actions[0] == Action.NONE  # press
        assert actions[1] == Action.DRAG_START  # move
        assert actions[2] == Action.DRAG_END  # release

    def test_arrastar_diagonal_manhattan(self):
        """Arrastar diagonal: distância de Manhattan ≥4px."""
        # dx=3, dy=1 → manhattan=4
        actions = self._drag((100, 100), (103, 101), 0.0)
        assert actions[1] == Action.DRAG_START
        assert actions[2] == Action.DRAG_END

    def test_movimento_3px_ainda_clique(self):
        """Movimento de 3px (Manhattan) → ainda é clique, não arraste."""
        self.machine.press((100, 100), 0.0, "left")
        move_action = self.machine.move((103, 100), 0.01)  # dx=3, manhattan=3
        release_action = self.machine.release((103, 100), 0.02, "idle", "left")

        assert move_action == Action.NONE  # Não passa do limite
        assert release_action == Action.START  # É um clique normal

    def test_arraste_nunca_conta_como_clique(self):
        """Arrastar nunca entra na contagem de duplo clique nem gera START/STOP."""
        # Faz um arraste
        self._drag((100, 100), (110, 100), 0.0)

        # Clique logo depois (dentro de 500ms do press do arraste)
        # Não deve ser tratado como 2º clique
        a = self._click(0.3, "idle")
        assert a == Action.START  # É um novo 1º clique

    # --- Botões não-esquerdos ---

    def test_botao_direito_none(self):
        """Botão direito → NONE."""
        self.machine.press((100, 100), 0.0, "right")
        action = self.machine.release((100, 100), 0.01, "idle", "right")
        assert action == Action.NONE

    def test_botao_meio_none(self):
        """Botão do meio → NONE."""
        self.machine.press((100, 100), 0.0, "middle")
        action = self.machine.release((100, 100), 0.01, "idle", "right")
        assert action == Action.NONE

    def test_press_botao_direito_none_imediato(self):
        """Press com botão direito retorna NONE imediatamente."""
        action = self.machine.press((100, 100), 0.0, "right")
        assert action == Action.NONE

    # --- Reset ---

    def test_reset_limpa_estado(self):
        """reset() limpa todo estado interno."""
        # Faz um clique
        self._click(0.0, "idle")
        # Faz reset
        self.machine.reset()
        # Novo clique deve funcionar como primeiro
        action = self._click(1.0, "idle")
        assert action == Action.START

    def test_reset_apos_duplo_clique(self):
        """reset() após duplo clique permite novo clique normal."""
        self._click(0.0, "idle")  # START
        self._click(0.2, "idle")  # DISCARD_AND_OPEN
        self.machine.reset()
        action = self._click(1.0, "idle")
        assert action == Action.START

    # --- Sequência mista ---

    def test_arrastar_depois_clicar(self):
        """Sequência: arrastar e depois clicar funciona normalmente."""
        # Arrasta
        self._drag((100, 100), (110, 100), 0.0)

        # Clica depois
        action = self._click(0.5, "idle")
        assert action == Action.START

    def test_clicar_depois_arrastar(self):
        """Sequência: clicar e depois arrastar funciona normalmente."""
        # Clica
        action = self._click(0.0, "idle")
        assert action == Action.START

        # Arrasta depois
        actions = self._drag((100, 100), (110, 100), 0.5)
        assert actions[1] == Action.DRAG_START
        assert actions[2] == Action.DRAG_END

    # --- Tempo de press longo sem movimento ---

    def test_press_longo_sem_movimento_ainda_clique(self):
        """Segurar 3s sem mexer → ainda é clique (movimento < drag_px)."""
        self.machine.press((100, 100), 0.0, "left")
        # Não chama move
        action = self.machine.release((100, 100), 3.0, "idle", "left")
        assert action == Action.START

    # --- Parâmetros customizados ---

    def test_double_click_ms_customizado(self):
        """double_click_ms customizado altera o intervalo."""
        machine = ClickMachine(double_click_ms=200, drag_px=4)
        machine.press((100, 100), 0.0, "left")
        a1 = machine.release((100, 100), 0.01, "idle", "left")
        assert a1 == Action.START

        # 2º clique a 250ms (fora do intervalo customizado de 200ms)
        machine.press((100, 100), 0.25, "left")
        a2 = machine.release((100, 100), 0.26, "idle", "left")
        assert a2 == Action.START  # Novo 1º clique

    def test_drag_px_customizado(self):
        """drag_px customizado altera limite de arraste."""
        machine = ClickMachine(double_click_ms=500, drag_px=10)
        machine.press((100, 100), 0.0, "left")
        # Move 5px - não passa do limite customizado de 10
        move_action = machine.move((105, 100), 0.01)
        release_action = machine.release((105, 100), 0.02, "idle", "left")
        assert move_action == Action.NONE
        assert release_action == Action.START

    # --- Estados internos ---

    def test_duplo_clique_em_estados_diferentes(self):
        """Testa duplo clique com app_state mudando entre cliques."""
        # 1º clique: app idle → START
        a1 = self._click(0.0, "idle")
        assert a1 == Action.START

        # 2º clique: app já está recording (simula início rápido)
        a2 = self._click(0.3, "recording")
        # 1º foi START → DISCARD_AND_OPEN
        assert a2 == Action.DISCARD_AND_OPEN

    def test_move_sem_press_nao_faz_nada(self):
        """move() sem press prévio → NONE."""
        action = self.machine.move((100, 100), 0.0)
        assert action == Action.NONE

    def test_release_sem_press_nao_faz_nada(self):
        """release() sem press prévio → NONE."""
        action = self.machine.release((100, 100), 0.0, "idle", "left")
        assert action == Action.NONE

    def test_press_durante_arraste_ignorado(self):
        """press() durante arraste (estado dragging) → NONE, não quebra estado."""
        self.machine.press((100, 100), 0.0, "left")
        self.machine.move((110, 100), 0.01)  # DRAG_START
        # Press extra durante arraste
        extra = self.machine.press((110, 100), 0.02, "left")
        assert extra == Action.NONE
        # Release final
        end = self.machine.release((110, 100), 0.03, "idle", "left")
        assert end == Action.DRAG_END

    def test_apos_duplo_clique_clique_engolido(self):
        """Após duplo clique, próximo clique dentro do intervalo é engolido."""
        self._click(0.0, "idle")      # START
        self._click(0.2, "idle")      # DISCARD_AND_OPEN
        # 3º clique a 0.4s (dentro de 500ms do 2º)
        a3 = self._click(0.4, "idle")
        assert a3 == Action.NONE

    def test_apos_duplo_clique_fora_intervalo_novo_clique(self):
        """Após duplo clique, clique FORA do intervalo é novo 1º clique."""
        self._click(0.0, "idle")      # START
        self._click(0.2, "idle")      # DISCARD_AND_OPEN
        # 3º clique a 0.7s (fora de 500ms do 2º)
        a3 = self._click(0.7, "idle")
        assert a3 == Action.START


    # --- casos de borda acrescentados pelo líder (revisão T42) ---

    def test_duplo_clique_com_relogio_comecando_em_zero(self):
        """1º clique em now=0 também conta para o duplo clique (antes `> 0` o ignorava)."""
        assert self._click(0.0, "idle") == Action.START
        assert self._click(0.2, "recording") == Action.DISCARD_AND_OPEN

    def test_quadruplo_clique_rapido_so_um_duplo(self):
        """Depois do duplo clique, TODOS os cliques da janela seguinte são engolidos."""
        assert self._click(10.0, "idle") == Action.START
        assert self._click(10.1, "recording") == Action.DISCARD_AND_OPEN
        assert self._click(10.2, "idle") == Action.NONE
        assert self._click(10.3, "idle") == Action.NONE
        # passada a janela, volta a ser um 1º clique normal
        assert self._click(11.0, "idle") == Action.START

    def test_arraste_limpa_primeiro_clique_pendente(self):
        """Clique (START) seguido de arraste: o próximo clique é um 1º clique, não um duplo."""
        assert self._click(20.0, "idle") == Action.START
        acts = self._drag((100, 100), (130, 100), 20.1)
        assert Action.DRAG_START in acts and acts[-1] == Action.DRAG_END
        assert self._click(20.3, "recording") == Action.STOP
