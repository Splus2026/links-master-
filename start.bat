@echo off
cd /d "%~dp0"
python links_loader.py
if errorlevel 1 pause
