@echo off
setlocal
cd /d "%~dp0"
if defined WEB_PYTHON (
  "%WEB_PYTHON%" run_web.py
) else (
  python -c "import fastapi, multipart, PIL, openai" >nul 2>nul
  if errorlevel 1 (
    where conda >nul 2>nul
    if errorlevel 1 (
      echo Python dependencies are missing. See docs/guides/web.md.
      pause
      exit /b 1
    )
    call conda run -n agent --no-capture-output python run_web.py
  ) else (
    python run_web.py
  )
)
if errorlevel 1 (
  echo.
  echo Please activate your Python environment and install requirements-web.txt.
  echo You can also set WEB_PYTHON to your Python executable path.
  pause
)
