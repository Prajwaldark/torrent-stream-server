@echo off
REM setup.bat — One-click Windows dependency installer
REM Run this from the project root: .\setup.bat

echo ==============================================
echo  Torrent Streaming Player — Windows Setup
echo ==============================================

REM 1. Create virtual environment
echo.
echo [1/4] Creating virtual environment...
python -m venv .venv
call .venv\Scripts\activate.bat

REM 2. Upgrade pip
echo.
echo [2/4] Upgrading pip...
python -m pip install --upgrade pip

REM 3. Install Python dependencies
echo.
echo [3/4] Installing Python packages...
pip install -r requirements.txt

REM If libtorrent fails, try the alternate package name:
REM   pip install python-libtorrent

REM 4. Check mpv installation
echo.
echo [4/4] Checking for mpv / libmpv...
where mpv >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo     mpv found on PATH — OK
) else (
    echo.
    echo     WARNING: mpv not found on PATH!
    echo     python-mpv needs mpv-2.dll / libmpv-2.dll to be discoverable.
    echo.
    echo     Options:
    echo       [A] Install via winget:  winget install mpv
    echo       [B] Download from https://mpv.io/installation/ and add to PATH
    echo       [C] Place mpv-2.dll next to main.py
    echo.
)

echo.
echo ==============================================
echo  Setup complete!  Run with:
echo    .venv\Scripts\python main.py
echo  Or check imports with:
echo    .venv\Scripts\python main.py --check
echo ==============================================
pause
