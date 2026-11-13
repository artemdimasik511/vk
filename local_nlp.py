"""
====================================================================
Локальный NLP-модуль для исправления ошибок распознавания речи
====================================================================
Нечёткое сопоставление (fuzzy matching) распознанного текста Vosk
с фамилиями преподавателей и названиями предметов из расписания студента.

Работает полностью офлайн, 0 токенов Gemini.
Исправляет:
- Орфографические ошибки Vosk ("гардин" → "Гордин", "матафонава" → "Матафонова")
- Ошибки в названиях предметов ("аксилогические" → "Аксиологические")
- Типичные замены при распознавании русской речи
"""

import re
import logging
from typing import List, Dict, Any, Optional, Tuple, Set

logger = logging.getLogger("VKStudentBot.LocalNLP")

# Попытка импорта rapidfuzz
RAPIDFUZZ_AVAILABLE = False
try:
    from rapidfuzz import fuzz, process as rfprocess
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    logger.info("rapidfuzz не установлен. Fuzzy-коррекция отключена (pip install rapidfuzz).")


# ====================================================================
# Типичные ошибки распознавания русской речи (Vosk / другие ASR)
# ====================================================================

# Словарь частых замен при распознавании голоса
PHONETIC_CORRECTIONS = {
    # Учебная лексика
    "домашнее": "домашнее",
    "домашние": "домашнее",
    "домашку": "домашку",
    "домашка": "домашка",
    "задание": "задание",
    "задания": "задания",
    "лекция": "лекция",
    "лекцию": "лекцию",
    "практика": "практика",
    "практику": "практику",
    "семинар": "семинар",
    "семинары": "семинары",
    "зачёт": "зачёт",
    "зачет": "зачёт",
    "экзамен": "экзамен",
    # Ошибочные формы
    "дэ зэ": "дз",
    "дэзэ": "дз",
    "де зе": "дз",
    "ди зи": "дз",
    "дэ зе": "дз",
    "казал": "сказал",
    "казала": "сказала",
}

# Ключевые слова, которые НЕ нужно менять на фамилии преподавателей
PROTECTED_WORDS = {
    "нужно", "надо", "задал", "задала", "задали", "собрать", "сделать",
    "написать", "решить", "подготовить", "выучить", "прочитать", "выполнить",
    "ответить", "составить", "заполнить", "разобрать", "повторить",
    "принести", "сдать", "оформить", "пройти", "найти", "скачать",
    "конспект", "доклад", "презентацию", "презентация", "реферат",
    "номер", "номера", "страница", "глава", "задачи", "задачу",
    "через", "занятие", "занятия", "следующей", "следующую", "паре", "пары", "пару",
    "эту", "неделю", "неделе", "понедельник", "вторник", "среда",
    "четверг", "пятница", "суббота", "воскресенье", "завтра", "послезавтра",
    "домашнее", "домашка", "домашку",
}


