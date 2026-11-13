"""
====================================================================
Локальный модуль распознавания расписания из фотографий (OCR)
====================================================================
Использует Tesseract OCR + эвристический парсер таблиц расписания.
Полностью офлайн, 0 токенов Gemini, работает на Windows и Linux.

Алгоритм:
1. Предобработка изображения (контраст, бинаризация, масштабирование)
2. Извлечение текста через Tesseract (русский язык)
3. Парсинг структуры расписания (дни недели, время, предметы, преподаватели, аудитории)
4. Нечёткое сопоставление (fuzzy matching) для исправления ошибок OCR
"""

import re
import os
import io
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("VKStudentBot.LocalOCR")

# Попытка импорта Tesseract
TESSERACT_AVAILABLE = False
try:
    import pytesseract
    from PIL import Image, ImageFilter, ImageEnhance, ImageOps
    TESSERACT_AVAILABLE = True
except ImportError:
    pytesseract = None
    Image = None
    logger.info("pytesseract или Pillow не установлены (pip install pytesseract Pillow).")

# Попытка импорта rapidfuzz для нечёткого исправления ошибок OCR
RAPIDFUZZ_AVAILABLE = False
try:
    from rapidfuzz import fuzz, process as rfprocess
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    logger.info("rapidfuzz не установлен (pip install rapidfuzz). Fuzzy-коррекция отключена.")


# ====================================================================
# Словари для парсинга расписания
# ====================================================================

DAY_NAMES = {
    "понедельник": 0, "пн": 0,
    "вторник": 1, "вт": 1,
    "среда": 2, "ср": 2,
    "четверг": 3, "чт": 3,
    "пятница": 4, "пт": 4,
    "суббота": 5, "сб": 5,
    "воскресенье": 6, "вс": 6,
}

# Типичные тайм-слоты пар (для нечёткого сопоставления)
COMMON_TIME_SLOTS = [
    "08:30 - 10:00", "10:10 - 11:40", "11:50 - 13:20",
    "14:00 - 15:30", "15:40 - 17:10", "17:20 - 18:50",
    "08:00 - 09:30", "09:40 - 11:10", "11:20 - 12:50",
    "13:00 - 14:30", "14:40 - 16:10", "16:20 - 17:50",
]

# Паттерны времени в тексте OCR
TIME_PATTERN = re.compile(
    r'(\d{1,2})\s*[:\.\-]\s*(\d{2})\s*[\-–—]\s*(\d{1,2})\s*[:\.\-]\s*(\d{2})'
)

# Паттерн для ФИО преподавателя (Фамилия И.О. или Фамилия)
TEACHER_PATTERN = re.compile(
    r'([А-ЯЁ][а-яё]{2,20})\s+([А-ЯЁ])\s*\.\s*([А-ЯЁ])\s*\.',
    re.UNICODE
)

# Паттерн аудитории
ROOM_PATTERN = re.compile(
    r'(?:ауд\.?\s*|каб\.?\s*|корп\.?\s*\d+\s*,?\s*)?(\d{1,3}(?:\s*,\s*\d{1,3})*)',
    re.IGNORECASE
)


def _auto_detect_tesseract() -> Optional[str]:
    """Автоматически находит установленный Tesseract OCR."""
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"C:\Users\{}\AppData\Local\Tesseract-OCR\tesseract.exe".format(os.environ.get("USERNAME", "")),
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # Пробуем найти через PATH
    import shutil
    found = shutil.which("tesseract")
    return found


def preprocess_image(image_bytes: bytes) -> "Image.Image":
    """
    Предобработка фотографии расписания для улучшения качества OCR:
    - Конвертация в оттенки серого
    - Увеличение контраста
    - Бинаризация (Otsu-like через адаптивный порог)
    - Масштабирование мелкого текста
    """
    img = Image.open(io.BytesIO(image_bytes))

    # Увеличиваем маленькие изображения (OCR лучше работает с крупным текстом)
    min_width = 2000
    if img.width < min_width:
        scale = min_width / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)

    # Конвертируем в оттенки серого
    img = img.convert("L")

    # Увеличиваем контраст
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.0)

    # Увеличиваем резкость
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(2.0)

    # Бинаризация (чёрно-белое для OCR)
    threshold = 140
    img = img.point(lambda x: 255 if x > threshold else 0, mode='1')

    return img


