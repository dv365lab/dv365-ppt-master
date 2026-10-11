@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"

if not exist "%~dp0skills\ppt-master\scripts\web_gui\server.py" goto missing_server
if not exist "%~dp0.venv\Scripts\python.exe" goto try_python
"%~dp0.venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 goto run_venv

:try_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 goto run_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 goto run_launcher
echo Python 3.10 or newer was not found.
echo Install Python, enable its PATH option, then open this file again.
goto failure

:run_venv
"%~dp0.venv\Scripts\python.exe" -c "import flask" >nul 2>nul
if errorlevel 1 goto dependencies_venv
"%~dp0.venv\Scripts\python.exe" "%~dp0skills\ppt-master\scripts\web_gui\server.py" %*
goto finished

:run_python
python -c "import flask" >nul 2>nul
if errorlevel 1 goto dependencies_python
python "%~dp0skills\ppt-master\scripts\web_gui\server.py" %*
goto finished

:run_launcher
py -3 -c "import flask" >nul 2>nul
if errorlevel 1 goto dependencies_launcher
py -3 "%~dp0skills\ppt-master\scripts\web_gui\server.py" %*
goto finished

:dependencies_venv
echo Flask is missing. Run this command, then open this file again:
echo "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
goto failure

:dependencies_python
echo Flask is missing. Run this command, then open this file again:
echo python -m pip install -r "%~dp0requirements.txt"
goto failure

:dependencies_launcher
echo Flask is missing. Run this command, then open this file again:
echo py -3 -m pip install -r "%~dp0requirements.txt"
goto failure

:missing_server
echo Web GUI server not found. Keep start-web.bat in the PPT Master repository root.
goto failure

:finished
set "PPT_MASTER_EXIT_CODE=%errorlevel%"
if "%PPT_MASTER_EXIT_CODE%"=="0" exit /b 0
echo.
echo The Web GUI stopped with an error. See the message above.
pause
exit /b %PPT_MASTER_EXIT_CODE%

:failure
echo.
pause
exit /b 1
