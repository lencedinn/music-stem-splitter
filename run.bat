@echo off
rem stem-splitter launcher (Windows)
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 separator.py %*
    goto end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python separator.py %*
    goto end
)

echo.
echo  [ERROR] Python not found. Install Python 3.9+ first.
echo.
pause
exit /b 1

:end
if errorlevel 1 (
    echo.
    echo  [ERROR] exit code %errorlevel%
    pause
)
