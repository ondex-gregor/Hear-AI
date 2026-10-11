import json

from hear_ai.config import DEFAULTS, Config, load_config, resolve_path, save_config


def test_defaults():
    c = Config()
    assert c.hotkey == "ctrl+alt+h"  # D17
    assert c.mode == "hold"
    assert c.language == "pt"
    assert c.model == "small"
    assert c.device == "cpu"
    assert c.compute_type == "int8"
    assert c.beep is True and c.restore_clipboard is True
    assert c.paste_delay_ms == 150
    assert (c.paste_base_ms, c.paste_chars_per_s, c.paste_max_ms) == (250, 2500, 3000)
    assert c.history_enabled is True and c.history_paused is False
    assert c.history_retention_days == 30 and c.max_record_minutes == 30
    assert c.config_version == 2
    assert c.export_dir is None
    assert DEFAULTS == c


def test_cria_arquivo_com_padroes(tmp_path):
    p = tmp_path / "config.json"
    cfg = load_config(p)
    assert p.exists()
    assert cfg == Config()
    assert json.loads(p.read_text(encoding="utf-8"))["hotkey"] == "ctrl+alt+h"


def test_roundtrip(tmp_path):
    p = tmp_path / "config.json"
    save_config(Config(mode="toggle", language="en", export_dir="C:/x"), p)
    cfg = load_config(p)
    assert (cfg.mode, cfg.language, cfg.export_dir) == ("toggle", "en", "C:/x")


def test_valores_invalidos_voltam_ao_padrao(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps({"mode": "xyz", "language": "fr", "device": 3, "beep": "sim",
                    "paste_delay_ms": -5, "model": "", "history_enabled": True}),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.mode == "hold" and cfg.language == "pt" and cfg.device == "cpu"
    assert cfg.beep is True and cfg.paste_delay_ms == 150 and cfg.model == "small"
    assert cfg.history_enabled is True  # o válido é mantido


def test_chaves_desconhecidas_ignoradas_com_aviso(tmp_path, caplog):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"inexistente": 1, "mode": "toggle"}), encoding="utf-8")
    with caplog.at_level("WARNING"):
        cfg = load_config(p)
    assert cfg.mode == "toggle"
    assert not hasattr(cfg, "inexistente")
    assert "inexistente" in caplog.text


def test_json_corrompido_usa_padroes(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{nao e json", encoding="utf-8")
    assert load_config(p) == Config()


def test_resolve_path(tmp_path):
    assert resolve_path("models", root=tmp_path) == tmp_path / "models"
    absoluto = tmp_path / "abs"
    assert resolve_path(absoluto, root=tmp_path / "outro") == absoluto


def test_bom_utf8_sig(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"mode": "toggle", "language": "en"}), encoding="utf-8-sig")
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")
    cfg = load_config(p)
    assert (cfg.mode, cfg.language) == ("toggle", "en")


def test_paste_delay_zero_e_aceito(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"paste_delay_ms": 0}), encoding="utf-8")
    assert load_config(p).paste_delay_ms == 0


def test_compute_type_validado(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"compute_type": "int4"}), encoding="utf-8")
    assert load_config(p).compute_type == "int8"
    for ok in ("int8", "int8_float16", "int8_float32", "float16", "float32"):
        p.write_text(json.dumps({"compute_type": ok}), encoding="utf-8")
        assert load_config(p).compute_type == ok


# --- T17: campos novos e migração da v1 (D16) -------------------------------------

# exatamente o que a v1 gravava (todos os padrões antigos) + escolhas do usuário
V1_ARQUIVO = {
    "hotkey": "ctrl+alt+space", "mode": "hold", "language": "pt", "model": "small",
    "device": "auto", "compute_type": "int8", "beep": True, "restore_clipboard": True,
    "paste_delay_ms": 150, "history_enabled": False, "history_dir": "historico",
    "export_dir": None, "models_dir": "models", "log_dir": "logs",
}


