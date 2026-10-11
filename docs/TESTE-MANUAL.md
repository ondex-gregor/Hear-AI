# Manual test — Hear AI (about 5 minutes)

The automated tests already cover configuration, transcription of the test phrase, simulated pasting and the controller. This checklist covers what can only be seen with a real keyboard, microphone and tray. Mark ✅/❌ at each step. (The app's interface is in Portuguese; on-screen labels are quoted with an English gloss.)

**Before:** microphone connected. If this is the first time, run `instalar.bat`.

1. **Open (30 s)** — Double-click `iniciar.bat`. An icon appears in the tray (it may be hidden behind the ^ arrow near the clock). It stays gray for a few seconds and then turns normal.
   ✅ if the icon appeared and left the gray state.

2. **First dictation (1 min)** — Open Notepad. Copy any word (e.g., select "banana" and press Ctrl+C). Click in Notepad, **hold** Ctrl+Alt+H, say *"Hoje é um ótimo dia para testar o ditado."* ("Today is a great day to test dictation."), **release**.
   ✅ if you heard a beep at the start, the icon turned red → yellow, and the sentence appeared in ~2 s.
   Then press Ctrl+V: ✅ if it pasted "banana" (the clipboard was restored).

3. **Toggle mode (1 min)** — Right-click the icon → switch to the *alternar* (toggle) mode. Press Ctrl+Alt+H once, say a short sentence, press it again.
   ✅ if the text appeared. Go back to the *segurar* (hold) mode from the same menu.

4. **Another program (1 min)** — Repeat step 2 in the browser (search bar or an e-mail).
   ✅ if it pasted there too.

5. **Hotkey side effect (30 s)** — Still in Notepad, hold Ctrl+Alt+H for about 3 seconds **without speaking** and release.
   ✅ if nothing was typed. ❌ if spaces or strange characters appeared (note which) — this confirms the known limitation D9 and becomes a fix.

6. **Privacy + quit (30 s)** — Menu → "Abrir pasta de logs" (Open logs folder) → open `hear-ai.log`.
   ✅ if NO sentence you dictated appears (only things like `len=42 words=8`). Then menu → **Sair** (Quit): the icon disappears.

**If something fails:** run `iniciar-console.bat` (shows errors on screen) and look at the contents of `logs\hear-ai.log` (it does not contain what you dictated; check before sharing it with anyone).
