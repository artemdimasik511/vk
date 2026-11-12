"""
====================================================================
Тестовый автономный запуск бота с ЛОКАЛЬНЫМИ МОДЕЛЯМИ (vk16_local_test)
====================================================================
Включает:
1. Локальное распознавание расписания по фото (Tesseract OCR + эвристический парсер)
2. Локальное офлайн-распознавание голосовых (Vosk)
3. Локальное исправление орфографии и нечеткий поиск (rapidfuzz NLP)
4. Локальный расчет дат практик (Zero-Token Parser)
5. Трехуровневый fallback для Gemini (Прокси -> Облачный европейский реле-шлюз -> Локальные движки)
"""

import os
import sys
import logging
from pathlib import Path

# Устанавливаем путь к текущей папке
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("VKStudentBot.LocalTest")

logger.info("=" * 65)
logger.info("🚀 Запуск ТЕСТОВОЙ ВЕРСИИ бота с локальными моделями")
logger.info("   • Локальное распознавание расписания: Tesseract OCR (rus + eng)")
logger.info("   • Локальное распознавание речи: Vosk (vosk-model-small-ru-0.22)")
logger.info("   • Локальное исправление опечаток: RapidFuzz NLP")
logger.info("   • Резервный шлюз Gemini: Cloud Relay (europe-west3)")
logger.info("=" * 65)

from main import VKStudentBot
from config import VK_TOKEN, VK_GROUP_ID

if __name__ == "__main__":
    if not VK_TOKEN:
        logger.error("ОШИБКА: VK_TOKEN не найден в .env или config.py!")
        sys.exit(1)
        
    bot = VKStudentBot(token=VK_TOKEN, group_id=VK_GROUP_ID)
    bot.run()
