"""Testes para history.py (T15: HistoryStore)."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from hear_ai import history as history_mod
from hear_ai.history import Entry, HistoryStore, JsonlHistorySink, NullSink, make_sink


class FakeConfig:
    def __init__(self, history_enabled: bool, history_dir: str = "historico", **extra) -> None:
        self.history_enabled = history_enabled
        self.history_dir = history_dir
        for k, v in extra.items():
            setattr(self, k, v)


class Clock:
    def __init__(self, start=datetime(2026, 10, 9, 10, 0, 0)):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def files(d: Path) -> list[Path]:
    return sorted(d.glob("historico-*.jsonl"))


def records(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# --- comportamento já existente (v1.0), mantido ----------------------------------

def test_null_sink_nao_faz_nada():
    assert NullSink().save("texto", {"meta": "valor"}) is None


def test_nome_antigo_continua_valendo():
    assert JsonlHistorySink is HistoryStore


def test_grava_linha_jsonl_com_campos_e_devolve_id(tmp_path: Path):
    store = HistoryStore(tmp_path, clock=Clock())
    rid = store.save("Olá, mundo!", {"duration_s": 1.5, "language": "pt"})
    assert isinstance(rid, str) and rid
    (arquivo,) = files(tmp_path)
    assert arquivo.name == "historico-2026-10-09.jsonl"
    (rec,) = records(arquivo)
    assert rec["id"] == rid and rec["text"] == "Olá, mundo!"
    assert rec["duration_s"] == 1.5 and rec["language"] == "pt"
    assert rec["status"] == "salvo"
    assert datetime.fromisoformat(rec["ts"]) == datetime(2026, 10, 9, 10, 0, 0)


def test_acento_e_emoji_em_utf8(tmp_path: Path):
    HistoryStore(tmp_path).save("Olá, árvore! 😊", {})
    (arquivo,) = files(tmp_path)
    assert "Olá, árvore! 😊" in arquivo.read_text(encoding="utf-8")


def test_meta_nao_sobrescreve_campos_reservados(tmp_path: Path):
    store = HistoryStore(tmp_path)
    rid = store.save("texto correto", {"ts": "2000-01-01T00:00:00", "text": "errado", "id": "x", "language": "pt"})
    (rec,) = records(files(tmp_path)[0])
    assert rec["text"] == "texto correto" and rec["id"] == rid != "x"
    assert rec["ts"] != "2000-01-01T00:00:00" and rec["language"] == "pt"


def test_serializa_numpy_float(tmp_path: Path):
    store = HistoryStore(tmp_path)
    store.save("teste", {"duration_s": np.float32(1.5)})
    (rec,) = records(files(tmp_path)[0])
    assert rec["duration_s"] == "1.5"
    assert store.entries()[0].duration_s == 1.5


def test_erro_de_disco_nao_propaga_e_loga_so_o_tipo(tmp_path: Path, caplog):
    bloqueio = tmp_path / "arquivo"
    bloqueio.write_text("x")
    store = HistoryStore(bloqueio / "sub")  # pasta impossível de criar
    with caplog.at_level(logging.WARNING, logger="hear_ai.history"):
        assert store.save("segredo ditado", {}) is None
    assert "falha ao salvar histórico" in caplog.text
    assert "segredo" not in caplog.text
    assert store.entries() == []


def test_pasta_criada_no_primeiro_save(tmp_path: Path):
    sub = tmp_path / "sub" / "pasta"
    store = HistoryStore(sub)
    assert not sub.exists()
    assert store.entries() == []
    store.save("teste", {})
    assert len(files(sub)) == 1


# --- entries / busca -------------------------------------------------------------

def test_entries_mais_recentes_primeiro_entre_dias(tmp_path: Path):
    clock = Clock()
    store = HistoryStore(tmp_path, clock=clock)
    a = store.save("primeiro", {})
    b = store.save("segundo", {})
    clock.advance(days=1)
    c = store.save("terceiro", {"duration_s": 2.0, "language": "en"})
    es = store.entries()
    assert [e.id for e in es] == [c, b, a]
    assert isinstance(es[0], Entry)
    assert es[0].duration_s == 2.0 and es[0].language == "en" and es[0].day == "2026-10-10"
    assert es[0].ts == datetime(2026, 10, 10, 10, 0, 0)
    assert [e.text for e in store.entries(limit=2)] == ["terceiro", "segundo"]


def test_busca_sem_diferenciar_acento_nem_maiuscula(tmp_path: Path):
    store = HistoryStore(tmp_path)
    store.save("Reunião de PLANEJAMENTO amanhã", {})
    store.save("comprar pão", {})
    store.save("Ação rápida", {})
    assert [e.text for e in store.entries(query="reuniao")] == ["Reunião de PLANEJAMENTO amanhã"]
    assert [e.text for e in store.entries(query="PAO")] == ["comprar pão"]
    assert [e.text for e in store.entries(query="acao RAPIDA")] == ["Ação rápida"]
    assert store.entries(query="inexistente") == []
    assert len(store.entries(query="   ")) == 3
    assert len(store.entries(query="a", limit=2)) == 2


def test_linha_quebrada_e_ignorada_na_leitura_e_preservada(tmp_path: Path):
    store = HistoryStore(tmp_path, clock=Clock())
    a = store.save("bom 1", {})
    path = files(tmp_path)[0]
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"text": "linha cort\n')  # escrita interrompida
        f.write("[1, 2, 3]\n")
        f.write('{"sem_texto": true}\n')
        f.write("\n")
    b = store.save("bom 2", {})
    assert [e.text for e in store.entries()] == ["bom 2", "bom 1"]
    assert store.set_status(a, "colado")
    texto = path.read_text(encoding="utf-8")
    assert '{"text": "linha cort' in texto  # reescrita não apaga o que não entende
    assert store.get(b).status == "salvo"


def test_arquivos_alheios_na_pasta_sao_ignorados(tmp_path: Path):
    (tmp_path / "outro.jsonl").write_text('{"text": "x"}\n', encoding="utf-8")
    (tmp_path / "historico-ontem.jsonl").write_text('{"text": "x"}\n', encoding="utf-8")
    store = HistoryStore(tmp_path)
    store.save("ok", {})
    assert [e.text for e in store.entries()] == ["ok"]


# --- ids estáveis ----------------------------------------------------------------

def test_ids_estaveis_depois_de_reabrir_apagar_e_mudar_status(tmp_path: Path):
    s1 = HistoryStore(tmp_path)
    ids = [s1.save(f"texto {i}", {}) for i in range(4)]
    s2 = HistoryStore(tmp_path)  # "reabriu o app"
    assert [e.id for e in s2.entries()] == ids[::-1]
    assert s2.delete(ids[0])
    assert s2.set_status(ids[2], "colado")
    s3 = HistoryStore(tmp_path)
    assert [e.id for e in s3.entries()] == [ids[3], ids[2], ids[1]]
    assert s3.get(ids[2]).status == "colado"


def test_linha_antiga_sem_id_tem_id_estavel_e_pode_mudar(tmp_path: Path):
    path = tmp_path / "historico-2026-10-01.jsonl"
    path.write_text(
        json.dumps({"ts": "2026-10-01T09:00:00", "text": "ditado v1.0", "duration_s": 3.2, "language": "pt"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    store = HistoryStore(tmp_path, retention_days=None)
    (e,) = store.entries()
    assert e.id.startswith("h-") and e.status == "salvo" and e.duration_s == 3.2
    assert HistoryStore(tmp_path).entries()[0].id == e.id
    assert store.set_status(e.id, "colado")
    (rec,) = records(path)
    assert rec["id"] == e.id and rec["status"] == "colado"
    assert store.delete(e.id)
    assert store.entries() == []


# --- status ----------------------------------------------------------------------

def test_status_pendente_depois_colado(tmp_path: Path):
    store = HistoryStore(tmp_path)
    rid = store.save("antes de colar", {"status": "pendente"})
    assert store.entries()[0].status == "pendente"  # pendentes aparecem normalmente
    assert store.set_status(rid, "colado") is True
    assert store.get(rid).status == "colado"
    assert store.set_status(rid, "salvo") is True
    assert store.get(rid).status == "salvo"


def test_status_invalido(tmp_path: Path):
    store = HistoryStore(tmp_path)
    rid = store.save("x", {"status": "qualquer"})
    assert store.get(rid).status == "salvo"  # status desconhecido no save vira o padrão
    with pytest.raises(ValueError):
        store.set_status(rid, "apagado")


def test_set_status_e_delete_de_id_inexistente(tmp_path: Path):
    store = HistoryStore(tmp_path)
    store.save("x", {})
    assert store.set_status("nao-existe", "colado") is False
    assert store.delete("nao-existe") is False
    assert len(store.entries()) == 1


# --- delete / clear_all ----------------------------------------------------------

def test_delete_remove_so_a_entrada(tmp_path: Path):
    store = HistoryStore(tmp_path)
    a, b, c = (store.save(t, {}) for t in ("um", "dois", "tres"))
    assert store.delete(b) is True
    assert [e.text for e in store.entries()] == ["tres", "um"]
    assert store.get(b) is None


def test_delete_da_ultima_linha_remove_o_arquivo(tmp_path: Path):
    store = HistoryStore(tmp_path)
    rid = store.save("único", {})
    assert store.delete(rid)
    assert files(tmp_path) == []


def test_clear_all_apaga_todos_os_dias(tmp_path: Path):
    clock = Clock()
    store = HistoryStore(tmp_path, clock=clock)
    for _ in range(3):
        store.save("x", {})
        clock.advance(days=1)
    (tmp_path / "outro.txt").write_text("não é histórico")
    assert store.clear_all() == 3
    assert store.entries() == [] and files(tmp_path) == []
    assert (tmp_path / "outro.txt").exists()
    assert store.clear_all() == 0


def test_reescrita_e_atomica_sem_temporarios_sobrando(tmp_path: Path, monkeypatch):
    store = HistoryStore(tmp_path)
    rid = store.save("a", {})
    store.save("b", {})
    chamadas = []
    real_replace = os.replace

    def spy(src, dst):
        chamadas.append((Path(src).parent, Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(history_mod.os, "replace", spy)
    assert store.set_status(rid, "colado")
    ((pasta_tmp, destino),) = chamadas
    assert pasta_tmp == tmp_path and destino == files(tmp_path)[0]  # temporário na MESMA pasta
    assert [p.name for p in tmp_path.iterdir()] == [destino.name]


def test_falha_na_reescrita_mantem_o_arquivo_original(tmp_path: Path, monkeypatch, caplog):
    store = HistoryStore(tmp_path)
    rid = store.save("original", {})
    store.save("outra", {})  # com 2 linhas, o delete também reescreve (não só apaga o arquivo)
    antes = files(tmp_path)[0].read_bytes()

    def falha(src, dst):
        raise PermissionError("arquivo aberto em outro programa")

    monkeypatch.setattr(history_mod.os, "replace", falha)
    with caplog.at_level(logging.WARNING, logger="hear_ai.history"):
        assert store.set_status(rid, "colado") is False
        assert store.delete(rid) is False
    assert files(tmp_path)[0].read_bytes() == antes
    assert [p.name for p in tmp_path.iterdir()] == [files(tmp_path)[0].name]  # sem .tmp
    assert "PermissionError" in caplog.text and "original" not in caplog.text


# --- pausa / retenção ------------------------------------------------------------

def test_pausado_nao_grava(tmp_path: Path):
    store = HistoryStore(tmp_path)
    store.paused = True
    assert store.paused is True
    assert store.save("não deve gravar", {}) is None
    assert files(tmp_path) == [] and not any(tmp_path.iterdir())
    store.paused = False
    assert store.save("agora sim", {})
    assert [e.text for e in store.entries()] == ["agora sim"]


def test_retencao_com_relogio_falso(tmp_path: Path):
    clock = Clock(datetime(2026, 9, 1, 12, 0))
    store = HistoryStore(tmp_path, retention_days=30, clock=clock)
    for _ in range(40):  # um arquivo por dia: 01/09 .. 10/10
        store.save("x", {})
        clock.advance(days=1)
    clock.now = datetime(2026, 10, 10, 12, 0)
    assert len(files(tmp_path)) == 40
    apagados = store.purge_expired()
    restantes = [p.name for p in files(tmp_path)]
    assert apagados == 9  # 01/09 .. 09/09 (< 10/09)
    assert restantes[0] == "historico-2026-09-10.jsonl"  # exatamente 30 dias: fica
    assert restantes[-1] == "historico-2026-10-10.jsonl"
    assert store.purge_expired() == 0


def test_retencao_desligada(tmp_path: Path):
    clock = Clock(datetime(2020, 1, 1))
    store = HistoryStore(tmp_path, retention_days=None, clock=clock)
    store.save("antigo", {})
    clock.now = datetime(2026, 1, 1)
    assert store.purge_expired() == 0
    assert HistoryStore(tmp_path, retention_days=0, clock=clock).purge_expired() == 0
    assert len(files(tmp_path)) == 1


# --- concorrência ----------------------------------------------------------------

def test_save_concorrente_com_set_status_nao_perde_registros(tmp_path: Path):
    store = HistoryStore(tmp_path)
    existentes = [store.save(f"pendente {i}", {"status": "pendente"}) for i in range(50)]
    novos: list[str] = []
    erros: list[BaseException] = []
    lock = threading.Lock()
    inicio = threading.Barrier(100)

    def salvar(i):
        try:
            inicio.wait()
            rid = store.save(f"novo {i}", {"status": "pendente"})
            with lock:
                novos.append(rid)
        except BaseException as e:  # pragma: no cover - só em falha
            erros.append(e)

    def marcar(rid):
        try:
            inicio.wait()
            assert store.set_status(rid, "colado")
        except BaseException as e:  # pragma: no cover - só em falha
            erros.append(e)

    ts = [threading.Thread(target=salvar, args=(i,)) for i in range(50)]
    ts += [threading.Thread(target=marcar, args=(rid,)) for rid in existentes]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert erros == []
    es = {e.id: e for e in store.entries()}
    assert len(es) == 100
    assert all(es[r].status == "colado" for r in existentes)
    assert all(r in es and es[r].status == "pendente" for r in novos)
    linhas = files(tmp_path)[0].read_text(encoding="utf-8").splitlines()
    assert len(linhas) == 100 and all(json.loads(x)["id"] for x in linhas)


# --- make_sink -------------------------------------------------------------------

def test_make_sink_ativa_retorna_store(tmp_path: Path):
    sink = make_sink(FakeConfig(True, "meu_historico"), tmp_path)
    assert isinstance(sink, HistoryStore)
    assert sink.retention_days == 30 and sink.paused is False
    assert not (tmp_path / "meu_historico").exists()  # pasta só no 1º save
    sink.save("teste", {})
    assert (tmp_path / "meu_historico").exists()


def test_make_sink_desativada_retorna_null(tmp_path: Path):
    assert isinstance(make_sink(FakeConfig(False), tmp_path), NullSink)


def test_make_sink_usa_base_dir_nao_cwd(tmp_path: Path):
    outro = tmp_path / "outro"
    sink = make_sink(FakeConfig(True), outro)
    sink.save("teste", {})
    assert (outro / "historico").exists() and not (tmp_path / "historico").exists()


def test_make_sink_le_retencao_pausa_e_purga_ao_criar(tmp_path: Path):
    pasta = tmp_path / "historico"
    pasta.mkdir()
    velho = pasta / "historico-2000-01-01.jsonl"
    velho.write_text('{"text": "antigo"}\n', encoding="utf-8")
    hoje = pasta / f"historico-{datetime.now().date().isoformat()}.jsonl"
    hoje.write_text('{"text": "hoje"}\n', encoding="utf-8")
    sink = make_sink(FakeConfig(True, history_retention_days=7, history_paused=True), tmp_path)
    assert sink.retention_days == 7 and sink.paused is True
    assert not velho.exists() and hoje.exists()
    assert sink.save("pausado", {}) is None
