"""Configuração de logging sem conteúdo ditado."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_logging(log_dir: str | Path) -> logging.Logger:
    """
    Configura logger 'hear_ai' com RotatingFileHandler.
    Arquivo: hear-ai.log (1 MB, 3 backups), UTF-8.
    Chamar múltiplas vezes não duplica handlers.
    Se chamado com log_dir diferente, troca o handler para o novo diretório.

    REGRA: O log NUNCA recebe o texto ditado.
    Use describe_text() para registrar apenas metadados.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("hear_ai")
    logger.setLevel(logging.DEBUG)

    # Se já existe handler, verifica se é para o mesmo diretório
    # Se for diferente, remove o antigo e cria novo
    if logger.handlers:
        existing_handler = logger.handlers[0]
        if isinstance(existing_handler, logging.handlers.RotatingFileHandler):
            existing_path = Path(existing_handler.baseFilename)
            if existing_path == log_dir / "hear-ai.log":
                return logger
            # Diretório diferente: remove handler antigo
            logger.removeHandler(existing_handler)
            existing_handler.close()

    handler = logging.handlers.RotatingFileHandler(
        log_dir / "hear-ai.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    return logger


def describe_text(text: str) -> str:
    """
    Descreve o texto sem revelar seu conteúdo.
    Ex.: "len=42 words=8"
    """
    words = len(text.split())
    return f"len={len(text)} words={words}"