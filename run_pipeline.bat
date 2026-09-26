@echo off
title Second Brain - Social Media Pipeline

:: Run from the repo folder regardless of where the shortcut starts us.
cd /d "%~dp0"

:: Use the project virtualenv when present; otherwise fall back to system Python.
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

echo Step 1: Extracting metadata for new links...
python -m second_brain extract || goto :error

echo.
echo Step 2: Categorizing videos with Gemini...
python -m second_brain categorize || goto :error

echo.
echo Step 3: Generating Obsidian notes...
python -m second_brain write-cards || goto :error

echo.
echo ========================================
echo Pipeline completed successfully!
echo ========================================
goto :end

:error
echo.
echo ========================================
echo An error occurred. Stopping pipeline.
echo Queues are saved after every batch; fix the issue and re-run.
echo ========================================

:end
pause
