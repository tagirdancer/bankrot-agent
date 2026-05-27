@echo off
cd /d C:\Users\user\Desktop\bankrot_agent
echo Устанавливаем playwright...
python -m pip install playwright --prefer-binary
echo.
echo Устанавливаем браузер...
python -m playwright install chromium
echo.
echo Запускаем агента...
python agent.py --now
echo.
pause
