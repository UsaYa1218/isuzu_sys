$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw ".venv がありません。.\\setup.ps1 を先に実行してください。"
}

& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --reload
