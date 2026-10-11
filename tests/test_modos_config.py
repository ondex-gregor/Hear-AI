"""Testes para configuração e validação dos modos (T47b P1)."""

import json
import tempfile
from pathlib import Path

import pytest

from hear_ai.config import Config, load_config, save_config
from hear_ai.history import Entry, HistoryStore
from hear_ai.settings_model import SettingsForm


class TestConfigDefaults:
    """Testa padrões do Config para os novos campos."""

    def test_operation_mode_default(self):
        cfg = Config()
        assert cfg.operation_mode == "fiel"

    def test_generator_provider_default(self):
        cfg = Config()
        assert cfg.generator_provider == "ollama"

    def test_ollama_url_default(self):
        cfg = Config()
        assert cfg.ollama_url == "http://127.0.0.1:11434"

    def test_ollama_model_default(self):
        cfg = Config()
        assert cfg.ollama_model == ""

    def test_cloud_base_url_default(self):
        cfg = Config()
        assert cfg.cloud_base_url == ""

    def test_cloud_model_default(self):
        cfg = Config()
        assert cfg.cloud_model == ""

    def test_cloud_key_env_default(self):
        cfg = Config()
        assert cfg.cloud_key_env == ""

    def test_cloud_consent_default(self):
        cfg = Config()
        assert cfg.cloud_consent is False

    def test_generator_app_context_default(self):
        cfg = Config()
        assert cfg.generator_app_context is False

    def test_generator_permanent_prefs_default(self):
        cfg = Config()
        assert cfg.generator_permanent_prefs is False

    def test_mode_hotkey_default(self):
        cfg = Config()
        assert cfg.mode_hotkey == "ctrl+alt+m"

    def test_generator_timeout_s_default(self):
        cfg = Config()
        assert cfg.generator_timeout_s == 60


class TestConfigLoadOldConfig:
    """Testa carregamento de config antigo (sem os novos campos)."""

    def test_load_old_config_gets_defaults(self, tmp_path: Path):
        old_config = {
            "hotkey": "ctrl+alt+h",
            "mode": "hold",
            "language": "pt",
            "model": "small",
            "device": "cpu",
            "compute_type": "int8",
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(old_config), encoding="utf-8")

        cfg = load_config(config_path)

        assert cfg.operation_mode == "fiel"
        assert cfg.generator_provider == "ollama"
        assert cfg.ollama_url == "http://127.0.0.1:11434"
        assert cfg.ollama_model == ""
        assert cfg.cloud_base_url == ""
        assert cfg.cloud_model == ""
        assert cfg.cloud_key_env == ""
        assert cfg.cloud_consent is False
        assert cfg.generator_app_context is False
        assert cfg.generator_permanent_prefs is False
        assert cfg.mode_hotkey == "ctrl+alt+m"
        assert cfg.generator_timeout_s == 60


class TestConfigInvalidValues:
    """Testa que valores inválidos voltam aos padrões."""

    def test_invalid_operation_mode_reverts(self, tmp_path: Path):
        config_data = {"operation_mode": "invalido"}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

        cfg = load_config(config_path)
        assert cfg.operation_mode == "fiel"

    def test_invalid_generator_provider_reverts(self, tmp_path: Path):
        config_data = {"generator_provider": "invalido"}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

        cfg = load_config(config_path)
        assert cfg.generator_provider == "ollama"

    def test_invalid_ollama_url_reverts(self, tmp_path: Path):
        config_data = {"ollama_url": "http://example.com:11434"}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

        cfg = load_config(config_path)
        assert cfg.ollama_url == "http://127.0.0.1:11434"

    def test_invalid_cloud_base_url_reverts(self, tmp_path: Path):
        config_data = {"cloud_base_url": "http://example.com"}  # não https
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

        cfg = load_config(config_path)
        assert cfg.cloud_base_url == ""

    def test_invalid_cloud_key_env_reverts(self, tmp_path: Path):
        config_data = {"cloud_key_env": "INVALID-KEY"}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

        cfg = load_config(config_path)
        assert cfg.cloud_key_env == ""

    def test_invalid_generator_timeout_reverts(self, tmp_path: Path):
        config_data = {"generator_timeout_s": 999}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

        cfg = load_config(config_path)
        assert cfg.generator_timeout_s == 60

    def test_invalid_mode_hotkey_reverts(self, tmp_path: Path):
        config_data = {"mode_hotkey": "invalid"}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

        cfg = load_config(config_path)
        assert cfg.mode_hotkey == "ctrl+alt+m"


