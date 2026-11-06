@echo off
rem mflowshorts AI subtitle removal (NVIDIA) install/check. Usage: scripts\install_ai_eraser.cmd [-CheckOnly] [-Archive path]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_ai_eraser.ps1" %*
exit /b %ERRORLEVEL%
