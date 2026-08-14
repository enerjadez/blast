@echo off
setlocal
cd /d "%~dp0"
title BLAST
echo.
echo  Starting BLAST...
echo  Leave this window open. Close it to stop.
echo.
python "%~dp0blast.py" %*
if errorlevel 1 (
  echo.
  echo  BLAST exited with an error.
  pause
)
endlocal
