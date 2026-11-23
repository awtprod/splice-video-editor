@echo off
setlocal
cd /d "%~dp0"
where py.exe >nul 2>&1
if not errorlevel 1 (
    py -3 -B "%~dp0splice_app.py" %*
    goto finished
)
where python.exe >nul 2>&1
if not errorlevel 1 (
    python.exe -B "%~dp0splice_app.py" %*
    goto finished
)
echo Install Python 3.10 or newer, enable the Python launcher or add Python to PATH, and try again.
pause
exit /b 1

:finished
set "SPLICE_EXIT=%errorlevel%"
if not "%SPLICE_EXIT%"=="0" pause
exit /b %SPLICE_EXIT%