def extract_text_from_image(image_bytes: bytes) -> str:
    """Извлекает текст из изображения через Tesseract OCR."""
    if not TESSERACT_AVAILABLE:
        raise RuntimeError("pytesseract не установлен. Установите: pip install pytesseract Pillow")

    # Автоопределение пути к Tesseract
    tesseract_path = _auto_detect_tesseract()
    if tesseract_path:
        pytesseract.pytesseract.tesseract_cmd = tesseract_path

    # Настройка папки с языковыми моделями (tessdata)
    local_tessdata = Path(__file__).resolve().parent / "tessdata"
    if local_tessdata.exists():
        os.environ["TESSDATA_PREFIX"] = str(local_tessdata)

    img = preprocess_image(image_bytes)

    try:
        text = pytesseract.image_to_string(img, lang="rus+eng", config="--psm 6 --oem 3")
        logger.info(f"📝 [OCR] Извлечено {len(text)} символов из фотографии расписания")
        return text
    except Exception as e:
        logger.error(f"Ошибка Tesseract OCR: {e}")
        raise RuntimeError(f"Не удалось распознать текст: {e}")


def _fuzzy_match_day(raw_text: str) -> Optional[int]:
    """Нечёткое определение дня недели из текста OCR (с учётом опечаток)."""
    text_lower = raw_text.lower().strip()
    
    # Точное совпадение
    for name, dow in DAY_NAMES.items():
        if name in text_lower:
            return dow
    
    # Fuzzy matching если rapidfuzz доступен
    if RAPIDFUZZ_AVAILABLE and len(text_lower) >= 2:
        full_day_names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        match = rfprocess.extractOne(text_lower, full_day_names, scorer=fuzz.ratio, score_cutoff=60)
        if match:
            return full_day_names.index(match[0])
    
    return None


def _normalize_time(h1: str, m1: str, h2: str, m2: str) -> str:
    """Нормализует время пары в формат 'HH:MM - HH:MM'."""
    return f"{int(h1):02d}:{m1} - {int(h2):02d}:{m2}"


def _fuzzy_match_time(raw_time: str) -> str:
    """Нечёткое сопоставление OCR-времени с типичными тайм-слотами."""
    if not RAPIDFUZZ_AVAILABLE:
        return raw_time
    match = rfprocess.extractOne(raw_time, COMMON_TIME_SLOTS, scorer=fuzz.ratio, score_cutoff=70)
    if match:
        return match[0]
    return raw_time


def _detect_week_type(line: str) -> str:
    """Определяет тип недели по маркерам ▲/■ или текстовым меткам."""
    if "▲" in line or "нечет" in line.lower() or "нечёт" in line.lower():
        return "ODD"
    if "■" in line or "чет" in line.lower():
        # Проверяем, что это не "нечет"
        if "нечет" not in line.lower() and "нечёт" not in line.lower():
            return "EVEN"
    return "ALL"


def _detect_lesson_type(line: str) -> str:
    """Определяет тип занятия по маркерам."""
    line_lower = line.lower()
    if any(m in line_lower for m in ["(л)", "лекция", "лек.", "лек ", "лк"]):
        return "LECTURE"
    if any(m in line_lower for m in ["(пр)", "практ", "семин", "(сем)", "лаб"]):
        return "PRACTICE"
    return "PRACTICE"  # По умолчанию практика


