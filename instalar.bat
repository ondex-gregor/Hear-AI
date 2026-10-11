@echo off
rem Hear AI - installation (first time). The only step that uses the internet.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py --version >nul 2>&1 || python --version >nul 2>&1 || goto sempython
    echo Creating the Python environment in .venv ...
    py -m venv .venv || python -m venv .venv || goto erro
)
echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto erro
if not exist "models\faster-whisper-small\model.bin" (
    echo Downloading the Whisper "small" model (~480 MB^) ...
    ".venv\Scripts\python.exe" scripts\baixar_modelo.py small || goto erro
)
echo.
echo Done! Now run iniciar.bat
pause
exit /b 0
:sempython
echo.
echo [ERROR] Python was not found on this computer.
echo Please install Python 3.14 from https://www.python.org/downloads/
echo On the first screen of the installer, check "Add python.exe to PATH".
echo Then close this window and double-click instalar.bat again.
pause
exit /b 1
:erro
echo.
echo [ERROR] The installation failed. See the message above.
pause
exit /b 1
