# Two operating modes: A (faithful) and B (prompt generator) — interface (T47b P4)

| | Mode A — Faithful transcription (**default**) | Mode B — Prompt generator |
|---|---|---|
| Flow | speech → local Whisper → vocabulary → voice commands → paste | speech → local Whisper → vocabulary → **generator (LLM)** → paste |
| Network | **none** | only the generator: Ollama on `127.0.0.1` (default) or the cloud you accepted |
| If it fails | — | **pastes the faithful transcript** and warns; the speech is never lost |

> **State of the author's machine (2026-10-10): Ollama is NOT installed**, so Mode B today pastes the faithful transcript and the
> interface says so ("indisponível: gerador local não encontrado" — unavailable: local generator not found). **The quality of the generated prompts was NOT measured
> (no local LLM).** Everything below was tested with a fake provider.

## How to switch modes

- **Configurações › Modo de operação › Operação** (Settings › Operating mode › Operation) (persisted in `operation_mode`).
- **Tray and the bubble's right button:** "Operação do ditado:" (Dictation operation:) with **✓ Transcrição fiel** (faithful transcription) / **Gerador de prompts** (prompt generator) (marks the current one).
- **Global hotkey `mode_hotkey`** (default `Ctrl+Alt+M`): toggles A ⇄ B; the bubble shows "Modo: …" (Mode: …) for ~1.5 s.
  The hotkey is a second `RegisterHotKey` (its own id). **Conflict** (it is the same as the dictation one or another program already uses it) →
  it does not register, warns (tray balloon + Settings footer) and **the rest of the app works**.
- The choice made through the tray/hotkey is saved in `config.json` (it is an explicit action); switching applies from the next dictation.
- The Controller's `prompt_step` only exists in Mode B: in `__main__` it is built **only with `operation_mode == "prompt"`**;
  switching modes later turns it on/off in real time (`modos.apply_operation_mode`).

## What the bubble shows

- **Mode B badge:** a **violet** disc with a glow at the corner of the isotype, at rest and while recording. **Amber** when the generator is
  a **cloud** one (and consent exists): each use sends the TEXT out.
- **"Gerando prompt…"** (Generating prompt…) (pill with the spinning ring) after transcribing; in the cloud, **"Gerando prompt… · nuvem"**.
- **Fallback:** "gerador indisponível — colei a transcrição" (generator unavailable — I pasted the transcript) (two lines, ~2.5 s, next to the pasted ✓).
- **Mode switch:** pill "Modo: Gerador de prompts" / "Modo: Transcrição fiel" for ~1.5 s. Turning Mode B on without an available generator
  then shows "Gerador indisponível — vou colar a transcrição" (Generator unavailable — I will paste the transcript).

## Honest provider availability (Settings)

The status box queries the provider **on a thread** (never freezes the window) and shows:

- Ollama down: **"indisponível: gerador local não encontrado"** + what to do; and the notice *"Sem o gerador disponível, o Modo B cola a transcrição fiel da sua fala."* (Without the generator available, Mode B pastes the faithful transcript of your speech.)
  Choosing Mode B like this **is allowed**.
- Ollama up: "encontrado, modelos: a, b, c; usando ‘x’" (found, models: a, b, c; using ‘x’) (a real list, coming from `/api/tags`; pick the model in the box).
  No installed models / configured model missing / none chosen: dedicated sentences.
- Ollama URL **127.0.0.1/localhost only** (any other host is refused); no redirects.
- Cloud: the status comes only from what is filled in (URL, model, variable name, whether the variable exists, whether consent exists). **The window
  never "tests" the cloud connection.**

**Installing Ollama (not done by Hear AI):** `ollama.com/download`; then, in the terminal, for example
`ollama pull qwen3:8b` (**5.2 GB**), `ollama pull llama3.1:8b` (**4.9 GB**) or, for little memory, `ollama pull qwen3:4b`
(**2.5 GB**). Sizes checked in the official catalog (`ollama.com/library/<model>/tags`) on 2026-10-10; confirm again before
downloading. Then click "Verificar de novo" (Check again).

## Cloud (optional, requires consent)

Choosing "Nuvem (compatível com OpenAI)" (Cloud, OpenAI-compatible) asks for a **URL** (https; loopback may be http), a **model** and the **NAME of the environment
variable** that holds the key. **The key is never typed, stored in config, log, history or Git**: it is read from the variable on every
call. Before the 1st use, the confirmation appears **inside the window** (never a MessageBox):

> *This mode sends the TEXT of your speech (never the audio), the active application's name and your vocabulary to `<host>`.*

