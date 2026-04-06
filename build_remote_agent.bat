@echo off
setlocal
set "NO_PAUSE=%~1"
echo Stopping running RemoteAgent.exe (if any)...
taskkill /F /IM RemoteAgent.exe >nul 2>&1

set "PYTHON_VER=3.12"
py -%PYTHON_VER% -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo Python %PYTHON_VER% not found. Falling back to 3.14...
  set "PYTHON_VER=3.14"
)
echo Using Python %PYTHON_VER% for build...

for /f %%I in ('powershell -NoLogo -NoProfile -Command "(Get-Date).ToString(\"yyyy.MM.dd.HHmmss\")"') do set "AGENT_VERSION=%%I"
echo %AGENT_VERSION%>"AGENT_VERSION_BUILD.txt"

py -%PYTHON_VER% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :BUILD_ERROR
set "WORKPATH=build_tmp_%RANDOM%%RANDOM%"
py -%PYTHON_VER% -m PyInstaller --clean --noconfirm --workpath "%WORKPATH%" RemoteAgent.spec
if errorlevel 1 goto :BUILD_ERROR
if exist ".env" copy /Y ".env" "dist\.env" >nul
if exist "dist\.env" powershell -NoLogo -NoProfile -Command "$p='dist\\.env';$v='AGENT_VERSION=%AGENT_VERSION%';$c=@();if(Test-Path $p){$c=Get-Content $p};$c=$c|Where-Object{$_ -notmatch '^AGENT_VERSION='};($c + $v)|Set-Content $p -Encoding UTF8"
if exist "dist\RemoteAgent.exe" copy /Y "dist\RemoteAgent.exe" "dist\RemoteAgent_%AGENT_VERSION%.exe" >nul
echo %AGENT_VERSION%>"dist\AGENT_VERSION.txt"
echo Build version: %AGENT_VERSION%
echo Build complete: dist\RemoteAgent.exe
if /I not "%NO_PAUSE%"=="--no-pause" pause
endlocal
exit /b 0

:BUILD_ERROR
echo.
echo Build failed. Please check the error output above.
if /I not "%NO_PAUSE%"=="--no-pause" pause
endlocal
exit /b 1