class TestSettingsModelValidation:
    """Testa validações do SettingsForm."""

    def test_valid_operation_mode(self):
        form = SettingsForm.from_config(Config())
        form.operation_mode = "prompt"
        errors = form.validate()
        assert "operation_mode" not in errors

    def test_invalid_operation_mode(self):
        form = SettingsForm.from_config(Config())
        form.operation_mode = "invalido"
        errors = form.validate()
        assert "operation_mode" in errors

    def test_valid_generator_provider(self):
        form = SettingsForm.from_config(Config())
        form.generator_provider = "openai_compat"
        errors = form.validate()
        assert "generator_provider" not in errors

    def test_invalid_generator_provider(self):
        form = SettingsForm.from_config(Config())
        form.generator_provider = "invalido"
        errors = form.validate()
        assert "generator_provider" in errors

    def test_valid_ollama_url_loopback(self):
        form = SettingsForm.from_config(Config())
        form.ollama_url = "http://127.0.0.1:11434"
        errors = form.validate()
        assert "ollama_url" not in errors

        form.ollama_url = "http://localhost:11434"
        errors = form.validate()
        assert "ollama_url" not in errors

        form.ollama_url = "http://[::1]:11434"
        errors = form.validate()
        assert "ollama_url" not in errors

    def test_invalid_ollama_url_not_loopback(self):
        form = SettingsForm.from_config(Config())
        form.ollama_url = "http://example.com:11434"
        errors = form.validate()
        assert "ollama_url" in errors

    def test_valid_cloud_base_url_https(self):
        form = SettingsForm.from_config(Config())
        form.cloud_base_url = "https://api.example.com"
        errors = form.validate()
        assert "cloud_base_url" not in errors

    def test_valid_cloud_base_url_loopback_no_https(self):
        form = SettingsForm.from_config(Config())
        form.cloud_base_url = "http://127.0.0.1:8080"
        errors = form.validate()
        assert "cloud_base_url" not in errors

        form.cloud_base_url = "http://[::1]:8080"
        errors = form.validate()
        assert "cloud_base_url" not in errors

    def test_invalid_cloud_base_url_no_https(self):
        form = SettingsForm.from_config(Config())
        form.cloud_base_url = "http://example.com"
        errors = form.validate()
        assert "cloud_base_url" in errors

    def test_empty_cloud_base_url_allowed(self):
        form = SettingsForm.from_config(Config())
        form.cloud_base_url = ""
        errors = form.validate()
        assert "cloud_base_url" not in errors

    def test_valid_cloud_key_env(self):
        form = SettingsForm.from_config(Config())
        form.cloud_key_env = "OPENAI_API_KEY"
        errors = form.validate()
        assert "cloud_key_env" not in errors

        form.cloud_key_env = "MY_KEY_123"
        errors = form.validate()
        assert "cloud_key_env" not in errors

    def test_invalid_cloud_key_env(self):
        form = SettingsForm.from_config(Config())
        form.cloud_key_env = "INVALID-KEY"
        errors = form.validate()
        assert "cloud_key_env" in errors

        form.cloud_key_env = "key with spaces"
        errors = form.validate()
        assert "cloud_key_env" in errors

    def test_empty_cloud_key_env_allowed(self):
        form = SettingsForm.from_config(Config())
        form.cloud_key_env = ""
        errors = form.validate()
        assert "cloud_key_env" not in errors

    def test_generator_timeout_s_valid(self):
        form = SettingsForm.from_config(Config())
        form.generator_timeout_s = 5
        errors = form.validate()
        assert "generator_timeout_s" not in errors

        form.generator_timeout_s = 300
        errors = form.validate()
        assert "generator_timeout_s" not in errors

        form.generator_timeout_s = 60
        errors = form.validate()
        assert "generator_timeout_s" not in errors

    def test_generator_timeout_s_invalid(self):
        form = SettingsForm.from_config(Config())
        form.generator_timeout_s = 4
        errors = form.validate()
        assert "generator_timeout_s" in errors

        form.generator_timeout_s = 301
        errors = form.validate()
        assert "generator_timeout_s" in errors

        form.generator_timeout_s = "60"
        errors = form.validate()
        assert "generator_timeout_s" in errors

    def test_mode_hotkey_validation(self):
        form = SettingsForm.from_config(Config())
        form.mode_hotkey = "ctrl+alt+m"
        errors = form.validate()
        assert "mode_hotkey" not in errors

        form.mode_hotkey = "invalid"
        errors = form.validate()
        assert "mode_hotkey" in errors

    def test_bool_fields_validation(self):
        form = SettingsForm.from_config(Config())
        form.cloud_consent = "true"
        errors = form.validate()
        assert "cloud_consent" in errors

        form.cloud_consent = True
        form.generator_app_context = "false"
        errors = form.validate()
        assert "generator_app_context" in errors

        form.generator_app_context = True
        form.generator_permanent_prefs = 1
        errors = form.validate()
        assert "generator_permanent_prefs" in errors


