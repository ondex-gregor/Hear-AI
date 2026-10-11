@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] The .venv environment was not found. Create it with:
    echo   python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
if not exist "models\faster-whisper-small\model.bin" (
    echo [ERROR] The Whisper model was not found. Download it once with:
    echo   .venv\Scripts\python.exe scripts\baixar_modelo.py small
    pause
    exit /b 1
)
set PYTHONPATH=src
start "" ".venv\Scripts\pythonw.exe" -m hear_ai
