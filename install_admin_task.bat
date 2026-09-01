@echo off
setlocal
echo ===================================================
echo  RemoteAgent Administrator Task Scheduler Setup
echo ===================================================

:: Check for administrative privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs"
    exit /b 0
)

set "AGENT_EXE=%~dp0RemoteAgent.exe"
if not exist "%AGENT_EXE%" set "AGENT_EXE=%~dp0dist\RemoteAgent.exe"

if not exist "%AGENT_EXE%" (
    echo [ERROR] RemoteAgent.exe not found at:
    echo   %AGENT_EXE%
    echo Please build or place RemoteAgent.exe first.
    pause
    exit /b 1
)

echo [OK] Using Executable: %AGENT_EXE%
echo Creating Windows Scheduled Task with HIGHEST privileges...

schtasks /create /tn "RemoteAgentAdmin" /tr "\"%AGENT_EXE%\"" /sc onlogon /rl HIGHEST /f >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create scheduled task.
    pause
    exit /b 1
)

echo [SUCCESS] Scheduled task "RemoteAgentAdmin" created successfully!
echo RemoteAgent will now run with Administrator privileges at logon (bypassing NTFS lock restrictions).
echo.
echo Starting task now...
schtasks /run /tn "RemoteAgentAdmin" >nul 2>&1

echo Done!
pause
endlocal
