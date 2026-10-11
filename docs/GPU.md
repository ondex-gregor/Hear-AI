# GPU (NVIDIA graphics card) in Hear AI

**Result: IT WORKS** on the RTX 5060 (Blackwell), with ctranslate2 4.8.2 and faster-whisper 1.2.1. The default in `config.json` is still `"device": "cpu"`; to use the GPU, change it to `"auto"` (or `"cuda"`).

## What was tried

1. **Without installing anything.** ctranslate2 already sees the card (`get_cuda_device_count() = 1`) and the model even loads on the GPU, but the first transcription fails with:
   `Library cublas64_12.dll is not found or cannot be loaded`.
   In other words, only NVIDIA's math library (cuBLAS 12) was missing. The CUDA 13.2 installed on the machine does not help, because ctranslate2 asks for version 12.
2. **Install just two pip packages inside the project's `.venv`** (nothing global):
   - `nvidia-cublas-cu12` 12.9.2.10 (553 MB file)
   - `nvidia-cuda-nvrtc-cu12` 12.9.86 (76 MB file; cuBLAS requires it)
3. **cuDNN was not needed.** `nvidia-cudnn-cu12` (743 MB) was not installed: ctranslate2's Whisper worked without it. Only install it if an error mentioning `cudnn` ever appears.

### Size
| Item | Size |
|---|---|
| Downloaded files (2 packages) | 629 MB |
| Installed in `.venv\Lib\site-packages\nvidia` | 915 MB |
| Whole `.venv` before → after | 324 MB → 1239 MB |

This stays below the 1.5 GB limit agreed for this project. With cuDNN it would go over (+743 MB of files), which is why it was left out.

### How to install (once, with internet)
```
"<project folder>\.venv\Scripts\python.exe" -m pip install nvidia-cublas-cu12 nvidia-cuda-nvrtc-cu12
```
After that the app works offline, as always. The packages are deliberately kept out of `requirements.txt`: whoever has no NVIDIA card does not need them.

## How it works in the code (`transcriber.py`)
- `device = "cpu"`: as before (int8).
- `device = "auto"`: uses the GPU if ctranslate2 sees one; otherwise CPU.
- `device = "cuda"`: tries the GPU (`float16`); if it fails, falls back to CPU.
- **Automatic fallback to CPU (int8)** if the GPU fails while loading, during warm-up or during transcription. The reason goes to the log (`logs\hear-ai.log`, never the dictated text) and `device_used` shows the real device (`"cuda"` or `"cpu"`).
- The app finds the cuBLAS DLLs in `site-packages\nvidia\*\bin` by itself (no need to touch the Windows PATH).
- **Warm-up:** when loading the model on the GPU, the app runs a short transcription of silence. This prevents the DLL error from only showing up on the first dictation and pays the first-run cost on the card up front (see below). It happens in the background load, with the gray icon.
- `load()` has a lock: loading and the first dictation at the same time do not load the model twice.

## Timings (test phrase of ~3.8 s, `small` model, on the author's machine)
| | CPU (int8) | GPU (float16) |
|---|---|---|
| Model load (+ warm-up) | ~1 s | ~1.2 s (with a warm NVIDIA cache) |
| Transcribing the phrase | 1.5 to 2.8 s | **0.12 to 0.25 s** |

The GPU is **about 10 to 20 times faster** at transcription.

**Note on the first run:** the RTX 5060 is new and ctranslate2 4.8.2 ships no ready-made code for it; the driver compiles the kernels on the first run. We measured **35 s** the first time and **14 s** the second; after that the NVIDIA cache (`%APPDATA%\NVIDIA\ComputeCache`) brings loading down to ~1 s. If the cache is deleted or the driver is updated, the wait comes back once. Since the warm-up runs in the background load, the user only sees the gray icon for longer; no dictation is lost.

Failure-path test with the real GPU: hiding the DLLs (simulating "nvidia packages not installed"), the app loaded on the CPU by itself (`used: cpu`) and transcribed normally (~1.7 to 2.8 s).

## Tests
- Without a GPU, with a fake model: `pytest -q tests/test_transcriber_device.py` (fallback to CPU on load, on warm-up and on transcription; the `load` lock; DLL lookup).
- With the real GPU (optional): `set HEAR_AI_GPU_TESTS=1` and run the same command. It only runs if a CUDA GPU exists and the variable is set.

## Recommendation
If you use the GPU day to day, set `"device": "auto"` in `config.json` (the default in `config.py` is still `"cpu"`). Run the install command above first.
