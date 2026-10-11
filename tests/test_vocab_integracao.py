"""T45b: vocabulário ligado ao fluxo REAL (controller → histórico → Recentes → aprender)."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from hear_ai.controller import Controller, vocab_postprocessor
from hear_ai.history import HistoryStore
from hear_ai.vocab import VocabStore, prompt_terms

AUDIO = np.zeros(16000, dtype=np.float32)
DIC = {"ondex": "Ondex", "maestri": "Maestri", "groq": "Groq", "hear ai": "Hear AI", "claude code": "Claude Code"}


class Rec:
    def start(self):
        pass

    def stop(self):
        return AUDIO


class Trans:
    def __init__(self, text):
        self.text = text
        self.initial_prompt = None

    def transcribe(self, audio):
        return self.text


class Paster:
    def __init__(self):
        self.pasted = []

    def paste(self, text):
        self.pasted.append(text)


class Fb:
    def start(self): pass
    def stop(self): pass
    def done(self): pass
    def error(self): pass


class Sink:
    def __init__(self):
        self.saved = []

    def save(self, text, meta):
        self.saved.append((text, dict(meta)))
        return "id-1"

    def set_status(self, *_a):
        return True


def cfg(**kw):
    base = dict(dictionary=dict(DIC), voice_commands=True, use_initial_prompt=True)
    base.update(kw)
    return SimpleNamespace(**base)


def dictate(text, config=None, store=None, sink=None, logger=None):
    paster, sink = Paster(), sink or Sink()
    c = Controller(Rec(), Trans(text), paster, Fb(), sink, logger or logging.getLogger("t45b"), "pt",
                   run_async=False, postprocess=vocab_postprocessor(config or cfg(), store))
    c.begin()
    c.end()
    return paster.pasted, getattr(sink, "saved", None)


@pytest.mark.parametrize("fala, colado", [
    ("a undex lançou", "a Ondex lançou"),
    ("abri o aondex", "abri o Ondex"),
    ("o Heer AI funciona", "o Hear AI funciona"),
    ("falei com o cloud code", "falei com o Claude Code"),
])
def test_variantes_corrigidas_no_texto_colado(fala, colado):
    pasted, _saved = dictate(fala)
    assert pasted == [colado]


def test_cloud_legitimo_intocado_e_sem_raw():
    pasted, saved = dictate("vou subir isso na cloud")
    assert pasted == ["vou subir isso na cloud"]
    assert "raw" not in saved[0][1] and "changes" not in saved[0][1]


def test_raw_e_changes_no_historico_so_quando_mudou():
    _p, saved = dictate("a undex usa o cloud code")
    text, meta = saved[0]
    assert text == "a Ondex usa o Claude Code"
    assert meta["raw"] == "a undex usa o cloud code"
    assert meta["changes"] == [["undex", "Ondex"], ["cloud code", "Claude Code"]]


def test_dicionario_antigo_do_usuario_continua_igual():
    pasted, _ = dictate("a ondex e o maestri com groq no hear ai e claude code")
    assert pasted == ["a Ondex e o Maestri com Groq no Hear AI e Claude Code"]


def test_dicionario_vem_do_config_e_nao_do_padrao():
    pasted, _ = dictate("o xpto chegou", config=cfg(dictionary={"xpto": "XPTO"}))
    assert pasted == ["o XPTO chegou"]


def test_comandos_de_voz_respeitam_o_config():
    assert dictate("linha um nova linha dois")[0] == ["linha um\nDois"]
    assert dictate("linha um nova linha dois", config=cfg(voice_commands=False))[0] == ["linha um nova linha dois"]


def test_confirmado_vale_no_proximo_ditado(tmp_path):
    store = VocabStore(tmp_path / "vocabulario.json")
    assert dictate("o zorbix chegou", store=store)[0] == ["o zorbix chegou"]
    store.confirm("zorbix", "Zorbix Labs")
    assert dictate("o zorbix chegou", store=store)[0] == ["o Zorbix Labs chegou"]


def test_historico_real_guarda_raw_e_entry_le(tmp_path):
    hist = HistoryStore(tmp_path / "historico", retention_days=30)
    dictate("a undex lançou", sink=hist)
    entry = hist.entries()[0]
    assert entry.text == "a Ondex lançou" and entry.raw == "a undex lançou"
    assert entry.changes == (("undex", "Ondex"),)


def test_sem_historico_nada_e_gravado(tmp_path):
    from hear_ai.history import NullSink
    pasted, _ = dictate("a undex lançou", sink=NullSink())
    assert pasted == ["a Ondex lançou"]
    assert not any(tmp_path.iterdir())


def test_logs_sem_texto(caplog):
    caplog.set_level(logging.DEBUG)
    dictate("SENTINELA a undex lançou SENTINELA", logger=logging.getLogger("hear_ai.t45b"))
    assert "SENTINELA" not in caplog.text and "undex" not in caplog.text
    assert "vocabulário: 1 correção" in caplog.text


def test_initial_prompt_tem_os_termos(tmp_path):
    store = VocabStore(tmp_path / "v.json")
    store.confirm("zorbix", "Zorbix Labs")
    out = prompt_terms(store, DIC)
    assert out.startswith("Zorbix Labs, Ondex, Maestri") and "Claude" in out


def test_main_liga_vocabulario_e_initial_prompt():
    """O __main__ de produção passa o initial_prompt e o pós-texto com vocabulário (antes: não passava)."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "hear_ai", "__main__.py"), encoding="utf-8").read()
    assert "initial_prompt=initial_prompt" in src
    assert "postprocess=vocab_postprocessor(cfg, vocab_store)" in src
    assert 'with_name("vocabulario.json")' in src