def build_vocabulary(user_schedule: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Строит словарь для нечёткой коррекции из расписания студента:
    - Фамилии преподавателей (все падежные формы)
    - Ключевые слова названий предметов
    """
    teachers: Dict[str, str] = {}   # lowercase_stem -> canonical_form
    subjects: Dict[str, str] = {}   # lowercase_keyword -> canonical_subject_name
    subject_keywords: List[str] = []

    for lesson in user_schedule:
        # Преподаватели
        teacher = (lesson.get("teacher") or "").strip()
        if teacher:
            surname = teacher.split()[0].replace(",", "").replace(".", "")
            if len(surname) >= 3:
                teachers[surname.lower()] = teacher
                # Добавляем падежные основы
                for suffix in ["", "а", "у", "ом", "е", "ой", "ы", "ин", "ину", "ином"]:
                    stem = surname.lower()
                    if stem.endswith(suffix) and suffix and len(stem) > len(suffix) + 2:
                        stem_base = stem[:-len(suffix)]
                        teachers[stem_base] = teacher

        # Предметы
        subject = (lesson.get("subject") or "").strip()
        if subject:
            # Извлекаем значимые слова предмета (>= 4 букв)
            words = re.findall(r'[а-яёА-ЯЁa-zA-Z]{4,}', subject)
            for w in words:
                w_lower = w.lower()
                if w_lower not in {"основы", "основам", "деятельности", "образования", "работы"}:
                    subjects[w_lower] = subject
                    subject_keywords.append(w_lower)

    return {
        "teachers": teachers,
        "subjects": subjects,
        "subject_keywords": list(set(subject_keywords)),
    }


def correct_word_fuzzy(
    word: str,
    vocabulary: Dict[str, Any],
    threshold: int = 72
) -> str:
    """
    Исправляет одно слово через нечёткое сопоставление с словарём расписания.
    Порог (threshold) = 72 — оптимальный баланс между коррекцией и ложными срабатываниями.
    """
    if not RAPIDFUZZ_AVAILABLE:
        return word

    word_lower = word.lower().strip()
    
    # Не трогаем короткие слова и защищённые ключевые слова
    if len(word_lower) < 4 or word_lower in PROTECTED_WORDS:
        return word

    # 1. Проверяем фамилии преподавателей
    teacher_names = list(vocabulary.get("teachers", {}).keys())
    if teacher_names:
        match = rfprocess.extractOne(word_lower, teacher_names, scorer=fuzz.ratio, score_cutoff=threshold)
        if match:
            canonical_teacher = vocabulary["teachers"][match[0]]
            surname = canonical_teacher.split()[0]
            logger.debug(f"🔧 Fuzzy-коррекция фамилии: '{word}' → '{surname}' (score={match[1]:.0f}%)")
            return surname

    # 2. Проверяем ключевые слова предметов
    subj_keywords = vocabulary.get("subject_keywords", [])
    if subj_keywords:
        match = rfprocess.extractOne(word_lower, subj_keywords, scorer=fuzz.ratio, score_cutoff=threshold)
        if match:
            # Возвращаем слово с исправлением регистра
            corrected = match[0]
            if word[0].isupper():
                corrected = corrected[0].upper() + corrected[1:]
            logger.debug(f"🔧 Fuzzy-коррекция предмета: '{word}' → '{corrected}' (score={match[1]:.0f}%)")
            return corrected

    return word


def correct_transcript(
    raw_text: str,
    user_schedule: List[Dict[str, Any]]
) -> str:
    """
    Исправляет текст распознавания речи (Vosk или другой ASR) через:
    1. Словарь фонетических замен (частые ошибки русской ASR)
    2. Нечёткое сопоставление каждого слова с фамилиями преподавателей
       и названиями предметов из расписания конкретного студента
    
    Возвращает очищенный и исправленный текст.
    """
    if not raw_text or not raw_text.strip():
        return raw_text

    text = raw_text.strip()

    # 1. Применяем словарь фонетических замен (строго по целым словам через границы слов \b)
    text_lower = text.lower()
    for wrong, correct in PHONETIC_CORRECTIONS.items():
        if wrong in text_lower:
            pattern = re.compile(rf'\b{re.escape(wrong)}\b', re.IGNORECASE)
            text = pattern.sub(correct, text)
            text_lower = text.lower()

    # 2. Если нет расписания — возвращаем с базовыми исправлениями
    if not user_schedule:
        return text

    # 3. Строим словарь и исправляем пословно
    vocab = build_vocabulary(user_schedule)

    words = text.split()
    corrected_words = []

    for word in words:
        # Очищаем от пунктуации для сопоставления
        clean = re.sub(r'^[^а-яёА-ЯЁa-zA-Z]+|[^а-яёА-ЯЁa-zA-Z]+$', '', word)
        if not clean or len(clean) < 3:
            corrected_words.append(word)
            continue

        corrected = correct_word_fuzzy(clean, vocab)
        if corrected != clean:
            # Восстанавливаем пунктуацию вокруг слова
            prefix = word[:word.find(clean[0])] if clean[0] in word else ""
            suffix_start = word.rfind(clean[-1])
            suffix = word[suffix_start + 1:] if suffix_start >= 0 and suffix_start + 1 < len(word) else ""
            corrected_words.append(prefix + corrected + suffix)
        else:
            corrected_words.append(word)

    result = " ".join(corrected_words)

    if result != raw_text:
        logger.info(f"🔧 [NLP] Текст после коррекции: «{result}»")

    return result


def fuzzy_match_subject_from_schedule(
    text: str,
    user_schedule: List[Dict[str, Any]],
    threshold: int = 55
) -> Optional[Dict[str, Any]]:
    """
    Нечёткий поиск наиболее подходящего предмета из расписания для текста.
    Возвращает запись расписания (lesson dict) с максимальным сходством,
    или None если сходство ниже порога.
    """
    if not RAPIDFUZZ_AVAILABLE or not user_schedule:
        return None

    text_lower = text.lower().strip()

    # Собираем уникальные предметы
    unique_subjects = {}
    for lesson in user_schedule:
        subj = (lesson.get("subject") or "").strip()
        if subj and subj not in unique_subjects:
            unique_subjects[subj] = lesson

    if not unique_subjects:
        return None

    subject_names = list(unique_subjects.keys())
    
    # Ищем лучшее совпадение по частичному сходству
    match = rfprocess.extractOne(
        text_lower,
        [s.lower() for s in subject_names],
        scorer=fuzz.partial_ratio,
        score_cutoff=threshold
    )

    if match:
        idx = [s.lower() for s in subject_names].index(match[0])
        matched_subject = subject_names[idx]
        logger.debug(f"🎯 Fuzzy match предмета: «{text}» → «{matched_subject}» (score={match[1]:.0f}%)")
        return unique_subjects[matched_subject]

    return None


def fuzzy_match_teacher_from_schedule(
    text: str,
    user_schedule: List[Dict[str, Any]],
    threshold: int = 65
) -> Optional[str]:
    """
    Нечёткий поиск фамилии преподавателя в тексте.
    Возвращает каноническое ФИО ("Гордин А.И.") или None.
    """
    if not RAPIDFUZZ_AVAILABLE or not user_schedule:
        return None

    text_lower = text.lower().strip()
    words = re.findall(r'[а-яёА-ЯЁ]{3,}', text_lower)

    teachers = {}
    for lesson in user_schedule:
        teacher = (lesson.get("teacher") or "").strip()
        if teacher:
            surname = teacher.split()[0].replace(",", "").replace(".", "").lower()
            if len(surname) >= 3:
                teachers[surname] = teacher

    if not teachers:
        return None

    teacher_surnames = list(teachers.keys())

    for word in words:
        if word in PROTECTED_WORDS or len(word) < 3:
            continue
        match = rfprocess.extractOne(word, teacher_surnames, scorer=fuzz.ratio, score_cutoff=threshold)
        if match:
            return teachers[match[0]]

    return None
