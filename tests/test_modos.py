"""T47b P4 — interface dos dois modos (A fiel / B gerador de prompts). Qt offscreen, fakes, `tmp_path`.

ZERO rede (nem 127.0.0.1): um guarda automático faz qualquer `socket.connect` falhar o teste; os provedores recebem um
`http` falso. Sem teclas reais, sem MessageBox; config, histórico e preferências sempre de `tmp_path`.
O Ollama NÃO está instalado nesta máquina: "indisponível: gerador local não encontrado" é o estado real aqui, e a
qualidade do gerador NÃO é medida (sem LLM local).

Imagens de evidência:  .venv\\Scripts\\python.exe tests\\test_modos.py [pasta]   (padrão: evidencias)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"))

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from hear_ai import gerador, modos  # noqa: E402
from hear_ai.config import load_config, save_config  # noqa: E402
from hear_ai.controller import Controller  # noqa: E402
from hear_ai.history import HistoryStore  # noqa: E402
from hear_ai.hotkey import ERROR_HOTKEY_ALREADY_REGISTERED, Win32HotkeyApi  # noqa: E402

FALA = "quero um relatório de vendas com 1234 clientes e o arquivo C:\\dados\\vendas.csv"
GERADO = "Crie um relatório de vendas.\n- 1234 clientes\n- Arquivo: C:\\dados\\vendas.csv"
CHAVE = "SEGREDO-DA-CHAVE-DE-API-98765"


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    """Guarda: nenhum teste deste arquivo pode abrir conexão de rede (nem para 127.0.0.1)."""
    def boom(*a, **k):
        raise AssertionError("teste do Modo B tentou usar a rede")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def limpar_qt(qapp):
    """Fecha e apaga as janelas que cada teste criou (janelas que sobram derrubam o pytest ao sair)."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    antes = {id(w) for w in QApplication.topLevelWidgets()}
    yield
    for w in list(QApplication.topLevelWidgets()):
        if id(w) not in antes:
            w.close()
            w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.processEvents()


@pytest.fixture(autouse=True)
def pasta_inicializar_falsa(tmp_path, monkeypatch):
    from hear_ai import startup

    monkeypatch.setattr(startup, "default_startup_dir", lambda env=None: tmp_path / "Inicializar-falsa")


def esperar(cond, timeout: float = 3.0) -> bool:
    from PySide6.QtCore import QCoreApplication

    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.003)
    QCoreApplication.processEvents()
    return cond()


# --- fakes -----------------------------------------------------------------------------------------------------

class HttpFalso:
    """`http(url, payload, timeout, headers=None)` dos provedores. Registra a thread e o que foi enviado."""

    def __init__(self, modelos=("qwen3:8b",), resposta=GERADO, erro=None, nuvem=False):
        self.modelos, self.resposta, self.erro, self.nuvem = modelos, resposta, erro, nuvem
        self.chamadas: list[tuple] = []
        self.threads: list[str] = []

    def __call__(self, url, payload, timeout, headers=None):
        self.chamadas.append((url, payload, headers))
        self.threads.append(threading.current_thread().name)
        if url.endswith("/api/tags"):
            if self.erro is not None:
                raise self.erro
            return {"models": [{"name": m} for m in self.modelos]}
        if self.erro is not None:
            raise self.erro
        if self.nuvem:
            return {"choices": [{"message": {"content": self.resposta}}]}
        return {"message": {"content": self.resposta}}

    def mensagens_do_usuario(self):
        out = []
        for _url, payload, _h in self.chamadas:
            if payload:
                out += [m["content"] for m in payload.get("messages", []) if m.get("role") == "user"]
        return out


def cfg_de(tmp_path, **kw):
    cfg = load_config(tmp_path / "config.json")
    cfg.models_dir = str(tmp_path / "models")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class GravadorFalso:
    last_spool_path = None

    def start(self):
        pass

    def stop(self):
        return np.zeros(16000, dtype=np.float32)


class TranscritorFalso:
    def __init__(self, texto=FALA):
        self.texto = texto

    def transcribe(self, audio):
        return self.texto


class ColadorFalso:
    def __init__(self):
        self.colado, self.threads = [], []

    def paste(self, text):
        self.colado.append(text)


class FeedbackFalso:
    def start(self):
        pass

    def stop(self):
        pass

    def done(self):
        pass

    def error(self):
        pass


def ditar(tmp_path, cfg, http, texto=FALA):
    """Um ditado completo pelo Controller REAL (run_async=True: _process roda numa thread de trabalho)."""
    store = HistoryStore(tmp_path / "historico")
    eventos = []
    colador = ColadorFalso()
    passo = modos.make_prompt_step(cfg, config_dir=tmp_path, http=http) if modos.is_prompt_mode(cfg) else None
    ctl = Controller(GravadorFalso(), TranscritorFalso(texto), colador, FeedbackFalso(), store,
                     logging.getLogger("teste.modos"), "pt", on_state_change=eventos.append, run_async=True,
                     postprocess=None, prompt_step=passo)
    ctl.begin()
    ctl.end()
    ctl.join(5)
    esperar(lambda: ctl.state == "idle", 3)
    return SimpleNamespace(ctl=ctl, store=store, eventos=eventos, colador=colador, passo=passo)


# --- Modo A: o fluxo de sempre, zero rede ---------------------------------------------------------------------

def test_modo_a_cola_a_fala_sem_gerador_e_sem_rede(tmp_path):
    cfg = cfg_de(tmp_path)
    assert cfg.operation_mode == "fiel"
    http = HttpFalso()
    r = ditar(tmp_path, cfg, http)
    assert r.passo is None and r.ctl.prompt_step is None  # nada de gerador montado
    assert r.colador.colado == [FALA] and http.chamadas == []
    assert "generating" not in r.eventos and "generator_fallback" not in r.eventos
    e, = r.store.entries()
    assert e.text == FALA and e.generated is None


def test_apply_operation_mode_liga_e_desliga_o_passo(tmp_path):
    cfg = cfg_de(tmp_path)
    ctl = SimpleNamespace(prompt_step="x")
    modos.apply_operation_mode(ctl, cfg, tmp_path / "config.json")
    assert ctl.prompt_step is None
    cfg.operation_mode = "prompt"
    modos.apply_operation_mode(ctl, cfg, tmp_path / "config.json")
    assert callable(ctl.prompt_step)
    cfg.operation_mode = "fiel"
    modos.apply_operation_mode(ctl, cfg, tmp_path / "config.json")
    assert ctl.prompt_step is None


# --- Modo B: sucesso, falhas, histórico ------------------------------------------------------------------------

