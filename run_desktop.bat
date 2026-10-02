@echo off
setlocal
cd /d "%~dp0"

if exist "%CD%\.venv\Scripts\python.exe" (
  "%CD%\.venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
  if not errorlevel 1 goto :launch_venv
)
if exist "%CD%\dist\Starployment\Starployment.exe" goto :launch_bundle

py -3.12 -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if not errorlevel 1 goto :launch_py
python -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if not errorlevel 1 goto :launch_python

echo Starployment needs Python 3.12. Run "uv sync --frozen" to create the project environment, then try again. 1>&2
exit /b 1

:launch_venv
call :set_data_dir
"%CD%\.venv\Scripts\python.exe" -m jobhub.desktop %*
exit /b %ERRORLEVEL%

:launch_bundle
"%CD%\dist\Starployment\Starployment.exe" %*
exit /b %ERRORLEVEL%

:launch_py
call :set_data_dir
py -3.12 -m jobhub.desktop %*
exit /b %ERRORLEVEL%

:launch_python
call :set_data_dir
python -m jobhub.desktop %*
exit /b %ERRORLEVEL%

:set_data_dir
if defined JOBHUB_DATA_DIR exit /b 0
if exist "%LOCALAPPDATA%\Starployment\.checkout-import.json" (
  set "JOBHUB_DATA_DIR=%LOCALAPPDATA%\Starployment"
) else if exist "%LOCALAPPDATA%\JobHub\.checkout-import.json" (
  set "JOBHUB_DATA_DIR=%LOCALAPPDATA%\JobHub"
) else (
  set "JOBHUB_DATA_DIR=%CD%\data"
)
exit /b 0
