"""Transcrição local com faster-whisper, 100% offline (local_files_only).

Dispositivo: "cpu" (int8), "cuda" (float16) ou "auto" (usa CUDA se houver GPU).
Se a GPU falhar ao carregar, no aquecimento ou na transcrição, cai sozinho para CPU int8
e registra o motivo no log (sem texto ditado). `device_used` mostra o dispositivo real.

T46: `reload(nome)` troca de modelo com o app rodando (ex.: small <-> medium): carrega o novo ao lado do atual e
só então troca; se o novo não carregar, o atual continua intacto e a exceção sobe para quem pediu.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

import numpy as np

from hear_ai.config import PROJECT_ROOT, resolve_path

log = logging.getLogger("hear_ai.transcriber")

# Opções de model.transcribe. condition_on_previous_text=False (D13): proteção padrão contra o
# laço de repetição do Whisper em fala longa, custo zero na medição da T11.
DEFAULT_TRANSCRIBE_OPTIONS = {"beam_size": 5, "vad_filter": True, "condition_on_previous_text": False}

# idioma automático (D3): o Whisper detecta; abaixo desta confiança, ou fora de pt/en, usa "pt"
AUTO_LANGUAGES = ("pt", "en")
AUTO_FALLBACK = "pt"
AUTO_MIN_PROBABILITY = 0.5

CUDA_COMPUTE_TYPE = "float16"
CPU_COMPUTE_TYPE = "int8"

_dll_dirs_done: set[str] = set()


class ModelNotFoundError(FileNotFoundError):
    pass


def add_nvidia_dll_dirs() -> list[str]:
    """Deixa o ctranslate2 achar cublas/cudnn instalados por pip (pacotes `nvidia-*-cu12`).

    Eles ficam em <site-packages>/nvidia/<pacote>/bin e não entram no PATH sozinhos.
    Só mexe no processo atual. Devolve as pastas adicionadas agora.
    """
    added = []
    for entry in sys.path:
        base = Path(entry) / "nvidia"
        if not base.is_dir():
            continue
        for bin_dir in sorted(base.glob("*/bin")):
            key = str(bin_dir)
            if key in _dll_dirs_done:
                continue
            _dll_dirs_done.add(key)
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(key)
            os.environ["PATH"] = key + os.pathsep + os.environ.get("PATH", "")
            added.append(key)
    return added


def cuda_device_count() -> int:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def prompt_from_dictionary(dictionary: dict | None) -> str | None:
    """Monta o `initial_prompt` com os termos do dicionário: "Ondex, Maestri, Groq, Hear AI, Claude Code."."""
    if not dictionary:
        return None
    terms = []
    for value in dictionary.values():
        value = str(value).strip()
        if value and value not in terms:
            terms.append(value)
    return (", ".join(terms) + ".") if terms else None


class Transcriber:
    def __init__(
        self,
        model_name: str = "small",
        models_dir: str | Path = "models",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "pt",
        model_factory=None,
        cuda_count=cuda_device_count,
        transcribe_options: dict | None = None,
        initial_prompt: str | None = None,
    ):
        self.model_name = model_name
        self.models_dir = resolve_path(models_dir)
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.device_used: str | None = None
        self._model = None
        self._factory = model_factory  # (path, device, compute_type) -> modelo; injetável em testes
        self._cuda_count = cuda_count
        self._lock = threading.RLock()  # um carregamento por vez (preload + 1ª ditada)
        self._infer_lock = threading.Lock()  # um bloco por vez no modelo (ditado + blocos em fundo)
        self._reload_lock = threading.Lock()  # uma troca de modelo por vez (T46)
        self.transcribe_options = {**DEFAULT_TRANSCRIBE_OPTIONS, **(transcribe_options or {})}
        # vai em CADA chamada (cada bloco da T13), já que condition_on_previous_text=False
        self.initial_prompt = initial_prompt or None
        self.last_language: str | None = None  # idioma usado na última transcrição (diagnóstico)

    @property
    def model_path(self) -> Path:
        return self.path_for(self.model_name)

    def path_for(self, name: str) -> Path:
        return self.models_dir / f"faster-whisper-{name}"

    # --- carregamento -----------------------------------------------------
    def _make_model(self, device: str, compute_type: str, path: Path | None = None):
        path = self.model_path if path is None else Path(path)
        if self._factory is not None:
            return self._factory(str(path), device, compute_type)
        if device == "cuda":
            add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel

        return WhisperModel(
            str(path),
            device=device,
            compute_type=compute_type,
            local_files_only=True,
        )

    def _warmup(self, model) -> None:
        """Roda uma transcrição curta: expõe falhas de CUDA (ex.: DLL ausente) no carregamento
        e paga o custo da primeira execução na GPU antes de o usuário falar."""
        segments, _ = model.transcribe(
            np.zeros(16000, dtype=np.float32),
            language=AUTO_FALLBACK if self.language == "auto" else self.language,
            beam_size=1,
            vad_filter=False,
        )
        for _seg in segments:
            pass

    def _wants_cuda(self) -> bool:
        if self.device == "cuda":
            return True
        return self.device == "auto" and self._cuda_count() > 0

    def _load_cpu(self) -> None:
        self._model = self._make_model("cpu", CPU_COMPUTE_TYPE if self.device != "cpu" else self.compute_type)
        self.device_used = "cpu"

    def _build(self, path: Path):
        """Carrega o modelo de `path` (GPU com fallback para CPU/int8) SEM mexer no estado do objeto.
        Devolve (modelo, dispositivo). Levanta ModelNotFoundError se faltar o model.bin e a exceção original
        se nem a CPU conseguir (arquivo corrompido, memória)."""
        name = path.name.removeprefix("faster-whisper-")
        if not (path / "model.bin").exists():
            raise ModelNotFoundError(
                f"Modelo '{name}' não encontrado em {path}. "
                f'Rode: "{sys.executable}" "{PROJECT_ROOT / "scripts" / "baixar_modelo.py"}" {name}'
            )
        if self._wants_cuda():
            try:
                model = self._make_model("cuda", CUDA_COMPUTE_TYPE, path)
                self._warmup(model)
            except Exception as e:
                log.warning("GPU indisponível (%s: %s); usando CPU", type(e).__name__, str(e)[:200])
            else:
                log.info("modelo '%s' carregado na GPU (%s)", name, CUDA_COMPUTE_TYPE)
                return model, "cuda"
        model = self._make_model("cpu", CPU_COMPUTE_TYPE if self.device != "cpu" else self.compute_type, path)
        return model, "cpu"

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            self._model, self.device_used = self._build(self.model_path)

    def reload(self, model_name: str | None = None) -> str:
        """Troca para `model_name` (padrão: recarrega o atual) com o app rodando. Devolve o dispositivo usado.

        O modelo novo é carregado AO LADO do atual (pode demorar segundos; o ditado continua usando o antigo);
        só quando está pronto a troca acontece, entre dois blocos de transcrição. Se o novo não carregar
        (arquivo ausente/corrompido, memória, CUDA), nada muda: o modelo anterior segue valendo e a exceção
        sobe para quem pediu avisar o usuário. A GPU que falhar cai para CPU/int8, como no carregamento normal."""
        name = model_name or self.model_name
        with self._reload_lock:
            model, device_used = self._build(self.path_for(name))
            with self._infer_lock:  # espera o bloco em andamento terminar (mesma ordem de _transcribe_locked)
                with self._lock:
                    old, self._model = self._model, model
                    self.model_name, self.device_used = name, device_used
            del old  # solta a memória do modelo antigo
            log.info("modelo trocado para '%s' (%s)", name, device_used)
            return device_used

    # --- transcrição ------------------------------------------------------
    def _language_for(self, audio: np.ndarray) -> str:
        """Idioma fixo, ou "auto": detecta (pt/en); confiança < 0,5 ou outro idioma -> "pt"."""
        if self.language != "auto":
            return self.language
        try:
            lang, prob, _all = self._model.detect_language(audio)
        except Exception as e:  # modelo sem detecção (testes) ou áudio curto demais
            log.debug("detecção de idioma indisponível: %s", type(e).__name__)
            return AUTO_FALLBACK
        if lang in AUTO_LANGUAGES and prob >= AUTO_MIN_PROBABILITY:
            return lang
        log.info("idioma detectado %s (%.2f): usando %s", lang, prob, AUTO_FALLBACK)
        return AUTO_FALLBACK

    def _run(self, audio: np.ndarray) -> str:
        language = self._language_for(audio)
        self.last_language = language
        options = {**self.transcribe_options, "language": language}
        if self.initial_prompt:
            options["initial_prompt"] = self.initial_prompt
        segments, _info = self._model.transcribe(audio, **options)
        return " ".join(s.text.strip() for s in segments).strip()

    def transcribe(self, audio: np.ndarray) -> str:
        """audio: float32, 16 kHz, mono. Chamadas simultâneas são feitas uma de cada vez."""
        self.load()
        audio = np.asarray(audio, dtype=np.float32)
        with self._infer_lock:
            return self._transcribe_locked(audio)

    def _transcribe_locked(self, audio: np.ndarray) -> str:
        try:
            return self._run(audio)
        except Exception as e:
            if self.device_used != "cuda":
                raise
            with self._lock:
                if self.device_used == "cuda":  # outro thread pode já ter trocado
                    log.warning("falha na GPU ao transcrever (%s: %s); passando para CPU", type(e).__name__, str(e)[:200])
                    self._load_cpu()
            return self._run(audio)
