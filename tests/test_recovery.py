import json
import os
import time
from datetime import datetime, timedelta

import numpy as np
import pytest

from hear_ai import recovery


def make_pcm(dir_, name, samples, rate=16000, meta=True, extra=b""):
    pcm = dir_ / f"gravacao-{name}.pcm"
    pcm.write_bytes(np.asarray(samples, dtype="<i2").tobytes() + extra)
    if meta is True:
        pcm.with_suffix(".json").write_text(
            json.dumps({"samplerate": rate, "started": "2026-10-09T10:00:00-03:00", "version": 1}),
            encoding="utf-8",
        )
    elif isinstance(meta, str):
        pcm.with_suffix(".json").write_text(meta, encoding="utf-8")
    return pcm


def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_pasta_inexistente_ou_vazia(tmp_path):
    assert recovery.find_orphans(tmp_path / "nao") == []
    assert recovery.find_orphans(tmp_path) == []
    assert recovery.discard_older_than(tmp_path / "nao", 1) == 0


def test_acha_orfao_com_duracao_e_data(tmp_path):
    make_pcm(tmp_path, "a", np.full(32000, 1000))
    (o,) = recovery.find_orphans(tmp_path)
    assert o.frames == 32000 and o.samplerate == 16000 and o.duration_s == 2.0
    assert o.started == datetime.fromisoformat("2026-10-09T10:00:00-03:00")
    assert o.when == o.started
    audio = recovery.load_orphan(o)
    assert audio.dtype == np.float32 and len(audio) == 32000
    assert np.allclose(audio, 1000 / 32768)


def test_ordem_mais_recente_primeiro_e_ignora_outros_arquivos(tmp_path):
    a = make_pcm(tmp_path, "a", np.ones(100))
    b = make_pcm(tmp_path, "b", np.ones(100))
    age(a, 1)
    (tmp_path / "outra-coisa.pcm").write_bytes(b"\0\0")
    (tmp_path / "gravacao-x.wav").write_bytes(b"\0\0")
    assert [o.path for o in recovery.find_orphans(tmp_path)] == [b, a]


def test_exclui_gravacao_em_curso(tmp_path):
    a = make_pcm(tmp_path, "a", np.ones(100))
    b = make_pcm(tmp_path, "b", np.ones(100))
    assert [o.path for o in recovery.find_orphans(tmp_path, exclude=a)] == [b]
    assert recovery.find_orphans(tmp_path, exclude=[a, str(b)]) == []
    assert len(recovery.find_orphans(tmp_path, exclude=None)) == 2


def test_pcm_truncado_byte_impar(tmp_path):
    make_pcm(tmp_path, "t", np.full(10, 500), extra=b"\x07")
    (o,) = recovery.find_orphans(tmp_path)
    assert o.frames == 10
    assert len(recovery.load_orphan(o)) == 10


def test_pcm_vazio_nao_e_listado(tmp_path):
    make_pcm(tmp_path, "v", [])
    make_pcm(tmp_path, "um_byte", [], extra=b"\x01")
    assert recovery.find_orphans(tmp_path) == []


@pytest.mark.parametrize("meta", [
    False,
    "{nao e json",
    "[1, 2]",
    json.dumps({"samplerate": "x", "started": 5}),
    json.dumps({"samplerate": True}),
    json.dumps({"started": "ontem"}),
])
def test_json_ausente_ou_corrompido_assume_16k(tmp_path, meta):
    make_pcm(tmp_path, "m", np.ones(1600), meta=meta)
    (o,) = recovery.find_orphans(tmp_path)
    assert o.samplerate == 16000 and o.started is None
    assert o.when == o.modified
    assert len(recovery.load_orphan(o)) == 1600


def test_outra_taxa_e_reamostrada_para_16k(tmp_path):
    make_pcm(tmp_path, "r", np.full(8000, 8192), rate=8000)
    (o,) = recovery.find_orphans(tmp_path)
    assert o.duration_s == 1.0
    audio = recovery.load_orphan(o)
    assert len(audio) == 16000 and np.allclose(audio, 0.25)


def test_discard_apaga_pcm_e_json(tmp_path):
    p = make_pcm(tmp_path, "d", np.ones(10))
    (o,) = recovery.find_orphans(tmp_path)
    recovery.discard(o)
    assert not p.exists() and not p.with_suffix(".json").exists()
    recovery.discard(o)  # de novo: não quebra


def test_discard_older_than(tmp_path):
    velho = make_pcm(tmp_path, "velho", np.ones(10))
    vazio_velho = make_pcm(tmp_path, "vazio", [])
    novo = make_pcm(tmp_path, "novo", np.ones(10))
    em_curso = make_pcm(tmp_path, "curso", np.ones(10))
    so_json = tmp_path / "gravacao-sozinho.json"
    so_json.write_text("{}", encoding="utf-8")
    for p in (velho, vazio_velho, em_curso, so_json):
        age(p, 10)
    n = recovery.discard_older_than(tmp_path, 7, exclude=em_curso)
    assert n == 2
    assert not velho.exists() and not velho.with_suffix(".json").exists()
    assert not vazio_velho.exists()
    assert not so_json.exists()
    assert novo.exists() and em_curso.exists()


def test_discard_older_than_com_now_injetado(tmp_path):
    p = make_pcm(tmp_path, "a", np.ones(10))
    assert recovery.discard_older_than(tmp_path, 1) == 0
    assert recovery.discard_older_than(tmp_path, 1, now=datetime.now() + timedelta(days=2)) == 1
    assert not p.exists()
