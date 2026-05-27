@echo off
echo ============================================
echo    Установка агента по банкротным торгам
echo ============================================
echo.

cd /d C:\Users\user\Desktop\bankrot_agent

echo Шаг 1: Проверяем Python...
python --version
if errorlevel 1 (
    echo ОШИБКА: Python не найден!
    pause
    exit
)

echo.
echo Шаг 2: Обновляем pip...
python -m ensurepip --upgrade

echo.
echo Шаг 3: Устанавливаем библиотеки...
python -m pip install --upgrade pip
python -m pip install playwright==1.44.0 --prefer-binary
python -m pip install httpx==0.27.0 --prefer-binary
python -m pip install pdfplumber==0.11.1 --prefer-binary
python -m pip install python-telegram-bot==21.3 --prefer-binary
python -m pip install python-dotenv==1.0.1 --prefer-binary
python -m pip install schedule==1.2.2 --prefer-binary
python -m pip install openai==1.35.0 --prefer-binary

echo.
echo Шаг 4: Устанавливаем браузер для агента...
python -m playwright install chromium

echo.
echo Шаг 5: Запускаем агента...
python agent.py --now

echo.
echo ============================================
echo    Готово!
echo ============================================
pause
