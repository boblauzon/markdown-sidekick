@echo off
REM Launch Markdown Sidekick using the project virtual environment.
setlocal
set "ROOT=%~dp0"

if not exist "%ROOT%.venv\Scripts\pythonw.exe" (
    echo Virtual environment not found. Running first-time setup...
    call "%ROOT%setup.bat"
    if errorlevel 1 (
        echo Setup failed. See messages above.
        pause
        exit /b 1
    )
)

start "" "%ROOT%.venv\Scripts\pythonw.exe" "%ROOT%app.py"
endlocal
