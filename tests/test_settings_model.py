"""Testes para settings_model.py."""

from __future__ import annotations

import pytest

from hear_ai.config import Config
from hear_ai.settings_model import SettingsForm


class TestSettingsForm:
    """Testes do modelo do formulário de configurações."""

    def make_form(self, **overrides) -> SettingsForm:
        """Cria um formulário com valores padrão e sobrescritas opcionais."""
        defaults = {
            "hotkey": "ctrl+alt+h",
            "mode": "hold",
            "language": "pt",
            "input_device": None,
            "model": "small",
            "device": "cpu",
            "history_enabled": True,
            "history_retention_days": 30,
            "history_paused": False,
            "bubble_hide_when_idle": False,
            "bubble_idle_opacity": 0.85,
            "start_with_windows": False,
        }
        defaults.update(overrides)
        return SettingsForm(**defaults)

    def test_from_config_cria_form_correto(self):
        """from_config cria form com valores do Config."""
        cfg = Config(hotkey="ctrl+alt+space", mode="toggle", language="en", model="base")
        form = SettingsForm.from_config(cfg)

        assert form.hotkey == "ctrl+alt+space"
        assert form.mode == "toggle"
        assert form.language == "en"
        assert form.model == "base"
        assert form.device == "cpu"
        assert form.history_enabled is True
        assert form.history_retention_days == 30
        assert form.input_device is None
        assert form.start_with_windows is False

    # --- validações de hotkey ---
    def test_valida_hotkey_valido(self):
        """Hotkey válido passa na validação."""
        form = self.make_form(hotkey="ctrl+alt+h")
        errors = form.validate()
        assert "hotkey" not in errors

    def test_valida_hotkey_invalido_vazio(self):
        """Hotkey vazio falha."""
        form = self.make_form(hotkey="")
        errors = form.validate()
        assert "hotkey" in errors

    def test_valida_hotkey_invalido_so_shift(self):
        """Hotkey só com Shift falha (precisa Ctrl/Alt/Win)."""
        form = self.make_form(hotkey="shift+h")
        errors = form.validate()
        assert "hotkey" in errors
        assert "pelo menos" in errors["hotkey"]

    def test_valida_hotkey_invalido_reservado(self):
        """Hotkey reservado (ex.: win+l) falha."""
        form = self.make_form(hotkey="win+l")
        errors = form.validate()
        assert "hotkey" in errors
        assert "reservado" in errors["hotkey"]

    def test_valida_hotkey_invalido_altgr_abnt2(self):
        """Ctrl+Alt+q (AltGr no ABNT2) falha."""
        form = self.make_form(hotkey="ctrl+alt+q")
        errors = form.validate()
        assert "hotkey" in errors
        assert "AltGr" in errors["hotkey"]

    # --- validações de mode ---
    def test_valida_mode_valido_hold(self):
        """Mode 'hold' é válido."""
        form = self.make_form(mode="hold")
        errors = form.validate()
        assert "mode" not in errors

    def test_valida_mode_valido_toggle(self):
        """Mode 'toggle' é válido."""
        form = self.make_form(mode="toggle")
        errors = form.validate()
        assert "mode" not in errors

    def test_valida_mode_invalido(self):
        """Mode inválido falha."""
        form = self.make_form(mode="invalid")
        errors = form.validate()
        assert "mode" in errors

    # --- validações de language ---
    def test_valida_language_valido_pt(self):
        """Language 'pt' é válido."""
        form = self.make_form(language="pt")
        errors = form.validate()
        assert "language" not in errors

    def test_valida_language_valido_en(self):
        """Language 'en' é válido."""
        form = self.make_form(language="en")
        errors = form.validate()
        assert "language" not in errors

    def test_valida_language_invalido(self):
        """Language inválido falha."""
        form = self.make_form(language="fr")
        errors = form.validate()
        assert "language" in errors

    # --- validações de device ---
    def test_valida_device_valido_cpu(self):
        """Device 'cpu' é válido."""
        form = self.make_form(device="cpu")
        errors = form.validate()
        assert "device" not in errors

    def test_valida_device_valido_cuda(self):
        """Device 'cuda' é válido."""
        form = self.make_form(device="cuda")
        errors = form.validate()
        assert "device" not in errors

    def test_valida_device_valido_auto(self):
        """Device 'auto' é válido."""
        form = self.make_form(device="auto")
        errors = form.validate()
        assert "device" not in errors

    def test_valida_device_invalido(self):
        """Device inválido falha."""
        form = self.make_form(device="gpu")
        errors = form.validate()
        assert "device" in errors

    # --- validações de model ---
    def test_valida_model_valido(self):
        """Model válido passa."""
        form = self.make_form(model="small")
        errors = form.validate()
        assert "model" not in errors

    def test_valida_model_vazio_falha(self):
        """Model vazio falha."""
        form = self.make_form(model="")
        errors = form.validate()
        assert "model" in errors

    def test_valida_model_nao_string_falha(self):
        """Model não-string falha."""
        form = self.make_form(model=123)  # type: ignore
        errors = form.validate()
        assert "model" in errors

    # --- validações de history_retention_days ---
    def test_valida_history_retention_dias_valido(self):
        """Dias de retenção válido passa."""
        form = self.make_form(history_retention_days=30)
        errors = form.validate()
        assert "history_retention_days" not in errors

    def test_valida_history_retention_muito_baixo(self):
        """Menos que 1 dia falha."""
        form = self.make_form(history_retention_days=0)
        errors = form.validate()
        assert "history_retention_days" in errors

    def test_valida_history_retention_muito_alto(self):
        """Mais que 3650 dias falha."""
        form = self.make_form(history_retention_days=4000)
        errors = form.validate()
        assert "history_retention_days" in errors

    def test_valida_history_retention_nao_inteiro(self):
        """Valor não-inteiro falha."""
        form = self.make_form(history_retention_days=30.5)  # type: ignore
        errors = form.validate()
        assert "history_retention_days" in errors

    def test_valida_history_retention_bool_falha(self):
        """Boolean falha (bool é subclasse de int)."""
        form = self.make_form(history_retention_days=True)  # type: ignore
        errors = form.validate()
        assert "history_retention_days" in errors

    # --- validações de bubble_idle_opacity ---
    def test_valida_bubble_opacity_valido(self):
        """Opacidade válida passa."""
        form = self.make_form(bubble_idle_opacity=0.5)
        errors = form.validate()
        assert "bubble_idle_opacity" not in errors

    def test_valida_bubble_opacity_muito_baixa(self):
        """Menos que 0.3 falha."""
        form = self.make_form(bubble_idle_opacity=0.2)
        errors = form.validate()
        assert "bubble_idle_opacity" in errors

    def test_valida_bubble_opacity_muito_alta(self):
        """Mais que 1.0 falha."""
        form = self.make_form(bubble_idle_opacity=1.1)
        errors = form.validate()
        assert "bubble_idle_opacity" in errors

    def test_valida_bubble_opacity_nao_numero(self):
        """Não-número falha."""
        form = self.make_form(bubble_idle_opacity="alto")  # type: ignore
        errors = form.validate()
        assert "bubble_idle_opacity" in errors

    # --- validações booleanas ---
    def test_valida_bool_bubble_hide(self):
        """bubble_hide_when_idle deve ser bool."""
        form = self.make_form(bubble_hide_when_idle="sim")  # type: ignore
        errors = form.validate()
        assert "bubble_hide_when_idle" in errors

    def test_valida_bool_history_enabled(self):
        """history_enabled deve ser bool."""
        form = self.make_form(history_enabled="sim")  # type: ignore
        errors = form.validate()
        assert "history_enabled" in errors

    def test_valida_bool_history_paused(self):
        """history_paused deve ser bool."""
        form = self.make_form(history_paused="sim")  # type: ignore
        errors = form.validate()
        assert "history_paused" in errors

    def test_valida_bool_start_with_windows(self):
        """start_with_windows deve ser bool."""
        form = self.make_form(start_with_windows="sim")  # type: ignore
        errors = form.validate()
        assert "start_with_windows" in errors

    # --- apply_to ---
    def test_apply_to_muda_campos_e_marca_precisa_reiniciar(self):
        """apply_to atualiza Config, retorna campos mudados e precisa_reiniciar."""
        cfg = Config()
        form = self.make_form(
            model="base",
            device="cuda",
            hotkey="ctrl+alt+space",
            history_enabled=False,
        )

        new_cfg, changed, needs_restart = form.apply_to(cfg)

        assert new_cfg is cfg
        assert "model" in changed
        assert "device" in changed
        assert "hotkey" in changed
        assert "history_enabled" in changed

        # model e device exigem reinício do modelo
        assert "model" in needs_restart
        assert "device" in needs_restart
        # hotkey e mode aplicam imediatamente
        assert "hotkey" not in needs_restart

    def test_apply_to_sem_mudancas_retorna_vazio(self):
        """Se nada mudou, retorna conjuntos vazios."""
        cfg = Config()
        form = SettingsForm.from_config(cfg)
        new_cfg, changed, needs_restart = form.apply_to(cfg)

        assert changed == set()
        assert needs_restart == set()

    def test_apply_to_campos_nao_no_config_nao_quebram(self):
        """Campos que não estão no Config (input_device, start_with_windows) não quebram."""
        cfg = Config()
        form = self.make_form(input_device="Microfone USB", start_with_windows=True)
        new_cfg, changed, needs_restart = form.apply_to(cfg)

        # Não deve quebrar, mas esses campos não estão no Config
        assert "input_device" not in changed
        assert "start_with_windows" not in changed

    def test_apply_to_campos_dinamicos_no_config(self):
        """Se Config tem atributos dinâmicos (bubble_hide_when_idle), eles são atualizados."""
        cfg = Config()
        cfg.bubble_hide_when_idle = False
        cfg.bubble_idle_opacity = 0.5

        form = self.make_form(
            bubble_hide_when_idle=True,
            bubble_idle_opacity=0.7,
        )
        new_cfg, changed, needs_restart = form.apply_to(cfg)

        assert "bubble_hide_when_idle" in changed
        assert "bubble_idle_opacity" in changed
        assert new_cfg.bubble_hide_when_idle is True
        assert new_cfg.bubble_idle_opacity == 0.7


