@echo off
setlocal
echo Stopping running RemoteAgent.exe (if any)...
taskkill /F /IM RemoteAgent.exe >nul 2>&1

py -3.14 -m pip install -r requirements.txt
if errorlevel 1 exit /b %errorlevel%
py -3.14 -m PyInstaller --clean --noconfirm RemoteAgent.spec
if errorlevel 1 exit /b %errorlevel%
echo Build complete: dist\RemoteAgent.exe
endlocal