# --- Recentes: aprender só por clique ---------------------------------------------------

@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _store_with(tmp_path, text, raw=None, changes=None):
    hist = HistoryStore(tmp_path / "historico", retention_days=30)
    meta = {"duration_s": 2.0, "language": "pt", "status": "colado"}
    if raw is not None:
        meta["raw"], meta["changes"] = raw, changes or []
    hist.save(text, meta)
    return hist


def _window(hist, learned):
    from hear_ai.ui.recent_window import RecentWindow
    w = RecentWindow(hist, now=lambda: datetime.now(),
                     on_learn=lambda pairs: (learned.extend(pairs), len(pairs))[1])
    w.refresh()
    return w


def test_recentes_mostra_corrigido_e_lembrar_so_com_clique(qapp, tmp_path):
    learned = []
    w = _window(_store_with(tmp_path, "a Ondex lançou", "a undex lançou", [["undex", "Ondex"]]), learned)
    row = w.rows()[0] if callable(getattr(w, "rows", None)) else w._rows[0]
    assert "undex → Ondex" in row.fixed_label.text()
    assert learned == []  # nada aprendido só por abrir
    row.learn_btn.click()
    assert learned == [("undex", "Ondex")] and not row.learn_btn.isEnabled()
    w.close()


def test_editar_sem_salvar_nao_aprende(qapp, tmp_path):
    learned = []
    w = _window(_store_with(tmp_path, "o zorbix chegou hoje cedo"), learned)
    row = w._rows[0]
    row.open_editor()
    row.edit_field.setText("o Zorbix Labs chegou hoje cedo")
    row.close_editor()
    assert learned == []
    # Salvar só MOSTRA os pares; guardar exige "Lembrar marcadas"
    row.open_editor()
    row.edit_field.setText("o Zorbex chegou hoje cedo")
    pairs = row.propose_pairs()
    assert pairs == [("zorbix", "Zorbex")] and learned == []
    row.edit_learn.click()
    assert learned == [("zorbix", "Zorbex")]
    w.close()


def test_desmarcar_par_nao_aprende(qapp, tmp_path):
    learned = []
    w = _window(_store_with(tmp_path, "o zorbix chegou hoje cedo"), learned)
    row = w._rows[0]
    row.open_editor()
    row.edit_field.setText("o Zorbex chegou hoje cedo")
    row.propose_pairs()
    row.pair_boxes[0].setChecked(False)
    row.edit_learn.click()
    assert learned == []
    w.close()


def test_sem_on_learn_nao_ha_botoes(qapp, tmp_path):
    from hear_ai.ui.recent_window import RecentWindow
    w = RecentWindow(_store_with(tmp_path, "a Ondex", "a undex", [["undex", "Ondex"]]))
    w.refresh()
    row = w._rows[0]
    assert row.learn_btn is None and row.teach_btn is None and row.fixed_label is not None
    w.close()


def test_qtui_learn_corrections_atualiza_initial_prompt(tmp_path):
    from hear_ai.ui.qt_app import QtUi
    store = VocabStore(tmp_path / "v.json")
    fake = SimpleNamespace(vocab_store=store, tray=SimpleNamespace(cfg=cfg()),
                           controller=SimpleNamespace(transcriber=Trans("x")), logger=logging.getLogger("t"))
    assert QtUi.learn_corrections(fake, [("zorbix", "Zorbix Labs"), ("de", "do")]) == 1
    assert store.entries() == {"zorbix": "Zorbix Labs"}
    assert fake.controller.transcriber.initial_prompt.startswith("Zorbix Labs, ")
