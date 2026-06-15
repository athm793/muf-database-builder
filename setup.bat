@echo off
REM ============================================================
REM  One-time setup for the Add-FDD tool on a new Windows PC.
REM  Run this once. It installs the Python packages and checks
REM  that the other required tools are present.
REM ============================================================
setlocal
cd /d "%~dp0"
echo ============================================================
echo   MUF Database Builder - one-time setup
echo ============================================================
echo.

REM ---- 1. Python -------------------------------------------------
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (where py >nul 2>nul && set "PY=py")
if not defined PY (
    echo [X] Python is NOT installed.
    echo     Install it from https://www.python.org/downloads/
    echo     IMPORTANT: tick "Add python.exe to PATH" during install.
    echo.
    pause
    exit /b 1
)
echo [OK] Python found: %PY%
%PY% --version
echo.

REM ---- 2. Python packages ---------------------------------------
echo Installing required Python packages...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] Package install failed. Check your internet connection and retry.
    pause
    exit /b 1
)
echo [OK] Python packages installed.
echo.

REM ---- 3. git ----------------------------------------------------
where git >nul 2>nul
if errorlevel 1 (
    echo [X] git is NOT installed (needed to sync to GitHub).
    echo     Install it from https://git-scm.com/download/win
) else (
    echo [OK] git found.
)
echo.

REM ---- 4. Claude CLI --------------------------------------------
where claude >nul 2>nul
if errorlevel 1 (
    echo [X] The Claude CLI is NOT installed (the tool needs it to read FDDs).
    echo     1. Install Node.js LTS from https://nodejs.org/
    echo     2. Then run:  npm install -g @anthropic-ai/claude-code
    echo     3. Then run:  claude   and sign in with your Claude Max plan.
) else (
    echo [OK] Claude CLI found.
    echo     Make sure you have signed in once with:  claude
)
echo.

echo ============================================================
echo   Setup checks complete. See SETUP.md for anything marked [X].
echo   When everything is [OK], double-click:
echo       "Add FDD to Database.bat"
echo ============================================================
echo.
pause
