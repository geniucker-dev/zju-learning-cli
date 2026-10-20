@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
where uv >nul 2>nul
if errorlevel 1 (
  echo ZJU LearnPilot requires uv: https://docs.astral.sh/uv/getting-started/installation/ 1>&2
  pause
  exit /b 127
)
uv run --quiet --script "%~dp0zju.py" login --reset %*
echo.
pause
