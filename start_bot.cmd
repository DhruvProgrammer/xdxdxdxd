@echo off
REM Start the xdmovies Telegram bot. Activates the venv if found, then
REM runs `python -m xdmovies` from this repo root. Stops cleanly on Ctrl+C.

cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set PY=".venv\Scripts\python.exe"
) else (
    set PY="python"
)

%PY% -m xdmovies %*
