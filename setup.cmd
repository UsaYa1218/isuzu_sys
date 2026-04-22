@echo off
setlocal
cd /d "%~dp0"

powershell -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
