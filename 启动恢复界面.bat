@echo off
setlocal
cd /d "%~dp0"

rem ===== admin check (required to control DMDE) =====
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [i] Requesting administrator privileges - please click YES on the popup...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem ===== python check =====
set PY=python
%PY% --version >nul 2>&1
if %errorlevel% neq 0 set PY=py
%PY% --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [x] Python not found. Please install Python 3.8+ with "Add to PATH".
    pause
    exit /b 1
)

rem ===== dependency check (auto install) =====
%PY% -c "import pywinauto" >nul 2>&1
if %errorlevel% neq 0 (
    echo [i] First run: installing pywinauto, please wait...
    %PY% -m pip install --quiet pywinauto
)
%PY% -c "import pywinauto" >nul 2>&1
if %errorlevel% neq 0 (
    echo [x] Install failed. Please run manually: python -m pip install pywinauto
    pause
    exit /b 1
)

rem ===== launch GUI =====
echo [i] Starting GUI... you can minimize this window (do not close)
echo [i] NOTE: do not touch mouse/keyboard while recovering
%PY% "%~dp0dmde_auto_gui.py"
echo.
echo [i] GUI exited.
pause