def test_modo_b_sucesso_cola_o_prompt_gerado_fora_da_thread_do_qt(tmp_path):
    cfg = cfg_de(tmp_path, operation_mode="prompt", ollama_model="qwen3:8b")
    http = HttpFalso()
    r = ditar(tmp_path, cfg, http)
    assert r.colador.colado == [GERADO]
    assert "generating" in r.eventos and "generator_fallback" not in r.eventos
    assert http.threads and "MainThread" not in http.threads  # o provedor roda numa thread de trabalho
    assert FALA in "\n".join(http.mensagens_do_usuario())  # a transcrição fiel foi a entrada do gerador
    e, = r.store.entries()
    assert e.text == GERADO and e.raw == FALA and e.generated == GERADO and e.revisar is False


@pytest.mark.parametrize("erro, resposta", [
    (ConnectionRefusedError("recusou"), GERADO),  # Ollama fora do ar
    (TimeoutError("demorou"), GERADO),  # timeout
    (None, ""),  # resposta vazia
    (None, "[[SEM_PEDIDO]]"),  # a fala não pedia nada
    (None, "   \n  "),
])
def test_modo_b_toda_falha_cola_a_transcricao_exata_e_avisa(tmp_path, erro, resposta):
    cfg = cfg_de(tmp_path, operation_mode="prompt", ollama_model="qwen3:8b")
    r = ditar(tmp_path, cfg, HttpFalso(erro=erro, resposta=resposta))
    assert r.colador.colado == [FALA]  # nunca perde a fala
    assert "generator_fallback" in r.eventos
    e, = r.store.entries()
    assert e.text == FALA and e.raw in (None, FALA) and e.generated is None  # o texto JÁ é a fala fiel


def test_modo_b_passo_que_levanta_excecao_cola_a_transcricao(tmp_path):
    cfg = cfg_de(tmp_path, operation_mode="prompt")
    store = HistoryStore(tmp_path / "historico")
    eventos, colador = [], ColadorFalso()

    def quebra(_texto):
        raise RuntimeError("explodiu")

    ctl = Controller(GravadorFalso(), TranscritorFalso(), colador, FeedbackFalso(), store,
                     logging.getLogger("teste.modos"), "pt", on_state_change=eventos.append, run_async=True,
                     postprocess=None, prompt_step=quebra)
    ctl.begin()
    ctl.end()
    ctl.join(5)
    assert colador.colado == [FALA] and "generator_fallback" in eventos


def test_historico_marca_revisar_quando_o_prompt_perde_numero_ou_caminho(tmp_path):
    cfg = cfg_de(tmp_path, operation_mode="prompt", ollama_model="qwen3:8b")
    r = ditar(tmp_path, cfg, HttpFalso(resposta="Crie um relatório de vendas para os clientes."))
    e, = r.store.entries()
    assert e.generated and e.revisar is True and e.raw == FALA  # 1234 e o caminho sumiram: "revisar"
    assert r.colador.colado == [e.generated]  # a colagem não é bloqueada por causa disso


# --- nuvem: consentimento, chave -------------------------------------------------------------------------------

def test_nuvem_sem_consentimento_nao_chama_o_provedor(tmp_path, monkeypatch):
    monkeypatch.setenv("HEARAI_TESTE_CHAVE", CHAVE)
    cfg = cfg_de(tmp_path, operation_mode="prompt", generator_provider="openai_compat",
                 cloud_base_url="https://api.exemplo.com/v1", cloud_model="m", cloud_key_env="HEARAI_TESTE_CHAVE",
                 cloud_consent=False)
    http = HttpFalso(nuvem=True)
    assert modos.build_provider(cfg, http=http) is None
    r = ditar(tmp_path, cfg, http)
    assert http.chamadas == []  # nada foi enviado
    assert r.colador.colado == [FALA] and "generator_fallback" in r.eventos
    assert modos.make_prompt_step(cfg, config_dir=tmp_path, http=http)(FALA).motivo == gerador.MOTIVO_SEM_CONSENTIMENTO


def test_nuvem_com_consentimento_envia_so_texto_e_a_chave_nunca_vaza(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HEARAI_TESTE_CHAVE", CHAVE)
    caplog.set_level(logging.DEBUG)
    cfg = cfg_de(tmp_path, operation_mode="prompt", generator_provider="openai_compat",
                 cloud_base_url="https://api.exemplo.com/v1", cloud_model="m", cloud_key_env="HEARAI_TESTE_CHAVE",
                 cloud_consent=True, cloud_consent_host="api.exemplo.com")
    http = HttpFalso(nuvem=True)
    r = ditar(tmp_path, cfg, http)
    url, payload, headers = http.chamadas[0]
    assert url == "https://api.exemplo.com/v1/chat/completions" and CHAVE in headers["Authorization"]  # a chave É usada
    assert r.colador.colado == [GERADO]
    save_config(cfg, tmp_path / "config.json")
    tudo = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in tmp_path.rglob("*") if p.is_file())
    assert CHAVE not in tudo and CHAVE not in caplog.text  # nem config, nem histórico, nem log
    assert "HEARAI_TESTE_CHAVE" in tudo  # só o NOME da variável fica no config


# --- o aceite da nuvem é amarrado ao HOST (revisão 023, achado ALTA) -------------------------------------------

def cfg_nuvem(tmp_path, url="https://api.exemplo.com/v1", host="api.exemplo.com", consent=True):
    return cfg_de(tmp_path, operation_mode="prompt", generator_provider="openai_compat", cloud_base_url=url,
                  cloud_model="m", cloud_key_env="HEARAI_TESTE_CHAVE", cloud_consent=consent,
                  cloud_consent_host=host)


def test_editar_a_url_direto_no_config_nao_chama_a_nuvem(tmp_path, monkeypatch):
    monkeypatch.setenv("HEARAI_TESTE_CHAVE", CHAVE)
    cfg_path = tmp_path / "config.json"
    save_config(cfg_nuvem(tmp_path), cfg_path)
    assert modos.build_provider(load_config(cfg_path), http=HttpFalso(nuvem=True)) is not None  # sanidade: host aceito
    # o usuário edita o config.json na mão (fora da janela) e aponta para OUTRO servidor
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").replace("api.exemplo.com/v1", "outro.exemplo.org/v1"),
                        encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.cloud_base_url == "https://outro.exemplo.org/v1" and cfg.cloud_consent is True  # o booleano ficou
    assert modos.consent_ok(cfg) is False and modos.build_provider(cfg, http=HttpFalso(nuvem=True)) is None
    http = HttpFalso(nuvem=True)
    r = ditar(tmp_path, cfg, http)
    assert http.chamadas == []  # NADA foi enviado para o servidor novo
    assert r.colador.colado == [FALA] and "generator_fallback" in r.eventos  # Modo B cola a transcrição
    assert modos.make_prompt_step(cfg, config_dir=tmp_path, http=http)(FALA).motivo == gerador.MOTIVO_SEM_CONSENTIMENTO
    st = modos.provider_status(cfg, http=http)
    assert not st.ok and "aceite foi dado para ‘api.exemplo.com’" in st.text and "‘outro.exemplo.org’" in st.text
    assert modos.uses_cloud(cfg) is False  # nem o selo âmbar


