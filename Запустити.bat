@echo off
chcp 65001 >nul
title LUT Grab
cd /d "%~dp0"

where py >nul 2>&1 && (py -3 server.py & goto :eof)
where python >nul 2>&1 && (python server.py & goto :eof)

echo.
echo   Не знайшов Python.
echo.
echo   Постав його з python.org або з Microsoft Store,
echo   під час встановлення обов'язково постав галочку "Add to PATH".
echo.
echo   Або візьми готовий LUT Grab.exe — там Python уже всередині.
echo.
pause