class TestSettingsFormFromConfig:
    """Testes específicos de from_config."""

    def test_from_config_copia_todos_campos(self):
        """from_config copia todos os campos relevantes do Config."""
        cfg = Config(
            hotkey="ctrl+alt+space",
            mode="toggle",
            language="en",
            model="base",
            device="cuda",
            history_enabled=False,
            history_retention_days=60,
            history_paused=True,
        )
        cfg.bubble_hide_when_idle = True
        cfg.bubble_idle_opacity = 0.6

        form = SettingsForm.from_config(cfg)

        assert form.hotkey == "ctrl+alt+space"
        assert form.mode == "toggle"
        assert form.language == "en"
        assert form.model == "base"
        assert form.device == "cuda"
        assert form.history_enabled is False
        assert form.history_retention_days == 60
        assert form.history_paused is True
        assert form.bubble_hide_when_idle is True
        assert form.bubble_idle_opacity == 0.6
        assert form.input_device is None
        assert form.start_with_windows is False


class TestSettingsFormValidateAll:
    """Testes que combinam múltiplas validações."""

    def test_form_completamente_valido(self):
        """Form com todos os campos válidos passa."""
        form = SettingsForm(
            hotkey="ctrl+alt+h",
            mode="hold",
            language="pt",
            input_device=None,
            model="small",
            device="cpu",
            history_enabled=True,
            history_retention_days=30,
            history_paused=False,
            bubble_hide_when_idle=False,
            bubble_idle_opacity=0.85,
            start_with_windows=False,
        )
        errors = form.validate()
        assert errors == {}

    def test_form_com_multiplos_erros(self):
        """Form com vários erros reporta todos."""
        form = SettingsForm(
            hotkey="",           # inválido
            mode="invalid",      # inválido
            language="fr",       # inválido
            device="gpu",        # inválido
            model="",            # inválido
            history_retention_days=0,  # inválido
            bubble_idle_opacity=1.5,   # inválido
            history_enabled="sim",     # inválido (bool)
            history_paused="nao",      # inválido (bool)
            bubble_hide_when_idle="sim",  # inválido (bool)
            start_with_windows="sim",   # inválido (bool)
            input_device=None,
        )
        errors = form.validate()

        assert len(errors) == 11
        assert "hotkey" in errors
        assert "mode" in errors
        assert "language" in errors
        assert "device" in errors
        assert "model" in errors
        assert "history_retention_days" in errors
        assert "bubble_idle_opacity" in errors
        assert "history_enabled" in errors
        assert "history_paused" in errors
        assert "bubble_hide_when_idle" in errors
        assert "start_with_windows" in errors


if __name__ == "__main__":
    pytest.main([__file__, "-v"])