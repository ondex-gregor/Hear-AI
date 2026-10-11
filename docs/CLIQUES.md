# Bubble clicks (T42b)

## The rule

| Gesture on the bubble | App idle (`idle`) | Recording (`recording`) | Transcribing (`transcribing`) |
|---|---|---|---|
| **1 click** | **starts recording** (`Controller.begin`) | **stops** and moves on to transcription (`Controller.end`) | nothing |
| **2 quick clicks** (≤ the Windows double-click interval) | discards the recording the 1st click started **and opens Recentes** (Recents) | the 1st click stopped it; the 2nd **only opens Recentes** | only opens Recentes |
| **Drag** (≥ 4 px) | moves the bubble and saves the position; never records | same | same |
| **Right button** | opens the menu; never records | same | same |

The global hotkey (`Ctrl+Alt+H` by default, or the user's) stays the same and drives the **same** `Controller`:
you can start from the bubble and finish with the hotkey, or the other way round. `Ctrl+Alt+Space` did not come back.

Behavior change: **a single click no longer opens Recentes** (it did in v1.2). Recentes opens with a double
click or through the "Abrir histórico" (Open history) item of the tray and bubble menus.

## Why the 1st click records right away

Waiting ~500 ms to find out whether a 2nd click is coming would delay the start of recording and eat the first words. So the
1st click already starts recording; if the 2nd click arrives within the interval, the recording is cancelled and discarded.
Cost: on the "open Recentes" double click the bubble may flash "recording" for a fraction of a second before going back
to rest.

## Double click, step by step (app idle)

1. The 1st click is released → `ClickMachine` returns `START` → `QtUi._bubble_start`: reasserts the bubble (T40) and queues
   `Controller.begin()`. Recording starts.
2. The 2nd click is released ≤ `QApplication.doubleClickInterval()` after the 1st (measured release→release) → `DISCARD_AND_OPEN`
   → `QtUi._bubble_discard_and_open`: the bubble goes back to rest (`driver.cancelled()`), the global Esc is released, and
   `Controller.cancel()` is queued — **the same path as Esc**: stops the microphone, deletes the spool (`.pcm` + `.json`),
   does not transcribe, does not paste, does not save to the history. Then it opens Recentes.
3. An accidental 3rd click within the following window is swallowed (the machine ignores clicks for one more interval).

The interval is read from Windows **on every click** (the user can change it in *Mouse › Double-click speed* without
restarting the app).

Click to stop + extra click: the stop happens on the 1st click (`END`); the 2nd only opens Recentes (`OPEN_RECENT`). The
transcription that already went into processing **is not cancelled** (end-to-end test: the text is pasted and saved).

## How it is wired

```
BubbleWidget (bubble.py)         mouse (only inside the shape) -> ClickMachine -> signal action(str)
        │                         press/move/release with GLOBAL position and the bubble's clock
        │  app_state = state_source() = controller.state ("idle"/"recording"/"transcribing")
QtUi._on_bubble_action (qt_app.py)   runs on the Qt thread and returns right away
        │  "start" / "stop" / "discard_and_open" / "open_recent"
SerialWorker (1 thread, queued)  begin() / end() / cancel() off the Qt thread, ONE AT A TIME
        ▼
Controller (unchanged)
```

- `ui/click_machine.py` (pure logic, 32 tests) was **not changed**.
- The right button does **not** go through the machine (the menu does not disturb a double click in progress).
- Dragging: `DRAG_START` turns on `bubble._dragging` (the T40 watchdog does not reposition the bubble while the user
  holds it); `DRAG_END` emits `moved` and `QtUi` saves the position as before.
- The `clicked` signal stays as **legacy**: the mouse no longer emits it (`QtUi` still connects it to "open Recentes" and an old
  T22 test uses it). It can be removed together with that test.

### Why a serial queue (the race with the microphone opening)

`Controller.begin()` marks `recording` **before** opening the microphone (`recorder.start()` takes a while) and only tells the
interface at the end; `Controller.cancel()` acts as soon as it sees `recording`. A double click arrives ~100–300 ms after the 1st,
exactly while the microphone is opening. If `begin` and `cancel` ran in loose threads, `cancel` would stop a recorder
that has not opened yet and `begin` would finish on top of it, leaving the "cancelled" recording recording. With the queue, `cancel` only
runs after `begin` has finished (test `test_ponta_a_ponta_duplo_clique_com_o_microfone_ainda_abrindo`; replacing the queue
with one thread per action makes this test fail).

## Focus: the bubble still does not steal the field you are dictating into

Flags kept: `FramelessWindowHint | WindowStaysOnTopHint | Tool | WindowDoesNotAcceptFocus` +
`WA_ShowWithoutActivating` (on Windows this becomes `WS_EX_NOACTIVATE`). The mouse handling does not call `activateWindow`,
`setFocus`, `raise_`, `grabMouse` or `grabKeyboard` (dynamic test and static code test).

**Limit of what Qt offscreen proves:** the `offscreen` plugin activates any window on `show()`, with or without these flags
(measured: with the bubble and with a control window without flags). So the tests prove that **the flags are still there, that mouse
events arrive with them and that the code does not ask for focus** — but they do not prove that Windows does not activate the window
on click. Only the manual test below proves that. About the click: the window is translucent with per-pixel alpha; on Windows,
pixels with alpha 0 let the click pass through to the window below and painted ones (the whole shape) receive it — which is what
`_hit` expects. Clicks on the transparent margin fall into that case (and, if they arrive, are ignored).

## Manual test on Windows (TEST instance — do not use the normal instance)

The test instance uses another mutex, another hotkey and its own config folder (its `bolha.json` goes to the same
folder, so dragging the test bubble does not touch the user's bubble position). In a prompt (`cmd`) in the project folder:

```bat
set HEAR_AI_INSTANCE_NAME=teste-t42b
set HEAR_AI_NO_DIALOGS=1
mkdir "%TEMP%\hear-ai-teste-t42b"
echo {"hotkey": "ctrl+alt+f9", "models_dir": "<project folder>\\models"} > "%TEMP%\hear-ai-teste-t42b\config.json"
set HEAR_AI_CONFIG=%TEMP%\hear-ai-teste-t42b\config.json
set PYTHONPATH=src
"<project folder>\.venv\Scripts\python.exe" -m hear_ai
```

(use an absolute `models_dir` if the `models` folder is not in the project folder.) The test bubble is born in the same place as the
user's bubble: **drag it to another corner before starting.** To quit, use the "Sair" (Quit) item of the tray menu *of the
test instance*, or close the console you opened; do not kill processes you did not start. Script:

1. **Focus:** open Notepad, click in it and type something; click **once** on the bubble. Expected: the bubble turns into a
   "recording" pill with the stopwatch running and **Notepad stays active** (the title bar does not turn gray,
   the cursor keeps blinking, typed text goes to it).
2. **Stop and paste:** say a sentence, wait more than 1 s and click again. Expected: it transcribes and pastes into Notepad.
3. **Double click when idle:** double-click the resting bubble quickly. Expected: the Recentes window opens; the bubble
   goes back to rest; **nothing** is pasted; no new item appears in Recentes; no new file stays in
   `spool\` (the test instance's `spool` and `logs` live in the test's working folder, not in the normal
   instance's folder; check that nothing new stayed there after ~2 s).
4. **Stop + extra click:** record, click to stop and click again right after. Expected: the text is transcribed and
   pasted normally and Recentes opens.
5. **Drag:** hold and drag the bubble. Expected: it moves, **does not** start recording; restart the test instance and
   check that it stayed where it was dropped.
6. **Right button:** opens the menu and does not record.
7. **Hotkey:** `Ctrl+Alt+F9` (the test one) records/stops as usual, including ending a recording started by a click.
8. **Transparent margin:** click on the corner of the bubble window, outside the shape: the click should reach the window below.
9. **Double-click speed:** change it in *Control Panel › Mouse › Double-click speed* and repeat step 3.
10. **Log:** `logs\bolha-diag.log` gets one line `EVT name=click action=start|stop|discard_and_open|
    open_recent state=idle|recording|transcribing` per action (states only; never the dictated text).
