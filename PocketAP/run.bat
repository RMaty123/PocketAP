@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto check_packages

echo [PocketAP] Vytvarim virtualni prostredi Pythonu...
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv "%~dp0.venv"
    goto venv_created
)
python -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
if errorlevel 1 (
    echo [PocketAP] CHYBA: Nebyl nalezen Python 3.11 nebo novejsi. Nainstalujte jej z python.org.
    goto failed
)
python -m venv "%~dp0.venv"

:venv_created
if not exist "%VENV_PY%" (
    echo [PocketAP] CHYBA: Virtualni prostredi se nepodarilo vytvorit.
    goto failed
)

:check_packages
"%VENV_PY%" -c "import winrt.windows.devices.wifidirect, winrt.windows.security.credentials, winrt.windows.networking" >nul 2>&1
if not errorlevel 1 goto launch

echo [PocketAP] Instaluji zavislosti...
"%VENV_PY%" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [PocketAP] CHYBA: Instalace zavislosti selhala.
    goto failed
)

:launch
"%VENV_PY%" "%~dp0main.py" %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
pause
exit /b 1
