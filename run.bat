@echo off
cd /d "%~dp0"
pip install -q -r requirements.txt
start "" http://localhost:8010
python app.py
