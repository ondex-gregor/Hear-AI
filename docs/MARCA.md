# Brand in the windows (T43)

## Hierarchy

- **Ondex** is the developer company. Its symbol (the **isotype**, `assets/icone.png`, black on transparent) is the
  app's signature: bubble, tray icon and application/window icon.
- **Hear AI** is the product. Its **logotype** (`assets/marca/logotipo-hear-ai.png`) identifies the product's windows.
- The two **never appear together** in the same header: the isotype is not used in the headers of Recentes (Recents) and
  Configurações (Settings) (test `test_cabecalhos_usam_o_logotipo_e_nao_o_isotipo`).

## Where each asset is used

| Asset | Where | How |
|---|---|---|
| Ondex isotype (`assets/icone.png`) | bubble (`bubble.py::_paint_logo` → `logo.white_logo(size, dpr)`, white on the dark bubble), tray (`tray.py::make_icon`), window icon (`setWindowIcon`) | **unchanged** |
| Hear AI logotype (`assets/marca/logotipo-hear-ai.png`) | header of **Recentes** and **Configurações** | `logo.brand_header(name)` → `BrandLogo` (white, 24 px of ink) + divider + window name |
| Gear ⚙ | right corner of the Recentes header | `recent_window.GearButton` (vector, no image file) |

## The logotype file (image description)

`assets/marca/logotipo-hear-ai.png` (copy of the author's original file):

- **Symbol/text:** the word **"hear ai"** in lowercase, rounded-corner geometric sans-serif, with a
  **5-bar waveform glyph** (the middle bars taller) between "hear" and "ai"; the dot of the "i" is round.
  **It does not contain the Ondex isotype.**
- **Color and background:** **black** ink (luminance ≤ 20) on a **transparent** background (ARGB32 PNG; 76.8% of pixels have
  alpha 0; antialiased edges).
- **Proportion:** the file is **1200 × 401 px (≈ 3:1)**, but with large transparent margins: the ink occupies only
  `x 52–1125, y 74–310` = **1074 × 237 px (≈ 4.53:1)**. That is why the code **crops to the ink's bounding box** before
  scaling; "height" in the code is always the height of the **ink** (from the top of the "h" to the baseline of the letters; there is no tail).
- Since the windows are dark and the file is black, the code generates a **white** version at runtime
  (`QPainter` with `CompositionMode_SourceIn`, same as the isotype's `white_logo`), preserving alpha.

## Sizes and sharpness

- Header: **24 logical px** of ink height (spec: 22–26) × ~109 px wide (ink proportion kept).
- Sharpness at 100/125/150/200%: `BrandLogo` draws with the **target's DPR** (`painter.device()`), so the real pixmap
  is 24 / 30 / 36 / 48 px tall (scaled from the original, `SmoothTransformation`, never enlarging the 1×) and stays
  sharp when dragging the window between monitors of different densities (DPR rounded to 0.25). Cache by
  `(file, height, dpr, color)` in `logo._logotipo`.
- Window name ("Recentes"/"Configurações"): 12 pt, weight 600, muted tone (`style.TEXT_MUTED`), after a thin
  1 × 18 px divider. The text baseline is aligned with the logotype's baseline (it drops 5 px; measured difference ≤ 1 px).
- The header row has a **fixed height of 32 px** (= the gear button), so the logotype stays in the same place in
  Recentes (with or without the gear) and in Configurações.

## If the file is missing

A missing, unreadable or 100% transparent file → `hear_ai_logotipo()` returns a null `QPixmap` and `BrandLogo`
draws the **text "Hear AI"** (Segoe UI semi-bold, white, `fallback = True`). The header keeps the same
layout; nothing breaks (test `test_sem_logotipo_o_cabecalho_cai_para_o_texto_sem_quebrar`, 3 variants).

## Settings gear (Recentes)

- `GearButton`: 32 × 32 px, 8-tooth gear **drawn as a vector** (`QPainterPath`; sharp at any DPI),
  muted tone at rest, light on hover/focus, blue background when pressed, blue ring with keyboard focus.
- Tooltip "Configurações", `accessibleName` "Configurações", `accessibleDescription` "Abre a janela de configurações
  do Hear AI" (Opens Hear AI's settings window). It takes part in the **Tab** order; **Enter** and **Space** activate it (`QPushButton` only accepts Enter in a dialog,
  so `keyPressEvent` handles Enter/Return).
- `RecentWindow(on_open_settings=...)`: `QtUi` injects `show_settings`; Recentes **never** creates the `SettingsWindow`.
  Without a callback (isolated tests) the button is **hidden**, like "Pausar histórico" without a store. There is only one
  Configurações window (tray, bubble and gear open the same one). Opening it does not touch recording, search or Recentes.

## How to regenerate the evidence

```
.venv\Scripts\python.exe tests\test_marca.py            # writes the marca-*.png captures
```

| File | Content |
|---|---|
| `marca-recentes.png` / `-1x.png` | the whole Recentes window at 2× and 1× (synthetic data) |
| `marca-config.png` / `-1x.png` | the Configurações window at 2× and 1× (temporary config) |
| `marca-cabecalhos.png` | the headers of both windows in **physical pixels**: 1× enlarged 2× without smoothing and real 2× |
| `marca-engrenagem.png` | the gear enlarged 4× at rest, with keyboard focus (blue ring) and pressed |
| `marca-logotipo-escalas.png` | the logotype at 100/125/150/200% (24/30/36/48 real px), on a dark background |

The tests (`tests/test_marca.py`) also generate these images in a temporary folder and check that they exist and differ.

## Not tested

- Windows on a real Windows screen (only `QT_QPA_PLATFORM=offscreen`): real 125–150% DPI/scale, switching monitors with
  different densities and the gear's focus/ring with a real keyboard.
- The gear was designed for a dark theme (the app only has the dark-blue theme).
