@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo .venv was not found. Run setup.cmd first.
  exit /b 1
)

".venv\Scripts\python.exe" -c "import PyInstaller, tkinterdnd2"
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install pyinstaller tkinterdnd2
  if errorlevel 1 exit /b 1
)

if exist "build\TransferSummaryTool" rmdir /s /q "build\TransferSummaryTool"
if exist "dist\TransferSummaryTool" rmdir /s /q "dist\TransferSummaryTool"

".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  TransferSummaryTool.spec
if errorlevel 1 exit /b 1

if not exist "dist\TransferSummaryTool\runtime\inbox" mkdir "dist\TransferSummaryTool\runtime\inbox"
if not exist "dist\TransferSummaryTool\runtime\exports" mkdir "dist\TransferSummaryTool\runtime\exports"
if not exist "dist\TransferSummaryTool\runtime\processed" mkdir "dist\TransferSummaryTool\runtime\processed"
if not exist "dist\TransferSummaryTool\runtime\uploads" mkdir "dist\TransferSummaryTool\runtime\uploads"
if exist "dist\TransferSummaryTool\runtime\.desktop_initialized" del /f /q "dist\TransferSummaryTool\runtime\.desktop_initialized"
if exist ".env.example" copy /Y ".env.example" "dist\TransferSummaryTool\.env.example" >nul
if exist ".env" if not exist "dist\TransferSummaryTool\.env" copy ".env" "dist\TransferSummaryTool\.env" >nul
copy /Y "PACKAGE_README.txt" "dist\TransferSummaryTool\README.txt" >nul

echo.
echo Package created:
echo %cd%\dist\TransferSummaryTool
echo Run dist\TransferSummaryTool\TransferSummaryTool.exe
