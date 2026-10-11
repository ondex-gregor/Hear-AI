import pytest

from conftest import word_match_ratio


@pytest.mark.slow
def test_ping_fixture_ate_colagem(transcriber_small, fixture_pt, tmp_path):
    audio, esperado = fixture_pt
    saida = tmp_path / "saida.txt"

    def colar_falso(texto: str) -> None:
        saida.write_text(texto, encoding="utf-8")

    colar_falso(transcriber_small.transcribe(audio))

    colado = saida.read_text(encoding="utf-8")
    assert colado
    assert word_match_ratio(esperado, colado) >= 0.7, colado
