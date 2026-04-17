@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
call "scripts\build_release.bat" %*
exit /b %errorlevel%

