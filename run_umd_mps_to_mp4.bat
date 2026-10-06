@echo off
setlocal

rem Drag a folder onto this BAT, or run from CMD:
rem   run_umd_mps_to_mp4.bat "C:\path\to\folder"

if "%~1"=="" (
    python "%~dp0umd_mps_to_mp4.py" "%~dp0"
) else (
    python "%~dp0umd_mps_to_mp4.py" "%~1"
)

set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Script exited with error code %RC%.
pause
exit /b %RC%
