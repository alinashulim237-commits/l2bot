@echo off
chcp 65001 >nul
cd /d "%~dp0"
"C:\Users\user\AppData\Local\Programs\Python\Python312\python.exe" test_telegram.py
echo.
pause