@pytest.mark.parametrize("url", ["https://api.exemplo.com/v1", "https://API.Exemplo.COM:8443/v2/",
                                 "https://api.exemplo.com./v1"])
def test_mesmo_host_continua_chamando_a_nuvem_mesmo_com_outro_caminho_porta_ou_caixa(tmp_path, monkeypatch, url):
    monkeypatch.setenv("HEARAI_TESTE_CHAVE", CHAVE)
    cfg = cfg_nuvem(tmp_path, url=url)
    http = HttpFalso(nuvem=True)
    r = ditar(tmp_path, cfg, http)
    assert modos.consent_ok(cfg) and modos.uses_cloud(cfg)
    assert len(http.chamadas) >= 1 and r.colador.colado == [GERADO]


def test_config_antigo_com_consentimento_sem_host_fica_pendente(tmp_path, monkeypatch):
    monkeypatch.setenv("HEARAI_TESTE_CHAVE", CHAVE)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"operation_mode": "prompt", "generator_provider": "openai_compat",
                                    "cloud_base_url": "https://api.exemplo.com/v1", "cloud_model": "m",
                                    "cloud_key_env": "HEARAI_TESTE_CHAVE", "cloud_consent": True}), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.cloud_consent is True and cfg.cloud_consent_host == ""  # como um config da versão anterior
    assert modos.consent_ok(cfg) is False and modos.build_provider(cfg, http=HttpFalso(nuvem=True)) is None
    http = HttpFalso(nuvem=True)
    r = ditar(tmp_path, cfg, http)
    assert http.chamadas == [] and r.colador.colado == [FALA]
    assert modos.provider_status(cfg, http=http).text == "falta o seu aceite do envio do texto da fala"


