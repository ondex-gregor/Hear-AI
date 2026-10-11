# Hear AI robustness (D2)

What Hear AI does when something unusual happens in the middle of a dictation. The rule behind every decision: **what you said must never be lost**, and when in doubt the text is kept (history + clipboard) instead of being pasted in the wrong place.

## 1. Microphone unplugged or device changed during recording
**What happens:**
1. A "watchdog" checks the microphone several times per second while recording. It notices a drop in two ways: the audio channel died (e.g., you pulled the USB cable, Bluetooth dropped) or audio stopped arriving for more than 1 second (stuck driver).
2. The bubble shows **"Microfone desconectado — reconectando…"** (Microphone disconnected — reconnecting…).
3. Hear AI tries to open again on the **current Windows default microphone** (whichever is marked as default now; if you switched headsets, it is the new one). Up to **3 attempts, half a second apart**.
4. If it comes back, the bubble shows **"Microfone reconectado"** (Microphone reconnected) and the recording continues, stitched together.
5. If it does not come back, the recording **ends and is transcribed with everything already recorded**, with the notice "Microfone não voltou — transcrevendo o que já foi gravado" (Microphone did not come back — transcribing what was already recorded). Nothing is discarded.

**Why it is safe:**
- The audio already recorded is kept in memory and also in the temporary file on disk (since T12).
- The drop erases neither of them, and the recording recovery from Phase A still applies if the app closes.

**Limits:**
- During reconnection (up to ~1.5 s), whatever you say is not recorded: there is no microphone.
- If you chose a specific microphone in Settings and it disappeared, reconnection uses the Windows default, so you are not left muted.

## 2. Target window opened as administrator
**The problem:** Windows does not let a regular program send keystrokes (Ctrl+V) to a window opened "as administrator". The Ctrl+V would simply not arrive.

**What happens:**
- Before pasting, Hear AI checks whether the foreground window belongs to an administrator program. When Windows does not even allow the check ("access denied"), it counts as administrator.
- If so, it **does not try to paste**. The text stays **on the clipboard and in the history**, and the bubble warns **"Texto salvo — Ctrl+V"** (Text saved — Ctrl+V): just press Ctrl+V.
- If Hear AI itself is running as administrator, it pastes normally.

## 3. Switching windows during transcription (decision)
**The situation:** you dictate in the editor, release the hotkey and, while the text is being transcribed, click on another window (e.g., a chat).

**Decision: do not paste.** Hear AI remembers which window was in the foreground when you **started recording**. If, at paste time, the foreground window is **another one**, it does not paste: the text stays on the **clipboard and in the history**, and the bubble warns **"Texto salvo — Ctrl+V"**.

**Why:**
- Pasting in the wrong place can be worse than not pasting (e.g., sending a personal text into a work chat).
- With the text kept, you just go back to the right window and press Ctrl+V.

**Details:**
- If you return to the same window before the transcription finishes, it pastes normally.
- If the window cannot be determined (e.g., the desktop, a locked screen), it pastes as before.

## 4. Multiple monitors and scale changes (DPI)
**What happens:** when a monitor is disconnected or connected, or the resolution/scale (100%, 125%, 150%…) changes, Hear AI checks that the bubble is still **entirely inside some visible screen**.
- If it is, it does nothing.
- If not, it puts it back at the position you saved. If that position no longer fits either, it goes to the default: **center, bottom, on the main screen**.

## How each thing was tested
Without actually unplugging anything and without pressing keys (`tests/test_robustez.py`):
- **Microphone:** a "fake microphone" that dies in the middle. The tests cover:
  - it comes back and the audio is stitched (memory and disk are identical);
  - silence that lasts too long also counts as a drop;
  - 3 failed attempts → ends and transcribes;
  - the 2nd attempt works;
  - stopping during reconnection does not hang.
- **Windows:**
  - a fake "window list": same window pastes; changed window does not paste; admin window does not paste; if Hear AI is also admin, it pastes; unknown window pastes;
  - real administrator detection was checked **by reading only** (the system process `csrss` → treated as administrator; the test itself → not).
- **Screens:** fake screens (a monitor that vanished, a smaller area) and Qt's DPI/geometry signals fired in the tests.
