"""Avalia o gerador de prompts (Modo B) contra `tests/gerador/casos.jsonl`.

Uso:
    .venv\\Scripts\\python.exe scripts\\avaliar_gerador.py --modelo qwen3:8b          (Ollama local)
    .venv\\Scripts\\python.exe scripts\\avaliar_gerador.py --provedor falso           (eco, só para testar o script)

Só fala com 127.0.0.1. Se o Ollama não responder, sai com código 2 e a mensagem
"provedor indisponível: qualidade do gerador NÃO medida". Não grava a fala em log.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hear_ai.gerador import (SEM_PEDIDO, Contexto, OllamaProvider, carregar_prompt_sistema,  # noqa: E402
                             gerar_com_fallback)

CASOS = ROOT / "tests" / "gerador" / "casos.jsonl"


class ProvedorFalso:
    """Devolve a própria mensagem (eco) — prova que o script roda, não mede qualidade."""
    nome = "falso"
    na_nuvem = False

    def completar(self, system_prompt: str, mensagem: str) -> str:
        return mensagem


def carregar_casos(path: Path = CASOS) -> list[dict]:
    casos = []
    for n, linha in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not linha.strip():
            continue
        caso = json.loads(linha)
        for chave in ("id", "fala", "deve_conter", "nao_deve_conter", "deve_ser_sem_pedido"):
            if chave not in caso:
                raise ValueError(f"linha {n}: falta a chave {chave!r}")
        casos.append(caso)
    return casos


def avaliar(provedor, casos: list[dict], system_prompt: str) -> list[dict]:
    resultados = []
    for caso in casos:
        ctx = Contexto(app=caso.get("app"), destino=caso.get("destino") or "auto")
        r = gerar_com_fallback(provedor, caso["fala"], ctx, system_prompt)
        motivos = []
        if caso["deve_ser_sem_pedido"]:
            if r.usou_gerador:
                motivos.append("devia ser [[SEM_PEDIDO]] (colar a fala)")
        else:
            if not r.usou_gerador:
                motivos.append(f"gerador não usado: {r.motivo}")
            baixo = r.texto.lower()
            motivos += [f"faltou {x!r}" for x in caso["deve_conter"] if str(x).lower() not in baixo]
            motivos += [f"não devia conter {x!r}" for x in caso["nao_deve_conter"] if str(x).lower() in baixo]
            if SEM_PEDIDO in r.texto:
                motivos.append("marcador [[SEM_PEDIDO]] vazou para o texto")
        resultados.append({"id": caso["id"], "passou": not motivos, "motivos": motivos})
    return resultados


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--provedor", choices=["ollama", "falso"], default="ollama")
    ap.add_argument("--modelo", default="")
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--casos", default=str(CASOS))
    args = ap.parse_args(argv)

    casos = carregar_casos(Path(args.casos))
    if args.provedor == "falso":
        provedor = ProvedorFalso()
    else:
        provedor = OllamaProvider(args.modelo, base_url=args.url)
        if not provedor.disponivel():
            print("provedor indisponível: qualidade do gerador NÃO medida (Ollama não respondeu em "
                  f"{args.url})")
            return 2
        if not args.modelo:
            print("escolha o modelo com --modelo; instalados:", ", ".join(provedor.listar_modelos()) or "nenhum")
            return 2
    system_prompt = carregar_prompt_sistema(None, ROOT / "prompts")
    resultados = avaliar(provedor, casos, system_prompt)
    for r in resultados:
        print(f"{'PASSOU' if r['passou'] else 'FALHOU'}  {r['id']}" + ("" if r["passou"] else "  — " + "; ".join(r["motivos"])))
    ok = sum(r["passou"] for r in resultados)
    print(f"\n{ok}/{len(resultados)} casos passaram (provedor={getattr(provedor, 'nome', '?')}, modelo={args.modelo or '-'})")
    return 0 if ok == len(resultados) else 1


if __name__ == "__main__":
    raise SystemExit(main())
