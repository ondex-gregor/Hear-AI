@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] The .venv environment was not found.
    pause
    exit /b 1
)
if not exist "models\faster-whisper-small\model.bin" (
    echo [ERROR] The Whisper model was not found. Run: .venv\Scripts\python.exe scripts\baixar_modelo.py small
    pause
    exit /b 1
)
set PYTHONPATH=src
".venv\Scripts\python.exe" -m hear_ai
echo.
echo Hear AI has exited.
pause
