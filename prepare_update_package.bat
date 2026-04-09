@echo off
setlocal

set "EXE_PATH=%~1"
set "DOWNLOAD_URL=%~2"
set "OUTPUT_DIR=%~3"

if "%EXE_PATH%"=="" set "EXE_PATH=dist\RemoteAgent.exe"
if "%OUTPUT_DIR%"=="" set "OUTPUT_DIR=dist\publish"

if "%DOWNLOAD_URL%"=="" (
  echo Usage:
  echo   prepare_update_package.bat [exe_path] [public_exe_url] [output_dir]
  echo Example:
  echo   prepare_update_package.bat dist\RemoteAgent.exe https://your-domain.com/remote-agent/RemoteAgent.exe dist\publish
  exit /b 1
)

if not exist "%EXE_PATH%" (
  echo EXE not found: %EXE_PATH%
  exit /b 1
)

if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

copy /Y "%EXE_PATH%" "%OUTPUT_DIR%\RemoteAgent.exe" >nul
if errorlevel 1 (
  echo Failed to copy EXE.
  exit /b 1
)

if exist "dist\AGENT_VERSION.txt" copy /Y "dist\AGENT_VERSION.txt" "%OUTPUT_DIR%\AGENT_VERSION.txt" >nul

py -3 generate_update_manifest.py --exe "%OUTPUT_DIR%\RemoteAgent.exe" --url "%DOWNLOAD_URL%" --output "%OUTPUT_DIR%\latest.json"
if errorlevel 1 (
  echo Failed to generate latest.json
  exit /b 1
)

echo.
echo Update package ready:
echo   %OUTPUT_DIR%\RemoteAgent.exe
echo   %OUTPUT_DIR%\latest.json
echo   %OUTPUT_DIR%\AGENT_VERSION.txt
echo.
echo Upload RemoteAgent.exe and latest.json to your server/CDN.

endlocal
exit /b 0
