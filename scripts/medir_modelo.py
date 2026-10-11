"""Mede, por modelo Whisper DISPONÍVEL LOCALMENTE, o tempo de carga, o tempo para transcrever as fixtures curtas
de tests/fixtures, a memória do processo e (se houver `nvidia-smi`) a VRAM antes/depois. NUNCA baixa nada: um modelo
que não está na pasta aparece como "NÃO medido: modelo não baixado".

Uso:
    .venv\\Scripts\\python.exe scripts\\medir_modelo.py [--saida evidencias\\modelos-medicao.txt]
    set HEAR_AI_MODELS_DIR=<pasta do projeto>\\models    (pasta de modelos; só é LIDA; padrão: models/ do projeto)

Cada (modelo, dispositivo) roda num processo filho próprio (`--um nome dispositivo`), para a memória e a VRAM de um
não contaminarem o outro. Dispositivos: "auto" (usa a GPU se houver; o relatório diz o que foi realmente usado) e
"cpu" (int8). A VRAM é o total usado da placa (nvidia-smi) antes/depois: inclui outros programas abertos no PC
(ex.: o próprio Hear AI em uso), então é uma estimativa; o relatório avisa disso.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import subprocess
import sys
import time
import unicodedata
import wave
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "tests" / "fixtures"
CURTAS = ("frase_pt", "frase_en", "nomes_pt")
EXECUCOES = 3
MODELOS_SEMPRE = ("small", "medium")
IDIOMA = {"frase_pt": "pt", "frase_en": "en", "nomes_pt": "pt"}


def models_dir() -> Path:
    return Path(os.environ.get("HEAR_AI_MODELS_DIR") or ROOT / "models")


def modelo_presente(nome: str) -> bool:
    return (models_dir() / f"faster-whisper-{nome}" / "model.bin").exists()


def modelos_presentes() -> list[str]:
    pasta = models_dir()
    achados = sorted(p.name.removeprefix("faster-whisper-") for p in pasta.glob("faster-whisper-*")
                     if (p / "model.bin").exists()) if pasta.is_dir() else []
    ordem = ["tiny", "base", "small", "medium", "large-v2", "large-v3"]
    return sorted(achados, key=lambda n: (ordem.index(n) if n in ordem else len(ordem), n))


# --- medidas do ambiente -------------------------------------------------------------------------------------

def memoria_processo() -> dict:
    """Working set atual e de pico (MB) do PRÓPRIO processo, pela API do Windows (sem psutil)."""
    if sys.platform != "win32":
        return {}

    class Contadores(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    c = Contadores()
    c.cb = ctypes.sizeof(c)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.windll.psapi
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Contadores), ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return {}
    mb = 1024 * 1024
    return {"rss_mb": c.WorkingSetSize / mb, "pico_mb": c.PeakWorkingSetSize / mb, "privada_mb": c.PrivateUsage / mb}


def _nvidia(args: list[str]) -> str | None:
    try:
        r = subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def vram_usada_mib() -> int | None:
    """Total de VRAM usada na placa (todos os processos), em MiB; None se não há nvidia-smi."""
    out = _nvidia(["--query-gpu=memory.used", "--format=csv,noheader,nounits"])
    try:
        return int(out.splitlines()[0]) if out else None
    except (ValueError, IndexError):
        return None


def vram_deste_processo_mib() -> int | None:
    """VRAM só deste processo, se o driver informar (no Windows/WDDM costuma vir "N/A")."""
    out = _nvidia(["--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"])
    for linha in (out or "").splitlines():
        partes = [p.strip() for p in linha.split(",")]
        if len(partes) == 2 and partes[0] == str(os.getpid()) and partes[1].isdigit():
            return int(partes[1])
    return None


def info_gpu() -> str:
    out = _nvidia(["--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"])
    return out.splitlines()[0] if out else "sem nvidia-smi (sem GPU NVIDIA detectada)"


# --- uma medição (processo filho) -----------------------------------------------------------------------------

def ler_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as w:
        dados = w.readframes(w.getnframes())
        return np.frombuffer(dados, dtype=np.int16).astype(np.float32) / 32768.0, w.getframerate()


def _palavras(texto: str) -> list[str]:
    texto = unicodedata.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", texto)


def acerto_de_palavras(esperado: str, obtido: str) -> float:
    """Fração das palavras esperadas que aparecem, em ordem, no texto obtido (0..1)."""
    ref, hip = _palavras(esperado), _palavras(obtido)
    if not ref:
        return 1.0
    casadas = sum(b.size for b in SequenceMatcher(None, ref, hip, autojunk=False).get_matching_blocks())
    return casadas / len(ref)


def medir_um(nome: str, dispositivo: str) -> dict:
    import numpy as np

    from hear_ai.transcriber import Transcriber

    res: dict = {"modelo": nome, "pedido": dispositivo}
    antes_mem, antes_vram = memoria_processo(), vram_usada_mib()
    t = Transcriber(nome, models_dir(), device=dispositivo, compute_type="int8", language="pt")
    t0 = time.perf_counter()
    t.load()  # inclui o aquecimento da GPU, como no app
    res["carga_s"] = time.perf_counter() - t0
    res["usado"] = t.device_used
    depois_mem, depois_vram = memoria_processo(), vram_usada_mib()
    res.update(mem_antes=antes_mem, mem_depois=depois_mem, vram_antes=antes_vram, vram_depois=depois_vram,
               vram_processo=vram_deste_processo_mib())
    res["fixtures"] = {}
    for fx in CURTAS:
        wav = FIXTURES / f"{fx}.wav"
        if not wav.exists():
            continue
        audio, taxa = ler_wav(wav)
        t.language = IDIOMA[fx]
        tempos, texto = [], ""
        for _ in range(EXECUCOES):
            t1 = time.perf_counter()
            texto = t.transcribe(np.asarray(audio, dtype=np.float32))
            tempos.append(time.perf_counter() - t1)
        ref = (FIXTURES / f"{fx}.txt").read_text(encoding="utf-8") if (FIXTURES / f"{fx}.txt").exists() else ""
        res["fixtures"][fx] = {"duracao_s": len(audio) / taxa, "tempos_s": tempos,
                               "acerto": acerto_de_palavras(ref, texto) if ref else None}
    res["mem_pos_transcricao"] = memoria_processo()
    res["vram_pos_transcricao"] = vram_usada_mib()
    return res


# --- relatório ------------------------------------------------------------------------------------------------

def _mediana(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def formatar(res: dict) -> list[str]:
    linhas = [f"== {res['modelo']} | dispositivo pedido: {res['pedido']} | usado: {res.get('usado', '?')}"]
    if "erro" in res:
        return linhas + [f"   FALHOU ({res['erro']})"]
    linhas.append(f"   carga (inclui aquecimento): {res['carga_s']:.2f} s")
    ma, md, mp = res.get("mem_antes") or {}, res.get("mem_depois") or {}, res.get("mem_pos_transcricao") or {}
    if md:
        linhas.append(f"   memória do processo (working set): antes {ma.get('rss_mb', 0):.0f} MB -> depois da carga "
                      f"{md['rss_mb']:.0f} MB -> depois de transcrever {mp.get('rss_mb', 0):.0f} MB "
                      f"(pico {mp.get('pico_mb', md['pico_mb']):.0f} MB; diferença da carga +{md['rss_mb'] - ma.get('rss_mb', 0):.0f} MB)")
    va, vd, vp = res.get("vram_antes"), res.get("vram_depois"), res.get("vram_pos_transcricao")
    if va is not None and vd is not None:
        so = res.get("vram_processo")
        linhas.append(f"   VRAM (total da placa, com outros programas): antes {va} MiB -> depois da carga {vd} MiB "
                      f"(diferença +{vd - va} MiB) -> depois de transcrever {vp} MiB"
                      + (f"; só este processo: {so} MiB" if so is not None else ""))
    else:
        linhas.append("   VRAM: não medida (sem nvidia-smi)")
    for fx, d in res["fixtures"].items():
        t = d["tempos_s"]
        med = _mediana(t)
        acerto = "" if d["acerto"] is None else f"; acerto de palavras {d['acerto'] * 100:.0f}%"
        linhas.append(f"   {fx}.wav ({d['duracao_s']:.1f} s de áudio): "
                      + " / ".join(f"{x:.2f}" for x in t) + f" s  (mediana {med:.2f} s = {med / d['duracao_s']:.2f}x o tempo "
                      f"do áudio{acerto})")
    return linhas


def cabecalho() -> list[str]:
    try:
        import ctranslate2
        import faster_whisper

        libs = f"ctranslate2 {ctranslate2.__version__}, faster-whisper {faster_whisper.__version__}"
        cuda = ctranslate2.get_cuda_device_count()
    except Exception as e:  # noqa: BLE001
        libs, cuda = f"bibliotecas indisponíveis ({type(e).__name__})", 0
    fixtures = ", ".join(f"{fx}.wav" for fx in CURTAS if (FIXTURES / f"{fx}.wav").exists())
    return [
        f"Medição de modelos Whisper (T46) — {datetime.now():%Y-%m-%d %H:%M}",
        f"Sistema: {platform.platform()} | Python {platform.python_version()} | {libs} | CUDA visível: {cuda}",
        f"GPU: {info_gpu()}",
        f"Pasta de modelos (somente leitura): {models_dir()}",
        f"Fixtures curtas: {fixtures}; {EXECUCOES} execuções cada (a 1ª já com o modelo aquecido).",
        "Observações: a VRAM é o total da placa (nvidia-smi) e inclui outros programas abertos no PC (ex.: o próprio",
        "Hear AI em uso); a diferença antes/depois da carga é uma ESTIMATIVA. Tempos de CPU dependem do uso do PC",
        "no momento. Nada foi baixado por este script.",
        "",
    ]


def medir_tudo(saida: Path) -> int:
    linhas = cabecalho()
    try:
        import ctranslate2

        usa_gpu = ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        usa_gpu = False
    dispositivos = (["auto"] if usa_gpu else []) + ["cpu"]
    nomes = list(dict.fromkeys([*MODELOS_SEMPRE, *modelos_presentes()]))
    for nome in nomes:
        if not modelo_presente(nome):
            linhas += [f"== {nome}", "   NÃO medido: modelo não baixado "
                       f"(procurado em {models_dir() / f'faster-whisper-{nome}'}; nada foi baixado por este script)", ""]
            continue
        for disp in dispositivos:
            print(f"medindo {nome} ({disp})…", flush=True)
            r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--um", nome, disp],
                               capture_output=True, text=True, timeout=1800,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            ultima = next((ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("{")), None)
            if r.returncode == 0 and ultima:
                res = json.loads(ultima)
            else:
                res = {"modelo": nome, "pedido": disp, "erro": f"código {r.returncode}: " + (r.stderr.strip().splitlines() or ["?"])[-1][:160],
                       "fixtures": {}}
            linhas += formatar(res) + [""]
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print("\n".join(linhas))
    print(f"salvo em {saida}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[1] == "--um":
        print(json.dumps(medir_um(argv[2], argv[3])))
        return 0
    saida = ROOT / "evidencias" / "modelos-medicao.txt"
    if "--saida" in argv:
        saida = Path(argv[argv.index("--saida") + 1])
    return medir_tudo(saida)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
