@echo off
REM ---------------------------------------------------------------------------
REM  start.bat - launch the Human Validation Review UI.
REM
REM  Double-click this file. It starts a small local server and opens your
REM  browser at http://localhost:5173. Leave the black window open while you
REM  review; close it when you are finished.
REM
REM  Requires Node.js (https://nodejs.org). If you just installed Node and a
REM  window says "node is not recognized", close any open terminals first -
REM  this script also falls back to the standard install path automatically.
REM ---------------------------------------------------------------------------
setlocal
set "HERE=%~dp0"

REM Prefer node on PATH; fall back to the standard install location.
set "NODE=node"
where node >nul 2>nul || set "NODE=%ProgramFiles%\nodejs\node.exe"

if not exist "%NODE%" (
  if "%NODE%"=="node" goto :run
  echo.
  echo   Could not find Node.js. Please install it from https://nodejs.org
  echo   then double-click start.bat again.
  echo.
  pause
  exit /b 1
)

:run
REM Open the browser a couple of seconds after the server starts.
start "" /min cmd /c "timeout /t 2 >nul & start "" http://localhost:5173"

echo.
echo   Starting the review UI...  (a browser window will open shortly)
echo   Keep this window open while you review. Close it when you are done.
echo.

"%NODE%" "%HERE%server.js"

echo.
echo   The server has stopped.
pause
