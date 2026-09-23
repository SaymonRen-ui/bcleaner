@echo off
chcp 65001 >nul
if not exist .venv (
  echo [*] Create venv...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
python main.py
pause