def _extract_room(line: str) -> str:
    """Извлекает номер аудитории из строки."""
    # Ищем паттерн "ауд. XXX" или "корп. X, XXX"
    m = re.search(r'(?:ауд\.?\s*|каб\.?\s*|корп\.?\s*\d+\s*[,;]\s*)(\d{1,4}(?:\s*[,;]\s*\d{1,4})*)', line, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    
    # Число (номер аудитории) в конце строки
    m = re.search(r'\b(\d{1,4}(?:\s*,\s*\d{1,4})*)\s*$', line)
    if m:
        return m.group(1).strip()
    
    return ""


def _extract_teacher(line: str) -> str:
    """Извлекает ФИО преподавателя из строки."""
    m = TEACHER_PATTERN.search(line)
    if m:
        return f"{m.group(1)} {m.group(2)}.{m.group(3)}."
    return ""


def _clean_subject_name(raw: str) -> str:
    """Очищает название предмета от маркеров типа занятия и мусора OCR."""
    cleaned = raw.strip()
    # Убираем маркеры типа занятия
    cleaned = re.sub(r'\s*\((?:л|пр|сем|лаб|лек)\)\s*', ' ', cleaned, flags=re.IGNORECASE)
    # Убираем маркеры недели
    cleaned = cleaned.replace("▲", "").replace("■", "").replace("△", "").replace("□", "")
    # Убираем лишние символы OCR
    cleaned = re.sub(r'[|{}\[\]~`@#$%^&*_=+]', '', cleaned)
    # Сжимаем пробелы
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .,;:-')
    return cleaned


def parse_schedule_from_ocr_text(raw_text: str) -> List[Dict[str, Any]]:
    """
    Парсит структуру расписания из текста, извлечённого OCR.
    Работает с типичными форматами расписания вузов:
    - Строки с днями недели как заголовки
    - Строки с временем пар
    - Названия предметов, ФИО преподавателей, аудитории
    """
    lines = raw_text.split("\n")
    lessons = []
    current_day: Optional[int] = None
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or len(line) < 2:
            i += 1
            continue
        
        # 1. Проверяем, является ли строка заголовком дня недели
        detected_day = _fuzzy_match_day(line)
        if detected_day is not None and len(line) < 25:
            current_day = detected_day
            i += 1
            continue
        
        # 2. Ищем время пары
        time_match = TIME_PATTERN.search(line)
        if time_match and current_day is not None:
            raw_time = _normalize_time(
                time_match.group(1), time_match.group(2),
                time_match.group(3), time_match.group(4)
            )
            time_str = _fuzzy_match_time(raw_time)
            
            # Собираем блок строк для этой пары (до следующего времени или дня)
            block_lines = [line]
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if not next_line:
                    j += 1
                    continue
                # Если это новое время или новый день — стоп
                if TIME_PATTERN.search(next_line):
                    break
                next_day = _fuzzy_match_day(next_line)
                if next_day is not None and len(next_line) < 25:
                    break
                block_lines.append(next_line)
                j += 1
            
            # Из блока извлекаем предмет, преподавателя, аудиторию, тип
            full_block = " ".join(block_lines)
            
            week_type = _detect_week_type(full_block)
            lesson_type = _detect_lesson_type(full_block)
            teacher = _extract_teacher(full_block)
            room = _extract_room(full_block)
            
            # Название предмета — всё, что не время, не преподаватель, не аудитория
            subject_text = full_block
            # Убираем время
            subject_text = TIME_PATTERN.sub("", subject_text)
            # Убираем ФИО
            if teacher:
                subject_text = subject_text.replace(teacher, "")
            # Убираем аудиторию (осторожно, только если нашли)
            if room:
                subject_text = re.sub(
                    r'(?:ауд\.?\s*|каб\.?\s*|корп\.?\s*\d+\s*[,;]\s*)?' + re.escape(room) + r'\b',
                    '', subject_text, flags=re.IGNORECASE
                )
            subject = _clean_subject_name(subject_text)
            
            if subject and len(subject) >= 3:
                lesson = {
                    "day_of_week": current_day,
                    "time": time_str,
                    "subject": subject,
                    "teacher": teacher,
                    "room": room,
                    "week_type": week_type,
                    "lesson_type": lesson_type,
                }
                lessons.append(lesson)
            
            i = j
            continue
        
        # 3. Если нет текущего дня, пробуем найти день в этой строке
        if current_day is None:
            detected_day = _fuzzy_match_day(line)
            if detected_day is not None:
                current_day = detected_day
        
        i += 1
    
    logger.info(f"📋 [OCR Parser] Извлечено {len(lessons)} пар из текста расписания")
    return lessons


def recognize_schedule_local(image_bytes: bytes, mime_type: str = "image/jpeg") -> List[Dict[str, Any]]:
    """
    Полностью локальное распознавание расписания из фотографии:
    1. Tesseract OCR → текст
    2. Эвристический парсер → структурированные данные
    
    Возвращает список пар в том же формате, что и Gemini:
    [{"day_of_week": 0, "time": "08:30 - 10:00", "subject": "...", "teacher": "...", "room": "...", "week_type": "ALL", "lesson_type": "PRACTICE"}]
    """
    if not TESSERACT_AVAILABLE:
        raise RuntimeError(
            "Для локального распознавания расписания требуется Tesseract OCR.\n"
            "Windows: https://github.com/UB-Mannheim/tesseract/wiki\n"
            "Linux: sudo apt install tesseract-ocr tesseract-ocr-rus\n"
            "Затем: pip install pytesseract Pillow"
        )
    
    raw_text = extract_text_from_image(image_bytes)
    if not raw_text or len(raw_text.strip()) < 10:
        raise RuntimeError("OCR не смог извлечь достаточно текста из фотографии")
    
    logger.debug(f"[OCR Raw Text]:\n{raw_text[:500]}...")
    
    lessons = parse_schedule_from_ocr_text(raw_text)
    if not lessons:
        raise RuntimeError(
            "Не удалось разобрать структуру расписания из OCR-текста.\n"
            "Попробуйте сделать более чёткое фото расписания при хорошем освещении."
        )
    
    return lessons
