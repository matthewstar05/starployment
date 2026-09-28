@echo off
setlocal
cd /d "%~dp0"
call "%CD%\run_desktop.bat" --setup
exit /b %ERRORLEVEL%
