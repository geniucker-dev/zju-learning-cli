@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
uv run --quiet --script "%~dp0zju.py" login --reset %*
echo.
pause
