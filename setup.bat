@echo off
REM First-time setup: create the virtual environment and install dependencies.
setlocal
set "ROOT=%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python launcher 'py' not found. Install Python 3.10+ from python.org first.
    exit /b 1
)

if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -m venv "%ROOT%.venv"
    if errorlevel 1 exit /b 1
)

echo Installing dependencies ^(this can take a few minutes^)...
"%ROOT%.venv\Scripts\python.exe" -m pip install --upgrade pip
"%ROOT%.venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 exit /b 1

echo.
echo Setup complete. Launch the app with run.bat
endlocal
