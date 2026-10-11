"""Testes para logs.py."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hear_ai.logs import describe_text, setup_logging


@pytest.fixture(autouse=True)
def _limpa_logger_hear_ai():
    """Fecha e remove handlers do logger 'hear_ai' antes e depois de cada teste."""
    logger = logging.getLogger("hear_ai")
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    yield
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)


def test_setup_logging_cria_arquivo_e_pasta(tmp_path: Path):
    logger = setup_logging(tmp_path)
    assert isinstance(logger, logging.Logger)
    assert logger.name == "hear_ai"

    log_file = tmp_path / "hear-ai.log"
    assert log_file.exists()

    logger.info("teste")
    handlers = logger.handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)


def test_setup_logging_chamada_com_outro_diretorio_troca_handler(tmp_path: Path):
    """Se chamado com log_dir diferente, troca o handler."""
    outro = tmp_path / "outro"
    logger1 = setup_logging(tmp_path)
    logger2 = setup_logging(outro)

    assert logger1 is logger2
    assert len(logger1.handlers) == 1
    handler = logger1.handlers[0]
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert Path(handler.baseFilename) == outro / "hear-ai.log"
    assert not (tmp_path / "hear-ai.log").exists() or (tmp_path / "hear-ai.log").stat().st_size == 0


def test_setup_logging_mesmo_diretorio_nao_duplica(tmp_path: Path):
    """Chamadas repetidas com mesmo diretório não duplicam handlers."""
    logger1 = setup_logging(tmp_path)
    logger2 = setup_logging(tmp_path)

    assert logger1 is logger2
    assert len(logger1.handlers) == 1


def test_describe_text_nao_revela_conteudo():
    desc = describe_text("Olá, mundo!")
    assert desc == "len=11 words=2"
    assert "Olá" not in desc
    assert "mundo" not in desc

    desc = describe_text("  múltiplas   palavras  ")
    assert desc == "len=24 words=2"

    desc = describe_text("")
    assert desc == "len=0 words=0"

    long_text = "a " * 1000
    desc = describe_text(long_text)
    assert desc == f"len={len(long_text)} words=1000"


def test_logging_nivel_debug(tmp_path: Path):
    logger = setup_logging(tmp_path)
    assert logger.level == logging.DEBUG


def test_logging_arquivo_contem_metadados_nao_texto(tmp_path: Path):
    """Log gravado contém metadados (len=...) mas NÃO o texto ditado."""
    logger = setup_logging(tmp_path)
    texto_ditado = "Minha senha é 123456"
    logger.info("ditado: %s", describe_text(texto_ditado))

    for handler in logger.handlers:
        handler.flush()

    log_file = tmp_path / "hear-ai.log"
    conteudo = log_file.read_text(encoding="utf-8")

    assert "len=20" in conteudo
    assert "words=4" in conteudo
    assert "senha" not in conteudo
    assert "123456" not in conteudo
    assert "Minha" not in conteudo


def test_logging_formato_iso(tmp_path: Path):
    logger = setup_logging(tmp_path)
    handler = logger.handlers[0]
    formatter = handler.formatter
    assert formatter is not None
    fmt = formatter._fmt
    assert "%(asctime)s" in fmt
    assert "%(levelname)s" in fmt
    assert "%(name)s" in fmt
    assert "%(message)s" in fmt