class TestSettingsModelApplyTo:
    """Testa aplicação das alterações ao Config."""

    def test_apply_new_fields_to_config(self):
        cfg = Config()
        form = SettingsForm.from_config(cfg)
        form.operation_mode = "prompt"
        form.generator_provider = "openai_compat"
        form.ollama_url = "http://127.0.0.1:11434"
        form.ollama_model = "qwen2.5:7b"
        form.cloud_base_url = "https://api.example.com"
        form.cloud_model = "gpt-4o-mini"
        form.cloud_key_env = "MY_API_KEY"
        form.cloud_consent = True
        form.generator_app_context = True
        form.generator_permanent_prefs = True
        form.mode_hotkey = "ctrl+alt+shift+m"
        form.generator_timeout_s = 45

        new_cfg, changed, needs_restart = form.apply_to(cfg)

        assert new_cfg.operation_mode == "prompt"
        assert new_cfg.generator_provider == "openai_compat"
        assert new_cfg.ollama_model == "qwen2.5:7b"
        assert new_cfg.cloud_base_url == "https://api.example.com"
        assert new_cfg.cloud_model == "gpt-4o-mini"
        assert new_cfg.cloud_key_env == "MY_API_KEY"
        assert new_cfg.cloud_consent is True
        assert new_cfg.generator_app_context is True
        assert new_cfg.generator_permanent_prefs is True
        assert new_cfg.mode_hotkey == "ctrl+alt+shift+m"
        assert new_cfg.generator_timeout_s == 45

        assert "operation_mode" in changed
        assert "generator_provider" in changed
        assert "ollama_model" in changed
        assert "cloud_base_url" in changed
        assert "cloud_model" in changed
        assert "cloud_key_env" in changed
        assert "cloud_consent" in changed
        assert "generator_app_context" in changed
        assert "generator_permanent_prefs" in changed
        assert "mode_hotkey" in changed
        assert "generator_timeout_s" in changed


