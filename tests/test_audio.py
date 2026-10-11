import wave

import numpy as np
import pytest

from conftest import FIXTURES
from hear_ai.audio import Recorder, load_wav


class FakeStream:
    def __init__(self, blocks, **kwargs):
        self.callback = kwargs["callback"]
        self.kwargs = kwargs
        self.blocks = blocks
        self.started = self.closed = False

    def start(self):
        self.started = True
        for b in self.blocks:
            self.callback(b.reshape(-1, 1), len(b), None, None)

    def stop(self):
        pass

    def close(self):
        self.closed = True


def factory_for(blocks, holder=None):
    def make(**kwargs):
        s = FakeStream(blocks, **kwargs)
        if holder is not None:
            holder.append(s)
        return s

    return make


def test_grava_e_concatena():
    blocks = [np.full(100, 0.1, np.float32), np.full(50, 0.2, np.float32)]
    holder = []
    r = Recorder(stream_factory=factory_for(blocks, holder))
    r.start()
    out = r.stop()
    assert out.dtype == np.float32 and out.ndim == 1 and len(out) == 150
    assert out[0] == np.float32(0.1) and out[-1] == np.float32(0.2)
    assert holder[0].kwargs["channels"] == 1 and holder[0].kwargs["samplerate"] == 16000
    assert holder[0].closed


def test_stop_sem_start_devolve_vazio():
    out = Recorder(stream_factory=factory_for([])).stop()
    assert out.dtype == np.float32 and out.shape == (0,)


def test_start_duas_vezes_nao_quebra():
    holder = []
    r = Recorder(stream_factory=factory_for([np.ones(10, np.float32)], holder))
    r.start()
    r.start()
    assert len(holder) == 1
    assert len(r.stop()) == 10


def test_sem_blocos_devolve_vazio():
    r = Recorder(stream_factory=factory_for([]))
    r.start()
    assert len(r.stop()) == 0


def test_max_seconds_descarta_excesso():
    blocks = [np.ones(1000, np.float32)] * 5
    r = Recorder(samplerate=1000, max_seconds=2, stream_factory=factory_for(blocks))
    r.start()
    assert len(r.stop()) == 2000


def test_reuso_apos_stop_nao_mistura_gravacoes():
    r = Recorder(stream_factory=factory_for([np.ones(10, np.float32)]))
    r.start()
    r.stop()
    r.start()
    assert len(r.stop()) == 10


def test_load_wav_fixture():
    a = load_wav(FIXTURES / "frase_pt.wav")
    assert a.dtype == np.float32 and a.ndim == 1
    assert len(a) > 16000 and 0.01 < np.abs(a).max() <= 1.0


def _write(path, data, rate, channels):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.astype(np.int16).tobytes())


def test_load_wav_estereo_para_mono(tmp_path):
    left = np.full(1600, 16384)
    right = np.full(1600, -16384)
    _write(tmp_path / "s.wav", np.column_stack([left, right]).ravel(), 16000, 2)
    a = load_wav(tmp_path / "s.wav")
    assert a.shape == (1600,) and np.allclose(a, 0.0)


def test_load_wav_reamostra(tmp_path):
    _write(tmp_path / "r.wav", np.full(8000, 8192), 8000, 1)
    a = load_wav(tmp_path / "r.wav")
    assert len(a) == 16000 and np.allclose(a, 0.25)


class StreamQueFalhaNoStart(FakeStream):
    def start(self):
        raise OSError("dispositivo ocupado")


def test_start_que_falha_fecha_o_stream_e_repassa_o_erro():
    holder = []

    def make(**kwargs):
        s = StreamQueFalhaNoStart([], **kwargs)
        holder.append(s)
        return s

    r = Recorder(stream_factory=make)
    with pytest.raises(OSError, match="ocupado"):
        r.start()
    assert holder[0].closed
    # o gravador continua utilizável: nenhum stream preso
    assert r._stream is None
    assert len(r.stop()) == 0


class StreamQueFalhaAoParar(FakeStream):
    def stop(self):
        raise OSError("USB desconectado")

    def close(self):
        self.closed = True
        raise OSError("USB desconectado")


def test_erro_ao_parar_ou_fechar_nao_perde_o_audio(caplog):
    holder = []

    def make(**kwargs):
        s = StreamQueFalhaAoParar([np.full(100, 0.5, np.float32)], **kwargs)
        holder.append(s)
        return s

    r = Recorder(stream_factory=make)
    r.start()
    with caplog.at_level("WARNING", logger="hear_ai.audio"):
        out = r.stop()
    assert len(out) == 100 and out[0] == np.float32(0.5)
    assert holder[0].closed  # tentou fechar mesmo após falha no stop()
    assert "OSError" in caplog.text
    assert r._chunks == []  # buffer liberado
    assert len(r.stop()) == 0


