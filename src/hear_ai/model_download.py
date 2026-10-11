"""Baixar um modelo de voz (D1, T32/T46). É a ÚNICA ação do app que usa a internet, e só com clique do usuário
(botão "Baixar agora" + confirmação). Nada aqui roda sozinho: ninguém chama `ModelDownloader.start()` sem uma ação
explícita.

O download roda num PROCESSO FILHO (`huggingface_hub.snapshot_download` num `python -c`):
- dá para CANCELAR de verdade (encerramos o processo que nós mesmos abrimos);
- a interface não trava;
- o progresso vem do tamanho da pasta de destino (o .incomplete cresce enquanto baixa).
Erros viram frases simples ("sem internet", "sem espaço"), e "tentar de novo" continua de onde parou
(o huggingface_hub retoma arquivos incompletos).
Testes: `popen` é injetável — nunca há rede nos testes.

T46: o `stderr` do filho é drenado numa thread (o huggingface_hub escreve barras de progresso nele; sem drenar,
o buffer do pipe enche em poucos minutos e o download TRAVA) e as barras são desligadas; antes de começar confere
se há espaço em disco.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("hear_ai.download")

REPO = "Systran/faster-whisper-{name}"
# tamanhos aproximados (bytes) para a barra de progresso
EXPECTED_BYTES = {"tiny": 77_000_000, "base": 145_000_000, "small": 486_000_000,
                  "medium": 1_530_000_000, "large-v3": 3_090_000_000}
SIZE_LABEL = {"tiny": "~75 MB", "base": "~145 MB", "small": "~480 MB", "medium": "~1,5 GB", "large-v3": "~3 GB"}
DISK_MARGIN = 1.10  # exige 10% a mais que o tamanho do modelo livres (arquivos temporários, tokenizer...)
STDERR_KEEP = 8192  # só o final do stderr importa para a frase de erro


def size_label(name: str) -> str:
    return SIZE_LABEL.get(name, "")

_CHILD = (
    "import os\n"
    "from huggingface_hub import snapshot_download\n"
    "snapshot_download(os.environ['HEARAI_REPO'], local_dir=os.environ['HEARAI_DIR'])\n"
)


def model_dir(models_dir, name: str = "small") -> Path:
    return Path(models_dir) / f"faster-whisper-{name}"


def model_ready(models_dir, name: str = "small") -> bool:
    return (model_dir(models_dir, name) / "model.bin").exists()


def resolve_startup_model(models_dir, wanted: str, fallback: str = "small") -> tuple[str, bool]:
    """Qual modelo carregar ao iniciar: `(nome, caiu_para_o_fallback)`.

    O modelo do config sem arquivo em disco (ex.: `medium` apagado) NÃO derruba o app: cai para `fallback`
    (`small`) se ele existir. Só devolve a decisão; quem chama avisa o usuário e NUNCA reescreve o config.json.
    Se nem o fallback existe, devolve `wanted` e o erro de sempre ("modelo não encontrado") segue seu caminho."""
    if model_ready(models_dir, wanted):
        return wanted, False
    if wanted != fallback and model_ready(models_dir, fallback):
        return fallback, True
    return wanted, False


def _free_label(name: str) -> str:
    return size_label(name).lstrip("~") or "500 MB"


def folder_bytes(path: Path) -> int:
    total = 0
    try:
        for p in Path(path).rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def friendly_error(stderr: str, returncode, name: str = "small") -> str:
    low = (stderr or "").lower()
    if any(k in low for k in ("connectionerror", "connecterror", "nameresolution", "getaddrinfo", "max retries",
                              "connecttimeout", "timed out", "offline", "network is unreachable",
                              "temporary failure", "failed to establish")):
        return "Sem conexão com a internet. Confira a rede e clique em “Tentar de novo”."
    if any(k in low for k in ("no space left", "errno 28", "disk full", "espaço insuficiente")):
        return f"Sem espaço em disco: o modelo precisa de cerca de {_free_label(name)} livres."
    if any(k in low for k in ("permission", "access is denied", "acesso negado", "errno 13")):
        return "Sem permissão para gravar na pasta de modelos."
    return f"Não foi possível baixar o modelo (código {returncode}). Clique em “Tentar de novo”."


class ModelDownloader:
    """Baixa `Systran/faster-whisper-<name>` para `models_dir`. Callbacks vêm da thread de download:
    `on_progress(fração 0..1, bytes)`, `on_done()`, `on_error(mensagem)`, `on_cancelled()`."""

    def __init__(self, models_dir, name: str = "small", *, popen=None, poll_s: float = 0.3,
                 on_progress=None, on_done=None, on_error=None, on_cancelled=None, disk_free=None):
        """`disk_free(pasta) -> bytes livres` é injetável (testes); o padrão é `shutil.disk_usage`."""
        self.models_dir = Path(models_dir)
        self._disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self.name = name
        self.target = model_dir(models_dir, name)
        self.expected = EXPECTED_BYTES.get(name, 500_000_000)
        self._popen = popen or subprocess.Popen
        self.poll_s = poll_s
        self.on_progress = on_progress
        self.on_done = on_done
        self.on_error = on_error
        self.on_cancelled = on_cancelled
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.proc = None
        self.running = False

    def _call(self, fn, *args) -> None:
        if fn is not None:
            try:
                fn(*args)
            except Exception as e:
                log.warning("callback do download falhou: %s", type(e).__name__)

    def start(self) -> bool:
        """Começa (ou recomeça, depois de erro/cancelamento). False se já está baixando."""
        if self.running:
            return False
        self._cancel.clear()
        self.running = True
        self._thread = threading.Thread(target=self._run, name="hear-ai-download", daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            self._download()
        finally:
            self.running = False

    def _has_room(self) -> bool:
        """Tem espaço para o que ainda falta baixar (retomada conta o que já está na pasta)?"""
        try:
            probe = self.models_dir
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            missing = max(0, self.expected - folder_bytes(self.target))
            return self._disk_free(probe) >= missing * DISK_MARGIN
        except Exception as e:  # noqa: BLE001 - medir o disco é só uma checagem: se falhar, deixa tentar
            log.warning("não foi possível medir o espaço livre: %s", type(e).__name__)
            return True

    @staticmethod
    def _drain_stderr(proc) -> list:
        """Lê o stderr do filho sem parar até o fim (evita o pipe encher e travar o download).
        Devolve [final do texto (bytes, últimos STDERR_KEEP), thread]; a thread só existe se há stderr."""
        keep = [b""]
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return keep

        def run() -> None:
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8", "replace")
                    keep[0] = (keep[0] + chunk)[-STDERR_KEEP:]
            except Exception:  # noqa: BLE001 - fim do pipe / processo encerrado
                return

        t = threading.Thread(target=run, name="hear-ai-download-stderr", daemon=True)
        t.start()
        keep.append(t)
        return keep

    def _download(self) -> None:
        if not self._has_room():
            log.warning("sem espaço em disco para o modelo %s", self.name)
            self._call(self.on_error, friendly_error("no space left", 1, self.name))
            return
        try:
            self.target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._call(self.on_error, friendly_error(str(e), type(e).__name__, self.name))
            return
        env = dict(os.environ, HEARAI_REPO=REPO.format(name=self.name), HEARAI_DIR=str(self.target),
                   HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_PROGRESS_BARS="1", PYTHONIOENCODING="utf-8")
        log.info("baixando o modelo %s", self.name)
        try:
            self.proc = self._popen(
                [sys.executable, "-c", _CHILD], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            self._call(self.on_error, friendly_error(str(e), type(e).__name__, self.name))
            return
        err = self._drain_stderr(self.proc)
        last = -1.0
        while self.proc.poll() is None:
            if self._cancel.is_set():
                self._stop_child()
                log.info("download cancelado")
                self._call(self.on_cancelled)
                return
            frac = min(0.99, folder_bytes(self.target) / self.expected)
            if frac - last >= 0.005:
                last = frac
                self._call(self.on_progress, frac, int(frac * self.expected))
            time.sleep(self.poll_s)
        if len(err) > 1:
            err[1].join(2)  # o filho acabou: o pipe fecha e a thread termina
        stderr = err[0].decode("utf-8", "replace")
        if self._cancel.is_set():
            self._call(self.on_cancelled)
            return
        if self.proc.returncode == 0 and model_ready(self.models_dir, self.name):
            log.info("modelo %s baixado", self.name)
            self._call(self.on_progress, 1.0, folder_bytes(self.target))
            self._call(self.on_done)
        else:
            log.warning("download falhou (código %s)", self.proc.returncode)
            self._call(self.on_error, friendly_error(stderr, self.proc.returncode, self.name))

    def _stop_child(self) -> None:
        """Encerra SÓ o processo de download que nós abrimos."""
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception as e:
            log.warning("não foi possível encerrar o download: %s", type(e).__name__)
            try:
                self.proc.kill()
            except Exception:
                pass
