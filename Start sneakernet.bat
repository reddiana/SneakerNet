@echo off
cd /d "%~dp0"
where pythonw >nul 2>&1 && (start "" pythonw sneakernet_gui.py) || (python sneakernet_gui.py)
