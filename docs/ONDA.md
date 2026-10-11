# Bubble voice waveform (T41b)

While recording, the bubble shows 18 bars that move from right to left: each bar is **one sample of the microphone
level over the last ~0.6 s**. Silence = a row of 3 px dots; speech = bars that rise and fall with the words and the
pauses.

> **Not tested with a real microphone.** Everything below was verified with synthetic signals (simulated clock) and
> offscreen captures. The "speech" numbers come from **synthetic** speech (0.17 s syllables, −15 dB valleys,
> 0.22 s pauses between words). See "Manual test" at the end.

## How it works

```
Recorder._callback (audio thread)         writes a single float: Recorder._level = linear RMS of the last block (0..1)
        │  (no Qt here, nothing blocks the capture)
BubbleWidget._tick (QTimer, Qt thread, 16 ms)   reads level_source() and calls model.on_level(rms)
        │
BubbleModel.on_level(rms)                 only counts in RECORDING; passes (rms, relative time) to the visualizer
        │
VisualizadorNivel.amostra  (ui/nivel.py, no Qt)
        │   db    = 20·log10(rms + 1e-6)
        │   floor = 10th percentile of the dB of the last 2.5 s, smoothed (tau 1 s)       <- background noise
        │   ceil  = max(floor + 38 dB, slow peak decaying with tau 2 s)                   <- automatic gain
        │   v     = (db − floor) / (ceil − floor), 0..1;  below floor + 4 dB (gate) → 0
        │   asymmetric smoothing: attack 0.06 s, decay 0.2 s
        │   rolling history at 30 Hz, 18 positions (oldest on the left)
        ▼
Frame.bars (18 floats 0..1)  →  bubble._paint_bars:  height = 3 px + v · (max_h − 3 px),  max_h = 52 − 12 = 40 px
```

Because it depends only on the level *relative* to the recent floor and peak, the waveform works with a weak or a strong
microphone with no configuration. A consequence (on purpose): **a constant level, high or low, becomes dots** — the
waveform shows variation, not absolute volume.

### What `BubbleModel` does around the visualizer
- **Time relative to the recording.** The visualizer receives `now = clock − recording start`, never the absolute
  clock (see "Finding in `nivel.py`" below).
- **A new recording starts clean:** when entering RECORDING from another state, `viz.reset()` and the bars go to zero. The
  floor is relearned from scratch (keep this in mind in the manual test: speaking in the first ~0.3 s of the recording leaves the floor
  high until the first pause).
- **Microphone warm-up:** `Recorder.level()` is exactly `0.0` until the 1st audio block arrives. During the
  first `WAVE_WARMUP_S = 0.25 s` those zeros are ignored — treating them as measured silence would pin the floor at
  −114 dB and normal room noise (≈ −60 dB) would fill the bars at the start of every recording. A digital zero that lasts
  longer than that (muted mic/noise gate) counts as measured silence again.
- **Outside recording** (transcribing, done, error...): `on_level` is ignored and the `Frame` bars fall to
  zero at `BAR_DECAY = 2/s` (≤ 0.5 s). The bubble only draws bars while recording, so this only affects `needs_animation`.

### Threading and repaint
- Only the window's `QTimer` touches widgets. The audio callback still writes only `Recorder._level`.
- The timer stays at 16 ms (pill-opening animation, transcription ring), but `_tick` only calls `update()` when
  `(Frame, visual state)` changed. While recording, the waveform changes at 30 Hz and the stopwatch at 1 Hz → **≤ ~30 repaints/s**
  (test: `test_repaint_no_maximo_30_por_segundo_gravando`). At rest the timer stays stopped.

## Adjustable parameters

| What | Where | Current value |
|---|---|---|
| Number of bars | `BubbleModel.N_BARS` | 18 (fit in ~102 usable px: ~5.7 px per bar) |
| History rate | `BubbleModel.WAVE_HZ` | 30 Hz → 0.6 s window |
| Mic warm-up | `BubbleModel.WAVE_WARMUP_S` | 0.25 s |
| Fall-off outside recording | `BubbleModel.BAR_DECAY` | 2/s |
| Dynamic range (dB) | `VisualizadorNivel(faixa_db=…)` | 38 |
| Attack / decay | `VisualizadorNivel(ataque_s=…, decaimento_s=…)` | 0.06 s / 0.2 s |
| Floor window / gate | `VisualizadorNivel(janela_piso_s=…, margem_gate_db=…)` | 2.5 s / 4 dB |
| Minimum height / vertical margin | `bubble.BAR_MIN_H`, `bubble.BAR_V_MARGIN` | 3 px / 12 px (max bar 40 px) |
| Max bar width, gaps | `bubble.BAR_MAX_W`, `BAR_X_GAP`, `BAR_RIGHT_SPACE` | 4 px, 4 px, 62 px |

