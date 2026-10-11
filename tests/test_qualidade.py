"""T30 (D3): initial_prompt do dicionário, pós-texto no controller, idioma auto, ÔNDEX."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from conftest import FIXTURES, MODELS_DIR, read_wav
from hear_ai.config import Config, load_config
from hear_ai.postprocess import DEFAULT_DICTIONARY, postprocess
from hear_ai.transcriber import Transcriber, prompt_from_dictionary

AUDIO = np.zeros(16000, dtype=np.float32)


class Seg:
    def __init__(self, text):
        self.text = text


class Modelo:
    """WhisperModel falso: registra os kwargs; detect_language configurável."""

    def __init__(self, detect=None, texto=" ok "):
        self.chamadas = []
        self.detect = detect
        self.texto = texto

    def transcribe(self, audio, **kw):
        self.chamadas.append(kw)
        return iter([Seg(self.texto)]), None

    def detect_language(self, audio):
        if self.detect is None:
            raise AttributeError("sem detecção")
        return self.detect


@pytest.fixture
def pasta_modelo(tmp_path):
    (tmp_path / "faster-whisper-small").mkdir()
    (tmp_path / "faster-whisper-small" / "model.bin").write_bytes(b"x")
    return tmp_path


def tr_falso(pasta, modelo, **kw):
    return Transcriber("small", pasta, device="cpu", model_factory=lambda *a: modelo, **kw)


# --- initial_prompt ------------------------------------------------------------------------------

def test_prompt_montado_com_os_termos_do_dicionario():
    assert prompt_from_dictionary(DEFAULT_DICTIONARY) == "Ondex, Maestri, Groq, Hear AI, Claude Code."
    assert prompt_from_dictionary({"a": "X", "b": "X", "c": " "}) == "X."
    assert prompt_from_dictionary({}) is None and prompt_from_dictionary(None) is None


def test_prompt_vai_em_cada_chamada_e_some_quando_desligado(pasta_modelo):
    m = Modelo()
    t = tr_falso(pasta_modelo, m, initial_prompt="Ondex, Groq.")
    t.transcribe(AUDIO)
    t.transcribe(AUDIO)
    assert [c.get("initial_prompt") for c in m.chamadas] == ["Ondex, Groq.", "Ondex, Groq."]
    assert all(c["condition_on_previous_text"] is False for c in m.chamadas)
    t.initial_prompt = None
    t.transcribe(AUDIO)
    assert "initial_prompt" not in m.chamadas[-1]


def test_prompt_vai_em_cada_bloco_da_t13(pasta_modelo):
    from hear_ai.streaming import StreamingTranscriber

    m = Modelo()
    t = tr_falso(pasta_modelo, m, initial_prompt="Ondex.")

    class Rec:
        def __init__(self, audio):
            self.audio = audio

        @property
        def frames_recorded(self):
            return len(self.audio)

        def snapshot(self, start):
            return self.audio[start:].copy()

    # 70 s de "fala" com pausas a cada 5 s -> vários blocos
    partes = []
    for _ in range(14):
        partes += [np.full(int(4.4 * 16000), 0.2, np.float32), np.zeros(int(0.6 * 16000), np.float32)]
    audio = np.concatenate(partes)
    st = StreamingTranscriber(Rec(audio), t, logging.getLogger("t"), poll_s=3600)
    st.start()
    st.finish(audio)
    assert len(m.chamadas) >= 3 and all(c.get("initial_prompt") == "Ondex." for c in m.chamadas)


# --- idioma auto -----------------------------------------------------------------------------------

@pytest.mark.parametrize("detect, esperado", [
    (("en", 0.97, []), "en"),
    (("pt", 0.88, []), "pt"),
    (("en", 0.40, []), "pt"),  # pouca confiança -> pt
    (("fr", 0.95, []), "pt"),  # fora de pt/en -> pt
    (None, "pt"),  # sem detecção (erro) -> pt
])
def test_idioma_auto(pasta_modelo, detect, esperado):
    m = Modelo(detect=detect)
    t = tr_falso(pasta_modelo, m, language="auto")
    t.transcribe(AUDIO)
    assert m.chamadas[-1]["language"] == esperado and t.last_language == esperado


def test_idioma_fixo_nao_detecta(pasta_modelo):
    m = Modelo(detect=("en", 0.99, []))
    t = tr_falso(pasta_modelo, m, language="pt")
    t.transcribe(AUDIO)
    assert m.chamadas[-1]["language"] == "pt"


def test_aquecimento_da_gpu_com_auto_usa_pt(pasta_modelo):
    m = Modelo()
    t = Transcriber("small", pasta_modelo, device="cuda", language="auto",
                    model_factory=lambda *a: m, cuda_count=lambda: 1)
    t.load()
    assert m.chamadas[0]["language"] == "pt"  # "auto" não é idioma válido para o Whisper


# --- config ----------------------------------------------------------------------------------------

def test_config_novos_campos_e_validacao(tmp_path):
    c = Config()
    assert c.dictionary == DEFAULT_DICTIONARY and c.dictionary is not DEFAULT_DICTIONARY
    assert c.voice_commands is True and c.use_initial_prompt is True
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"config_version": 2, "language": "auto", "dictionary": {"hirai": "Hear AI"},
                             "voice_commands": False, "use_initial_prompt": False}), encoding="utf-8")
    c = load_config(p)
    assert (c.language, c.dictionary, c.voice_commands, c.use_initial_prompt) == \
        ("auto", {"hirai": "Hear AI"}, False, False)
    for ruim in ({"dictionary": ["a"]}, {"dictionary": {"a": ""}}, {"dictionary": {"": "b"}},
                 {"dictionary": {"a": 1}}, {"language": "fr"}, {"voice_commands": "sim"}):
        p.write_text(json.dumps({"config_version": 2, **ruim}), encoding="utf-8")
        campo = next(iter(ruim))
        assert getattr(load_config(p), campo) == getattr(Config(), campo), ruim


def test_settings_model_aceita_auto():
    from hear_ai.settings_model import SettingsForm

    f = SettingsForm.from_config(Config(language="auto"))
    assert "language" not in f.validate()


# --- pós-texto -------------------------------------------------------------------------------------

def test_dicionario_troca_maiuscula_acentuada():
    assert postprocess("ÔNDEX é ótimo") == "Ondex é ótimo"
    assert postprocess("o ôndex e o GRÔQ") == "o Ondex e o Groq"
    assert postprocess("meu hear ái") == "meu Hear AI"
    assert postprocess("ondexx não muda") == "ondexx não muda"
    assert postprocess("Clãude Códe") == "Claude Code"


class Paster:
    def __init__(self):
        self.colado = []
        self.clipboard = SimpleNamespace(set=lambda t: None)

    def paste(self, t):
        self.colado.append(t)
        return SimpleNamespace(pasted=True, restored=True, reason="ok", in_clipboard=False)


class Sink:
    def __init__(self):
        self.salvo = []

    def save(self, t, meta):
        self.salvo.append(t)
        return "id1"

    def set_status(self, *a):
        pass


def _controller(texto, **kw):
    from hear_ai.controller import Controller

    class Rec:
        def start(self):
            pass

        def stop(self):
            return np.zeros(16000, np.float32)

    class Tr:
        def transcribe(self, audio):
            return texto

    fb = SimpleNamespace(start=lambda: None, stop=lambda: None, done=lambda: None, error=lambda: None)
    log = logging.getLogger(f"t30.{id(object())}")
    linhas = []
    h = logging.Handler()
    h.emit = lambda r: linhas.append(r.getMessage())
    log.addHandler(h)
    log.setLevel(logging.DEBUG)
    paster, sink = Paster(), Sink()
    c = Controller(Rec(), Tr(), paster, fb, sink, log, "pt", run_async=False, **kw)
    return c, paster, sink, linhas


def test_controller_aplica_pos_texto_antes_de_salvar_e_colar():
    c, paster, sink, _ = _controller("o ÔNDEX abriu. nova linha teste do hear ai")
    c.begin()
    c.end()
    esperado = "o Ondex abriu.\nTeste do Hear AI"
    assert sink.salvo == [esperado] and paster.colado == [esperado]


def test_controller_com_dicionario_do_config():
    from hear_ai.controller import postprocessor_from_config

    cfg = Config(dictionary={"hirai": "Hear AI"}, voice_commands=False)
    c, paster, sink, _ = _controller("teste do hirai nova linha", postprocess=postprocessor_from_config(cfg))
    c.begin()
    c.end()
    assert paster.colado == ["teste do Hear AI nova linha"]  # comandos de voz desligados


def test_controller_sem_pos_texto_e_com_pos_texto_quebrado():
    c, paster, _, _ = _controller("o ondex", postprocess=None)
    c.begin()
    c.end()
    assert paster.colado == ["o ondex"]

    def quebra(t):
        raise RuntimeError(f"falhou com {t}")

    c, paster, sink, linhas = _controller("segredo ditado", postprocess=quebra)
    c.begin()
    c.end()
    assert paster.colado == ["segredo ditado"]  # nunca perde o ditado
    assert any("RuntimeError" in l for l in linhas) and not any("segredo" in l for l in linhas)


def test_recuperacao_tambem_aplica_pos_texto():
    c, paster, sink, _ = _controller("o ondex recuperado")
    clip = []
    paster.clipboard = SimpleNamespace(set=clip.append)
    assert c.recover(["o1"], lambda o: np.zeros(16000, np.float32), lambda o: None)
    assert sink.salvo == ["o Ondex recuperado"] and clip == ["o Ondex recuperado"]


# --- com o modelo de verdade -------------------------------------------------------------------

@pytest.fixture(scope="module")
def tr_real():
    if not (MODELS_DIR / "faster-whisper-small" / "model.bin").exists():
        pytest.skip("modelo 'small' não baixado")
    t = Transcriber("small", MODELS_DIR, device="auto", language="pt",
                    initial_prompt=prompt_from_dictionary(DEFAULT_DICTIONARY))
    t.load()
    return t


@pytest.mark.slow
def test_real_nomes_proprios_com_prompt(tr_real):
    texto = tr_real.transcribe(read_wav(FIXTURES / "nomes_pt.wav"))
    for nome, vezes in (("Ondex", 2), ("Maestri", 2), ("Groq", 1), ("Hear AI", 1), ("Claude Code", 1)):
        assert texto.count(nome) >= vezes, (nome, texto)


@pytest.mark.slow
def test_real_silencio_com_prompt_fica_vazio(tr_real):
    assert tr_real.transcribe(np.zeros(3 * 16000, np.float32)) == ""
    ruido = (np.random.default_rng(1).standard_normal(3 * 16000) * 0.003).astype(np.float32)
    assert tr_real.transcribe(ruido) == ""


@pytest.mark.slow
def test_real_idioma_auto(tr_real):
    tr_real.language = "auto"
    try:
        tr_real.transcribe(read_wav(FIXTURES / "frase_en.wav"))
        assert tr_real.last_language == "en"
        tr_real.transcribe(read_wav(FIXTURES / "frase_pt.wav"))
        assert tr_real.last_language == "pt"
    finally:
        tr_real.language = "pt"
