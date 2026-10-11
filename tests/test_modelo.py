"""T46 — modelo de voz `medium` funcional: lista, download (só com clique), troca sem reiniciar, fallback.

ZERO rede e zero processo real: um guarda automático (abaixo) faz qualquer `socket.connect` ou `subprocess.Popen`
sem injeção falhar o teste. Modelos, config, histórico e `models/` são sempre de `tmp_path` (nunca o `models/`
real, nunca o `config.json` do usuário). Fábricas de modelo e downloaders são falsos; o único processo filho real
é um `python -c` LOCAL que nós mesmos abrimos (prova do stderr cheio e do cancelar), nunca o download de verdade.

Imagens de evidência:  .venv\\Scripts\\python.exe tests\\test_modelo.py [pasta]   (padrão: evidencias)
"""

from __future__ import annotations

import gc
import io
import os
import socket
import subprocess
import sys
import threading
import time
import weakref
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

from hear_ai import model_download  # noqa: E402
from hear_ai.config import load_config, save_config  # noqa: E402
from hear_ai.model_download import ModelDownloader, friendly_error, resolve_startup_model  # noqa: E402
from hear_ai.settings_model import (SettingsForm, describe_model_failure, model_label,  # noqa: E402
                                    model_options)
from hear_ai.transcriber import ModelNotFoundError, Transcriber  # noqa: E402

REAL_POPEN = subprocess.Popen  # só para o `python -c` local dos testes do processo filho


@pytest.fixture(autouse=True)
def sem_rede_e_sem_processo_real(monkeypatch):
    """Guarda: nenhum teste deste arquivo pode usar a internet nem abrir processo que não tenha sido injetado."""
    def boom(*a, **k):
        raise AssertionError("teste de modelo tentou usar a rede / abrir um processo real")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


_DOWNLOADERS: list = []  # downloaders reais (com thread) criados pelos testes: são encerrados e juntados no fim


@pytest.fixture(autouse=True)
def limpar_qt_e_threads(qapp):
    """Ao fim de CADA teste: encerra os downloads com thread e fecha/apaga todas as janelas que o teste criou.
    (Janelas que sobram até o fim da sessão são destruídas em ordem arbitrária na saída do interpretador e podem
    derrubar o processo com uma violação de acesso: a suíte inteira chega a deixar centenas delas vivas.)"""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    antes = {id(w) for w in QApplication.topLevelWidgets()}
    yield
    for dl in _DOWNLOADERS:
        dl.cancel()
        dl.join(5)
    _DOWNLOADERS.clear()
    for w in list(QApplication.topLevelWidgets()):
        if id(w) not in antes:
            w.close()
            w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.processEvents()


@pytest.fixture(autouse=True)
def pasta_inicializar_falsa(tmp_path, monkeypatch):
    """A janela de Configurações nunca chega perto da pasta Inicializar real."""
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


# --- fakes ---------------------------------------------------------------------------------------------------

def criar_modelo(models: Path, nome: str) -> Path:
    d = models / f"faster-whisper-{nome}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.bin").write_bytes(b"x")
    (d / "config.json").write_text("{}")
    (d / "tokenizer.json").write_text("{}")
    return d


class Seg:
    def __init__(self, text):
        self.text = text


class ModeloFalso:
    def __init__(self, path, device, compute):
        self.path, self.device, self.compute = path, device, compute
        self.nome = Path(path).name.removeprefix("faster-whisper-")
        self.gate = None  # Event: segura a transcrição (para testar "espera o bloco em andamento")

    def transcribe(self, audio, **opts):
        if self.gate is not None:
            self.gate.wait(5)
        return iter([Seg(f"texto {self.nome}")]), None


class Fabrica:
    """`model_factory` do Transcriber: registra cada carga, pode falhar ou travar por modelo/dispositivo."""

    def __init__(self):
        self.chamadas: list[tuple[str, str, str]] = []
        self.falhas: dict = {}  # (nome, "cuda"|"cpu"|"*") -> exceção
        self.travar: dict = {}  # nome -> Event (a carga espera até ser liberada)
        self.criados: list = []

    def __call__(self, path, device, compute):
        nome = Path(path).name.removeprefix("faster-whisper-")
        self.chamadas.append((nome, device, compute))
        gate = self.travar.get(nome)
        if gate is not None:
            gate.wait(5)
        exc = self.falhas.get((nome, device)) or self.falhas.get((nome, "*"))
        if exc is not None:
            raise exc
        modelo = ModeloFalso(path, device, compute)
        self.criados.append(weakref.ref(modelo))
        return modelo


def transcritor(models: Path, fab: Fabrica, nome="small", cuda=0, device="auto") -> Transcriber:
    t = Transcriber(nome, models, device=device, compute_type="int8", language="pt", model_factory=fab,
                    cuda_count=lambda: cuda)
    t.load()
    return t


