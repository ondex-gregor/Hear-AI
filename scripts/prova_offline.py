"""E1 — prova em execução de que o Hear AI não usa a rede.

Bloqueia toda conexão de rede do processo (socket.connect / connect_ex / getaddrinfo / create_connection)
e registra qualquer tentativa. Com o bloqueio ativo, roda o caminho real do app:
config → Transcriber (modelo local) → StreamingTranscriber em blocos sobre uma fixture →
HistoryStore → Controller com colagem simulada → interface Qt (bolha + bandeja) fora da tela.
Nada do usuário é tocado: config, histórico e logs vão para uma pasta temporária.
Uso: .venv\\Scripts\\python.exe scripts\\prova_offline.py
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ATTEMPTS: list[str] = []


def _blocked(name):
    def fn(*args, **kwargs):
        ATTEMPTS.append(f"{name}{args[1:2] if len(args) > 1 else args[:1]}")
        raise OSError(f"rede bloqueada pela prova offline ({name})")
    return fn


socket.socket.connect = _blocked("socket.connect")
socket.socket.connect_ex = _blocked("socket.connect_ex")
socket.getaddrinfo = _blocked("getaddrinfo")
socket.create_connection = _blocked("create_connection")
os.environ.pop("HF_HUB_OFFLINE", None)  # sem "ajuda": se algo tentar a rede, o bloqueio pega

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hearai-offline-"))
    from hear_ai.audio import load_wav
    from hear_ai.config import Config, save_config
    from hear_ai.controller import Controller
    from hear_ai.history import make_sink
    from hear_ai.logs import setup_logging
    from hear_ai.streaming import StreamingTranscriber
    from hear_ai.transcriber import Transcriber

    cfg = Config(models_dir=str(ROOT / "models"), log_dir=str(tmp / "logs"), history_dir=str(tmp / "hist"),
                 device=os.environ.get("PROVA_DEVICE", "cpu"))
    save_config(cfg, tmp / "config.json")
    logger = setup_logging(tmp / "logs")

    t0 = time.perf_counter()
    tr = Transcriber(cfg.model, cfg.models_dir, cfg.device, cfg.compute_type, cfg.language)
    tr.load()
    print(f"modelo carregado ({tr.device_used}) em {time.perf_counter() - t0:.1f}s")

    audio = load_wav(ROOT / "tests" / "fixtures" / "nomes_pt.wav")

    class FakeRecorder:  # entrega a fixture como se fosse o microfone
        frames_recorded = len(audio)
        last_spool_path = None
        def start(self): pass
        def stop(self): return audio
        def snapshot(self, start): return audio[start:]
        def level(self): return 0.0

    class FakePaster:
        clipboard = type("C", (), {"set": lambda self, t: None, "get": lambda self: ""})()
        pasted: list[str] = []
        def paste(self, text):
            self.pasted.append(text)
            from hear_ai.paster import PasteResult
            return PasteResult(pasted=True, restored=True, reason="ok", in_clipboard=False)

    class FakeFeedback:
        def __getattr__(self, name): return lambda *a, **k: None

    rec, paster = FakeRecorder(), FakePaster()
    sink = make_sink(cfg, tmp)
    streaming = StreamingTranscriber(rec, tr, logger)
    ctrl = Controller(rec, tr, paster, FakeFeedback(), sink, logger, cfg.language, run_async=False,
                      streaming=streaming) if "streaming" in Controller.__init__.__code__.co_varnames else \
        Controller(rec, tr, paster, FakeFeedback(), sink, logger, cfg.language, run_async=False)
    ctrl.begin()
    ctrl.end()
    print(f"ditado simulado: colado={bool(paster.pasted)} ({len(paster.pasted[0]) if paster.pasted else 0} caracteres)")
    hist = list((tmp / "hist").glob("*.jsonl"))
    print(f"histórico gravado na pasta temporária: {len(hist)} arquivo(s)")

    from PySide6.QtWidgets import QApplication
    from hear_ai.ui.bubble import BubbleWidget  # noqa: F401  (importa a interface real)
    app = QApplication.instance() or QApplication([])
    from hear_ai.ui import qt_app  # noqa: F401
    app.processEvents()
    print("interface Qt importada e iniciada (offscreen)")

    log_text = "".join(p.read_text(encoding="utf-8") for p in (tmp / "logs").glob("*.log"))
    leaked = bool(paster.pasted) and any(w in log_text for w in paster.pasted[0].split() if len(w) > 5)
    print(f"texto ditado no log: {'SIM — VAZOU' if leaked else 'não'}")
    print(f"tentativas de rede: {len(ATTEMPTS)} {ATTEMPTS[:5]}")
    return 0 if not ATTEMPTS and not leaked else 1


if __name__ == "__main__":
    raise SystemExit(main())
