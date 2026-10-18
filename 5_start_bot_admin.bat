@echo off
cd /d "%~dp0"
taskkill /F /IM pythonw.exe 2>nul
ping -n 2 127.0.0.1 >nul
start "" "C:\Users\user\AppData\Local\Programs\Python\Python312\pythonw.exe" bot.py
