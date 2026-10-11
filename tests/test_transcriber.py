import time

import pytest

from conftest import word_match_ratio
from hear_ai.transcriber import ModelNotFoundError, Transcriber


def test_modelo_ausente_da_erro_claro(tmp_path):
    t = Transcriber("small", tmp_path)
    with pytest.raises(ModelNotFoundError, match="baixar_modelo.py"):
        t.load()


@pytest.mark.slow
def test_transcreve_fixture_pt(transcriber_small, fixture_pt):
    audio, esperado = fixture_pt
    t0 = time.perf_counter()
    texto = transcriber_small.transcribe(audio)
    print(f"\ntranscrição: {time.perf_counter() - t0:.2f}s (modelo já carregado)")
    assert transcriber_small.device_used == "cpu"
    assert texto == texto.strip() and texto
    assert word_match_ratio(esperado, texto) >= 0.7, texto


def test_models_dir_relativo_resolve_pela_raiz(monkeypatch, tmp_path):
    from hear_ai.config import PROJECT_ROOT

    monkeypatch.chdir(tmp_path)
    t = Transcriber("small", "models")
    assert t.models_dir == PROJECT_ROOT / "models"


def test_mensagem_de_erro_tem_caminho_absoluto_do_script(tmp_path):
    from hear_ai.config import PROJECT_ROOT

    with pytest.raises(ModelNotFoundError) as exc:
        Transcriber("small", tmp_path).load()
    assert str(PROJECT_ROOT / "scripts" / "baixar_modelo.py") in str(exc.value)


def test_carrega_somente_arquivos_locais(tmp_path, monkeypatch):
    """Sem rede: WhisperModel recebe local_files_only=True."""
    import faster_whisper

    (tmp_path / "faster-whisper-small").mkdir()
    (tmp_path / "faster-whisper-small" / "model.bin").write_bytes(b"x")
    chamadas = []

    class FalsoModelo:
        def __init__(self, *args, **kwargs):
            chamadas.append((args, kwargs))

    monkeypatch.setattr(faster_whisper, "WhisperModel", FalsoModelo)
    Transcriber("small", tmp_path).load()
    assert len(chamadas) == 1
    assert chamadas[0][1]["local_files_only"] is True
