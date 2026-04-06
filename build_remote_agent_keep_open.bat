@echo off
setlocal
cd /d "%~dp0"

call build_remote_agent.bat --no-pause
set "EXIT_CODE=%errorlevel%"

echo.
echo Build script exit code: %EXIT_CODE%
if not "%EXIT_CODE%"=="0" echo Build failed. Check console output above.
echo Press any key to close this window...
pause >nul

endlocal
exit /b %EXIT_CODE%
