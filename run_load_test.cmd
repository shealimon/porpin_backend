@echo off
setlocal
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"
python -u load_test.py --presets --quiet --file sample.txt --url http://127.0.0.1:8000/translate %*
exit /b %ERRORLEVEL%
