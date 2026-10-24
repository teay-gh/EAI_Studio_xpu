@echo off
rem ============================================================================
rem EAI Studio launcher (2026-10-24)
rem
rem One script, two jobs:
rem   1) environment scan: Electron runtime (installed here if missing) and the
rem      Python training env (missing -> the setup screen takes over)
rem   2) start EAI Studio: full environment -> main window, missing -> setup screen
rem
rem Electron lookup order: %EAI_ELECTRON% -> <app>\node_modules\electron\dist\electron.exe
rem                         -> <parent>\node_modules\electron\dist\electron.exe -> PATH
rem
rem Node.js / npm are OPTIONAL. The bundled downloader (tools\get-electron.ps1)
rem fetches the electron zip itself, so a user with a bare Windows install can
rem still get the app running. npm is only a fallback if that script is missing.
rem
rem Downloads: every byte stays inside <app>\_install_cache (the zip, npm cache,
rem electron's own cache, curl log) and that folder is deleted when done.
rem Console output is English on purpose - the setup screen has the language switch.
rem
rem Mirror policy (2026-10-24): machine-level ELECTRON_MIRROR is ignored, so the
rem official GitHub release is always tried first; the npmmirror CDN is only used
rem when the official download fails or stays below 50 KB/s for 20 seconds.
rem ============================================================================
setlocal EnableDelayedExpansion
set "ELECTRON_RUN_AS_NODE="
set "APP=%~dp0"
if "%APP:~-1%"=="\" set "APP=%APP:~0,-1%"
set "CACHE=%APP%\_install_cache"

echo.
echo   EAI Studio - startup check
echo   ------------------------------------------

rem ---------- [1/3] Electron runtime ----------
set "ELECTRON="
if defined EAI_ELECTRON if exist "%EAI_ELECTRON%" set "ELECTRON=%EAI_ELECTRON%"
if not defined ELECTRON if exist "%APP%\node_modules\electron\dist\electron.exe" set "ELECTRON=%APP%\node_modules\electron\dist\electron.exe"
if not defined ELECTRON if exist "%APP%\..\node_modules\electron\dist\electron.exe" set "ELECTRON=%APP%\..\node_modules\electron\dist\electron.exe"
if not defined ELECTRON for %%I in (electron.exe) do if exist "%%~$PATH:I" set "ELECTRON=%%~$PATH:I"
if defined ELECTRON goto :electron_ready

echo   [1/3] Electron runtime .... missing, installing ^(first run needs network, ~100-300 MB^)
if not exist "%CACHE%" mkdir "%CACHE%"
rem ignore the machine-level mirror vars: official source first (see header)
set "ELECTRON_MIRROR="
set "ELECTRON_BUILDER_BINARIES_MIRROR="
set "electron_config_cache=%CACHE%\electron"

if not exist "%APP%\tools\get-electron.ps1" goto :install_via_npm
echo         fetching the electron binary ^(official source first, mirror as fallback^) ...
call powershell -NoProfile -ExecutionPolicy Bypass -File "%APP%\tools\get-electron.ps1" -AppDir "%APP%"
echo         downloader exit code !errorlevel!
goto :install_done

:install_via_npm
echo         tools\get-electron.ps1 not found - falling back to npm
where npm >nul 2>nul
if errorlevel 1 goto :no_npm
pushd "%APP%"
call npm install --no-audit --no-fund --cache "%CACHE%\npm"
popd
if exist "%APP%\node_modules\electron\dist\electron.exe" goto :install_done
if exist "%APP%\node_modules\electron\install.js" (
  call node "%APP%\node_modules\electron\install.js"
  echo         install.js exit code !errorlevel!
)

:install_done
if exist "%APP%\node_modules\electron\dist\electron.exe" set "ELECTRON=%APP%\node_modules\electron\dist\electron.exe"
if exist "%CACHE%" rmdir /s /q "%CACHE%"
if defined ELECTRON goto :electron_ready

echo   [x] Electron install failed
echo       expected: %APP%\node_modules\electron\dist\electron.exe
echo       check the network / proxy, then run this script again
pause
exit /b 3

:no_npm
echo         [x] neither tools\get-electron.ps1 nor Node.js / npm is available
echo             Restore tools\get-electron.ps1 from the release package, or
echo             install Node.js LTS: https://nodejs.org/en/download
if exist "%CACHE%" rmdir /s /q "%CACHE%"
pause
exit /b 3

:electron_ready
if defined ELECTRON echo   [1/3] Electron runtime .... ready

rem ---------- [2/3] Python training env ----------
if exist "%APP%\.venv\Scripts\python.exe" (
  echo   [2/3] Python runtime .... ready
  goto :py_done
)
if exist "%APP%\..\.venv\Scripts\python.exe" (
  echo   [2/3] Python runtime .... ready ^(parent folder .venv^)
  goto :py_done
)
echo   [2/3] Python runtime .... missing, will be installed in the setup screen

:py_done
rem ---------- [3/3] start ----------
if not exist "%APP%\main.js" (
  echo   [x] entry point not found: %APP%\main.js
  pause
  exit /b 4
)

echo   [3/3] Starting EAI Studio ...
echo.
start "" /D "%APP%" "%ELECTRON%" "%APP%" --remote-debugging-port=9333 --disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows
exit /b 0
