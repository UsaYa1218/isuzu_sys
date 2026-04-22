@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo .venv was not found. Run setup.cmd first.
  exit /b 1
)

".venv\Scripts\python.exe" -m uvicorn app.main:app --reload