# --- T12: limite de 30 min, avisos, cópia em disco, snapshot, nível ----------------

import json  # noqa: E402
import logging  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from hear_ai import recovery  # noqa: E402

SR = 16000


class SecondsStream:
    """Stream falso que entrega `seconds` segundos em blocos de 1 s (sem sleep real).
    Cada bloco tem um valor diferente para conferir a ordem no arquivo."""

    def __init__(self, seconds, block_s=1.0, **kwargs):
        self.callback = kwargs["callback"]
        self.seconds = seconds
        self.n = int(SR * block_s)
        self.closed = False

    def start(self):
        total = int(self.seconds * SR)
        sent = 0
        i = 0
        while sent < total:
            n = min(self.n, total - sent)
            block = np.full(n, ((i % 200) - 100) / 128.0, np.float32)
            self.callback(block.reshape(-1, 1), n, None, None)
            sent += n
            i += 1

    def stop(self):
        pass

    def close(self):
        self.closed = True


def seconds_factory(seconds, **kw):
    return lambda **kwargs: SecondsStream(seconds, **kw, **kwargs)


def test_padrao_sem_corte_em_120s_grava_150s():
    r = Recorder(stream_factory=seconds_factory(150))
    assert r.max_seconds == 1800
    r.start()
    out = r.stop()
    assert len(out) == 150 * SR


def test_31_min_respeita_limite_de_30_com_aviso_e_limite_uma_vez(caplog):
    avisos, limites = [], []
    r = Recorder(
        stream_factory=seconds_factory(31 * 60),
        on_limit_warning=avisos.append,
        on_limit_reached=lambda: limites.append(1),
    )
    with caplog.at_level(logging.INFO, logger="hear_ai.audio"):
        r.start()
        assert r.frames_recorded == 1800 * SR
        out = r.stop()
    assert len(out) == 1800 * SR
    assert len(avisos) == 1 and avisos[0] == pytest.approx(60, abs=1)
    assert limites == [1]
    # o excedente não some em silêncio
    assert "limite de gravação atingido" in caplog.text
    assert "60.0 s de áudio passaram do limite" in caplog.text


def test_aviso_antes_do_limite_com_parametros_pequenos():
    eventos = []
    r = Recorder(
        max_seconds=10, warn_before_s=3,
        on_limit_warning=lambda s: eventos.append(("aviso", round(s))),
        on_limit_reached=lambda: eventos.append(("limite",)),
        stream_factory=seconds_factory(12),
    )
    r.start()
    assert len(r.stop()) == 10 * SR
    assert eventos == [("aviso", 3), ("limite",)]


def test_sem_aviso_se_nao_chega_perto_do_limite():
    eventos = []
    r = Recorder(max_seconds=100, warn_before_s=10, on_limit_warning=eventos.append,
                 on_limit_reached=lambda: eventos.append("x"), stream_factory=seconds_factory(80))
    r.start()
    r.stop()
    assert eventos == []


def test_aviso_e_limite_rearmam_na_proxima_gravacao():
    avisos = []
    r = Recorder(max_seconds=5, warn_before_s=1, on_limit_warning=avisos.append,
                 stream_factory=seconds_factory(6))
    r.start()
    r.stop()
    r.start()
    r.stop()
    assert len(avisos) == 2


def test_callback_de_limite_com_erro_nao_derruba_a_gravacao():
    def quebra(*a):
        raise RuntimeError("ui")

    r = Recorder(max_seconds=3, warn_before_s=1, on_limit_warning=quebra, on_limit_reached=quebra,
                 stream_factory=seconds_factory(5))
    r.start()
    assert len(r.stop()) == 3 * SR


def _pcm(path):
    return np.frombuffer(path.read_bytes(), dtype="<i2")


def test_copia_em_disco_tem_o_mesmo_conteudo(tmp_path):
    r = Recorder(spool_dir=tmp_path, stream_factory=seconds_factory(150))
    r.start()
    out = r.stop()
    path = r.last_spool_path
    assert path is not None and path.exists()  # stop() NÃO apaga
    assert path.name.startswith("gravacao-") and path.suffix == ".pcm"
    disco = _pcm(path)
    assert len(disco) == len(out) == 150 * SR
    np.testing.assert_allclose(disco.astype(np.float32) / 32768.0, out, atol=1 / 16000)
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["samplerate"] == SR and meta["version"] == 1 and "T" in meta["started"]


def test_copia_em_disco_so_ate_o_limite(tmp_path):
    r = Recorder(max_seconds=5, spool_dir=tmp_path, stream_factory=seconds_factory(8))
    r.start()
    r.stop()
    assert len(_pcm(r.last_spool_path)) == 5 * SR


