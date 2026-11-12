@echo off
chcp 65001 > nul
echo ========================================================
echo  Запуск студенческого ВК-бота с Gemini AI
echo ========================================================
echo.

if not exist venv (
    echo [1/3] Создание виртуального окружения Python...
    python -m venv venv
)

echo [2/3] Активация виртуального окружения и установка зависимостей...
call venv\Scripts\activate.bat
pip install -r requirements.txt

echo.
echo [3/3] Запуск бота (main.py)...
python main.py

pause
