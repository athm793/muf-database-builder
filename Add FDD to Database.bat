@echo off
REM ============================================================
REM  Add FDD to Database  --  double-click this file to start.
REM  Opens the point-and-click tool for adding an FDD PDF.
REM ============================================================
setlocal
cd /d "%~dp0FDD Parser\Code"

REM Prefer pythonw so no black console window appears.
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "add_fdd.py"
    goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "" python "add_fdd.py"
    goto :end
)

where py >nul 2>nul
if %errorlevel%==0 (
    start "" py "add_fdd.py"
    goto :end
)

echo.
echo  Python was not found on this PC.
echo  Please run the one-time setup first  --  see SETUP.md
echo.
pause
:end