Only **"Aceito"** (I accept) writes `cloud_consent = true` **together with the accepted host in `cloud_consent_host`** (lowercase hostname, no port or
path); **saving Mode B in the cloud without consent is refused**. The consent is valid **only for that host**, on **every** path:
`build_provider`/`make_prompt_step` only create/call the cloud if `cloud_consent` is true **and** the current host of `cloud_base_url`
equals `cloud_consent_host`. So **editing `cloud_base_url` directly in `config.json` (outside the window) invalidates the consent**;
an **old** config with `cloud_consent: true` and no host is treated as **pending**. Pending consent = Mode B does **not even call** the
provider, pastes the transcript, the amber badge does not show up and the window asks for consent again ("o aceite foi dado para ‘x’; aceite de
novo para ‘y’" — consent was given for ‘x’; accept again for ‘y’). Changing only the path, the port or the case of the URL (same host) does not ask again.

## What the generator receives and what is kept

- Input: the **faithful** transcript (after the vocabulary) inside `<transcricao>`, plus `<destino>`, `<vocabulario>` and, only if
  enabled, `<aplicativo_ativo>` and `<preferencias_permanentes>`. Markup coming from the speech is neutralized (spoken text is data,
  never instruction); assembly in `gerador.py`.
- **Application context** (`generator_app_context`, off by default): only the **process name** (never the window title),
  mapped to a destination (e.g., terminals/editors → `agente_de_codigo`) by `app_context.py` (P3). When off, Win32 is **not even
  queried**. If the P3 module does not exist yet, the context is simply empty.
- **System prompt:** the project's `prompts/gerador-de-prompts.md`; **the copy in the `config.json` folder takes precedence**.
  **Permanent preferences** (`preferencias-permanentes.md`, lines "- …") only with `generator_permanent_prefs = true`.
- **History (Mode B):** `text` = what was pasted; `raw` = the faithful transcript; `generated` = the prompt (if any); `revisar` (review) =
  the fidelity check (no LLM) did not find some number/path/URL/name from the speech in the prompt. The "revisar" mark **does not
  block** pasting.
- **Recentes:** entries with a generated prompt show **"Usar o original"** (Use the original) and **"Usar o prompt gerado"** (Use the generated prompt) (they only **copy** to the clipboard;
  they do not paste by themselves) and the **● revisar** mark when applicable.

## Failures: always pastes the faithful transcript

Ollama down, timeout (`generator_timeout_s`, 5–300 s, default 60), empty response, `[[SEM_PEDIDO]]`, cloud without consent, invalid
URL, missing system prompt or an unexpected error in the step → **the exact transcript is pasted**, the bubble warns and the history
keeps the speech. The generator runs **on a worker thread** (`Controller._process`), never on the Qt thread.

## VRAM (RTX 5060, 8 GB) — **not measured without Ollama**

Whisper `medium` (float16 weights ≈ 1.5 GB, plus working memory) **and** a quantized ~8B LLM (the download alone is 4.9–5.2 GB,
not counting the context cache) **may not fit together in 8 GB**. Ollama uses a short `keep_alive` (2 min), so it frees the LLM's VRAM
after a while idle; Windows and other programs also take some (we measured ~2.4 GB in use in T46). **Recommendation for
Mode B: Whisper `small`** (≈ +740 MiB of VRAM, measured in T46) and, if memory runs short, a smaller model (`qwen3:4b`, 2.5 GB) or
Whisper on the CPU. None of this was measured with a real Ollama: once it is installed, it is worth measuring (`scripts/medir_modelo.py` +
`nvidia-smi` during a dictation in Mode B).

## Tests

`tests/test_modos.py` (fake `http` provider, **network guard** that makes any `socket.connect` fail the test, `tmp_path`, real Controller):
Mode A without generator and without network; Mode B success pastes the generated text (off the Qt thread); each kind of failure pastes the exact
transcript and warns; `[[SEM_PEDIDO]]`; history with raw/generated/revisar; cloud without consent does not call; key never in
config/log/history (sentinel); app context off does not query Win32; prompt/preferences (precedence); provider
state (Ollama down/up/no model, loopback only, cloud only from the config); mode hotkey (registers, conflict, invalid);
tray, hotkey and bubble; Settings (persistence, consent inside the window, validates hotkey/variable, content fits the
width); Recentes; `__main__`. `python tests\test_modos.py` regenerates the captures `modos-config.png`, `modos-config-nuvem.png`,
`modos-recentes.png`, `bolha-modo-b.png` (A, B, B-cloud at rest and B recording) and `bolha-gerando.png` (generating, generating in the cloud,
fallback, switch notice).

## Not tested / pending

- **Quality of the prompts** and the generator's real latency: without a local LLM, nothing was measured.
- Real Ollama (the connection to `127.0.0.1:11434`) and the real cloud: only with a fake `http`.
- The mode hotkey and the tray balloon on real Windows (offscreen does not show the balloon; the Win32 registration was tested with a fake API).
- The application context comes from P3 (`app_context.py`); here it is only exercised with a fake module.
