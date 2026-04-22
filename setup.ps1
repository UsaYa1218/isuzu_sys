param(
    [string]$PythonVersion = "3.12",
    [string]$OllamaModel = "qwen2.5:7b",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

function Test-CommandExists {
    param([string]$CommandName)
    return $null -ne (Get-Command $CommandName -ErrorAction SilentlyContinue)
}

function Resolve-PythonCommand {
    $candidates = @(
        "python",
        "py",
        "C:\Users\taker\anaconda3\python.exe"
    )

    foreach ($candidate in $candidates) {
        if ($candidate -like "*.exe") {
            if (Test-Path $candidate) {
                return $candidate
            }
            continue
        }

        if (Test-CommandExists $candidate) {
            return $candidate
        }
    }

    return $null
}

if (-not $SkipInstaller) {
    if (-not (Test-CommandExists "winget")) {
        Write-Host "winget was not found. Python and Ollama installation will be skipped."
    }
    else {
        if (-not (Resolve-PythonCommand)) {
            winget install --id Python.Python.$PythonVersion -e --accept-package-agreements --accept-source-agreements
        }

        if (-not (Test-CommandExists "ollama")) {
            winget install --id Ollama.Ollama -e --accept-package-agreements --accept-source-agreements
        }
    }
}

$pythonCmd = Resolve-PythonCommand
if (-not $pythonCmd) {
    throw "Python was not found. Install Python first, then run setup again."
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $pythonCmd -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m pip install paddlepaddle==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/

New-Item -ItemType Directory -Force -Path "runtime" | Out-Null
New-Item -ItemType Directory -Force -Path "runtime\uploads" | Out-Null
New-Item -ItemType Directory -Force -Path "runtime\exports" | Out-Null
New-Item -ItemType Directory -Force -Path "runtime\paddleocr" | Out-Null
New-Item -ItemType Directory -Force -Path "runtime\tmp" | Out-Null

if (Test-CommandExists "ollama") {
    ollama pull $OllamaModel
}

Write-Host "Setup finished. Start the app with run.cmd or .\run.ps1."
