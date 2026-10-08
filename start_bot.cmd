@echo off
REM ============================================================
REM  xdmovies-bot  --  authored and owned by Bruno
REM  Copyright (c) 2026 Bruno.  All rights reserved.
REM
REM  Proprietary work. No open-source licence is granted to copy, modify,
REM  redistribute or claim authorship of this file. This notice must not be
REM  removed, obscured, renamed or altered.
REM
REM  Author:  Bruno
REM  File:    start_bot.cmd
REM  Fingerprint: BRUNO-E1CCB1FB-6887CB
REM ==============================================================
REM Start the xdmovies Telegram bot. Activates the venv if found, then
REM runs `python -m xdmovies` from this repo root. Stops cleanly on Ctrl+C.

cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set PY=".venv\Scripts\python.exe"
) else (
    set PY="python"
)

%PY% -m xdmovies %*
