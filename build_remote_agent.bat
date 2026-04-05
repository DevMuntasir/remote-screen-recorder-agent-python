@echo off
setlocal
echo Stopping running RemoteAgent.exe (if any)...
taskkill /F /IM RemoteAgent.exe >nul 2>&1

for /f %%I in ('powershell -NoLogo -NoProfile -Command "(Get-Date).ToString(\"yyyy.MM.dd.HHmmss\")"') do set "AGENT_VERSION=%%I"
echo %AGENT_VERSION%>"AGENT_VERSION_BUILD.txt"

py -3.14 -m pip install -r requirements.txt
if errorlevel 1 exit /b %errorlevel%
set "WORKPATH=build_tmp_%RANDOM%%RANDOM%"
py -3.14 -m PyInstaller --clean --noconfirm --workpath "%WORKPATH%" RemoteAgent.spec
if errorlevel 1 exit /b %errorlevel%
if exist ".env" copy /Y ".env" "dist\.env" >nul
if exist "dist\.env" powershell -NoLogo -NoProfile -Command "$p='dist\\.env';$v='AGENT_VERSION=%AGENT_VERSION%';$c=@();if(Test-Path $p){$c=Get-Content $p};$c=$c|Where-Object{$_ -notmatch '^AGENT_VERSION='};($c + $v)|Set-Content $p -Encoding UTF8"
if exist "dist\RemoteAgent.exe" copy /Y "dist\RemoteAgent.exe" "dist\RemoteAgent_%AGENT_VERSION%.exe" >nul
echo %AGENT_VERSION%>"dist\AGENT_VERSION.txt"
echo Build version: %AGENT_VERSION%
echo Build complete: dist\RemoteAgent.exe
endlocal
