# The Hear AI hotkey

## What caused the conflict
When pressing **Ctrl+Alt+Space**, besides Hear AI starting to record, **another interface opened**. Up to v1.0 Hear AI only *observed* the keyboard (a non-blocking "hook"): the combination kept reaching Windows and any other program that also used Ctrl+Alt+Space. Both fired together, and Hear AI had no way of knowing there was another owner.

## What was measured
Two ways of "taking ownership" of the hotkey were tested. No test pressed real keys in the user's session.

**1. Block the key inside the hook** (`suppress=True` in the `keyboard` library). With blocking, **every key on the computer** waits for the Python hook code to decide whether it passes. If that decision takes longer than ~300 ms, Windows silently turns the hook off.

To measure how long Python can take to respond, a Windows timer called a Python function every 10 ms while the program did other work:

| Parallel work | Maximum delay |
|---|---|
| none | 7 ms |
| numpy math | 14 ms |
| ordinary Python loop | 23 ms |
| large `json.dumps` (common in programs) | 224 ms |
| `sorted()` of 1 million items | **357 ms (12 times above 300 ms in 3 s)** |

Conclusion: **not safe**. A single heavy operation (sorting, serializing) holds Python and can freeze the whole computer's keyboard or silently turn the hotkey off.

**2. Register the hotkey with Windows** (`RegisterHotKey`). Windows then delivers the combination only to Hear AI and **tells you if another program already registered it**. Tests (the author's disposable experiments, outside this repository), always with the unlikely combination Ctrl+Alt+Shift+F23, unregistered right afterwards:
- register: ok, in 0.01 ms;
- register the same combination again, on another thread **or in another program**: refused with error 1409 ("already registered"). This is the conflict detection;
- the "hotkey pressed" notification reaches Hear AI's thread in 0.05 ms;
- reading a key's state (`GetAsyncKeyState`) costs 0.3 µs.

## What was chosen
**Option 2: `RegisterHotKey`** (`src/hear_ai/hotkey.py`).
- **Pressed:** Windows sends a `WM_HOTKEY` message to a Hear AI thread. The registration uses `MOD_NOREPEAT`, so holding the key does not repeat the event.
- **Released (hold mode):** another thread reads the keys every ~15 ms. When **any** key of the combination appears released in **2 consecutive readings**, the recording ends. The 2 readings avoid ending because of an isolated glitch.
- **Toggle mode:** each press turns recording on or off.
- **Conflict:** if Windows refuses the registration, Hear AI **installs nothing in its place**. It sets `conflict=True`, keeps the reason (for example, "the shortcut ctrl+alt+space is already in use by another program"), writes it to the log and calls `on_conflict(reason)`. The app uses this to play the error beep and show the notice (T17 integration).
- **Rejected combinations:**
  - the ones reserved by Windows (Win+L, Ctrl+Alt+Del, Alt+Tab, Alt+F4, Ctrl+Esc, Ctrl+Shift+Esc, Alt+Esc, Alt+Space…);
  - the ones without Ctrl, Alt or Win (Shift alone would get in the way of typing);
  - Win + a single key (Windows uses almost all of them);
  - Ctrl+Alt+Q/W/E/C/1–6, because on the Brazilian keyboard (ABNT2) Ctrl+Alt is AltGr and those keys type `/ ? ° ₢ ¹ ² ³ £ ¢ ¬`.

**Known limit:** conflicts can only be detected with programs that **register** the hotkey. A program that merely observes the keyboard with a hook still sees the combination: Windows calls the hooks before handling registered hotkeys. If the conflict continues even without a warning, change the hotkey.

## Recommended default: **Ctrl+Alt+H**
- "H" for Hear, and easy to hold with one hand.
- Not AltGr of anything on ABNT2 nor on the US layout.
- Not a Windows shortcut. Win+H is Windows voice typing, but Ctrl+Alt+H is not.
- Not a common shortcut of the most used programs (Office, browsers, VS Code).
- Discarded options:
  - Ctrl+Alt+Space: this is the reported conflict.
  - Alt+Space: window menu and PowerToys Run.
  - Ctrl+Shift+Space: non-breaking space in Word.
  - Win+H: Windows dictation.
  - F keys (F9–F12): uncomfortable on laptops with Fn.
- If Ctrl+Alt+H is taken on the computer, Hear AI warns on first use: that is the detected conflict.

The default in `config.py` today is `ctrl+alt+h` (the change was made after this study).

## How to change the hotkey
- **Today:** open the settings from the tray ("Abrir configurações" menu), change `"hotkey"` (example: `"ctrl+alt+h"`) and restart Hear AI.
  - Format: modifiers `ctrl`, `alt`, `shift`, `win` + **one** key: a letter, a number, `f1`–`f24`, `space`, `enter`, `tab`, `insert`, `delete`, `home`, `end`, `page up`, `page down`, arrows, `num 0`–`num 9`.
  - If the hotkey is invalid or reserved, Hear AI uses the default and logs the reason.
- **Settings screen:** the "Gravar novo atalho" (Record new hotkey) button. It uses `record_hotkey()`:
  - waits for you to press the combination, only observing the keyboard, blocking nothing;
  - rejects reserved combinations, explaining why;
  - Esc cancels;
  - the app then tries to register it and warns if there is a conflict.
