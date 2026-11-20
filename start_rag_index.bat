@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" rag_index.py --loop
pause
