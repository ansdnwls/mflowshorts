@echo off
rem mflowshorts Windows install/check. Usage: scripts\install_windows.cmd [-CheckOnly] [-SkipMedia]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1" %*
exit /b %ERRORLEVEL%
