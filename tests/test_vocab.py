"""Testes do vocabulário contextual (T45a): variantes, contexto, falsos positivos, aprendizagem."""

from __future__ import annotations

import json
import logging

import pytest

from hear_ai.vocab import (BUILTIN_TERMS, Mudanca, VocabStore, corrigir, prompt_terms, suggest_pairs)

DICIONARIO_USUARIO = {"ondex": "Ondex", "maestri": "Maestri", "groq": "Groq",
                      "hear ai": "Hear AI", "claude code": "Claude Code"}


def fix(text, **kw):
    return corrigir(text, **kw).texto


# --- variantes corrigidas sem contexto -----------------------------------------------

@pytest.mark.parametrize("ouvido", ["undex", "aondex", "ondecs", "ôndex", "ondéx", "on dex", "a um dex", "ÔNDEX"])
def test_variantes_de_ondex(ouvido):
    assert fix(f"a {ouvido} lançou o produto") == "a Ondex lançou o produto"


@pytest.mark.parametrize("ouvido", ["heer ai", "hear a i", "here ai", "hir ai", "heer a.i", "Here AI", "hear ai"])
def test_variantes_de_hear_ai(ouvido):
    assert fix(f"abri o {ouvido} agora") == "abri o Hear AI agora"


def test_hear_ai_com_ponto_final_mantem_o_ponto_da_frase():
    assert fix("Gostei do Heer a.i.") == "Gostei do Hear AI."


@pytest.mark.parametrize("ouvido", ["cloude", "clôde", "claud", "Cloude"])
def test_variantes_de_claude(ouvido):
    assert fix(f"o {ouvido} respondeu") == "o Claude respondeu"


@pytest.mark.parametrize("fala, esperado", [
    ("abre o cloud code", "abre o Claude Code"),
    ("eu falei com o cloud code ontem", "eu falei com o Claude Code ontem"),
    ("o cloud opus é bom", "o Claude Opus é bom"),
    ("instalei o Cloud Desktop", "instalei o Claude Desktop"),
    ("vou usar o cloud para isso", "vou usar o Claude para isso"),
    ("pergunta ao cloud se dá", "pergunta ao Claude se dá"),
    ("preciso conversar com o cloud", "preciso conversar com o Claude"),
])
def test_cloud_so_com_contexto_forte(fala, esperado):
    assert fix(fala) == esperado


# --- palavras legítimas intocadas ----------------------------------------------------

@pytest.mark.parametrize("fala", [
    "cloud computing é caro", "the cloud is down", "vou subir isso na cloud", "nuvem cloud",
    "o cloudflare caiu", "here is the file", "come here", "I hear you", "I hear a noise",
    "where are you", "the undexed list", "a Claudia ligou", "andar claudicante",
    "o Claude respondeu", "abri o Hear AI", "hearing test", "Ondex Ondex",
    "tomei a um dexamentasona", "o cloud", "no cloud", "deploy na cloud code-free",
])
def test_palavras_legitimas_intocadas(fala):
    r = corrigir(fala)
    assert r.texto == fala and r.mudancas == ()


def test_texto_vazio():
    assert corrigir("").texto == "" and corrigir("").mudancas == ()


# --- forma do texto preservada --------------------------------------------------------

@pytest.mark.parametrize("fala, esperado", [
    ("Undex é a empresa.", "Ondex é a empresa."),
    ("(aondex)", "(Ondex)"),
    ("Abri o Heer AI, testei.", "Abri o Hear AI, testei."),
    ("undex-like", "Ondex-like"),
    ("linha 1\naondex\n\nfim", "linha 1\nOndex\n\nfim"),
    ("Ondex, Claude Code e Hear AI.", "Ondex, Claude Code e Hear AI."),
])
def test_pontuacao_hifen_quebras_e_inicio_de_frase(fala, esperado):
    assert fix(fala) == esperado


def test_frase_com_varios_termos_e_mudancas_registradas():
    r = corrigir("a undex usa o cloud code e o heer ai")
    assert r.texto == "a Ondex usa o Claude Code e o Hear AI"
    assert [(m.ouvido, m.correto) for m in r.mudancas] == [
        ("undex", "Ondex"), ("cloud code", "Claude Code"), ("heer ai", "Hear AI")]
    assert all(isinstance(m, Mudanca) for m in r.mudancas)
    assert r.mudancas[0].posicao == 2 and r.mudancas[0].regra == "ondex"


# --- prioridades ----------------------------------------------------------------------

def test_dicionario_antigo_do_usuario_continua_igual():
    r = corrigir("a ondex e o maestri com groq no hear ai e claude code", extras=DICIONARIO_USUARIO)
    assert r.texto == "a Ondex e o Maestri com Groq no Hear AI e Claude Code"
    assert {m.regra for m in r.mudancas} == {"dicionario"}


def test_confirmado_vence_dicionario_e_embutida():
    r = corrigir("o undex voltou", extras={"undex": "UNDEX-DIC"}, confirmados={"undex": "Ondex Labs"})
    assert r.texto == "o Ondex Labs voltou" and r.mudancas[0].regra == "confirmado"


def test_dicionario_vence_embutida():
    r = corrigir("o undex voltou", extras={"undex": "Undex Corp"})
    assert r.texto == "o Undex Corp voltou" and r.mudancas[0].regra == "dicionario"


def test_confirmado_sem_caixa_e_sem_acento():
    assert fix("falei com a Joao Silva", confirmados={"joão silva": "João Silva"}) == "falei com a João Silva"


