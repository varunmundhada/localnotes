@echo off
rem Launch LocalNotes without a console window.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" app.py
