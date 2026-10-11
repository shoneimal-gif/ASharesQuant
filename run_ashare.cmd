@echo off
setlocal
cd /d "%~dp0"
if not defined ASHARE_PYTHON set "ASHARE_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%ASHARE_PYTHON%" (
  echo Python runtime not found. Use Python 3.10+ to run main.py directly.
  pause
  exit /b 1
)
if "%~1"=="" (
  "%ASHARE_PYTHON%" -X utf8 main.py --demo
) else (
  "%ASHARE_PYTHON%" -X utf8 main.py %*
)
set "ASHARE_EXIT=%ERRORLEVEL%"
if "%~1"=="" (
  if "%ASHARE_EXIT%"=="0" start "" "%~dp0runtime\demo\report.html"
  pause
)
exit /b %ASHARE_EXIT%
