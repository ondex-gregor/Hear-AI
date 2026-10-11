# Speech models: `small` and `medium` (T46)

Hear AI transcribes **locally** with faster-whisper. Two models always appear in Configurações › Voz › Modelo (Settings › Voice › Model):

| Model | Status | What for |
|---|---|---|
| `small` | **default**, already downloaded (`models/faster-whisper-small`) | fast and light |
| `medium` | **optional, not downloaded yet** | tends to make fewer mistakes, but is slower and uses more graphics-card memory |

> **We do not promise that `medium` is faster.** The reason it exists is to offer a potentially more accurate
> alternative. The window text says exactly that: "O ‘medium’ tende a errar menos, mas é mais lento e usa
> mais memória da placa de vídeo." (The ‘medium’ tends to make fewer mistakes, but is slower and uses more graphics-card memory.)

## Sizes

| Model | `model.bin` | Whole folder | Source |
|---|---|---|---|
| `small` | 483,546,902 bytes (≈ 484 MB) | ≈ 486 MB | local file in `models/` (read, not changed) |
| `medium` | **1,527,906,378 bytes** (≈ 1.53 GB) | **1,530,575,217 bytes ≈ 1.53 GB (1.43 GiB)** | public metadata of the `Systran/faster-whisper-medium` repository on Hugging Face, queried on 2026-10-10 (only the file list and sizes; **nothing was downloaded**) |

The other `medium` files (`config.json`, `tokenizer.json`, `vocabulary.txt`) add up to ~2.7 MB. The app requires
**10% more** free disk space than what is left to download (≈ 1.7 GB for `medium`) and says "Sem espaço em
disco: o modelo precisa de cerca de 1,5 GB livres." (Not enough disk space: the model needs about 1.5 GB free.) without even starting.

## Download policy (decided by the user)

**The app never downloads anything by itself.** The only network use is the **"Baixar agora (~1,5 GB)"** (Download now) button, clicked by you:

1. Configurações › Voz › Modelo › choose **medium — não baixado (~1,5 GB)** (not downloaded). The window explains and shows the button.
2. "Baixar agora" asks for confirmation **inside the window**: *"Isto vai baixar ~1,5 GB da internet. Continuar?"* (This will download ~1.5 GB from the internet. Continue?) (**Sim, baixar** / **Agora não**).
3. A progress bar (measured by the folder size), **Cancelar download** and, if it fails, a simple sentence
   ("Sem conexão com a internet…", "Sem espaço em disco…", "Sem permissão…") with **Tentar de novo** (Try again), which
   **continues from where it stopped**.
4. When it finishes, the label becomes just "medium" and the window says "Modelo ‘medium’ baixado. Clique em Salvar para usá-lo." (Model ‘medium’ downloaded. Click Save to use it.)

Choosing a model that is not downloaded **does not write** `config.json` (Save refuses, with an explanation). Opening the window,
changing the selection or reopening it **downloads nothing**; only the confirmation click does.

The download runs in a child process (`huggingface_hub.snapshot_download`), which the app can really terminate on
cancel. The child's `stderr` is drained and `huggingface_hub`'s progress bars are turned off (without that, the pipe
buffer filled up in a few minutes and the download hung).

**From the terminal** (alternative, same network, without the window):

```
.venv\Scripts\python.exe scripts\baixar_modelo.py medium
```

## Switching models without restarting

Choose the model (already downloaded) and click **Salvar** (Save):

- `QtUi` loads the new one **in the background** (the bubble shows "loading"). Dictation keeps using the current model
  until the new one is ready; the switch happens between two transcription chunks.
- **During recording or transcription the switch is postponed** ("será carregado assim que o ditado terminar" — will be loaded as soon as the dictation ends) and done by itself
  when the app becomes idle again.
- **If the new one does not load** (missing/corrupt file, out of memory, CUDA), the previous model stays in effect,
  you get a notice in the window, on the bubble and in the tray ("Não consegui carregar o modelo ‘medium’ (faltou memória).
  Continuo com o ‘small’." — I could not load the ‘medium’ model (out of memory). Continuing with ‘small’.), and `config.json` **goes back to the model that was working** (so the choice can be
  repeated later, for example when memory frees up).
- **Going back to `small` is the same operation**, without restarting the app or the computer.
- The chunk flow, the bubble, the history, pasting and the vocabulary do not change: it is the same `Transcriber` object,
  only the internal model is swapped.

**At startup:** if the `config.json` model does not exist on disk (e.g., `medium` deleted), the app **does not die**: it uses
`small`, warns (log, bubble, tray balloon and Settings footer) and **does not rewrite `config.json`**.
When the file comes back, just save again.