def test_correcao_nao_e_aplicada_duas_vezes():
    # "Claude Code" já correto não é tocado por nenhuma regra
    r = corrigir("Claude Code", extras=DICIONARIO_USUARIO)
    assert r.texto == "Claude Code" and r.mudancas == ()


# --- VocabStore -----------------------------------------------------------------------

def test_aprende_so_depois_de_confirmar(tmp_path):
    store = VocabStore(tmp_path / "vocabulario.json")
    assert fix("o xpto chegou", confirmados=store.entries()) == "o xpto chegou"
    assert store.confirm("xpto", "XPTO Ltda")
    assert fix("o xpto chegou", confirmados=store.entries()) == "o XPTO Ltda chegou"


def test_persistencia_e_reabrir(tmp_path):
    p = tmp_path / "sub" / "vocabulario.json"
    VocabStore(p).confirm("undex", "Ondex")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["confirmados"] == {"undex": "Ondex"}
    assert VocabStore(p).entries() == {"undex": "Ondex"}
    assert not (tmp_path / "sub" / "vocabulario.json.tmp").exists()


@pytest.mark.parametrize("conteudo", [None, "", "{quebrado", "[]", '{"confirmados": 3}'])
def test_arquivo_ausente_ou_corrompido_vira_vazio(tmp_path, conteudo):
    p = tmp_path / "vocabulario.json"
    if conteudo is not None:
        p.write_text(conteudo, encoding="utf-8")
    assert VocabStore(p).entries() == {}


@pytest.mark.parametrize("ouvido, correto", [
    ("", "x"), ("x", ""), ("a", "b"), ("de", "do"), ("the", "a"), ("ondex", "ONDEX"),
    ("linha\nquebrada", "x"), ("x" * 61, "y"), ("ok", "y" * 61), (None, "x"),
])
def test_confirm_recusa_pares_invalidos(tmp_path, ouvido, correto):
    store = VocabStore(tmp_path / "v.json")
    assert store.confirm(ouvido, correto) is False
    assert store.entries() == {}


def test_confirm_mesma_chave_substitui_e_remove(tmp_path):
    store = VocabStore(tmp_path / "v.json")
    store.confirm("Undex", "Ondex")
    store.confirm("undex", "Ondex SA")
    assert store.entries() == {"undex": "Ondex SA"}
    assert store.remove("UNDEX") and store.entries() == {}
    assert store.remove("nada") is False


def test_forget_all(tmp_path):
    store = VocabStore(tmp_path / "v.json")
    store.confirm("undex", "Ondex")
    assert store.forget_all() and VocabStore(tmp_path / "v.json").entries() == {}


def test_arquivo_carregado_ignora_entradas_invalidas(tmp_path):
    p = tmp_path / "v.json"
    p.write_text(json.dumps({"confirmados": {"de": "do", "undex": "Ondex", "x": 3}}), encoding="utf-8")
    assert VocabStore(p).entries() == {"undex": "Ondex"}


# --- sugestões a partir de edição -----------------------------------------------------

def test_sugere_correcao_de_uma_palavra():
    assert suggest_pairs("a undex lançou o app hoje", "a Ondex lançou o app hoje") == [("undex", "Ondex")]


def test_sugere_duas_palavras_e_ignora_pontuacao():
    assert suggest_pairs("abri o heer ai, e funcionou bem demais", "abri o Hear AI, e funcionou bem demais") == [
        ("heer ai", "Hear AI")]


def test_reescrita_grande_nao_e_correcao():
    assert suggest_pairs("eu quero que você faça isso amanhã cedo", "por favor execute a tarefa hoje") == []


def test_trecho_longo_dentro_de_texto_grande_e_ignorado():
    raw = " ".join(["palavra"] * 20) + " um dois três quatro " + " ".join(["fim"] * 20)
    final = " ".join(["palavra"] * 20) + " cinco seis sete oito " + " ".join(["fim"] * 20)
    assert suggest_pairs(raw, final) == []


def test_so_caixa_ou_stopword_nao_sugere():
    assert suggest_pairs("a ondex lançou", "a Ondex lançou") == []
    assert suggest_pairs("eu fui de carro", "eu fui do carro") == []


def test_sugestao_nao_aprende_sozinha(tmp_path):
    store = VocabStore(tmp_path / "v.json")
    suggest_pairs("a undex lançou o app", "a Ondex lançou o app")
    assert store.entries() == {} and not (tmp_path / "v.json").exists()


# --- initial_prompt -------------------------------------------------------------------

def test_prompt_terms_ordem_e_unicos(tmp_path):
    store = VocabStore(tmp_path / "v.json")
    store.confirm("xpto", "XPTO Ltda")
    out = prompt_terms(store, DICIONARIO_USUARIO)
    assert out == "XPTO Ltda, Ondex, Maestri, Groq, Hear AI, Claude Code, Claude."


def test_prompt_terms_sem_nada_usa_embutidos():
    assert prompt_terms(None, None) == ", ".join(BUILTIN_TERMS) + "."


def test_prompt_terms_limites():
    extras = {f"t{i}": f"Termo{i:02d}" for i in range(80)}
    extras["longo"] = "x" * 41
    out = prompt_terms(None, extras)
    terms = out.rstrip(".").split(", ")
    assert len(terms) <= 40 and len(out) <= 300 and "x" * 41 not in out


# --- privacidade ----------------------------------------------------------------------

def test_logs_nunca_tem_texto(caplog):
    caplog.set_level(logging.DEBUG, logger="hear_ai.vocab")
    corrigir("SENTINELA-SECRETA a undex usou o cloud code")
    assert "SENTINELA" not in caplog.text and "undex" not in caplog.text
