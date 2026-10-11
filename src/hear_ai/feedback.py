"""Sons curtos de feedback (Windows), sem bloquear."""

from __future__ import annotations

import sys
import threading

# (frequência Hz, duração ms)
TONES = {
    "start": [(880, 90)],
    "stop": [(660, 90)],
    "done": [(660, 70), (990, 90)],
    "error": [(300, 200), (200, 250)],
}


def _winsound_beep(freq: int, ms: int) -> None:
    import winsound

    winsound.Beep(freq, ms)


class Feedback:
    def __init__(self, enabled: bool = True, beep=None):
        if beep is None:
            enabled = enabled and sys.platform == "win32"
            beep = _winsound_beep
        self._beep = beep
        self.enabled = enabled
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def _play(self, name: str) -> None:
        if not self.enabled:
            return

        def run():
            try:
                for freq, ms in TONES[name]:
                    self._beep(freq, ms)
            except Exception:
                pass  # som nunca deve derrubar o app

        t = threading.Thread(target=run, daemon=True)
        t.start()
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()] + [t]

    def join(self, timeout: float = 2.0) -> None:
        """Espera sons pendentes (útil em testes)."""
        with self._lock:
            pending = list(self._threads)
        for t in pending:
            t.join(timeout)

    def start(self) -> None:
        self._play("start")

    def stop(self) -> None:
        self._play("stop")

    def done(self) -> None:
        self._play("done")

    def error(self) -> None:
        self._play("error")