def test_janela_trata_o_aceite_antigo_como_pendente_e_pede_de_novo(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("HEARAI_TESTE_CHAVE", CHAVE)
    w = janela(tmp_path, operation_mode="prompt", generator_provider="openai_compat",
               cloud_base_url="https://api.exemplo.com/v1", cloud_model="m", cloud_key_env="HEARAI_TESTE_CHAVE",
               cloud_consent=True)  # aceite SEM host: config antigo
    assert w.modo.consent_effective() is False and "ainda não dado" in w.modo.consent_label.text()
    assert w.save() is False and "aceite" in w.errors["cloud_consent"].text().lower()
    assert load_config(tmp_path / "config.json").cloud_consent_host == ""
    w.modo.consent_btn.click()
    w.modo.consent_yes.click()
    assert w.save() is True
    cfg = load_config(tmp_path / "config.json")
    assert cfg.cloud_consent is True and cfg.cloud_consent_host == "api.exemplo.com" and modos.consent_ok(cfg)


def test_janela_nao_deixa_aceitar_sem_endereco_e_o_aceite_cai_ao_trocar_de_servidor(qapp, tmp_path):
    w = janela(tmp_path)
    selecionar(w.modo.mode_combo, "prompt")
    selecionar(w.modo.provider_combo, "openai_compat")
    w.modo.consent_btn.click()  # sem URL: não há para quem aceitar
    assert w.modo.consent_box.isHidden() and "Preencha o endereço" in w.errors["cloud_base_url"].text()
    w.modo.cloud_url.setText("https://api.exemplo.com/v1")
    w.modo.consent_btn.click()
    w.modo.consent_yes.click()
    assert w.modo.values()["cloud_consent_host"] == "api.exemplo.com"
    w.modo.cloud_url.setText("https://outro.exemplo.org/v1")  # trocou de servidor na janela: o aceite cai
    v = w.modo.values()
    assert v["cloud_consent"] is False and v["cloud_consent_host"] == ""


def test_cloud_consent_host_validacao_e_padrao(tmp_path):
    from hear_ai.settings_model import SettingsForm

    assert load_config(tmp_path / "novo.json").cloud_consent_host == ""
    form = SettingsForm.from_config(cfg_de(tmp_path))
    for bom in ("", "api.exemplo.com", "127.0.0.1", "::1"):
        form.cloud_consent_host = bom
        assert "cloud_consent_host" not in form.validate(), bom
    for ruim in ("https://api.exemplo.com", "api.exemplo.com/v1", "a b", "API.EXEMPLO.COM", ".x", None, 5):
        form.cloud_consent_host = ruim
        assert "cloud_consent_host" in form.validate(), ruim
    arq = tmp_path / "ruim.json"
    arq.write_text(json.dumps({"cloud_consent_host": "http://x/ y"}), encoding="utf-8")
    assert load_config(arq).cloud_consent_host == ""  # valor inválido no arquivo volta ao padrão


# --- contexto do aplicativo (P3 tolerante) --------------------------------------------------------------------

class AppContextoFalso:
    def __init__(self, app="Code"):
        self.chamadas, self.app = 0, app

    def foreground_process_name(self, *a, **k):
        self.chamadas += 1
        return self.app

    def destino_para(self, app):
        return "agente_de_codigo" if app == "Code" else "auto"


def test_contexto_do_app_desligado_nao_consulta_o_win32(tmp_path, monkeypatch):
    espiao = AppContextoFalso()
    monkeypatch.setattr(modos, "app_context", espiao)
    cfg = cfg_de(tmp_path, operation_mode="prompt", ollama_model="qwen3:8b", generator_app_context=False)
    http = HttpFalso()
    ditar(tmp_path, cfg, http)
    assert espiao.chamadas == 0
    assert "<aplicativo_ativo>" not in "\n".join(http.mensagens_do_usuario())


def test_contexto_do_app_ligado_vai_no_pedido_com_o_destino(tmp_path, monkeypatch):
    monkeypatch.setattr(modos, "app_context", AppContextoFalso("Code"))
    cfg = cfg_de(tmp_path, operation_mode="prompt", ollama_model="qwen3:8b", generator_app_context=True)
    http = HttpFalso()
    ditar(tmp_path, cfg, http)
    msg = "\n".join(http.mensagens_do_usuario())
    assert "<aplicativo_ativo>Code</aplicativo_ativo>" in msg and "<destino>agente_de_codigo</destino>" in msg


def test_sem_o_modulo_de_contexto_o_modo_b_funciona(tmp_path, monkeypatch):
    monkeypatch.setattr(modos, "app_context", None)  # P3 ainda não integrada
    cfg = cfg_de(tmp_path, operation_mode="prompt", ollama_model="qwen3:8b", generator_app_context=True)
    http = HttpFalso()
    r = ditar(tmp_path, cfg, http)
    assert r.colador.colado == [GERADO] and "<aplicativo_ativo>" not in "\n".join(http.mensagens_do_usuario())


def test_preferencias_permanentes_so_com_a_opcao_ligada(tmp_path):
    (tmp_path / "preferencias-permanentes.md").write_text("explicação\n- Responda em português do Brasil.\n",
                                                           encoding="utf-8")
    http = HttpFalso()
    ditar(tmp_path, cfg_de(tmp_path, operation_mode="prompt", ollama_model="m"), http)
    assert "preferencias_permanentes" not in "\n".join(http.mensagens_do_usuario())
    http2 = HttpFalso()
    ditar(tmp_path, cfg_de(tmp_path, operation_mode="prompt", ollama_model="m", generator_permanent_prefs=True), http2)
    assert "Responda em português do Brasil." in "\n".join(http2.mensagens_do_usuario())


def test_prompt_da_pasta_do_config_tem_precedencia_sobre_o_do_projeto(tmp_path):
    (tmp_path / "gerador-de-prompts.md").write_text("PROMPT DO USUARIO", encoding="utf-8")
    http = HttpFalso()
    ditar(tmp_path, cfg_de(tmp_path, operation_mode="prompt", ollama_model="m"), http)
    sistemas = [m["content"] for _u, p, _h in http.chamadas if p for m in p["messages"] if m["role"] == "system"]
    assert sistemas == ["PROMPT DO USUARIO"]


# --- estado honesto do provedor --------------------------------------------------------------------------------

def test_ollama_fora_do_ar_diz_indisponivel_e_como_instalar(tmp_path):
    st = modos.provider_status(cfg_de(tmp_path), http=HttpFalso(erro=ConnectionRefusedError()))
    assert st.ok is False and st.text == "indisponível: gerador local não encontrado"
    texto = "\n".join(st.instructions)
    assert "ollama.com/download" in texto and "ollama pull" in texto and "5,2 GB" in texto and "4,9 GB" in texto


def test_ollama_encontrado_lista_os_modelos(tmp_path):
    cfg = cfg_de(tmp_path, ollama_model="qwen3:8b")
    st = modos.provider_status(cfg, http=HttpFalso(modelos=("qwen3:8b", "llama3.1:8b")))
    assert st.ok and st.models == ("qwen3:8b", "llama3.1:8b")
    assert st.text == "encontrado, modelos: qwen3:8b, llama3.1:8b; usando ‘qwen3:8b’"
    outro = modos.provider_status(cfg_de(tmp_path, ollama_model="x"), http=HttpFalso())
    assert not outro.ok and "não está instalado" in outro.text
    sem_modelo = modos.provider_status(cfg_de(tmp_path), http=HttpFalso())
    assert not sem_modelo.ok and "escolha um modelo" in sem_modelo.text
    vazio = modos.provider_status(cfg_de(tmp_path), http=HttpFalso(modelos=()))
    assert not vazio.ok and "sem nenhum modelo" in vazio.text and "ollama pull" in "\n".join(vazio.instructions)


def test_ollama_so_loopback_nunca_consulta_outro_host(tmp_path):
    http = HttpFalso()
    st = modos.provider_status(cfg_de(tmp_path, ollama_url="http://192.168.0.5:11434"), http=http)
    assert not st.ok and "inválida" in st.text and http.chamadas == []
    assert modos.build_provider(cfg_de(tmp_path, ollama_url="http://exemplo.com:11434"), http=http) is None


def test_estado_da_nuvem_vem_so_do_config_sem_rede(tmp_path, monkeypatch):
    monkeypatch.delenv("HEARAI_TESTE_CHAVE", raising=False)
    http = HttpFalso(nuvem=True)
    base = dict(generator_provider="openai_compat")
    s1 = modos.provider_status(cfg_de(tmp_path, **base), http=http)
    assert not s1.ok and "faltam URL, modelo, nome da variável da chave" in s1.text
    cfg = cfg_de(tmp_path, cloud_base_url="https://api.exemplo.com/v1", cloud_model="m",
                 cloud_key_env="HEARAI_TESTE_CHAVE", **base)
    assert "não está definida" in modos.provider_status(cfg, http=http).text
    monkeypatch.setenv("HEARAI_TESTE_CHAVE", CHAVE)
    assert "aceite" in modos.provider_status(cfg, http=http).text
    cfg.cloud_consent, cfg.cloud_consent_host = True, "api.exemplo.com"
    s4 = modos.provider_status(cfg, http=http)
    assert s4.ok and s4.na_nuvem and "api.exemplo.com" in s4.text and CHAVE not in s4.text
    assert http.chamadas == []  # o estado da nuvem nunca "testa" a conexão


# --- atalho do modo --------------------------------------------------------------------------------------------

class ApiFalsa:
    def __init__(self, register_ok=True, err=ERROR_HOTKEY_ALREADY_REGISTERED):
        self.register_ok, self.err = register_ok, err
        self.msgs: queue.Queue = queue.Queue()
        self.register_calls, self.unregistered = [], []

    def thread_id(self):
        return 4242

    def ensure_queue(self):
        pass

    def register(self, hk_id, mods, vk):
        self.register_calls.append((hk_id, mods, vk))
        return (True, 0) if self.register_ok else (False, self.err)

    def unregister(self, hk_id):
        self.unregistered.append(hk_id)

    def get_message(self):
        return self.msgs.get()

    def post_quit(self, tid):
        self.msgs.put(None)

    def is_down(self, vk):
        return False


def test_atalho_do_modo_registra_e_alterna(tmp_path):
    api, vezes = ApiFalsa(), []
    hk = modos.ModeHotkey("ctrl+alt+m", lambda: vezes.append(1), main_hotkey="ctrl+alt+h", api=api)
    assert hk.install() is True and api.register_calls[0][0] == modos.ModeHotkey.HOTKEY_ID
    assert modos.ModeHotkey.HOTKEY_ID != 0x4841  # id próprio, diferente do atalho do ditado
    api.msgs.put((Win32HotkeyApi.WM_HOTKEY, modos.ModeHotkey.HOTKEY_ID))
    assert esperar(lambda: vezes == [1])
    hk.uninstall()
    assert api.unregistered == [modos.ModeHotkey.HOTKEY_ID]


def test_atalho_do_modo_religa_com_o_novo_atalho_e_recusa_o_igual_ao_do_ditado(tmp_path):
    from hear_ai.hotkey import KEY_VK

    api = ApiFalsa()
    hk = modos.ModeHotkey("ctrl+alt+m", lambda: None, main_hotkey="ctrl+alt+h", api=api)
    assert hk.install() is True
    assert hk.rebind("ctrl+alt+j", main_hotkey="ctrl+alt+h") is True  # as Configurações trocaram o atalho
    assert hk.hotkey == "ctrl+alt+j" and len(api.register_calls) == 2
    assert api.register_calls[1][2] == KEY_VK["j"] and api.unregistered  # soltou o antigo, registrou o novo
    assert hk.rebind("ctrl+alt+h", main_hotkey="ctrl+alt+h") is False and hk.conflict  # igual ao do ditado
    assert len(api.register_calls) == 2  # e nem tentou registrar
    hk.uninstall()


def test_atalho_do_modo_igual_ao_do_ditado_nao_registra_e_avisa(tmp_path):
    api, avisos = ApiFalsa(), []
    hk = modos.ModeHotkey("Ctrl+Alt+H", lambda: None, main_hotkey="ctrl+alt+h", api=api, on_conflict=avisos.append)
    assert hk.install() is False and hk.conflict and api.register_calls == []  # nem tenta registrar
    assert avisos and "mesmo do ditado" in avisos[0]


def test_atalho_do_modo_em_uso_por_outro_programa_e_invalido(tmp_path):
    avisos = []
    hk = modos.ModeHotkey("ctrl+alt+m", lambda: None, main_hotkey="ctrl+alt+h", api=ApiFalsa(register_ok=False),
                          on_conflict=avisos.append)
    assert hk.install() is False and hk.conflict and "já está em uso" in avisos[0]
    ruim = modos.ModeHotkey("so-uma-palavra", lambda: None, api=ApiFalsa())
    assert ruim.install() is False and ruim.conflict_reason and "inválido" in ruim.conflict_reason


# --- QtUi: bandeja, atalho, bolha ------------------------------------------------------------------------------

class ControleFalso:
    def __init__(self, store):
        self.state, self.sink, self.prompt_step, self.language = "idle", store, None, "pt"

    def cancel(self):
        return False


class EscFalso:
    def register(self):
        return True, 0

    def unregister(self):
        pass


@pytest.fixture
def app(qapp, tmp_path):
    from hear_ai.tray import TrayApp
    from hear_ai.ui.qt_app import QtUi

    cfg = cfg_de(tmp_path)
    save_config(cfg, tmp_path / "config.json")
    controller = ControleFalso(HistoryStore(tmp_path / "historico"))
    tray = TrayApp(controller, cfg, on_quit=lambda: None, config_path=tmp_path / "config.json",
                   log_dir=tmp_path / "logs")
    ui = QtUi(controller, tray, level_source=lambda: 0.0, bubble_state_path=tmp_path / "bolha.json",
              esc_api=EscFalso(), show_bubble=False, watch_interval_ms=0)
    ui.provider_status_fn = _sem_ollama
    ui.tmp, ui.controle, ui.cfg = tmp_path, controller, cfg
    yield ui
    if ui._mode_thread is not None:
        ui._mode_thread.join(5)
    ui.bubble.timer.stop()
    ui.bubble.hide()
    tray._icon.qt.hide()


def itens_operacao(tray):
    return [i[0] for i in tray.menu_items() if i and ("Transcrição fiel" in i[0] or "Gerador de prompts" in i[0])]


def test_bandeja_tem_os_dois_modos_e_marca_o_atual(app):
    assert itens_operacao(app.tray) == ["✓ Transcrição fiel", "     Gerador de prompts"]
    acao = {i[0].strip(): i[1] for i in app.tray.menu_items() if i and "Gerador de prompts" in i[0]}
    acao["Gerador de prompts"]()  # clique no item da bandeja
    assert esperar(lambda: app.cfg.operation_mode == "prompt")
    assert itens_operacao(app.tray) == ["     Transcrição fiel", "✓ Gerador de prompts"]
    assert callable(app.controle.prompt_step)  # o passo do gerador foi ligado
    assert load_config(app.tmp / "config.json").operation_mode == "prompt"  # escolha explícita fica salva
    assert "gerador de prompts" in app.tray.title()
    acao_fiel = {i[0].strip(): i[1] for i in app.tray.menu_items() if i and "Transcrição fiel" in i[0]}
    acao_fiel["✓ Transcrição fiel".lstrip("✓ ")]() if False else acao_fiel["Transcrição fiel"]()
    assert esperar(lambda: app.cfg.operation_mode == "fiel")
    assert app.controle.prompt_step is None and "gerador de prompts" not in app.tray.title()


def test_atalho_do_modo_alterna_e_a_bolha_mostra_o_novo_modo(app):
    app.toggle_operation_mode()
    assert esperar(lambda: app.cfg.operation_mode == "prompt")
    f = app.bubble.frame()
    assert f.message == "Modo: Gerador de prompts" and f.badge == "prompt"  # aviso curto + selo
    assert app.bubble.model._message_end - app.bubble.model._now() <= 1.5 + 1e-6  # ~1,5 s
    app.toggle_operation_mode()
    assert esperar(lambda: app.cfg.operation_mode == "fiel")
    assert app.bubble.frame().badge is None and app.bubble.frame().message == "Modo: Transcrição fiel"


def test_ligar_o_modo_b_sem_gerador_avisa_que_vai_colar_a_transcricao(app):
    app._set_operation_mode("prompt")
    assert esperar(lambda: app.bubble.frame().message == "Gerador indisponível — vou colar a transcrição")


def test_selo_da_nuvem_so_com_consentimento(app):
    app.cfg.generator_provider = "openai_compat"
    app._set_operation_mode("prompt")
    assert app.bubble.frame().cloud is False  # sem consentimento: nada vai para a nuvem
    app.cfg.cloud_base_url, app.cfg.cloud_consent = "https://api.exemplo.com/v1", True
    app._apply_operation_visuals()
    assert app.bubble.frame().cloud is False  # aceite SEM host (config antigo): pendente, nada vai para a nuvem
    app.cfg.cloud_consent_host = "api.exemplo.com"
    app._apply_operation_visuals()
    assert app.bubble.frame().cloud is True and app.bubble.frame().badge == "prompt"


def test_estados_do_controller_chegam_na_bolha(app):
    app._on_state("generating")
    assert app.bubble.visual().value == "generating"
    app.cfg.operation_mode, app.cfg.generator_provider, app.cfg.cloud_consent = "prompt", "openai_compat", True
    app.cfg.cloud_base_url, app.cfg.cloud_consent_host = "https://api.exemplo.com/v1", "api.exemplo.com"
    app._apply_operation_visuals()
    assert app.bubble.frame().cloud  # a pílula diz "· nuvem" nesse estado
    app._on_state("generator_fallback")
    assert app.bubble.frame().message == "gerador indisponível — colei a transcrição"
    app.bubble.driver.pasted()
    assert app.bubble.visual().value == "done" and app.bubble.frame().message  # o aviso segue visível no "colado"


def test_apply_settings_religa_o_passo_quando_o_modo_muda(app):
    app.cfg.operation_mode = "prompt"
    app._apply_settings(app.cfg, {"operation_mode"}, set())
    assert callable(app.controle.prompt_step) and app.bubble.frame().badge == "prompt"
    app.cfg.operation_mode = "fiel"
    app._apply_settings(app.cfg, {"operation_mode"}, set())
    assert app.controle.prompt_step is None and app.bubble.frame().badge is None


def test_hotkey_do_modo_e_religado_quando_muda_nas_configuracoes(app):
    chamadas = []
    app.mode_hotkey = SimpleNamespace(rebind=lambda hk, main_hotkey=None: chamadas.append((hk, main_hotkey)) or True,
                                      conflict_reason=None)
    app.cfg.mode_hotkey = "ctrl+alt+j"
    app._apply_settings(app.cfg, {"mode_hotkey"}, set())
    assert esperar(lambda: chamadas == [("ctrl+alt+j", app.cfg.hotkey)])


# --- bolha pura ------------------------------------------------------------------------------------------------

def test_modelo_da_bolha_selo_gerando_e_aviso_em_repouso():
    from hear_ai.ui.bubble_model import AppState, BubbleModel, VisualState

    t = {"v": 0.0}
    m = BubbleModel(clock=lambda: t["v"])
    m.set_operation(True, cloud=True)
    assert m.frame(0.0).badge == "prompt" and m.frame(0.0).cloud is True
    m.set_operation(False, cloud=True)
    assert m.frame(0.0).badge is None and m.frame(0.0).cloud is False  # nuvem só vale com o Modo B
    m.on_state(AppState.GENERATING)
    for i in range(40):
        m.tick(i / 60)
    assert m._visual == VisualState.GENERATING and m.frame(0.7).shape_progress == 1.0  # pílula
    m.on_state(AppState.IDLE)
    m.on_notice("Modo: Gerador de prompts", 1.5)
    for i in range(1, 30):
        t["v"] = 1.0 + i / 60
        m.tick(t["v"])
    assert m.frame(t["v"]).shape_progress == 1.0 and m.frame(t["v"]).message == "Modo: Gerador de prompts"
    t["v"] = 3.0
    for i in range(30):
        t["v"] += 1 / 60
        m.tick(t["v"])
    assert m.frame(t["v"]).message is None and m.frame(t["v"]).shape_progress == 0.0  # voltou a círculo


# --- Configurações: seção Modo de operação ---------------------------------------------------------------------

def _sem_ollama(cfg):
    """O estado real desta máquina: a função de verdade, com um `http` que recusa a conexão (sem rede)."""
    return modos.provider_status(cfg, http=HttpFalso(erro=ConnectionRefusedError()))


SEM_OLLAMA = _sem_ollama


def janela(tmp_path, status=None, **kw):
    from hear_ai.ui.settings_window import SettingsWindow

    cfg = cfg_de(tmp_path)
    for k, v in kw.items():
        setattr(cfg, k, v)
    cfg_path = tmp_path / "config.json"
    save_config(cfg, cfg_path)
    chamadas = []
    w = SettingsWindow(cfg, cfg_path, startup_dir=tmp_path / "Inicializar", startup_target=tmp_path / "x.bat",
                       startup_runner=lambda *a, **k: SimpleNamespace(returncode=0), models_dir=tmp_path / "models",
                       list_mics=lambda: ["Microfone de teste"], on_applied=lambda *a: chamadas.append(a),
                       provider_status_fn=status or SEM_OLLAMA,
                       provider_async=False)
    w.resize(540, 1100)
    w._aplicados = chamadas
    return w


def selecionar(combo, data):
    combo.setCurrentIndex(combo.findData(data))


def test_configuracoes_modo_padrao_e_fiel_sem_a_parte_do_gerador(qapp, tmp_path):
    w = janela(tmp_path)
    assert w.modo.mode_combo.currentData() == "fiel" and w.modo.prompt_box.isHidden()
    assert "Nada sai do computador" in w.modo.mode_hint.text()
    assert w.save() is True and load_config(tmp_path / "config.json").operation_mode == "fiel"


def test_configuracoes_modo_b_sem_ollama_diz_indisponivel_e_avisa_da_transcricao(qapp, tmp_path):
    w = janela(tmp_path)
    selecionar(w.modo.mode_combo, "prompt")
    assert not w.modo.prompt_box.isHidden()
    assert "indisponível: gerador local não encontrado" in w.modo.status_label.text()
    assert "ollama pull" in w.modo.instructions.text() and "5,2 GB" in w.modo.instructions.text()
    assert not w.modo.warn_label.isHidden() and "cola a transcrição fiel" in w.modo.warn_label.text()
    assert w.save() is True  # escolher o Modo B sem gerador é permitido
    assert load_config(tmp_path / "config.json").operation_mode == "prompt"  # persistido
    (mudou, _), = [(a[1], a[2]) for a in w._aplicados]
    assert "operation_mode" in mudou


def test_configuracoes_ollama_encontrado_lista_modelos_e_salva_o_escolhido(qapp, tmp_path):
    st = modos.ProviderStatus("ollama", True, "encontrado, modelos: qwen3:8b, llama3.1:8b",
                              ("qwen3:8b", "llama3.1:8b"))
    w = janela(tmp_path, status=lambda c: st)
    selecionar(w.modo.mode_combo, "prompt")
    assert "encontrado, modelos: qwen3:8b" in w.modo.status_label.text() and w.modo.warn_label.isHidden()
    assert [w.modo.ollama_model.itemData(i) for i in range(w.modo.ollama_model.count())] == ["qwen3:8b", "llama3.1:8b"]
    selecionar(w.modo.ollama_model, "llama3.1:8b")
    w.modo.timeout.setValue(90)
    w.modo.app_ctx.setChecked(True)
    assert w.save() is True
    cfg = load_config(tmp_path / "config.json")
    assert (cfg.ollama_model, cfg.generator_timeout_s, cfg.generator_app_context) == ("llama3.1:8b", 90, True)


def test_configuracoes_nuvem_pede_consentimento_dentro_da_janela(qapp, tmp_path):
    w = janela(tmp_path)
    selecionar(w.modo.mode_combo, "prompt")
    selecionar(w.modo.provider_combo, "openai_compat")
    w.modo.cloud_url.setText("https://api.exemplo.com/v1")
    w.modo.cloud_model.setText("modelo-x")
    w.modo.cloud_env.setText("MINHA_CHAVE")
    assert w.save() is False  # sem aceite: não grava
    assert "aceite o envio do texto" in w.errors["cloud_consent"].text().lower()
    assert not w.errors["cloud_consent"].isHidden()
    assert load_config(tmp_path / "config.json").cloud_consent is False
    w.modo.consent_btn.click()
    assert not w.modo.consent_box.isHidden()
    assert w.modo.consent_text.text() == ("Este modo envia o TEXTO da sua fala (nunca o áudio), o nome do aplicativo "
                                          "ativo e seu vocabulário para api.exemplo.com.")
    w.modo.consent_no.click()
    assert w.save() is False and load_config(tmp_path / "config.json").cloud_consent is False  # recusou
    w.modo.consent_btn.click()
    w.modo.consent_yes.click()
    assert w.save() is True
    cfg = load_config(tmp_path / "config.json")
    assert cfg.cloud_consent is True and cfg.generator_provider == "openai_compat" and cfg.cloud_key_env == "MINHA_CHAVE"
    assert cfg.cloud_consent_host == "api.exemplo.com"  # o aceite fica amarrado ao host aceito
    # trocar o provedor (host) invalida o aceite: pergunta de novo
    w.modo.cloud_url.setText("https://outro.exemplo.org/v1")
    assert w.save() is False and "aceite" in w.errors["cloud_consent"].text().lower()


def test_configuracoes_estado_da_nuvem_acompanha_o_que_foi_digitado(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("HEARAI_TESTE_CHAVE", CHAVE)
    w = janela(tmp_path)
    selecionar(w.modo.mode_combo, "prompt")
    selecionar(w.modo.provider_combo, "openai_compat")
    assert "faltam URL, modelo" in w.modo.status_label.text()
    w.modo.cloud_url.setText("https://api.exemplo.com/v1")
    w.modo.cloud_model.setText("modelo-x")
    w.modo.cloud_env.setText("HEARAI_TESTE_CHAVE")
    assert esperar(lambda: "faltam" not in w.modo.status_label.text())  # reverifica sozinho depois de digitar
    assert "aceite" in w.modo.status_label.text()  # só falta o aceite
    w.modo.consent_btn.click()
    w.modo.consent_yes.click()
    assert "configurado (nuvem: api.exemplo.com)" in w.modo.status_label.text() and CHAVE not in w.modo.status_label.text()


def test_configuracoes_nuvem_escolhida_mas_modo_a_nao_exige_consentimento(qapp, tmp_path):
    w = janela(tmp_path)
    selecionar(w.modo.provider_combo, "openai_compat")  # modo continua fiel
    assert w.save() is True and load_config(tmp_path / "config.json").cloud_consent is False


def test_configuracoes_valida_atalho_do_modo_e_nome_da_variavel(qapp, tmp_path):
    w = janela(tmp_path)
    w.modo.hotkey.setText("so-uma-palavra")
    assert w.save() is False and not w.errors["mode_hotkey"].isHidden()
    w.modo.hotkey.setText("ctrl+alt+m")
    selecionar(w.modo.provider_combo, "openai_compat")
    w.modo.cloud_env.setText("sk-chave-de-verdade!")  # alguém colou a chave no lugar do NOME
    assert w.save() is False and "apenas letras" in w.errors["cloud_key_env"].text()


def test_configuracoes_o_conteudo_do_modo_cabe_na_largura_sem_corte_a_direita(qapp, tmp_path):
    """Regressão vista na imagem: caixas de seleção com texto longo não quebram linha e empurravam o cartão para
    além da janela (o QScrollArea não rola na horizontal), cortando o lado direito de tudo."""
    from PySide6.QtWidgets import QScrollArea

    w = janela(tmp_path)
    w.resize(540, 1100)
    selecionar(w.modo.mode_combo, "prompt")
    w.show()
    for nome in ("ollama", "openai_compat"):  # as duas variantes do bloco do gerador
        selecionar(w.modo.provider_combo, nome)
        w.modo.consent_box.show()
        qapp.processEvents()
        scroll = w.findChildren(QScrollArea)[0]
        assert scroll.widget().minimumSizeHint().width() <= scroll.viewport().width(), nome
        assert not scroll.horizontalScrollBar().isVisible()
    w.close()


def test_configuracoes_acompanham_o_modo_trocado_pela_bandeja(qapp, tmp_path):
    w = janela(tmp_path)
    w.cfg.operation_mode = "prompt"
    w.refresh_operation_mode()
    assert w.modo.mode_combo.currentData() == "prompt" and not w.modo.prompt_box.isHidden()


# --- Recentes ---------------------------------------------------------------------------------------------------

class Clip:
    def __init__(self):
        self.texto = None

    def set(self, t):
        self.texto = t


def test_recentes_usar_original_ou_gerado_so_copia_e_marca_revisar(qapp, tmp_path):
    from hear_ai.ui.recent_window import RecentWindow

    store = HistoryStore(tmp_path / "historico")
    store.save(GERADO, {"duration_s": 5.0, "language": "pt", "status": "colado", "raw": FALA, "generated": GERADO,
                        "revisar": True})
    store.save("só a fala", {"duration_s": 2.0, "language": "pt", "status": "colado"})
    clip = Clip()
    w = RecentWindow(store, clipboard=clip)
    por_texto = {r.entry.text: r for r in w.rows()}  # (a lista mostra a mais recente primeiro)
    modo_b, comum = por_texto[GERADO], por_texto["só a fala"]
    assert modo_b.entry.generated == GERADO and comum.entry.generated is None
    assert modo_b.revisar is not None and "revisar" in modo_b.revisar.text()
    assert comum.original_btn is None and comum.generated_btn is None and comum.revisar is None
    modo_b.original_btn.click()
    assert clip.texto == FALA and "Original copiado" in w.footer.text()  # a transcrição fiel, na área de transferência
    modo_b.generated_btn.click()
    assert clip.texto == GERADO and "Prompt gerado copiado" in w.footer.text()


def test_recentes_sem_original_guardado_desabilita_so_o_botao_do_original(qapp, tmp_path):
    from hear_ai.ui.recent_window import RecentWindow

    store = HistoryStore(tmp_path / "historico")
    store.save(GERADO, {"duration_s": 5.0, "language": "pt", "status": "colado", "generated": GERADO})
    row = RecentWindow(store, clipboard=Clip()).rows()[0]
    assert not row.original_btn.isEnabled() and row.generated_btn.isEnabled() and row.revisar is None


# --- __main__ ---------------------------------------------------------------------------------------------------

def test_main_monta_o_passo_so_no_modo_b_e_instala_o_atalho_do_modo():
    import inspect

    from hear_ai import __main__ as principal

    fonte = inspect.getsource(principal.main)
    assert "if is_prompt_mode(cfg)" in fonte and "prompt_step=prompt_step" in fonte
    assert fonte.index("HotkeyBinder(") < fonte.index("ModeHotkey(") < fonte.index("mode_binder.install()")
    assert "mode_binder.uninstall()" in fonte and "ui.mode_hotkey = mode_binder" in fonte


# --- evidência visual ----------------------------------------------------------------------------------------------

def _render(w, caminho: Path, escala: int = 2) -> None:
    from PySide6.QtCore import QCoreApplication, QPoint
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QScrollArea

    w.show()
    for _ in range(5):
        QCoreApplication.processEvents()
    for sc in w.findChildren(QScrollArea):
        sc.verticalScrollBar().setValue(0)
    QCoreApplication.processEvents()
    pm = QPixmap(w.width() * escala, w.height() * escala)
    pm.setDevicePixelRatio(escala)
    w.render(pm, QPoint(0, 0))
    assert pm.save(str(caminho))


def _bolha_png(cenas, caminho: Path) -> None:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QColor, QPainter, QPixmap

    from hear_ai.ui.bubble import WIN_H, WIN_W, BubbleWidget

    class Rel:
        t = 100.0

        def __call__(self):
            return self.t

    esc = 2
    folha = QPixmap(WIN_W * esc * 2, WIN_H * esc * ((len(cenas) + 1) // 2))
    folha.fill(QColor("#2B2F36"))
    pintor = QPainter(folha)
    for i, (setup, segundos) in enumerate(cenas):
        rel = Rel()
        w = BubbleWidget(clock=rel)
        setup(w)
        for _ in range(int(segundos * 60)):
            rel.t += 1 / 60
            w._tick()
        w.timer.stop()
        pm = QPixmap(WIN_W * esc, WIN_H * esc)
        pm.setDevicePixelRatio(esc)
        pm.fill(Qt.transparent)
        w.render(pm, QPoint(0, 0))
        pintor.drawPixmap((i % 2) * WIN_W * esc, (i // 2) * WIN_H * esc, pm)
        w.deleteLater()
    pintor.end()
    assert folha.save(str(caminho))


def gerar_evidencias(pasta) -> dict[str, Path]:
    import tempfile

    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    saidas = {}
    with tempfile.TemporaryDirectory(prefix="hear-ai-modos-") as tmp_s:
        tmp = Path(tmp_s)
        w = janela(tmp)
        selecionar(w.modo.mode_combo, "prompt")
        _render(w, pasta / "modos-config.png")
        saidas["modos-config.png"] = pasta / "modos-config.png"
        selecionar(w.modo.provider_combo, "openai_compat")
        w.modo.cloud_url.setText("https://api.exemplo.com/v1")
        w.modo.cloud_model.setText("modelo-x")
        w.modo.cloud_env.setText("MINHA_CHAVE")
        w.modo.refresh_status()  # (na janela real isso acontece sozinho, ~0,4 s depois de digitar)
        w.modo.consent_btn.click()
        _render(w, pasta / "modos-config-nuvem.png")
        saidas["modos-config-nuvem.png"] = pasta / "modos-config-nuvem.png"
        w.close()

        from hear_ai.ui.recent_window import RecentWindow

        store = HistoryStore(tmp / "historico")
        store.save(GERADO, {"duration_s": 5.0, "language": "pt", "status": "colado", "raw": FALA,
                            "generated": GERADO, "revisar": True})
        store.save("Ligar para o João amanhã às nove.", {"duration_s": 3.0, "language": "pt", "status": "colado"})
        rw = RecentWindow(store, clipboard=Clip(), on_toggle_pause=lambda: None)
        rw.resize(480, 520)
        _render(rw, pasta / "modos-recentes.png")
        saidas["modos-recentes.png"] = pasta / "modos-recentes.png"
        rw.close()

    def idle_a(w): w.driver.loading(False); w.driver.state("idle")  # noqa: E702
    def idle_b(w): w.driver.loading(False); w.driver.state("idle"); w.driver.set_operation(True)  # noqa: E702
    def rec_b(w): w.driver.set_operation(True); w.driver.state("recording")  # noqa: E702
    def idle_nuvem(w): w.driver.loading(False); w.driver.state("idle"); w.driver.set_operation(True, True)  # noqa: E702
    _bolha_png([(idle_a, 0.5), (idle_b, 0.5), (idle_nuvem, 0.5), (rec_b, 1.0)], pasta / "bolha-modo-b.png")
    saidas["bolha-modo-b.png"] = pasta / "bolha-modo-b.png"

    def gerando(w): w.driver.set_operation(True); w.driver.state("recording"); w.driver.state("transcribing"); w.driver.state("generating")  # noqa: E702,E501
    def gerando_nuvem(w): w.driver.set_operation(True, True); w.driver.state("generating")  # noqa: E702
    def fallback(w): w.driver.set_operation(True); w.driver.state("generating"); w.driver.state("generator_fallback"); w.driver.pasted()  # noqa: E702,E501
    def aviso(w): w.driver.loading(False); w.driver.state("idle"); w.driver.notice("Modo: Gerador de prompts")  # noqa: E702
    _bolha_png([(gerando, 0.8), (gerando_nuvem, 0.8), (fallback, 0.5), (aviso, 0.4)], pasta / "bolha-gerando.png")
    saidas["bolha-gerando.png"] = pasta / "bolha-gerando.png"
    return saidas


def test_evidencias_sao_geradas_e_diferentes(qapp, tmp_path):
    saidas = gerar_evidencias(tmp_path)
    assert set(saidas) == {"modos-config.png", "modos-config-nuvem.png", "modos-recentes.png", "bolha-modo-b.png",
                           "bolha-gerando.png"}
    assert all(p.stat().st_size > 4000 for p in saidas.values())
    assert len({p.read_bytes() for p in saidas.values()}) == len(saidas)


if __name__ == "__main__":
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv[:1])
    destino = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "evidencias"
    for caminho in gerar_evidencias(destino).values():
        print(caminho)