def test_duas_gravacoes_no_mesmo_segundo_nao_colidem(tmp_path):
    r = Recorder(spool_dir=tmp_path, stream_factory=seconds_factory(1))
    r.start()
    r.stop()
    a = r.last_spool_path
    r.start()
    r.stop()
    b = r.last_spool_path
    assert a != b and a.exists() and b.exists()


def test_sem_spool_dir_nao_grava_em_disco():
    r = Recorder(stream_factory=seconds_factory(2))
    r.start()
    r.stop()
    assert r.last_spool_path is None


def test_spool_dir_invalido_segue_so_em_memoria(tmp_path, caplog):
    arquivo = tmp_path / "nao_e_pasta"
    arquivo.write_text("x")
    r = Recorder(spool_dir=arquivo, stream_factory=seconds_factory(2))
    with caplog.at_level(logging.WARNING, logger="hear_ai.audio"):
        r.start()
        out = r.stop()
    assert len(out) == 2 * SR
    assert r.last_spool_path is None
    assert "sem cópia em disco" in caplog.text


class ManualStream:
    """Stream controlado pelo teste: blocos entregues por push(), como um microfone real."""

    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]

    def push(self, seconds, value=0.25):
        n = int(seconds * SR)
        self.callback(np.full((n, 1), value, np.float32), n, None, None)

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def _manual():
    holder = []

    def make(**kwargs):
        s = ManualStream(**kwargs)
        holder.append(s)
        return s

    return make, holder


def _wait_size(path, size, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if path.exists() and path.stat().st_size >= size:
            return True
        time.sleep(0.02)
    return False


def test_travamento_simulado_arquivo_existe_e_e_recuperavel(tmp_path):
    """O app trava no meio da gravação (stop() nunca é chamado): o .pcm já está em disco
    (flush periódico) e a recuperação devolve o áudio."""
    make, holder = _manual()
    r = Recorder(spool_dir=tmp_path, stream_factory=make)
    r.start()
    holder[0].push(3.0, 0.25)
    path = r.current_spool_path
    t0 = time.monotonic()
    assert _wait_size(path, 3 * SR * 2), "o flush periódico deveria levar os dados ao disco"
    assert time.monotonic() - t0 < 1.5

    # outro processo (o app reaberto) procura órfãos
    orfaos = recovery.find_orphans(tmp_path)
    assert [o.path for o in orfaos] == [path]
    assert orfaos[0].duration_s == pytest.approx(3.0)
    audio = recovery.load_orphan(orfaos[0])
    assert len(audio) == 3 * SR and np.allclose(audio, 0.25, atol=1e-3)
    # enquanto este Recorder grava, o arquivo dele é ignorado quando excluído
    assert recovery.find_orphans(tmp_path, exclude=r.current_spool_path) == []
    r.stop()


def test_snapshot_parcial_e_frames_recorded():
    make, holder = _manual()
    r = Recorder(stream_factory=make)
    r.start()
    holder[0].push(1.0, 0.1)
    holder[0].push(1.0, 0.2)
    assert r.frames_recorded == 2 * SR
    s = r.snapshot(SR + SR // 2)
    assert len(s) == SR // 2 and np.allclose(s, 0.2)
    s0 = r.snapshot(0)
    assert len(s0) == 2 * SR and np.allclose(s0[:SR], 0.1)
    s0[:] = 9  # é cópia
    assert np.allclose(r.snapshot(0)[:SR], 0.1)
    assert len(r.snapshot(5 * SR)) == 0
    assert len(r.snapshot(-10)) == 2 * SR
    r.stop()
    assert r.frames_recorded == 0 and len(r.snapshot(0)) == 0


def test_snapshot_concorrente_com_gravacao():
    make, holder = _manual()
    r = Recorder(stream_factory=make)
    r.start()
    erros, tamanhos = [], []
    parar = threading.Event()

    def leitor():
        try:
            while not parar.is_set():
                n = r.frames_recorded
                s = r.snapshot(0)
                assert len(s) >= n and len(s) % 160 == 0
                tamanhos.append(len(s))
        except Exception as e:  # pragma: no cover - só em falha
            erros.append(e)

    ts = [threading.Thread(target=leitor) for _ in range(3)]
    for t in ts:
        t.start()
    for _ in range(300):
        holder[0].push(0.01)
    parar.set()
    for t in ts:
        t.join()
    out = r.stop()
    assert erros == []
    assert len(out) == 300 * 160
    assert tamanhos and max(tamanhos) <= len(out)


def test_level_reflete_o_ultimo_bloco():
    make, holder = _manual()
    r = Recorder(stream_factory=make)
    assert r.level() == 0.0
    r.start()
    holder[0].push(0.1, 0.5)
    assert r.level() == pytest.approx(0.5, abs=1e-3)
    holder[0].push(0.1, 0.0)
    assert r.level() == 0.0
    holder[0].push(0.1, 2.0)  # saturado
    assert r.level() == 1.0
    r.stop()
    assert r.level() == 0.0
