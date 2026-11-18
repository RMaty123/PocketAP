@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto check_packages

echo Vytvarim virtualni prostredi Pythonu...
py -3 -m venv "%~dp0.venv" >nul 2>&1
if not exist "%VENV_PY%" python -m venv "%~dp0.venv"
if not exist "%VENV_PY%" (
    echo CHYBA: Nebyl nalezen Python 3.11 nebo novejsi. Nainstaluj jej z python.org.
    goto failed
)

:check_packages
"%VENV_PY%" -c "import winrt.windows.devices.wifidirect, winrt.windows.security.credentials, winrt.windows.networking" >nul 2>&1
if not errorlevel 1 goto launch

echo Instaluji zavislosti, poprve je potreba internet...
"%VENV_PY%" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo CHYBA: Instalace zavislosti selhala.
    goto failed
)

:launch
"%VENV_PY%" "%~dp0pocketap.py" %*
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
pause
exit /b 1