class ProcFalso:
    """Processo filho falso do download: termina sozinho (`ret`) ou só quando recebe `terminate()`."""

    def __init__(self, ret=None, stderr=b"", cria: Path | None = None):
        self._ret = ret
        self.returncode = None
        self.terminated = False
        self.stderr = io.BytesIO(stderr)
        self.cria = cria
        self._polls = 0

    def poll(self):
        if self.terminated:
            return self.returncode
        if self._ret is None:
            return None
        self._polls += 1
        if self._polls < 3:
            return None
        self.returncode = self._ret
        if self.cria is not None and self._ret == 0:
            criar_modelo(self.cria.parent, self.cria.name.removeprefix("faster-whisper-"))
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.terminate()


class FabricaPopen:
    def __init__(self, *procs):
        self.procs = list(procs)
        self.chamadas = []

    def __call__(self, args, **kw):
        self.chamadas.append((args, kw))
        return self.procs.pop(0)


# --- pura: lista, rótulos, validação, textos ----------------------------------------------------------------

def test_lista_pura_small_e_medium_sempre_e_mais_os_baixados():
    nomes = lambda opts: [o.name for o in opts]  # noqa: E731
    assert nomes(model_options([], "small")) == ["small", "medium"]
    assert nomes(model_options(["small", "base"], "small")) == ["base", "small", "medium"]
    opts = {o.name: o for o in model_options(["small"], "small")}
    assert opts["small"].downloaded and opts["small"].label == "small"
    assert not opts["medium"].downloaded and opts["medium"].label == "medium — não baixado (~1,5 GB)"
    # o modelo do config que sumiu do disco continua na lista, marcado como não baixado
    assert [o.name for o in model_options(["small"], "large-v3")] == ["small", "medium", "large-v3"]
    assert model_label("large-v3", False) == "large-v3 — não baixado (~3 GB)"
    assert model_label("foo", False) == "foo — não baixado"


def test_validacao_recusa_escolher_modelo_nao_baixado_mas_aceita_manter_o_atual(tmp_path):
    cfg = load_config(tmp_path / "config.json")
    form = SettingsForm.from_config(cfg)
    form.model = "medium"
    assert "model" in form.validate(downloaded={"small"}, current_model="small")
    assert "model" not in form.validate(downloaded={"small", "medium"}, current_model="small")
    assert "model" not in form.validate(downloaded={"small"}, current_model="medium")  # manter o atual ausente
    assert "model" not in form.validate()  # sem informação de disco: comportamento antigo


def test_descricao_amigavel_das_falhas_de_carga():
    assert "arquivo do modelo" in describe_model_failure(ModelNotFoundError("x"))
    assert describe_model_failure(RuntimeError("CUDA out of memory")) == "faltou memória"
    assert describe_model_failure(MemoryError()) == "faltou memória"
    assert "placa de vídeo" in describe_model_failure(RuntimeError("cublas64_12.dll not loaded"))
    assert "corrompido" in describe_model_failure(ValueError("unexpected end of file"))


# --- Configurações: lista, escolher não baixado, download ------------------------------------------------------

