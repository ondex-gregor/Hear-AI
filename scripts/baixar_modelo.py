"""Baixa o modelo faster-whisper para models/. ÚNICA etapa do projeto que usa rede.

Uso: python scripts/baixar_modelo.py [nome]   (padrão: small, ~480 MB)
"""

import sys
from pathlib import Path

from huggingface_hub import snapshot_download

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    nome = argv[1] if len(argv) > 1 else "small"
    destino = ROOT / "models" / f"faster-whisper-{nome}"
    destino.mkdir(parents=True, exist_ok=True)
    print(f"Baixando Systran/faster-whisper-{nome} -> {destino}")
    snapshot_download(f"Systran/faster-whisper-{nome}", local_dir=str(destino))
    print("Pronto.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