class TestHistoryEntryNewFields:
    """Testa leitura/omissão dos novos campos no histórico."""

    def test_entry_with_new_fields(self):
        entry = Entry(
            id="test-1",
            ts=None,
            text="teste",
            duration_s=1.0,
            language="pt",
            status="colado",
            day="2026-10-10",
            raw="teste bruto",
            changes=(("teste bruto", "teste"),),
            generated="prompt gerado",
            revisar=True,
        )
        assert entry.generated == "prompt gerado"
        assert entry.revisar is True

    def test_entry_without_new_fields_defaults(self):
        entry = Entry(
            id="test-1",
            ts=None,
            text="teste",
            duration_s=1.0,
            language="pt",
            status="colado",
            day="2026-10-10",
        )
        assert entry.generated is None
        assert entry.revisar is False

    def test_history_store_reads_new_fields(self, tmp_path: Path):
        store = HistoryStore(tmp_path)
        rid = store.save("texto colado", {
            "raw": "texto bruto",
            "changes": [("bruto", "colado")],
            "generated": "prompt gerado",
            "revisar": True,
            "status": "colado",
            "duration_s": 1.0,
            "language": "pt",
        })
        assert rid is not None

        entries = store.entries()
        assert len(entries) == 1
        e = entries[0]
        assert e.generated == "prompt gerado"
        assert e.revisar is True

    def test_history_store_omits_new_fields_when_absent(self, tmp_path: Path):
        store = HistoryStore(tmp_path)
        rid = store.save("texto colado", {
            "status": "colado",
            "duration_s": 1.0,
            "language": "pt",
        })
        assert rid is not None

        entries = store.entries()
        assert len(entries) == 1
        e = entries[0]
        assert e.generated is None
        assert e.revisar is False

    def test_history_store_reads_legacy_entry_without_new_fields(self, tmp_path: Path):
        # Escreve uma linha JSONL antiga manualmente (sem generated/revisar)
        store = HistoryStore(tmp_path)
        day_path = store._path_for("2026-10-10")
        day_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_line = json.dumps({
            "id": "legacy-1",
            "ts": "2026-10-10T10:00:00",
            "text": "texto antigo",
            "status": "colado",
            "duration_s": 2.0,
            "language": "pt",
            "raw": "texto bruto",
            "changes": [("bruto", "antigo")],
        }, ensure_ascii=False)
        day_path.write_text(legacy_line + "\n", encoding="utf-8")

        entries = store.entries()
        assert len(entries) == 1
        e = entries[0]
        assert e.id == "legacy-1"
        assert e.text == "texto antigo"
        assert e.generated is None
        assert e.revisar is False


class TestConfigSaveLoadRoundtrip:
    """Testa roundtrip de save/load com novos campos."""

    def test_save_load_preserves_new_fields(self, tmp_path: Path):
        cfg = Config(
            operation_mode="prompt",
            generator_provider="openai_compat",
            ollama_url="http://127.0.0.1:11434",
            ollama_model="qwen2.5:7b",
            cloud_base_url="https://api.example.com",
            cloud_model="gpt-4o-mini",
            cloud_key_env="MY_API_KEY",
            cloud_consent=True,
            generator_app_context=True,
            generator_permanent_prefs=True,
            mode_hotkey="ctrl+alt+m",
            generator_timeout_s=45,
        )
        config_path = tmp_path / "config.json"
        save_config(cfg, config_path)

        loaded = load_config(config_path)
        assert loaded.operation_mode == "prompt"
        assert loaded.generator_provider == "openai_compat"
        assert loaded.ollama_url == "http://127.0.0.1:11434"
        assert loaded.ollama_model == "qwen2.5:7b"
        assert loaded.cloud_base_url == "https://api.example.com"
        assert loaded.cloud_model == "gpt-4o-mini"
        assert loaded.cloud_key_env == "MY_API_KEY"
        assert loaded.cloud_consent is True
        assert loaded.generator_app_context is True
        assert loaded.generator_permanent_prefs is True
        assert loaded.mode_hotkey == "ctrl+alt+m"
        assert loaded.generator_timeout_s == 45


if __name__ == "__main__":
    pytest.main([__file__, "-v"])