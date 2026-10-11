import logging
import os
import threading
import time

import numpy as np
import pytest

from conftest import MODELS_DIR, word_match_ratio
from hear_ai.transcriber import Transcriber, add_nvidia_dll_dirs, cuda_device_count


class FakeSeg:
    def __init__(self, text):
        self.text = text


class FakeModel:
    """Modelo falso: pode falhar no transcribe quando device == 'cuda'."""

    def __init__(self, device, compute_type, fail_on_cuda_transcribe=False, delay=0.0):
        self.device = device
        self.compute_type = compute_type
        self.fail = fail_on_cuda_transcribe and device == "cuda"
        self.delay = delay
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return iter([FakeSeg(f" texto {self.device} ")]), None


class Fabrica:
    def __init__(self, fail_load_cuda=False, fail_transcribe_cuda=False, delay=0.0):
        self.fail_load_cuda = fail_load_cuda
        self.fail_transcribe_cuda = fail_transcribe_cuda
        self.delay = delay
        self.chamadas = []
        self.modelos = []

    def __call__(self, path, device, compute_type):
        self.chamadas.append((device, compute_type))
        if self.delay:
            time.sleep(self.delay)
        if device == "cuda" and self.fail_load_cuda:
            raise RuntimeError("CUDA driver version is insufficient")
        m = FakeModel(device, compute_type, self.fail_transcribe_cuda)
        self.modelos.append(m)
        return m


@pytest.fixture
def models_dir(tmp_path):
    d = tmp_path / "faster-whisper-small"
    d.mkdir()
    (d / "model.bin").write_bytes(b"x")
    return tmp_path


def make(models_dir, device, fabrica, cuda_count=lambda: 1, compute_type="int8"):
    return Transcriber("small", models_dir, device=device, compute_type=compute_type,
                       model_factory=fabrica, cuda_count=cuda_count)


AUDIO = np.zeros(16000, dtype=np.float32)


def test_cpu_nao_toca_na_gpu(models_dir):
    f = Fabrica()
    t = make(models_dir, "cpu", f, compute_type="int8")
    assert t.transcribe(AUDIO) == "texto cpu"
    assert f.chamadas == [("cpu", "int8")]
    assert t.device_used == "cpu"


def test_cuda_usa_float16_e_device_used_cuda(models_dir):
    f = Fabrica()
    t = make(models_dir, "cuda", f)
    t.load()
    assert f.chamadas == [("cuda", "float16")]
    assert t.device_used == "cuda"
    assert t.transcribe(AUDIO) == "texto cuda"
    # aquecimento + transcrição real no mesmo modelo
    assert len(f.modelos[0].calls) == 2


def test_auto_sem_gpu_vai_direto_para_cpu_int8(models_dir):
    f = Fabrica()
    t = make(models_dir, "auto", f, cuda_count=lambda: 0, compute_type="float16")
    t.load()
    assert f.chamadas == [("cpu", "int8")]
    assert t.device_used == "cpu"


def test_auto_com_gpu_usa_cuda(models_dir):
    f = Fabrica()
    t = make(models_dir, "auto", f, cuda_count=lambda: 1)
    t.load()
    assert t.device_used == "cuda" and f.chamadas == [("cuda", "float16")]


def test_falha_ao_carregar_cuda_cai_para_cpu_e_registra(models_dir, caplog):
    f = Fabrica(fail_load_cuda=True)
    t = make(models_dir, "cuda", f)
    with caplog.at_level(logging.WARNING, logger="hear_ai.transcriber"):
        assert t.transcribe(AUDIO) == "texto cpu"
    assert f.chamadas == [("cuda", "float16"), ("cpu", "int8")]
    assert t.device_used == "cpu"
    assert "RuntimeError" in caplog.text and "CPU" in caplog.text


def test_falha_no_aquecimento_da_gpu_cai_para_cpu_no_load(models_dir):
    """cublas ausente: o modelo carrega, mas a 1ª execução quebra; o aquecimento pega isso."""
    f = Fabrica(fail_transcribe_cuda=True)
    t = make(models_dir, "cuda", f)
    t.load()
    assert t.device_used == "cpu"
    assert f.chamadas == [("cuda", "float16"), ("cpu", "int8")]


def test_falha_na_primeira_transcricao_cai_para_cpu_e_refaz(models_dir, caplog):
    f = Fabrica()
    t = make(models_dir, "cuda", f)
    t.load()
    assert t.device_used == "cuda"
    f.modelos[0].fail = True  # a GPU quebra depois do aquecimento
    with caplog.at_level(logging.WARNING, logger="hear_ai.transcriber"):
        assert t.transcribe(AUDIO) == "texto cpu"
    assert t.device_used == "cpu"
    assert "passando para CPU" in caplog.text
    # permanece na CPU nas próximas
    assert t.transcribe(AUDIO) == "texto cpu"
    assert f.chamadas.count(("cpu", "int8")) == 1


