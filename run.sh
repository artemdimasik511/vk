#!/usr/bin/env bash
set -e

echo "========================================================"
echo " Запуск студенческого ВК-бота с Gemini AI (Linux/macOS)"
echo "========================================================"

if [ ! -d "venv" ]; then
    echo "[1/3] Создание venv..."
    python3 -m venv venv
fi

echo "[2/3] Установка зависимостей..."
source venv/bin/activate
pip install -r requirements.txt

echo "[3/3] Запуск main.py..."
python3 main.py