@pytest.fixture
def amb(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg = load_config(cfg_path)
    models = tmp_path / "models"
    criar_modelo(models, "small")
    cfg.models_dir = str(models)
    save_config(cfg, cfg_path)
    return SimpleNamespace(tmp=tmp_path, cfg=cfg, cfg_path=cfg_path, models=models)


def janela(amb, factory=None, **kw):
    from hear_ai.ui.settings_window import SettingsWindow

    kw.setdefault("startup_dir", amb.tmp / "Inicializar")
    kw.setdefault("startup_target", amb.tmp / "iniciar.bat")
    kw.setdefault("startup_runner", lambda *a, **k: SimpleNamespace(returncode=0))
    kw.setdefault("models_dir", amb.models)
    kw.setdefault("list_mics", lambda: ["Microfone de teste"])
    w = SettingsWindow(amb.cfg, amb.cfg_path, downloader_factory=factory, **kw)
    w.resize(540, 900)
    return w


def selecionar(w, nome):
    w.model_combo.setCurrentIndex(w.model_combo.findData(nome))


def test_configuracoes_lista_small_e_medium_com_rotulo_do_nao_baixado(qapp, amb):
    w = janela(amb)
    assert [w.model_combo.itemData(i) for i in range(w.model_combo.count())] == ["small", "medium"]
    assert [w.model_combo.itemText(i) for i in range(w.model_combo.count())] == [
        "small", "medium — não baixado (~1,5 GB)"]
    criar_modelo(amb.models, "base")  # outro já baixado entra na lista ao reabrir
    w.load_from_config()
    assert [w.model_combo.itemData(i) for i in range(w.model_combo.count())] == ["base", "small", "medium"]


def test_escolher_nao_baixado_nao_grava_o_config_e_mostra_a_explicacao(qapp, amb):
    aplicados = []
    w = janela(amb, on_applied=lambda *a: aplicados.append(a))
    antes = amb.cfg_path.read_bytes()
    selecionar(w, "medium")
    assert not w.model_box.isHidden()
    assert "ainda não foi baixado" in w.model_info.text() and "~1,5 GB" in w.model_info.text()
    assert w.model_download_btn.text() == "Baixar agora (~1,5 GB)" and not w.model_download_btn.isHidden()
    assert w.save() is False
    assert amb.cfg_path.read_bytes() == antes and amb.cfg.model == "small" and aplicados == []
    assert "ainda não foi baixado" in w.errors["model"].text() and not w.errors["model"].isHidden()
    assert w.status.text() == "Corrija os campos marcados em vermelho."


def test_texto_do_medium_e_honesto_nao_promete_velocidade(qapp, amb):
    w = janela(amb)
    selecionar(w, "medium")
    dica = w.model_hint.text()
    assert not w.model_hint.isHidden()
    assert "tende a errar menos" in dica and "mais lento" in dica and "memória da placa de vídeo" in dica
    assert "mais rápido" not in dica and "rápido" not in dica
    selecionar(w, "small")
    assert w.model_hint.isHidden() and w.model_box.isHidden()  # small baixado: nada a baixar


def test_manter_o_modelo_atual_ausente_nao_impede_salvar_outras_configuracoes(qapp, amb):
    amb.cfg.model = "medium"  # config aponta para um medium que não está no disco
    save_config(amb.cfg, amb.cfg_path)
    w = janela(amb)
    assert w.model_combo.currentData() == "medium" and "não baixado" in w.model_combo.currentText()
    w.opacity_slider.setValue(60)
    assert w.save() is True
    assert amb.cfg.model == "medium" and abs(amb.cfg.bubble_idle_opacity - 0.6) < 1e-9


class DownloaderDeMentira:
    """downloader_factory falso: grava `model.bin` ao concluir (ou falha/cancela) e conta as chamadas."""

    def __init__(self, models_dir, name, *, on_progress, on_done, on_error, on_cancelled, resultado="ok"):
        self.models_dir, self.name = Path(models_dir), name
        self.cb = SimpleNamespace(progress=on_progress, done=on_done, error=on_error, cancelled=on_cancelled)
        self.resultado = resultado
        self.running = False
        self.cancelado = False

    def start(self):
        self.running = True
        self.cb.progress(0.37, 566_000_000)
        if self.resultado == "ok":
            criar_modelo(self.models_dir, self.name)
            self.cb.done()
        elif self.resultado == "erro":
            self.cb.error("Sem conexão com a internet. Confira a rede e clique em “Tentar de novo”.")
        return True

    def cancel(self):
        self.cancelado = True
        self.cb.cancelled()


def test_baixar_pede_confirmacao_antes_de_usar_a_internet(qapp, amb):
    fabricas = []

    def fabrica(*a, **kw):
        fabricas.append((a, kw))
        return DownloaderDeMentira(*a, **kw)

    w = janela(amb, fabrica)
    selecionar(w, "medium")
    w.model_download_btn.click()
    assert fabricas == []  # só pediu confirmação
    assert "Isto vai baixar ~1,5 GB da internet. Continuar?" == w.model_confirm_label.text()
    assert not w.model_confirm.isHidden() and w.model_download_btn.isHidden()
    w.model_confirm_no.click()
    assert fabricas == [] and not w.model_download_btn.isHidden() and w.model_confirm.isHidden()
    w.model_download_btn.click()
    w.model_confirm_yes.click()
    assert len(fabricas) == 1 and fabricas[0][0][1] == "medium"  # agora sim, e uma vez só


def test_download_concluido_habilita_o_modelo_e_ele_pode_ser_aplicado(qapp, amb):
    aplicados = []
    w = janela(amb, DownloaderDeMentira, on_applied=lambda cfg, mudou, reiniciar: aplicados.append((mudou, reiniciar)))
    selecionar(w, "medium")
    w.model_download_btn.click()
    w.model_confirm_yes.click()
    assert esperar(lambda: w.model_box.isHidden())  # concluiu: nada mais a baixar
    assert w.model_combo.itemText(w.model_combo.findData("medium")) == "medium"
    assert w.model_combo.currentData() == "medium"
    assert "baixado" in w.status.text() and "Salvar" in w.status.text()
    assert w.save() is True
    assert amb.cfg.model == "medium" and '"model": "medium"' in amb.cfg_path.read_text(encoding="utf-8")
    (mudou, reiniciar), = aplicados
    assert "model" in mudou and "model" not in reiniciar  # troca na hora, sem pedir para reiniciar
    assert "Reinicie" not in w.status.text() and "modelo" in w.status.text()


def test_progresso_do_download_aparece_na_barra(qapp, amb):
    w = janela(amb, lambda *a, **kw: DownloaderDeMentira(*a, resultado="progresso", **kw))
    selecionar(w, "medium")
    w.model_download_btn.click()
    w.model_confirm_yes.click()
    assert esperar(lambda: w.model_progress.value() == 370)
    assert not w.model_progress_box.isHidden() and not w.model_cancel_btn.isHidden()
    assert "37%" in w.model_progress_label.text() and "566 MB" in w.model_progress_label.text()
    assert w.model_info.isHidden() and w.model_download_btn.isHidden()


def _fabrica_com_popen(popen, **extra):
    def fabrica(models_dir, name, **cb):
        dl = ModelDownloader(models_dir, name, popen=popen, poll_s=0.005, disk_free=lambda p: 10 ** 13,
                             **extra, **cb)
        _DOWNLOADERS.append(dl)
        return dl
    return fabrica


def test_cancelar_o_download_deixa_estado_coerente_e_permite_tentar_de_novo(qapp, amb):
    proc1, proc2 = ProcFalso(ret=None), ProcFalso(ret=None)
    popen = FabricaPopen(proc1, proc2)
    w = janela(amb, _fabrica_com_popen(popen))
    selecionar(w, "medium")
    w.model_download_btn.click()
    w.model_confirm_yes.click()
    assert esperar(lambda: len(popen.chamadas) == 1)
    w.model_cancel_btn.click()
    assert esperar(lambda: not w.model_retry_btn.isHidden())
    assert proc1.terminated  # encerrou o processo que nós abrimos
    assert "cancelado" in w.model_error.text().lower() and "Tentar de novo" in w.model_error.text()
    assert not (amb.models / "faster-whisper-medium" / "model.bin").exists()
    assert w.save() is False and "ainda não foi baixado" in w.errors["model"].text()  # nada de modelo "fingindo" pronto
    w.model_retry_btn.click()  # tentar de novo continua de onde parou
    assert esperar(lambda: len(popen.chamadas) == 2)
    w.model_cancel_btn.click()
    assert esperar(lambda: proc2.terminated)


def test_erro_de_rede_vira_frase_simples_e_estado_coerente(qapp, amb):
    proc = ProcFalso(ret=1, stderr=b"requests.exceptions.ConnectionError: getaddrinfo failed")
    w = janela(amb, _fabrica_com_popen(FabricaPopen(proc)))
    selecionar(w, "medium")
    w.model_download_btn.click()
    w.model_confirm_yes.click()
    assert esperar(lambda: not w.model_retry_btn.isHidden())
    assert w.model_error.text() == "Sem conexão com a internet. Confira a rede e clique em “Tentar de novo”."
    assert "Traceback" not in w.model_error.text() and "getaddrinfo" not in w.model_error.text()
    assert w.model_progress_box.isHidden() and not (amb.models / "faster-whisper-medium" / "model.bin").exists()
    assert w.save() is False


def test_sem_espaco_em_disco_nem_chega_a_baixar(qapp, amb):
    popen = FabricaPopen(ProcFalso(ret=0))
    def fabrica(d, n, **cb):
        dl = ModelDownloader(d, n, popen=popen, poll_s=0.005, disk_free=lambda p: 100_000_000, **cb)
        _DOWNLOADERS.append(dl)
        return dl
    w = janela(amb, fabrica)
    selecionar(w, "medium")
    w.model_download_btn.click()
    w.model_confirm_yes.click()
    assert esperar(lambda: not w.model_retry_btn.isHidden())
    assert "Sem espaço em disco" in w.model_error.text() and "1,5 GB" in w.model_error.text()
    assert popen.chamadas == []  # nenhum processo foi aberto


def test_sair_do_app_com_download_em_andamento_cancela_o_filho(qapp, amb):
    criados = []

    def fabrica(*a, **kw):
        criados.append(DownloaderDeMentira(*a, resultado="progresso", **kw))
        return criados[-1]

    w = janela(amb, fabrica)
    selecionar(w, "medium")
    w.model_download_btn.click()
    w.model_confirm_yes.click()
    assert esperar(lambda: w._dl_phase == "downloading")
    w._cancel_download_on_quit()  # é o que o aboutToQuit do app chama
    assert criados[0].cancelado  # o processo que nós abrimos é encerrado: nada fica baixando escondido
    w2 = janela(amb, fabrica)  # sem download em andamento: não faz nada
    w2._cancel_download_on_quit()
    assert len(criados) == 1


def test_download_so_comeca_com_clique_ninguem_chama_start_sozinho(qapp, amb):
    iniciou = []
    w = janela(amb, lambda *a, **kw: iniciou.append(1))
    selecionar(w, "medium")
    w.refresh_model_choices()
    w.load_from_config()
    w.show()
    qapp.processEvents()
    assert iniciou == []  # abrir, trocar a seleção e reabrir não baixam nada
    w.close()


# --- o módulo de download (sem a janela) --------------------------------------------------------------------------

def test_download_conclui_com_processo_falso_e_desliga_as_barras_de_progresso(tmp_path):
    models = tmp_path / "models"
    popen = FabricaPopen(ProcFalso(ret=0, cria=models / "faster-whisper-medium"))
    eventos = []
    dl = ModelDownloader(models, "medium", popen=popen, poll_s=0.005, disk_free=lambda p: 10 ** 13,
                         on_progress=lambda f, b: eventos.append(("p", f)), on_done=lambda: eventos.append("ok"),
                         on_error=lambda m: eventos.append(("erro", m)))
    assert dl.start() is True
    dl.join(5)
    assert eventos[-1] == "ok" and model_download.model_ready(models, "medium")
    args, kw = popen.chamadas[0]
    assert kw["env"]["HF_HUB_DISABLE_PROGRESS_BARS"] == "1" and kw["env"]["HEARAI_REPO"] == \
        "Systran/faster-whisper-medium"
    assert "snapshot_download" in args[-1] and args[0] == sys.executable


def test_filho_real_local_com_stderr_gigante_nao_trava(tmp_path):
    """O huggingface_hub escreve barras de progresso no stderr; sem drenar, o pipe enche (~64 KB) e o download
    trava. Filho REAL local (nunca o download de verdade) que escreve 600 KB no stderr antes de terminar."""
    models = tmp_path / "models"
    destino = models / "faster-whisper-medium"
    script = (
        "import sys, pathlib\n"
        "sys.stderr.write('x' * 600_000)\n"
        "sys.stderr.flush()\n"
        f"d = pathlib.Path({str(destino)!r}); d.mkdir(parents=True, exist_ok=True)\n"
        "(d / 'model.bin').write_bytes(b'ok')\n"
    )

    def popen_local(args, **kw):
        return REAL_POPEN([sys.executable, "-c", script], **{k: v for k, v in kw.items() if k != "creationflags"})

    resultado = []
    dl = ModelDownloader(models, "medium", popen=popen_local, poll_s=0.01, disk_free=lambda p: 10 ** 13,
                         on_done=lambda: resultado.append("ok"), on_error=lambda m: resultado.append(m))
    dl.start()
    dl.join(15)
    if dl.running:  # travou: encerra o filho que nós abrimos para não deixar processo para trás
        dl.cancel()
        dl.join(5)
    assert resultado == ["ok"], resultado


def test_cancelar_encerra_so_o_filho_que_nos_abrimos(tmp_path):
    script = "import time\ntime.sleep(60)\n"
    procs = []

    def popen_local(args, **kw):
        p = REAL_POPEN([sys.executable, "-c", script], stderr=subprocess.PIPE)
        procs.append(p)
        return p

    cancelado = []
    dl = ModelDownloader(tmp_path / "models", "medium", popen=popen_local, poll_s=0.01,
                         disk_free=lambda p: 10 ** 13, on_cancelled=lambda: cancelado.append(1))
    dl.start()
    assert esperar(lambda: len(procs) == 1, 5)
    dl.cancel()
    dl.join(10)
    assert cancelado == [1] and procs[0].poll() is not None  # o filho foi encerrado
    assert not dl.running


def test_frases_de_erro_sao_simples_e_dependem_do_modelo():
    assert friendly_error("ConnectionError: Max retries exceeded", 1).startswith("Sem conexão com a internet")
    assert "1,5 GB" in friendly_error("OSError: [Errno 28] No space left on device", 1, "medium")
    assert "480 MB" in friendly_error("no space left", 1, "small")
    assert friendly_error("PermissionError: [Errno 13]", 1).startswith("Sem permissão")
    assert "código 7" in friendly_error("???", 7) and "Tentar de novo" in friendly_error("???", 7)


def test_o_guarda_de_rede_esta_ligado():
    with pytest.raises(AssertionError):
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(AssertionError):
        subprocess.Popen(["x"])


# --- Transcriber.reload -----------------------------------------------------------------------------------------

def test_recarga_bem_sucedida_troca_o_modelo_e_libera_o_antigo(tmp_path):
    models = tmp_path / "models"
    criar_modelo(models, "small")
    criar_modelo(models, "medium")
    fab = Fabrica()
    t = transcritor(models, fab)
    assert t.transcribe(np.zeros(16000, dtype=np.float32)) == "texto small"
    antigo = fab.criados[0]
    assert t.reload("medium") == "cpu"
    assert t.model_name == "medium" and t.device_used == "cpu"
    assert t.transcribe(np.zeros(16000, dtype=np.float32)) == "texto medium"
    assert antigo() is None  # a memória do modelo anterior foi solta (sem ciclos: basta a contagem de referências)
    t.reload("small")  # voltar ao small é a mesma operação
    assert t.model_name == "small" and t.transcribe(np.zeros(16000, dtype=np.float32)) == "texto small"
    assert t.language == "pt" and t.transcribe_options["beam_size"] == 5  # o resto da configuração não mudou


def test_recarga_que_falha_mantem_o_modelo_anterior_funcionando(tmp_path):
    models = tmp_path / "models"
    criar_modelo(models, "small")
    criar_modelo(models, "medium")
    fab = Fabrica()
    t = transcritor(models, fab)
    fab.falhas[("medium", "*")] = RuntimeError("arquivo corrompido")
    with pytest.raises(RuntimeError):
        t.reload("medium")
    assert t.model_name == "small" and t.device_used == "cpu"
    assert t.transcribe(np.zeros(16000, dtype=np.float32)) == "texto small"  # nada de estado quebrado


def test_recarga_de_modelo_ausente_levanta_e_mantem_o_anterior(tmp_path):
    models = tmp_path / "models"
    criar_modelo(models, "small")
    t = transcritor(models, Fabrica())
    with pytest.raises(ModelNotFoundError) as e:
        t.reload("medium")
    assert "medium" in str(e.value)
    assert t.model_name == "small" and t.transcribe(np.zeros(16000, dtype=np.float32)) == "texto small"


def test_erro_de_memoria_na_gpu_cai_para_cpu_na_recarga(tmp_path):
    models = tmp_path / "models"
    criar_modelo(models, "small")
    criar_modelo(models, "medium")
    fab = Fabrica()
    t = transcritor(models, fab, cuda=1)
    assert t.device_used == "cuda"
    fab.chamadas.clear()
    fab.falhas[("medium", "cuda")] = RuntimeError("CUDA failed with error out of memory")
    assert t.reload("medium") == "cpu"
    assert fab.chamadas == [("medium", "cuda", "float16"), ("medium", "cpu", "int8")]
    assert t.model_name == "medium" and t.device_used == "cpu"
    assert t.transcribe(np.zeros(16000, dtype=np.float32)) == "texto medium"


def test_o_ditado_continua_com_o_modelo_antigo_enquanto_o_novo_carrega(tmp_path):
    models = tmp_path / "models"
    criar_modelo(models, "small")
    criar_modelo(models, "medium")
    fab = Fabrica()
    t = transcritor(models, fab)
    fab.travar["medium"] = liberar = threading.Event()
    th = threading.Thread(target=t.reload, args=("medium",), daemon=True)
    th.start()
    assert esperar(lambda: ("medium", "cpu", "int8") in fab.chamadas)  # a carga do novo começou e está presa
    assert t.model_name == "small"
    assert t.transcribe(np.zeros(16000, dtype=np.float32)) == "texto small"  # ditar continua possível
    liberar.set()
    th.join(5)
    assert t.model_name == "medium"


def test_a_troca_espera_o_bloco_em_andamento_terminar(tmp_path):
    models = tmp_path / "models"
    criar_modelo(models, "small")
    criar_modelo(models, "medium")
    t = transcritor(models, Fabrica())
    t._model.gate = segurar = threading.Event()  # o bloco em andamento fica preso no modelo antigo
    textos = []
    bloco = threading.Thread(target=lambda: textos.append(t.transcribe(np.zeros(16000, dtype=np.float32))),
                             daemon=True)
    bloco.start()
    time.sleep(0.1)
    troca = threading.Thread(target=t.reload, args=("medium",), daemon=True)
    troca.start()
    time.sleep(0.15)
    assert t.model_name == "small" and troca.is_alive()  # a troca aguarda o bloco, sem atropelá-lo
    segurar.set()
    bloco.join(5)
    troca.join(5)
    assert textos == ["texto small"] and t.model_name == "medium"  # o bloco terminou com o modelo em que começou


# --- fallback na inicialização ---------------------------------------------------------------------------------------

def test_fallback_para_small_na_inicializacao_sem_tocar_no_config(tmp_path):
    models = tmp_path / "models"
    criar_modelo(models, "small")
    cfg_path = tmp_path / "config.json"
    cfg = load_config(cfg_path)
    cfg.model, cfg.models_dir = "medium", str(models)
    save_config(cfg, cfg_path)
    antes = cfg_path.read_bytes()
    assert resolve_startup_model(models, "medium") == ("small", True)
    nome, caiu = resolve_startup_model(models, load_config(cfg_path).model)
    t = Transcriber(nome, models, device="cpu", model_factory=Fabrica())
    t.load()  # o app NÃO morre
    assert caiu and t.model_name == "small" and t.transcribe(np.zeros(16000, dtype=np.float32)) == "texto small"
    assert cfg_path.read_bytes() == antes  # o config.json do usuário não foi reescrito
    assert load_config(cfg_path).model == "medium"


def test_resolver_modelo_da_inicializacao_nos_outros_casos(tmp_path):
    models = tmp_path / "models"
    assert resolve_startup_model(models, "medium") == ("medium", False)  # nada em disco: o erro de sempre segue
    criar_modelo(models, "small")
    assert resolve_startup_model(models, "small") == ("small", False)
    criar_modelo(models, "medium")
    assert resolve_startup_model(models, "medium") == ("medium", False)
    assert resolve_startup_model(models, "large-v3") == ("small", True)


def test_o_main_resolve_o_modelo_antes_de_criar_o_transcritor():
    import inspect

    from hear_ai import __main__ as principal

    fonte = inspect.getsource(principal.main)
    assert fonte.index("resolve_startup_model(") < fonte.index("Transcriber(model_name")
    assert "show_notice(" in fonte and "remember=True" in fonte


# --- QtUi: troca sem reiniciar ------------------------------------------------------------------------------------

class ControleFalso:
    def __init__(self, transcriber, store):
        self.state = "idle"
        self.transcriber = transcriber
        self.sink = store
        self.language = "pt"

    def cancel(self):
        return False


class EscFalso:
    def register(self):
        return True, 0

    def unregister(self):
        pass


@pytest.fixture
def app(qapp, amb, monkeypatch):
    from hear_ai.history import HistoryStore
    from hear_ai.tray import TrayApp
    from hear_ai.ui import qt_app
    from hear_ai.ui.qt_app import QtUi

    monkeypatch.setattr(qt_app, "MODEL_RETRY_MS", 10)
    criar_modelo(amb.models, "medium")
    fab = Fabrica()
    t = transcritor(amb.models, fab, nome="small")
    controller = ControleFalso(t, HistoryStore(amb.tmp / "historico"))
    tray = TrayApp(controller, amb.cfg, on_quit=lambda: None, config_path=amb.cfg_path, log_dir=amb.tmp / "logs")
    ui = QtUi(controller, tray, level_source=lambda: 0.0, bubble_state_path=amb.tmp / "bolha.json",
              esc_api=EscFalso(), show_bubble=False, watch_interval_ms=0)
    ui.set_loading(False)
    esperar(lambda: not ui.tray.loading, 1)
    ui.fab, ui.controle, ui.transcritor, ui.amb = fab, controller, t, amb
    yield ui
    if ui._model_thread is not None:
        ui._model_thread.join(5)
    ui.bubble.timer.stop()
    ui.bubble.hide()
    if ui.settings_window is not None:
        ui.settings_window.close()
    tray._icon.qt.hide()


def salvar_modelo(ui, nome):
    ui.show_settings()
    w = ui.settings_window
    selecionar(w, nome)
    assert w.save() is True
    return w


def test_salvar_modelo_baixado_troca_sem_reiniciar_com_carregando_na_bolha(app):
    ui = app
    app.fab.travar["medium"] = liberar = threading.Event()
    w = salvar_modelo(ui, "medium")
    assert "sendo carregado" in w.status.text() and "Reinicie" not in w.status.text()
    assert esperar(lambda: ui.tray.loading)  # carregando...
    assert ui.bubble.visual().value == "loading"
    assert ui.transcritor.model_name == "small"  # o antigo segue valendo até o novo ficar pronto
    liberar.set()
    assert esperar(lambda: ui.transcritor.model_name == "medium" and not ui.tray.loading)
    assert esperar(lambda: ui.bubble.visual().value != "loading")
    assert "Modelo ‘medium’ carregado" in w.status.text()
    assert ui.controle.transcriber is ui.transcritor  # o mesmo objeto: o fluxo de blocos/streaming não muda
    assert '"model": "medium"' in app.amb.cfg_path.read_text(encoding="utf-8")


def test_voltar_para_o_small_e_a_mesma_operacao(app):
    ui = app
    salvar_modelo(ui, "medium")
    assert esperar(lambda: ui.transcritor.model_name == "medium" and not ui._model_switching)
    salvar_modelo(ui, "small")
    assert esperar(lambda: ui.transcritor.model_name == "small" and not ui._model_switching)
    assert ui.transcritor.transcribe(np.zeros(16000, dtype=np.float32)) == "texto small"
    assert '"model": "small"' in app.amb.cfg_path.read_text(encoding="utf-8")


def test_falha_de_carga_volta_ao_modelo_anterior_e_avisa(app):
    ui = app
    ui.fab.falhas[("medium", "*")] = RuntimeError("arquivo corrompido")
    w = salvar_modelo(ui, "medium")
    assert esperar(lambda: not ui._model_switching and "Não consegui" in w.status.text())
    assert ui.transcritor.model_name == "small"
    assert "Continuo com o ‘small’" in w.status.text() and "corrompido" in w.status.text()
    assert w.status.objectName() == "error"
    assert app.amb.cfg.model == "small" and '"model": "small"' in app.amb.cfg_path.read_text(encoding="utf-8")
    assert w.model_combo.currentData() == "small"  # a janela volta ao que está valendo
    assert not ui.tray.loading and ui.bubble.visual().value != "loading"
    assert ui.transcritor.transcribe(np.zeros(16000, dtype=np.float32)) == "texto small"  # tudo funcionando
    ui.fab.falhas.clear()  # tentar de novo (ex.: a memória liberou) funciona: o config voltou atrás de propósito
    salvar_modelo(ui, "medium")
    assert esperar(lambda: ui.transcritor.model_name == "medium")


def test_troca_recusada_durante_a_gravacao_e_aplicada_quando_termina(app):
    ui = app
    ui.controle.state = "recording"
    w = salvar_modelo(ui, "medium")
    esperar(lambda: False, 0.1)
    assert ui._model_thread is None and ui.transcritor.model_name == "small"  # não trocou
    assert ui._wanted_model == "medium" and ("medium", "cpu", "int8") not in ui.fab.chamadas
    assert esperar(lambda: "assim que o ditado terminar" in w.status.text())
    ui.controle.state = "transcribing"
    esperar(lambda: False, 0.1)
    assert ui.transcritor.model_name == "small"  # transcrevendo também adia
    ui.controle.state = "idle"
    assert esperar(lambda: ui.transcritor.model_name == "medium")
    assert ui._wanted_model is None


def test_novo_pedido_no_meio_da_troca_e_aplicado_depois(app):
    ui = app
    ui.fab.travar["medium"] = liberar = threading.Event()
    salvar_modelo(ui, "medium")
    assert esperar(lambda: ui._model_switching and ("medium", "cpu", "int8") in ui.fab.chamadas)
    salvar_modelo(ui, "small")  # o usuário mudou de ideia enquanto o medium carregava
    assert ui._wanted_model == "small"
    liberar.set()
    assert esperar(lambda: ui.transcritor.model_name == "small" and not ui._model_switching and
                   ui._wanted_model is None)
    assert '"model": "small"' in app.amb.cfg_path.read_text(encoding="utf-8")


def test_depois_de_baixar_o_modelo_do_config_e_aplicado_ao_salvar(app):
    """Config já aponta para `medium` (que faltava e foi baixado depois), mas o app roda o small: Salvar aplica."""
    ui = app
    ui.tray.cfg.model = "medium"
    save_config(ui.tray.cfg, ui.amb.cfg_path)
    ui.show_settings()
    w = ui.settings_window
    assert w.model_combo.currentData() == "medium" and ui.transcritor.model_name == "small"
    assert w.save() is True
    assert esperar(lambda: ui.transcritor.model_name == "medium")


def test_config_aponta_para_modelo_ausente_e_salvar_nao_tenta_carregar_nem_reescreve(app):
    ui = app
    ui.tray.cfg.model = "large-v3"  # não existe em disco
    save_config(ui.tray.cfg, ui.amb.cfg_path)
    antes = ui.amb.cfg_path.read_bytes()
    ui.show_settings()
    assert ui.settings_window.save() is True
    esperar(lambda: False, 0.1)
    assert ui._model_thread is None and ui.transcritor.model_name == "small"  # nada a carregar
    assert load_config(ui.amb.cfg_path).model == "large-v3"  # o modelo do config continua o mesmo


def test_aviso_de_inicializacao_aparece_nas_configuracoes_e_nao_derruba(app):
    ui = app
    ui.show_notice("O modelo ‘medium’ não está no disco; estou usando o ‘small’.", "warn", remember=True)
    esperar(lambda: False, 0.05)
    ui.show_settings()
    assert "estou usando o ‘small’" in ui.settings_window.status.text()
    assert ui.settings_window.status.objectName() == "warn"


# --- evidência visual ----------------------------------------------------------------------------------------------

def _renderizar(w, caminho: Path, escala: int = 2) -> None:
    from PySide6.QtCore import QCoreApplication, QPoint
    from PySide6.QtGui import QPixmap

    w.show()
    for _ in range(5):
        QCoreApplication.processEvents()
    # rola a janela até a seção "Voz", onde fica a linha do modelo
    for scroll in w.findChildren(__import__("PySide6.QtWidgets", fromlist=["QScrollArea"]).QScrollArea):
        scroll.verticalScrollBar().setValue(0)
    QCoreApplication.processEvents()
    pm = QPixmap(w.width() * escala, w.height() * escala)
    pm.setDevicePixelRatio(escala)
    w.render(pm, QPoint(0, 0))
    assert pm.save(str(caminho))


def gerar_evidencias(pasta) -> dict[str, Path]:
    import tempfile

    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    saidas = {}
    with tempfile.TemporaryDirectory(prefix="hear-ai-modelo-") as tmp_s:
        tmp = Path(tmp_s)
        cfg_path = tmp / "config.json"
        cfg = load_config(cfg_path)
        models = tmp / "models"
        criar_modelo(models, "small")
        cfg.models_dir = str(models)
        save_config(cfg, cfg_path)
        amb_ = SimpleNamespace(tmp=tmp, cfg=cfg, cfg_path=cfg_path, models=models)
        w = janela(amb_)
        w.resize(540, 900)
        selecionar(w, "medium")
        _renderizar(w, pasta / "config-modelo.png")
        saidas["config-modelo.png"] = pasta / "config-modelo.png"
        w.model_download_btn.click()
        _renderizar(w, pasta / "config-modelo-confirmar.png")
        saidas["config-modelo-confirmar.png"] = pasta / "config-modelo-confirmar.png"
        w._dl_phase, w._dl_name = "downloading", "medium"
        w._refresh_model_panel()
        w._on_dl_progress(0.37, 566_000_000)
        _renderizar(w, pasta / "config-modelo-baixando.png")
        saidas["config-modelo-baixando.png"] = pasta / "config-modelo-baixando.png"
        w._on_dl_error("Sem conexão com a internet. Confira a rede e clique em “Tentar de novo”.")
        _renderizar(w, pasta / "config-modelo-erro.png")
        saidas["config-modelo-erro.png"] = pasta / "config-modelo-erro.png"
        criar_modelo(models, "medium")
        w._on_dl_done()
        _renderizar(w, pasta / "config-modelo-pronto.png")
        saidas["config-modelo-pronto.png"] = pasta / "config-modelo-pronto.png"
        w.close()
    return saidas


def test_evidencias_sao_geradas_e_diferentes(qapp, tmp_path):
    saidas = gerar_evidencias(tmp_path)
    assert set(saidas) == {"config-modelo.png", "config-modelo-confirmar.png", "config-modelo-baixando.png",
                           "config-modelo-erro.png", "config-modelo-pronto.png"}
    assert all(p.stat().st_size > 5000 for p in saidas.values())
    assert len({p.read_bytes() for p in saidas.values()}) == len(saidas)


if __name__ == "__main__":
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv[:1])
    destino = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "evidencias"
    for nome, caminho in gerar_evidencias(destino).items():
        print(caminho)