def test_erro_na_cpu_nao_e_engolido(models_dir):
    class Quebrado:
        def transcribe(self, *a, **k):
            raise ValueError("áudio inválido")

    t = Transcriber("small", models_dir, device="cpu", model_factory=lambda *a: Quebrado())
    with pytest.raises(ValueError):
        t.transcribe(AUDIO)


def test_load_em_paralelo_carrega_o_modelo_uma_vez(models_dir):
    f = Fabrica(delay=0.2)
    t = make(models_dir, "cpu", f)
    ts = [threading.Thread(target=t.load) for _ in range(6)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    assert len(f.chamadas) == 1
    assert t.device_used == "cpu"


def test_transcribe_e_load_concorrentes_nao_duplicam(models_dir):
    f = Fabrica(delay=0.2)
    t = make(models_dir, "cuda", f)
    ts = [threading.Thread(target=t.load)] + [threading.Thread(target=t.transcribe, args=(AUDIO,)) for _ in range(3)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    assert f.chamadas == [("cuda", "float16")]


def test_add_nvidia_dll_dirs_acha_bin_e_nao_repete(tmp_path, monkeypatch):
    import sys

    import hear_ai.transcriber as tr

    bin_dir = tmp_path / "nvidia" / "cublas" / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setattr(sys, "path", [str(tmp_path)] + sys.path)
    monkeypatch.setattr(tr, "_dll_dirs_done", set())
    monkeypatch.setenv("PATH", os.environ["PATH"])  # restaura o PATH ao final do teste
    if hasattr(os, "add_dll_directory"):
        monkeypatch.setattr(os, "add_dll_directory", lambda p: None)
    assert str(bin_dir) in add_nvidia_dll_dirs()
    assert str(bin_dir) in os.environ["PATH"].split(os.pathsep)
    assert str(bin_dir) not in add_nvidia_dll_dirs()


# --- GPU de verdade: só com GPU e HEAR_AI_GPU_TESTS=1 -----------------------

@pytest.mark.slow
def test_gpu_real_transcreve_fixture(fixture_pt, model_small):
    if os.environ.get("HEAR_AI_GPU_TESTS") != "1":
        pytest.skip("defina HEAR_AI_GPU_TESTS=1 para testar a GPU de verdade")
    if cuda_device_count() <= 0:
        pytest.skip("nenhuma GPU CUDA visível para o ctranslate2")
    audio, esperado = fixture_pt
    t = Transcriber("small", MODELS_DIR, device="cuda", language="pt")
    t.load()  # inclui o aquecimento (pode levar dezenas de segundos na 1ª vez)
    assert t.device_used == "cuda", "a GPU caiu para CPU; veja o log"
    t0 = time.perf_counter()
    texto = t.transcribe(audio)
    print(f"\nGPU: {time.perf_counter() - t0:.2f}s")
    assert word_match_ratio(esperado, texto) >= 0.7, texto


# --- T13: opções de transcrição e um bloco por vez ---------------------------------

def test_opcoes_padrao_incluem_condition_on_previous_text_false(models_dir):
    f = Fabrica()
    t = make(models_dir, "cpu", f)
    t.transcribe(AUDIO)
    kwargs = f.modelos[0].calls[-1]
    assert kwargs["condition_on_previous_text"] is False
    assert kwargs["beam_size"] == 5 and kwargs["vad_filter"] is True and kwargs["language"] == "pt"


def test_transcribe_options_sobrescreve_e_acrescenta(models_dir):
    f = Fabrica()
    t = Transcriber("small", models_dir, device="cpu", model_factory=f,
                    transcribe_options={"beam_size": 1, "condition_on_previous_text": True, "temperature": 0.0})
    t.transcribe(AUDIO)
    kwargs = f.modelos[0].calls[-1]
    assert kwargs["beam_size"] == 1 and kwargs["condition_on_previous_text"] is True
    assert kwargs["temperature"] == 0.0 and kwargs["vad_filter"] is True


def test_chamadas_simultaneas_sao_serializadas(models_dir):
    em_uso = {"agora": 0, "max": 0}
    trava = threading.Lock()

    class Lento(FakeModel):
        def transcribe(self, audio, **kwargs):
            with trava:
                em_uso["agora"] += 1
                em_uso["max"] = max(em_uso["max"], em_uso["agora"])
            time.sleep(0.02)
            with trava:
                em_uso["agora"] -= 1
            return iter([FakeSeg("ok")]), None

    t = Transcriber("small", models_dir, device="cpu", model_factory=lambda *a: Lento("cpu", "int8"))
    ts = [threading.Thread(target=t.transcribe, args=(AUDIO,)) for _ in range(8)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    assert em_uso["max"] == 1