## CPU / GPU (RTX 5060, 8 GB)

`device=auto` uses the graphics card (float16) if there is one, with a **fallback to CPU/int8** if loading or
warm-up fails (for example, out of memory on the card). The fallback also applies when switching models. If the switch
ends up on the processor, the window warns ("Está rodando no processador (mais lento)" — Running on the processor (slower)).

When switching, the new model is loaded **next to** the current one; for a moment both take memory. With 8 GB that fits
comfortably for `small` ⇄ `medium`, but if it runs short, the failure takes the safe path above.

## Coexistence with Mode B's local LLM (VRAM)

Mode B uses a local LLM (Ollama) besides Whisper. The two compete for the card's 8 GB:

- Measured today (RTX 5060, 8151 MiB, with ~2.4 GB already in use by other programs, including Hear AI and Windows):
  `small` on the GPU costs **≈ +740 MiB** of VRAM (estimated from the card's total).
- **`medium` NOT measured** (it is not downloaded). Reference by construction: the float16 weights alone are ≈ 1.5 GB, so
  expect well more than `small`; the real number is pending the measurement below.
- A quantized ~8B Ollama model usually takes several GB; **medium + that LLM + what Windows already uses may
  not fit in 8 GB.** The generator uses a short `keep_alive` (frees the LLM's VRAM after ~2 min), and the app falls back to
  the CPU if Whisper does not fit. If Mode B becomes slow or unstable with `medium`, go back to `small` or run Whisper
  on the CPU (Configurações › Processar no).

## Measurement (scripts/medir_modelo.py)

```
set HEAR_AI_MODELS_DIR=<project folder>\models      (models folder; only READ)
.venv\Scripts\python.exe scripts\medir_modelo.py          (writes a measurement report)
```

Each (model, device) runs in a child process (isolated memory/VRAM). It measures load time (with the GPU warm-up,
as in the app), the time of 3 runs per short fixture (`tests/fixtures`), process memory (working set, via the Windows
API, without psutil) and total VRAM through `nvidia-smi` before/after. A missing model shows up as
**"NÃO medido: modelo não baixado"** (NOT measured: model not downloaded); the script never downloads anything.

### Real result for `small` (2026-10-10, RTX 5060)

| | GPU (`auto` → cuda, float16) | CPU (int8) |
|---|---|---|
| Load (with warm-up) | 1.20 s | 0.97 s |
| Process memory (working set) | 31 → 689 MB (**+659 MB**; peak 756 MB) | 31 → 366 MB (**+336 MB**; peak 696 MB) |
| Card's total VRAM | 2417 → 3154 MiB (**+737 MiB**, estimate*) | no relevant card use |
| `frase_pt.wav` (3.8 s of audio), median | 0.14 s (0.04× the audio) | 3.87 s (1.03×; ranged from 1.53 to 5.85 s) |
| `frase_en.wav` (3.1 s) | 0.14 s (0.04×) | 4.20 s (1.36×) |
| `nomes_pt.wav` (17.8 s) | 0.56 s (0.03×) | 5.57 s (0.31×) |
| Word accuracy** | 75% / 88% / 72% | 75% / 88% / 72% |

\* The card's total includes other open programs (Hear AI in use, Windows): the difference is an estimate.
In the CPU measurement the total dropped at the end because another program freed memory.
\** A simple metric (fraction of the reference words that appear in order) over the short, synthetic fixtures;
it only serves to compare `small` × `medium` later, it is not an official error rate. CPU timings depend on PC load.

Honest reading: on the GPU `small` transcribes ~25–35× faster than real time; on the CPU, short phrases take about
as long as the phrase itself. `medium` is bigger and tends to be slower in both cases, but this **needs to be measured**.

### Pending: measure `medium`

1. Download `medium` with the button (or the command above), when it makes sense.
2. Run `scripts\medir_modelo.py` (with `HEAR_AI_MODELS_DIR` pointing to the models folder): the report will then
   include load, VRAM, memory and timings of `medium` on the GPU and on the CPU.
3. With the result, update this page (the table above and the Mode B section).

## Tests

`tests/test_modelo.py` uses **fake** model factories and downloaders and `tmp_path` (never the real `models/` nor the
user's `config.json`). An automatic guard makes any `socket.connect` or `subprocess.Popen` without injection
fail the test, so there is no network in the tests. The only two real processes are **local** `python -c` ones opened by the
test itself (huge stderr without hanging; cancel terminates only the child we opened).
`python tests\test_modelo.py` regenerates the `config-modelo*.png` captures.
