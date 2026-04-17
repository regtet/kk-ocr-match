@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

cd /d "%~dp0\.."
set "BUILD_LOG=build\build_release_last.log"
set "NO_PAUSE="
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"

if not exist "build" mkdir "build" >nul 2>nul

echo [1/5] Check Python...
python --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Please install Python 3.8+ and add to PATH.
  if not defined NO_PAUSE pause
  exit /b 1
)

echo [2/5] Prepare venv...
if not exist "venv\Scripts\python.exe" (
  python -m venv venv
  if errorlevel 1 (
    echo [ERROR] Failed to create venv.
    if not defined NO_PAUSE pause
    exit /b 1
  )
)

echo [3/5] Install dependencies...
call "venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
  echo [ERROR] Failed to upgrade pip.
  if not defined NO_PAUSE pause
  exit /b 1
)
call "venv\Scripts\python.exe" -m pip install pyinstaller PySide6 pillow fuzzywuzzy python-Levenshtein
if errorlevel 1 (
  echo [ERROR] Failed to install dependencies.
  if not defined NO_PAUSE pause
  exit /b 1
)

echo [4/6] Prepare icon...
if not exist "@logo.ico" (
  if exist "logo\@logo.ico" copy /y "logo\@logo.ico" "@logo.ico" >nul
)
if not exist "@logo.ico" (
  if exist "logo\logo_ico_256x256.ico" copy /y "logo\logo_ico_256x256.ico" "@logo.ico" >nul
)
if not exist "@logo.ico" (
  if exist "logo\logo_ico_64x64.ico" copy /y "logo\logo_ico_64x64.ico" "@logo.ico" >nul
)
if not exist "@logo.ico" (
  echo [WARN] @logo.ico not found. EXE will use default icon.
) else (
  echo [INFO] Use icon: @logo.ico
)

echo [5/6] Build EXE...
if exist "dist\kk-ocr-match.exe" del /f /q "dist\kk-ocr-match.exe" >nul 2>nul
echo [INFO] Writing build log to: %BUILD_LOG%
call "venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm "kk-ocr-match.spec" > "%BUILD_LOG%" 2>&1
if errorlevel 1 (
  echo [ERROR] PyInstaller build failed.
  echo [INFO] Open log: %BUILD_LOG%
  type "%BUILD_LOG%"
  if not defined NO_PAUSE pause
  exit /b 1
)
type "%BUILD_LOG%"

echo [6/6] Copy OCR engine folder...
if not exist "PaddleOCR-json_v1.4.1" (
  echo [WARN] PaddleOCR-json_v1.4.1 not found, skip copy.
) else (
  robocopy "PaddleOCR-json_v1.4.1" "dist\PaddleOCR-json_v1.4.1" /E /NFL /NDL /NJH /NJS /NP >nul
)

echo.
echo Build completed.
echo Output:
echo   dist\kk-ocr-match.exe
echo   dist\PaddleOCR-json_v1.4.1\
echo [INFO] Full build log: %BUILD_LOG%
if not defined NO_PAUSE pause
exit /b 0

