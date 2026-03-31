@echo off
setlocal
if "%~1"=="" (
    set "SERVER_URL=https://remote-agent-node.onrender.com"
) else (
    set "SERVER_URL=%~1"
)
echo Using SERVER_URL=%SERVER_URL%

set "AGENT_EXE=%~dp0RemoteAgent.exe"
if not exist "%AGENT_EXE%" set "AGENT_EXE=%~dp0dist\RemoteAgent.exe"

if not exist "%AGENT_EXE%" (
    echo RemoteAgent executable not found.
    exit /b 1
)

reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "RemoteAgent" /t REG_SZ /d "\"%AGENT_EXE%\"" /f >nul 2>&1
if errorlevel 1 (
    echo Warning: could not enable Windows startup entry.
) else (
    echo Windows startup entry enabled.
)

start "" "%AGENT_EXE%"
endlocal
