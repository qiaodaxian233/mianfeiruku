@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在检查依赖...
python -m pip install -r requirements.txt -q
start "" pythonw steam_free_finder.py