To change the visualizer parameters without editing `nivel.py`: `BubbleModel(clock=…, wave=VisualizadorNivel(…))`
(`BubbleWidget` builds the model by itself; to change the default, adjust the creation in `BubbleModel.__init__`).

## What the waveform shows (measured, synthetic signals)

Maximum bar height (px, max bar = 40), 1.5 s of room noise (RMS 0.0008) and then 1 s of the signal:

| Signal (constant RMS) | Height |
|---|---|
| silence / constant noise | 3.0 (all) |
| 0.005 | 18.5 |
| 0.02 | 30.2 |
| 0.1 | 40.0 |

A 0.4 s burst (RMS 0.1) followed by silence: the newest bar drops 40 → 25 → 17 → 11 → 8 → 6 px every 0.1 s; the
0.6 s history scrolls out and the pill goes back to dots ~1.6 s later (exponential tail < 0.5 px).

Captures generated by the tests (recording pill, 2x DPI; regenerate with
`.venv\Scripts\python.exe tests\test_onda_bolha.py`):

| File | Bar heights (px) |
|---|---|
| `silencio.png` | 3 (18 dots) |
| `fala-baixa.png` (RMS peak 0.01) | 8 – 21 |
| `fala-normal.png` (RMS peak 0.05) | 12 – 32 (≈ 60% of the pill height) |
| `fala-forte.png` (RMS peak 0.25) | 14 – 36 |
| `fala-normal-continua.png` (extra: back-to-back syllables, no pause) | 32 – 36 |

The "typical" captures are taken at 2.75 s of speech: the window catches a pause, a 1-syllable word, another pause and the
start of the next word.

### Known limits (by design of `VisualizadorNivel`)
1. **Ripple between syllables is subtle.** With a 0.2 s decay the smoother acts as a peak holder: continuous
   syllables (~5 Hz) vary only ~±3 px, and it is the **words and pauses** that draw the wave (see `fala-normal-continua.png`
   × `fala-normal.png`). Measured with continuous syllables and a −20 dB valley: decay 0.2 s → ripple 3 px;
   0.15 s → 4 px; 0.10 s → 6 px; 0.06 s → 9 px. Room noise oscillating ±3 dB stays at ≤ 7 px in all these cases.
   Lowering the decay makes syllables more visible; **I did not change the default** (the spec asks for 150–250 ms and there is no
   real microphone to judge) — a decision for the manual test.
2. **Normal × loud speech differ little** (≈ 4 px at the peaks): everything above `floor + 38 dB` saturates at 1.0 because of
   the automatic gain. Quiet × normal speech differ well (≈ 11 px).
3. Starting to speak in the first ~0.3 s of the recording (before the floor is learned) leaves the wave flattened until the
   first pause.

## Finding in `ui/nivel.py` (fixed later; kept as a record)
On the 1st call of `amostra`, `_proximo_push_t` is `0.0`; if `now` is a large absolute clock (e.g.,
`time.monotonic()` of a PC that has been on for days), the `while now >= self._proximo_push_t` pushes `now × rate_hz`
times. Measured: `now = 200 000 s` → 0.32 s and ~6 million iterations (≈ 1.4 s with 10 days of uptime), on the
interface thread, when starting to record. `BubbleModel` works around it by passing time relative to the recording (test
`test_relogio_monotonico_grande_nao_trava_a_interface`). Suggested fix in `nivel.py`: on the 1st sample, set
`_proximo_push_t = now` (and `reset()` does not need to zero it).

## Manual test (pending — needs a real microphone and someone speaking)
1. Open the app, record 5 s in silence: expected, still dots (no full bars in the first seconds).
2. Say a normal sentence with pauses: bars of ~30 px, valleys at the pauses, the wave moving to the left.
3. Speak quietly and then loudly: height goes up from quiet to normal; loud ≈ normal (saturates, see limit 2).
4. Turn on a constant fan/background music: after ~5 s the wave goes back to low by itself.
5. Release the recording and start another right away: the second starts clean.
6. Judge whether the syllables show up enough; if not, lower `decaimento_s` (limit 1).
7. Check the CPU of the recording bubble (expected negligible) and that dictation/pasting continue normal.