def test_migracao_v1_usa_padroes_novos_so_em_memoria(tmp_path):
    p = tmp_path / "config.json"
    original = json.dumps(V1_ARQUIVO, indent=2)
    p.write_text(original, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.hotkey == "ctrl+alt+h"  # padrão antigo gravado pela v1 → padrão novo
    assert cfg.history_enabled is True
    assert cfg.device == "auto"  # escolha real do usuário mantida
    assert cfg.config_version == 2
    assert p.read_text(encoding="utf-8") == original  # arquivo NÃO foi regravado


def test_migracao_v1_mantem_escolhas_diferentes_do_padrao_antigo(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({**V1_ARQUIVO, "hotkey": "ctrl+shift+f9", "paste_delay_ms": 400}), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.hotkey == "ctrl+shift+f9"
    assert cfg.paste_base_ms == 400  # o atraso escolhido na v1 vira a espera mínima
    assert cfg.history_enabled is True


def test_arquivo_v2_respeita_valores_iguais_aos_padroes_antigos(tmp_path):
    """Com config_version, o usuário escolheu de verdade: nada é migrado."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"config_version": 2, "hotkey": "ctrl+alt+space", "history_enabled": False,
                             "paste_delay_ms": 400}), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.hotkey == "ctrl+alt+space" and cfg.history_enabled is False
    assert cfg.paste_base_ms == 250


def test_salvar_depois_da_migracao_grava_versao_2(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(V1_ARQUIVO), encoding="utf-8")
    cfg = load_config(p)
    cfg.mode = "toggle"  # o usuário mudou algo pelo app
    save_config(cfg, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["config_version"] == 2 and raw["mode"] == "toggle"
    again = load_config(p)
    assert again.hotkey == "ctrl+alt+h" and again.history_enabled is True and again.device == "auto"


def test_faixas_dos_campos_novos(tmp_path):
    p = tmp_path / "config.json"
    casos = {
        "max_record_minutes": ([1, 30, 180], [0, 181, -5, 2.5, True]),
        "history_retention_days": ([1, 3650], [0, 3651]),
        "paste_chars_per_s": ([100, 2500], [0, 99]),
        "paste_max_ms": ([0, 30000], [-1, 30001]),
    }
    for campo, (bons, ruins) in casos.items():
        padrao = getattr(Config(), campo)
        for v in bons:
            p.write_text(json.dumps({"config_version": 2, campo: v}), encoding="utf-8")
            assert getattr(load_config(p), campo) == v, (campo, v)
        for v in ruins:
            p.write_text(json.dumps({"config_version": 2, campo: v}), encoding="utf-8")
            assert getattr(load_config(p), campo) == padrao, (campo, v)
    p.write_text(json.dumps({"config_version": 2, "history_paused": "sim"}), encoding="utf-8")
    assert load_config(p).history_paused is False


# --- T17: variáveis de ambiente do __main__ (fumaça sem tocar no app do usuário) ---------

def test_env_de_instancia_dialogos_e_config(tmp_path):
    from hear_ai.__main__ import (DEFAULT_INSTANCE, config_path_from_env, dialogs_enabled,
                                  instance_mutex_name)
    from hear_ai.config import PROJECT_ROOT

    assert instance_mutex_name({}) == "Local\\" + DEFAULT_INSTANCE
    assert instance_mutex_name({"HEAR_AI_INSTANCE_NAME": "hear-ai-teste"}) == r"Local\hear-ai-teste"
    assert instance_mutex_name({"HEAR_AI_INSTANCE_NAME": r"a\b c"}) == r"Local\a_b_c"
    assert dialogs_enabled({}) is True
    assert dialogs_enabled({"HEAR_AI_NO_DIALOGS": "1"}) is False
    assert config_path_from_env({}) == PROJECT_ROOT / "config.json"
    alt = tmp_path / "cfg.json"
    assert config_path_from_env({"HEAR_AI_CONFIG": str(alt)}) == alt


def test_sem_dialogos_message_box_nao_abre(monkeypatch):
    import hear_ai.__main__ as m

    monkeypatch.setenv("HEAR_AI_NO_DIALOGS", "1")
    chamou = []
    monkeypatch.setattr(m, "dialogs_enabled", lambda env=None: False)
    import ctypes

    if hasattr(ctypes, "windll"):
        monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", lambda *a: chamou.append(a))
    m._message_box("teste", error=True)
    assert chamou == []
