import os
import json
import re
import base64
import hashlib
import logging
from typing import List, Dict, Any, Optional
from datetime import date, timedelta
import time
import urllib.request
import urllib.error

from config import (
    GEMINI_API_KEY,
    GEMINI_API_KEY_BACKUP,
    GEMINI_API_KEYS,
    GEMINI_MODEL,
    GEMINI_PROXY,
    GEMINI_RELAY_URL
)
from week_helper import (
    get_week_type,
    format_day_name,
    normalize_target_date,
    WEEK_TYPE_ODD,
    WEEK_TYPE_EVEN,
    WEEK_TYPE_ALL
)

logger = logging.getLogger(__name__)

# Кэш распознанных медиафайлов (SHA-256 -> результат) для 100% экономии токенов при повторной отправке
_media_cache: Dict[str, Any] = {}

# Попытка импорта официального google.generativeai SDK (подавляем предупреждение FutureWarning в консоли)
GENAI_AVAILABLE = False
try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    genai = None
    logger.info("Пакет google-generativeai не установлен. Используется встроенный REST/Relay API Gemini.")


def configure_gemini(api_key: Optional[str] = None):
    """Настройка клиента Gemini API при наличии SDK."""
    target_key = api_key or GEMINI_API_KEY
    if GENAI_AVAILABLE and genai and target_key:
        try:
            genai.configure(api_key=target_key)
        except Exception as e:
            logger.warning(f"Ошибка вызова genai.configure: {e}")


def clean_json_response(raw_text: str) -> str:
    """
    Очищает ответ модели от возможных markdown-тегов ```json ... ``` и пробелов.
    """
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


def call_relay_api(endpoint: str, payload: dict) -> dict:
    """
    Ретрансляция запроса через европейский облачный сервер (Германия, Франкфурт).
    Позволяет студентам из РФ и других регионов работать с Gemini без настройки собственного VPN/прокси.
    """
    if not GEMINI_RELAY_URL:
        raise ValueError("GEMINI_RELAY_URL не настроен в .env")

    url = f"{GEMINI_RELAY_URL.rstrip('/')}/api/gemini/{endpoint}"
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "VKStudentBot/1.0"
        },
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_gemini_rest(
    model_name: str,
    prompt_text: str,
    media_bytes: Optional[bytes] = None,
    mime_type: Optional[str] = None,
    api_key: Optional[str] = None
) -> str:
    """
    Прямой вызов REST API Google Gemini через urllib с поддержкой прокси,
    экономией выходных токенов и поддержкой текстовых/мультимодальных запросов.
    """
    target_key = api_key or GEMINI_API_KEY
    if not target_key:
        raise ValueError("Ключ GEMINI_API_KEY не задан в .env или config.py")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={target_key}"
    
    parts: List[Dict[str, Any]] = [{"text": prompt_text}]
    if media_bytes and mime_type:
        b64_data = base64.b64encode(media_bytes).decode("utf-8")
        parts.append({
            "inline_data": {
                "mime_type": mime_type,
                "data": b64_data
            }
        })
    
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2048
        }
    }
    
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "aistudio-build-vkbot"
        },
        method="POST"
    )

    handlers = []
    if GEMINI_PROXY:
        logger.info(f"Используется прокси для Gemini: {GEMINI_PROXY}")
        handlers.append(urllib.request.ProxyHandler({"http": GEMINI_PROXY, "https": GEMINI_PROXY}))

    opener = urllib.request.build_opener(*handlers)

    try:
        # Таймаут 30 сек (вместо 15), чтобы тяжелые таблицы расписания не обрывались на полпути
        with opener.open(req, timeout=30) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            
            candidates = res_data.get("candidates", [])
            if candidates:
                resp_parts = candidates[0].get("content", {}).get("parts", [])
                if resp_parts and "text" in resp_parts[0]:
                    return resp_parts[0]["text"]
    except urllib.error.HTTPError as http_err:
        err_body = http_err.read().decode("utf-8", errors="ignore")
        if "User location is not supported" in err_body:
            raise RuntimeError(f"User location is not supported: {err_body}")
        raise RuntimeError(f"HTTP {http_err.code}: {err_body}")

    raise RuntimeError("Gemini REST API вернул пустой ответ или некорректный формат")


# Глобальный указатель на текущий активный ключ и словарь меток времени кулдауна
_active_key_index: int = 0
_key_exhausted_until: Dict[int, float] = {}


def generate_content_with_fallback(
    prompt_text: str,
    media_bytes: Optional[bytes] = None,
    mime_type: Optional[str] = None
) -> str:
    """
    Универсальный вызов Gemini с автоматическим запоминанием работающего ключа,
    авто-повтором (retry) при пиковой нагрузке 503 UNAVAILABLE и кулдауном исчерпанных ключей (429).
    """
    global _active_key_index, _key_exhausted_until

    # Убрали дублирующий алиас gemini-flash-latest, чтобы не тратить двойную квоту на ту же модель
    models_to_try = [GEMINI_MODEL, "gemini-3.8-flash", "gemini-3.1-flash-lite"]
    unique_models = list(dict.fromkeys(models_to_try))
    keys_to_try = GEMINI_API_KEYS if GEMINI_API_KEYS else [GEMINI_API_KEY]
    now = time.time()
    last_error = None

    # Порядок проверки ключей: первым идет текущий активный ключ!
    key_order: List[int] = []
    if 0 <= _active_key_index < len(keys_to_try):
        key_order.append(_active_key_index)
    for idx in range(len(keys_to_try)):
        if idx not in key_order:
            key_order.append(idx)

    for key_idx in key_order:
        key = keys_to_try[key_idx]
        if not key:
            continue
        key_label = "Основной" if key_idx == 0 else f"Резервный #{key_idx}"

        # Если ключ недавно вернул ошибку квоты 429, мгновенно пропускаем его без задержек
        if key_idx in _key_exhausted_until and now < _key_exhausted_until[key_idx]:
            logger.info(f"Ключ {key_label} на кулдауне (лимит исчерпан). Пропускаем...")
            continue

        key_quota_exhausted = False
        for m in unique_models:
            if key_quota_exhausted:
                break

            # До 2 попыток на каждую модель при временном всплеске нагрузки HTTP 503 (high demand)
            for attempt in range(2):
                try:
                    logger.info(
                        f"Запрос к Gemini через REST API ({key_label} ключ, модель: {m}, попытка {attempt + 1})..."
                    )
                    text = call_gemini_rest(
                        model_name=m,
                        prompt_text=prompt_text,
                        media_bytes=media_bytes,
                        mime_type=mime_type,
                        api_key=key
                    )
                    if text:
                        _active_key_index = key_idx
                        return text
                except Exception as e:
                    err_str = str(e)
                    if "User location is not supported" in err_str:
                        raise
                    logger.warning(f"REST Gemini ({key_label} ключ, {m}, попытка {attempt + 1}) ошибка: {e}")
                    last_error = e

                    # Если лимит квоты исчерпан (429, RESOURCE_EXHAUSTED)
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                        logger.warning(
                            f"⚠️ Лимит квоты на ключе {key_label} исчерпан. Помещаем на кулдаун (10 минут) и переключаем активный ключ!"
                        )
                        _key_exhausted_until[key_idx] = time.time() + 600
                        _active_key_index = (key_idx + 1) % len(keys_to_try)
                        key_quota_exhausted = True
                        break

                    # Если сервер Google перегружен (HTTP 503 High Demand / UNAVAILABLE) или таймаут — ждем 2 сек и повторяем
                    if ("503" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str.lower() or "timed out" in err_str.lower()) and attempt == 0:
                        logger.info(f"⏳ Сервер модели {m} временно перегружен (503). Автоматический повтор через 2 секунды...")
                        time.sleep(2.0)
                        continue
                    break

            # Резервная попытка через SDK, если библиотека установлена и прокси не задан
            if not key_quota_exhausted and not GEMINI_PROXY and GENAI_AVAILABLE and genai:
                try:
                    configure_gemini(api_key=key)
                    logger.info(f"Запрос к Gemini через SDK ({key_label} ключ, модель: {m})...")
                    model = genai.GenerativeModel(m)
                    sdk_inputs: List[Any] = [prompt_text]
                    if media_bytes and mime_type:
                        sdk_inputs.append({"mime_type": mime_type, "data": media_bytes})
                    response = model.generate_content(sdk_inputs)
                    if response and response.text:
                        _active_key_index = key_idx
                        return response.text
                except Exception as e:
                    err_s = str(e).lower()
                    logger.warning(f"SDK Gemini ({key_label} ключ, {m}) ошибка: {e}")
                    last_error = e
                    if "user location is not supported" in err_s:
                        raise
                    if "429" in err_s or "resource_exhausted" in err_s or "quota" in err_s:
                        logger.warning(f"⚠️ Лимит на ключе {key_label} исчерпан в SDK. Переключаемся...")
                        _key_exhausted_until[key_idx] = time.time() + 600
                        _active_key_index = (key_idx + 1) % len(keys_to_try)
                        break

    if last_error:
        raise last_error
    raise RuntimeError("Все ключи и модели Gemini временно недоступны. Попробуйте еще раз.")


def normalize_text_tokens(text: str) -> set:
    """Извлекает нормализованные основы слов для сопоставления названий дисциплин."""
    if not text:
        return set()
    cleaned = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9\s]', ' ', text.lower())
    words = cleaned.split()
    stopwords = {
        'в', 'и', 'по', 'на', 'с', 'к', 'о', 'об', 'за', 'для', 'от', 'до',
        'пр', 'л', 'сем', 'лаб', 'практикумом', 'деятельности', 'основы'
    }
    tokens = set()
    for w in words:
        if len(w) >= 3 and w not in stopwords:
            tokens.add(w[:6] if len(w) > 6 else w)
    return tokens


def match_subject_similarity(target_subj: str, lesson_subj: str) -> float:
    """Вычисляет сходство названий предметов по подстрокам и пересечению основ слов."""
    t_clean = target_subj.lower().strip()
    l_clean = lesson_subj.lower().strip()
    if not t_clean or not l_clean:
        return 0.0
    if t_clean in l_clean or l_clean in t_clean:
        return 1.0
    t_tokens = normalize_text_tokens(target_subj)
    l_tokens = normalize_text_tokens(lesson_subj)
    if not t_tokens or not l_tokens:
        return 0.0
    common = t_tokens.intersection(l_tokens)
    return len(common) / len(t_tokens)


def match_teacher(target_teach: str, lesson_teach: str) -> bool:
    """Проверяет совпадение фамилии преподавателя."""
    t_clean = target_teach.lower().strip()
    l_clean = lesson_teach.lower().strip()
    if not t_clean or not l_clean:
        return False
    t_surname = t_clean.split()[0].replace(',', '').replace('.', '')
    l_surname = l_clean.split()[0].replace(',', '').replace('.', '')
    return (t_surname in l_clean) or (l_surname in t_clean) or (t_clean in l_clean) or (l_clean in t_clean)


def find_matching_practice_lessons(
    practice_lessons: List[Dict[str, Any]],
    target_subject: str,
    target_teacher: str
) -> List[Dict[str, Any]]:
    """
    Интеллектуальный подбор занятий строго по дисциплине:
    - Предмет имеет абсолютный приоритет над преподавателем, так как один преподаватель
      может вести разные предметы в разные дни (например, Матафонова ведет 'Нормативно-правовые основы'
      в понедельник и 'Психолого-педагогические технологии' в среду).
    - Запрещено подмешивать занятия другого предмета только из-за совпадения фамилии преподавателя!
    """
    subj_lower = target_subject.lower().strip()
    teach_lower = target_teacher.lower().strip()
    
    is_generic_subject = (
        not subj_lower
        or subj_lower in ("общее задание", "задание", "дз", "домашка", "предмет", "пара")
        or len(subj_lower) < 3
    )

    # 1. Если указан конкретный предмет, ищем совпадения строго по предмету
    if not is_generic_subject:
        scored_by_subject = []
        for l in practice_lessons:
            sim = match_subject_similarity(target_subject, l.get("subject", ""))
            if sim >= 0.4:
                scored_by_subject.append((sim, l))
                
        if scored_by_subject:
            max_sim = max(s[0] for s in scored_by_subject)
            # Отбираем только занятия, максимально близкие к искомому предмету
            best_subj_lessons = [l for (sim, l) in scored_by_subject if sim >= max_sim - 0.25]
            
            # Если также назван преподаватель, фильтруем среди них по преподавателю
            if teach_lower:
                teach_matched = [
                    l for l in best_subj_lessons
                    if match_teacher(target_teacher, l.get("teacher", ""))
                ]
                if teach_matched:
                    return teach_matched
                    
            return best_subj_lessons

    # 2. Если предмет не указан студентом, но назван преподаватель
    if teach_lower:
        teacher_lessons = [
            l for l in practice_lessons
            if match_teacher(target_teacher, l.get("teacher", ""))
        ]
        if teacher_lessons:
            return teacher_lessons

    # 3. Резервный поиск по подстроке
    fallback = []
    for l in practice_lessons:
        l_subj = l.get("subject", "").lower()
        if subj_lower and (subj_lower in l_subj or l_subj in subj_lower):
            fallback.append(l)
    if fallback:
        return fallback

    return practice_lessons


def calculate_next_practice_date(
    practice_lessons: List[Dict[str, Any]],
    target_subject: str,
    target_teacher: str,
    start_from: Optional[date] = None,
    skip_practices: int = 0
) -> Optional[str]:
    """
    Детерминированный расчет даты ПРАКТИЧЕСКОГО занятия:
    - Начинает поиск со следующего дня (завтра).
    - Строго сопоставляет занятия по предмету (не путая разные дисциплины одного преподавателя).
    - Параметр skip_practices позволяет пропустить N ближайших практических дней:
      * skip_practices = 0 -> ближайшая практика ('на эту неделю', 'к следующей паре')
      * skip_practices = 1 -> через одно занятие ('через занятие', 'через занятия', 'через пару')
      * skip_practices = 2 -> через два занятия ('через 2 занятия')
    - Просматривает вперед до 60 дней (чтобы гарантированно найти пары с чередованием ODD/EVEN даже через занятие).
    - Возвращает строку формата 'DD.MM (Дн)', например '18.11 (Ср)'.
    """
    if start_from is None:
        start_from = date.today()

    start_day = start_from + timedelta(days=1)
    matching_lessons = find_matching_practice_lessons(
        practice_lessons=practice_lessons,
        target_subject=target_subject,
        target_teacher=target_teacher
    )

    if not matching_lessons:
        return None

    found_practice_days = 0
    for day_offset in range(60):
        current_date = start_day + timedelta(days=day_offset)
        curr_dow = current_date.weekday()
        curr_week_type = get_week_type(current_date)
        
        has_practice_on_day = False
        for lesson in matching_lessons:
            lesson_dow = int(lesson.get("day_of_week", -1))
            lesson_week_type = lesson.get("week_type", "ALL").upper()
            
            if lesson_dow == curr_dow:
                if lesson_week_type == "ALL" or lesson_week_type == curr_week_type:
                    has_practice_on_day = True
                    break

        if has_practice_on_day:
            if found_practice_days == skip_practices:
                day_short = format_day_name(curr_dow, short=True)
                return f"{current_date.strftime('%d.%m')} ({day_short})"
            found_practice_days += 1
                    
    return None


def recognize_schedule_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> List[Dict[str, Any]]:
    """
    Распознает расписание занятий по фотографии с помощью Gemini API.
    Включает SHA-256 кэширование (0 токенов при повторной отправке того же фото)
    и сжатый JSON-промпт для ускорения ответа и защиты от HTTP 503.
    """
    img_hash = f"sched_{hashlib.sha256(image_bytes).hexdigest()}"
    if img_hash in _media_cache:
        logger.info("⚡ Расписание мгновенно получено из локального SHA-256 кэша (0 токенов потрачено)!")
        return _media_cache[img_hash]

    prompt = """Извлеки все учебные пары с фото расписания в компактный JSON-массив (без ```json и лишних пробелов).
Поля каждого объекта:
- "day_of_week": 0..6 (0=Пн, 1=Вт, 2=Ср, 3=Чт, 4=Пт, 5=Сб, 6=Вс)
- "time": "08:30 - 10:00"
- "subject": название дисциплины без (л)/(пр)
- "teacher": "Фамилия И.О." или ""
- "room": аудитория или ""
- "week_type": ▲ или нечетная -> "ODD", ■ или четная -> "EVEN", иначе -> "ALL"
- "lesson_type": (пр)/(сем)/практика -> "PRACTICE", (л)/лекция -> "LECTURE", по умолчанию -> "PRACTICE"
Верни ТОЛЬКО валидный JSON-массив."""

    try:
        raw_text = generate_content_with_fallback(
            prompt_text=prompt,
            media_bytes=image_bytes,
            mime_type=mime_type
        )
        
        cleaned_json = clean_json_response(raw_text)
        data = json.loads(cleaned_json)
        
        lessons = []
        if isinstance(data, list):
            lessons = data
        elif isinstance(data, dict) and "schedule" in data:
            lessons = data["schedule"]

        if lessons:
            _media_cache[img_hash] = lessons
        return lessons
    except Exception as e:
        err_msg = str(e)
        if "User location is not supported" in err_msg or "400" in err_msg:
            logger.info("⚡ Обнаружено региональное ограничение Google Gemini (User location is not supported). Переключение на облачный европейский шлюз...")
            try:
                b64_img = base64.b64encode(image_bytes).decode("utf-8")
                relay_res = call_relay_api("recognize-schedule", {
                    "imageBase64": b64_img,
                    "mimeType": mime_type
                })
                if relay_res and "lessons" in relay_res:
                    logger.info(f"✅ Расписание успешно распознано через реле-шлюз! Найдено пар: {len(relay_res['lessons'])}")
                    _media_cache[img_hash] = relay_res["lessons"]
                    return relay_res["lessons"]
            except Exception as relay_err:
                logger.error(f"Ошибка при вызове реле-шлюза: {relay_err}")
                
        logger.error(f"Ошибка при распознавании расписания через Gemini: {e}")
        raise RuntimeError(f"Не удалось распознать расписание: {e}")


def build_compact_schedule_context(user_schedule: List[Dict[str, Any]]) -> str:
    """
    Сжимает расписание для промпта в 10-15 раз (экономия ~90% входных токенов):
    Вместо двух огромных JSON-массивов со всеми полями передает только уникальные
    связки 'Предмет | Преподаватель', так как точную дату следующей практики
    Python-алгоритм calculate_next_practice_date вычисляет локально.
    """
    seen = set()
    lines = []
    for l in user_schedule:
        subj = (l.get("subject") or "").strip()
        teach = (l.get("teacher") or "").strip()
        key = (subj.lower(), teach.lower())
        if subj and key not in seen:
            seen.add(key)
            lines.append(f"- {subj} | {teach or 'Не указан'}")
    return "\n".join(lines) if lines else "Расписание пусто"


# Словарь числительных для локального zero-token распознавания дат ("к двадцать пятому числу")
RU_NUMBERS = {
    "первому": 1, "первое": 1, "второму": 2, "второе": 2, "третьему": 3, "третье": 3,
    "четвертому": 4, "четвертое": 4, "пятому": 5, "пятое": 5, "шестому": 6, "шестое": 6,
    "седьмому": 7, "седьмое": 7, "восьмому": 8, "восьмое": 8, "девятому": 9, "девятое": 9,
    "десятому": 10, "десятое": 10, "одиннадцатому": 11, "двенадцатому": 12, "тринадцатому": 13,
    "четырнадцатому": 14, "пятнадцатому": 15, "шестнадцатому": 16, "семнадцатому": 17,
    "восемнадцатому": 18, "девятнадцатому": 19, "двадцатому": 20, "двадцатое": 20,
    "двадцать первому": 21, "двадцать второму": 22, "двадцать третьему": 23,
    "двадцать четвертому": 24, "двадцать пятому": 25, "двадцать шестому": 26,
    "двадцать седьмому": 27, "двадцать восьмому": 28, "двадцать девятому": 29,
    "тридцатому": 30, "тридцать первому": 31
}


def split_into_homework_segments(text: str, user_schedule: List[Dict[str, Any]]) -> List[str]:
    """
    Разделяет одно голосовое или текстовое сообщение на отдельные фрагменты домашних заданий,
    если студент написал или продиктовал сразу несколько преподавателей/предметов в одном сообщении.
    Примеры:
    - "ДЗ Костин Учебная практика: собрать аккумулятор на эту неделю. ДЗ Кибальник Методики: через занятия нужно собрать шесть тапочек."
    - "- ДЗ Костин: собрать аккумулятор на эту неделю.\n- ДЗ Кибальник: через занятия нужно собрать шесть тапочек."
    - "дз костин учебная практика собрать аккумулятор на эту неделю дз кибальник методики через занятия нужно собрать шесть тапочек"
    - "Костин собрать аккумулятор на эту неделю, а Кибальник через занятия собрать шесть тапочек"
    """
    if not text or not text.strip():
        return []

    # 1. Сначала режем по явным переносам строк, точкам с запятой, нумерованным спискам и повторяющимся маркерам "ДЗ / домашнее задание"
    raw_lines = re.split(r'[\n;]+', text.strip())
    preliminary_chunks: List[str] = []

    for line in raw_lines:
        line_clean = re.sub(r'^\s*[\-•*]+\s*', '', line.strip())
        if not line_clean:
            continue
        # Разбиваем, если внутри одной строки несколько раз встречается "ДЗ <текст>" или "1) ДЗ ..." / "2. ДЗ ..."
        parts = re.split(
            r'(?=(?:^|\s|[.,!])(?:\d+[.)]\s*)?(?:а\s+также\s+|и\s+еще\s+|дальше\s+|второе\s+)?(?:домашнее\s+задание|домашка|дз)\b)',
            line_clean,
            flags=re.IGNORECASE
        )
        for p in parts:
            p_str = p.strip(' .,;-•*')
            if len(p_str) >= 5:
                preliminary_chunks.append(p_str)

    if not preliminary_chunks:
        preliminary_chunks = [text.strip()]

    # 2. Собираем список уникальных фамилий преподавателей из расписания студента
    surnames = set()
    for l in user_schedule:
        t = (l.get("teacher") or "").strip()
        if t:
            sur = t.split()[0].replace(',', '').replace('.', '').lower()
            if len(sur) >= 3:
                surnames.add(sur)

    if not surnames:
        return preliminary_chunks

    # 3. Проверяем каждый кусок: если в одном куске названы 2 и более РАЗНЫХ преподавателя,
    # разрезаем кусок перед началом второго (и последующих) преподавателей!
    final_segments: List[str] = []
    for chunk in preliminary_chunks:
        chunk_lower = chunk.lower()
        found_teachers = []
        for sur in surnames:
            # Ищем основу фамилии (с возможным окончанием падежа: Костин / Костина / Костину)
            stem = sur[:-1] if len(sur) > 5 and sur[-1] in 'аяоеыи' else sur
            m = re.search(rf'\b{re.escape(stem)}[а-яё]*\b', chunk_lower)
            if m:
                found_teachers.append((m.start(), sur))

        found_teachers.sort(key=lambda x: x[0])

        # Оставляем только вхождения разных преподавателей
        distinct_positions: List[int] = []
        seen_sur = set()
        for pos, sur in found_teachers:
            if sur not in seen_sur:
                seen_sur.add(sur)
                distinct_positions.append(pos)

        if len(distinct_positions) <= 1:
            final_segments.append(chunk)
            continue

        # Вычисляем точки разреза перед 2-м, 3-м и т.д. преподавателем
        split_points = [0]
        for pos in distinct_positions[1:]:
            # Смотрим немного назад от фамилии преподавателя, чтобы захватить предлог/маркер ("а у ", "и ", "дз ", "по ... ")
            prefix_slice = chunk[:pos]
            m_prefix = re.search(
                r'(?:[.,!]\s*|\s+)(?:\d+[.)]\s*)?(?:(?:а\s+|и\s+|также\s+|еще\s+)?(?:у\s+|по\s+[а-яё\-]+\s+|домашнее\s+задание\s+|домашка\s+|дз\s+))?$',
                prefix_slice,
                flags=re.IGNORECASE
            )
            cut_idx = m_prefix.start() if m_prefix else pos
            if cut_idx > split_points[-1] + 5:
                split_points.append(cut_idx)

        split_points.append(len(chunk))
        for i in range(len(split_points) - 1):
            seg = chunk[split_points[i]:split_points[i + 1]].strip(' .,;-•*')
            if len(seg) >= 5:
                final_segments.append(seg)

    return final_segments if final_segments else [text.strip()]


# Ключевые слова начала самого домашнего задания (глаголы и существительные действия)
TASK_ACTION_STARTERS = {
    "сделать", "собрать", "решить", "написать", "подготовить", "выучить", "прочитать",
    "выполнить", "ответить", "нарисовать", "составить", "заполнить", "разобрать",
    "повторить", "перевести", "принести", "сдать", "оформить", "пройти", "найти",
    "конспект", "доклад", "презентацию", "презентация", "реферат", "эссе", "таблицу",
    "таблица", "отчет", "отчёт", "лабораторную", "задачи", "задачу", "номер", "номера",
    "упражнение", "упражнения", "параграф", "стр", "страница", "глава", "главу",
    "вопросы", "билеты", "тест", "тесты", "нужно", "надо", "задал", "задала", "задали"
}


def _strip_subject_and_teacher_prefix(task_body: str, subject: str, teacher: str) -> str:
    """
    Аккуратно очищает начало текста задания от:
    - маркера 'ДЗ' / 'Домашнее задание'
    - Фамилии и инициалов преподавателя
    - Названия предмета (как до двоеточия/тире, так и без разделителей: 'ДЗ Костин Учебная практика собрать аккумулятор')
    Возвращает чистый текст самого задания.
    """
    body = task_body.strip()

    # 1. Убираем маркер ДЗ и возможную нумерацию в начале
    body = re.sub(
        r'^\s*(?:\d+[.)]\s*)?(?:а\s+|и\s+|также\s+|еще\s+)*(?:запиши\s+)?(?:домашнее\s+задание|домашка|дз|задание)\b\s*[:\-—]?\s*',
        '',
        body,
        flags=re.IGNORECASE
    ).strip()

    # 2. Если есть явный разделитель заголовка (':' или ' - ' или ' — '), например:
    #    "Костин Учебная практика: собрать аккумулятор" или "Костин (Учебная практика) - собрать аккумулятор"
    m_sep = re.match(r'^([^:]{2,85}?)\s*(?::|\s+[-—]\s+)\s*(.{3,})$', body)
    if m_sep:
        left_header = m_sep.group(1).strip()
        right_task = m_sep.group(2).strip()
        left_words = [re.sub(r'[^а-яёa-z0-9]', '', w.lower()) for w in left_header.split()]
        # Проверяем, что слева действительно шапка (преподаватель/предмет), а не само задание вида "Сделать доклад: тема 1"
        if not any(w in TASK_ACTION_STARTERS for w in left_words[:2]):
            body = right_task

    # 3. Удаляем фамилию и инициалы преподавателя (если они остались в тексте)
    surname = teacher.split()[0].replace(',', '').replace('.', '') if teacher else ""
    surname_stem = surname[:-1] if len(surname) > 5 and surname[-1].lower() in 'аяоеыи' else surname
    if surname_stem:
        body = re.sub(
            rf'(?:^\s*|\b(?:у|от|преподаватель|препод)\s+){re.escape(surname_stem)}[а-яА-ЯёЁ]*(?:\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.?)?\s*[,:\-—]?\s*',
            ' ',
            body,
            flags=re.IGNORECASE
        ).strip()

    # 4. Пословно убираем название предмета в начале строки (если студент написал без двоеточия:
    #    "ДЗ Костин учебная практика собрать аккумулятор" или "ДЗ Матафонова психолого-педагогические технологии сделать доклад")
    subj_clean = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9\s\-]', ' ', subject.lower())
    subj_stems = set()
    for w in subj_clean.split():
        for sub_w in w.split('-'):
            if len(sub_w) >= 3:
                subj_stems.add(sub_w[:5] if len(sub_w) > 5 else sub_w)

    connector_words = {
        "по", "и", "в", "на", "с", "со", "для", "от", "до", "о", "об",
        "предмет", "предмету", "дисциплина", "дисциплине", "курс", "курсу",
        "пара", "паре", "практика", "практике", "лекция", "семинар",
        "основы", "основам", "деятельности", "деятельность", "работы", "работа", "работе",
        "профессиональной", "социального", "педагога", "лиц", "овз"
    }

    tokens = body.split()
    consume_idx = 0
    matched_any_subj_word = False

    while consume_idx < len(tokens):
        raw_tok = tokens[consume_idx]
        clean_tok = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9\-]', '', raw_tok.lower()).strip('-')
        if not clean_tok:
            consume_idx += 1
            continue

        # Если встретили глагол/существительное начала самого задания — сразу останавливаемся!
        if clean_tok in TASK_ACTION_STARTERS:
            break

        # Проверяем, совпадает ли слово (или его части через дефис) со словами предмета
        parts_tok = [p for p in clean_tok.split('-') if p]
        is_subj_match = False
        for pt in parts_tok:
            pt_stem = pt[:5] if len(pt) > 5 else pt
            if any(pt_stem.startswith(st) or st.startswith(pt_stem) for st in subj_stems if len(pt_stem) >= 3):
                is_subj_match = True
                break

        if is_subj_match:
            matched_any_subj_word = True
            consume_idx += 1
            continue

        # Пропускаем предлоги/связки в начале или между словами предмета ("по учебной практике")
        if clean_tok in connector_words and (consume_idx == 0 or matched_any_subj_word):
            consume_idx += 1
            continue

        # Если вдруг фамилия преподавателя шла после названия предмета ("ДЗ Учебная практика Костин собрать аккумулятор")
        if surname_stem and clean_tok.startswith(surname_stem.lower()):
            consume_idx += 1
            continue

        break

    if consume_idx > 0 and consume_idx < len(tokens):
        body = " ".join(tokens[consume_idx:])

    return body.strip(' ,.-:;!—')


def _parse_fallback_explicit_dz_command(
    text_clean: str,
    date_type: str,
    explicit_raw: str,
    skip_practices: int,
    today_date: date
) -> Optional[Dict[str, Any]]:
    """
    Резервный парсер для явной текстовой команды 'ДЗ <Преподаватель> <Предмет> <Задание>',
    когда названный преподаватель или предмет еще не занесен в расписание студента.
    """
    if not re.match(
        r'^\s*(?:\d+[.)]\s*)?(?:а\s+|и\s+|также\s+|еще\s+)*(?:запиши\s+)?(?:домашнее\s+задание|домашка|дз)\b',
        text_clean,
        flags=re.IGNORECASE
    ):
        return None

    # Убираем "ДЗ" в начале и фразы дат
    body = re.sub(
        r'^\s*(?:\d+[.)]\s*)?(?:а\s+|и\s+|также\s+|еще\s+)*(?:запиши\s+)?(?:домашнее\s+задание|домашка|дз)\b\s*[:\-—]?\s*',
        '',
        text_clean,
        flags=re.IGNORECASE
    ).strip()

    date_cleanup_patterns = [
        r'(?:с\s+открытой\s+датой|без\s+дедлайна|без\s+срока|бессрочно)',
        r'(?:к|на|до)\s+\d{1,2}(?:\.\d{1,2})?(?:-?му|-?го|-?е)?(?:\s+числ\w*)?',
        r'(?:к|на)\s+следующ\w+\s+(?:пар\w+|заняти\w+|недел\w+)',
        r'(?:на|к)\s+эт\w+\s+недел\w+',
        r'через\s+(?:два|две|2|одн[оу])?\s*(?:заняти\w*|пар\w*|урок\w*)',
    ]
    for word_num in sorted(RU_NUMBERS.keys(), key=len, reverse=True):
        date_cleanup_patterns.append(rf'(?:к|на|до)\s+{re.escape(word_num)}(?:\s+числ\w*)?')
        date_cleanup_patterns.append(rf'{re.escape(word_num)}\s+числ\w*')

    for pat in date_cleanup_patterns:
        body = re.sub(pat, ' ', body, flags=re.IGNORECASE)
    body = re.sub(r'\s+', ' ', body).strip(' ,.-:;!')

    if len(body) < 5:
        return None

    teacher = "Не указан"
    subject = "Общее задание"
    task_part = ""

    # Вариант А: Есть разделитель ':' или ' - ' / ' — ' ("Костин Учебная практика: собрать аккумулятор")
    m_sep = re.match(r'^([^:]{2,80}?)\s*(?::|\s+[-—]\s+)\s*(.{3,})$', body)
    if m_sep:
        header = m_sep.group(1).strip(' ,.-')
        task_part = m_sep.group(2).strip(' ,.-')
        h_tokens = header.split()
        if len(h_tokens) == 1:
            teacher = h_tokens[0].capitalize()
        elif len(h_tokens) >= 2:
            # Проверяем инициалы после фамилии (например: "Костин А.К. Учебная практика")
            if re.match(r'^[А-ЯЁA-Z]\.(?:[А-ЯЁA-Z]\.)?$', h_tokens[1], flags=re.IGNORECASE):
                teacher = f"{h_tokens[0].capitalize()} {h_tokens[1].upper()}"
                subj_tokens = h_tokens[2:]
            else:
                teacher = h_tokens[0].capitalize()
                subj_tokens = h_tokens[1:]
            if subj_tokens:
                subj_str = " ".join(subj_tokens).strip(' ()—-,:')
                if subj_str:
                    subject = subj_str[0].upper() + subj_str[1:]
    else:
        # Вариант Б: Без двоеточия ("ДЗ Смирнов Философия написать эссе по Канту")
        tokens = body.split()
        if len(tokens) < 3:
            return None
        teacher = tokens[0].strip(' ,.:').capitalize()
        idx = 1
        if idx < len(tokens) and re.match(r'^[А-ЯЁA-Z]\.(?:[А-ЯЁA-Z]\.)?$', tokens[idx], flags=re.IGNORECASE):
            teacher = f"{teacher} {tokens[idx].upper()}"
            idx += 1

        subj_words = []
        while idx < len(tokens) and len(subj_words) < 4:
            clean_w = re.sub(r'[^а-яёa-z0-9]', '', tokens[idx].lower())
            if clean_w in TASK_ACTION_STARTERS:
                break
            subj_words.append(tokens[idx])
            idx += 1

        if subj_words and idx < len(tokens):
            subj_str = " ".join(subj_words).strip(' ()—-,:')
            if subj_str.lower().startswith("по "):
                subj_str = subj_str[3:].strip()
            if subj_str:
                subject = subj_str[0].upper() + subj_str[1:]
            task_part = " ".join(tokens[idx:])
        else:
            task_part = " ".join(tokens[1:])

    task_part = re.sub(r'^\s*(?:нужно|надо|задал[аи]?)\s+', '', task_part, flags=re.IGNORECASE).strip(' ,.-:;!')
    if len(task_part) < 3:
        return None

    task_text = task_part[0].upper() + task_part[1:]
    if date_type == "EXPLICIT" and explicit_raw:
        target_date = normalize_target_date(explicit_raw, base_date=today_date)
    else:
        target_date = "Без срока"

    return {
        "teacher": teacher,
        "subject": subject,
        "taskText": task_text,
        "dateType": date_type,
        "skipPractices": skip_practices,
        "targetDate": target_date,
        "zeroToken": True
    }


def parse_single_homework_locally(
    text: str,
    user_schedule: List[Dict[str, Any]],
    today_date: date
) -> Optional[Dict[str, Any]]:
    """
    Разбирает одиночный фрагмент домашнего задания (для одного преподавателя/предмета) за 0 токенов.
    Понимает формат 'ДЗ <Преподаватель> <Предмет> <Само задание>', а также свободный голосовой/текстовый ввод:
    - 'на эту неделю' / 'к следующей паре' -> ближайшая практика (skip_practices=0)
    - 'через занятие' / 'через занятия' / 'через пару' -> следующая практика после ближайшей (skip_practices=1)
    - 'через два занятия' / 'через 2 пары' -> через две практики (skip_practices=2)
    - 'к 25 числу' / 'к двадцать пятому числу' -> явная дата (EXPLICIT)
    - 'с открытой датой' / 'без срока' -> открытая дата (OPEN)
    """
    if not text or len(text.strip()) < 4:
        return None

    text_clean = text.strip()
    text_lower = text_clean.lower()
    practice_lessons = [l for l in user_schedule if l.get("lesson_type") == "PRACTICE"]
    all_lessons = practice_lessons or user_schedule

    # 1. Определяем режим даты (OPEN / EXPLICIT / SKIP_PRACTICE / NEXT_PRACTICE)
    date_type = "NEXT_PRACTICE"
    explicit_raw = ""
    skip_practices = 0

    open_markers = ["без срока", "открытой датой", "открытая дата", "без дедлайна", "бессрочно", "без даты", "просто нужно сделать"]
    if any(m in text_lower for m in open_markers):
        date_type = "OPEN"
    elif re.search(r'через\s+(?:два|две|2)\s+(?:заняти\w*|пар\w*|урок\w*)', text_lower):
        date_type = "SKIP_PRACTICE"
        skip_practices = 2
    elif re.search(r'через\s+(?:одн[оу]\s+)?(?:заняти\w*|пар\w*|урок\w*)', text_lower):
        date_type = "SKIP_PRACTICE"
        skip_practices = 1
    else:
        # Проверяем цифровую дату "к 25 числу", "к 25.11", "на 25-е"
        m_date = re.search(r'(?:к|на|до)\s+(\d{1,2}(?:\.\d{1,2})?)(?:-?му|-?го|-?е)?(?:\s+числ\w*)?', text_lower)
        if m_date:
            date_type = "EXPLICIT"
            explicit_raw = m_date.group(1)
        else:
            # Важно: проверяем числительное даты только со словами "к/на/до ... числу/числа",
            # чтобы не перепутать "собрать шесть тапочек" с "к шестому числу"!
            for word_num, day_val in sorted(RU_NUMBERS.items(), key=lambda x: -len(x[0])):
                if re.search(rf'(?:(?:к|на|до)\s+{re.escape(word_num)}|{re.escape(word_num)}\s+числ\w*)', text_lower):
                    date_type = "EXPLICIT"
                    explicit_raw = str(day_val)
                    break

    # 2. Ищем совпадение по предмету и преподавателю в расписании
    matched_lesson = None
    best_score = 0.0

    for l in all_lessons:
        l_subj = l.get("subject", "")
        l_teach = l.get("teacher", "")
        subj_sim = match_subject_similarity(text_clean, l_subj)
        teach_surname = l_teach.split()[0].lower().replace(',', '').replace('.', '') if l_teach else ""
        teach_stem = teach_surname[:-1] if len(teach_surname) > 5 and teach_surname[-1] in 'аяоеыи' else teach_surname
        teach_ok = match_teacher(text_clean, l_teach) or (
            bool(teach_stem) and bool(re.search(rf'\b{re.escape(teach_stem)}[а-яё]*\b', text_lower))
        )
        score = subj_sim * 2.0 + (1.2 if teach_ok else 0.0)
        if score > best_score and (subj_sim >= 0.35 or teach_ok):
            best_score = score
            matched_lesson = l

    if not matched_lesson:
        return _parse_fallback_explicit_dz_command(
            text_clean=text_clean,
            date_type=date_type,
            explicit_raw=explicit_raw,
            skip_practices=skip_practices,
            today_date=today_date
        )

    teacher = matched_lesson.get("teacher", "")
    subject = matched_lesson.get("subject", "")

    # 3. Очищаем суть задания от служебных слов, предмета, фамилии преподавателя и дат
    task_body = _strip_subject_and_teacher_prefix(text_clean, subject=subject, teacher=teacher)

    remove_date_patterns = [
        r'(?:с\s+открытой\s+датой|без\s+дедлайна|без\s+срока|бессрочно)',
        r'(?:к|на|до)\s+\d{1,2}(?:\.\d{1,2})?(?:-?му|-?го|-?е)?(?:\s+числ\w*)?',
        r'(?:к|на)\s+следующ\w+\s+(?:пар\w+|заняти\w+|недел\w+)',
        r'(?:на|к)\s+эт\w+\s+недел\w+',
        r'через\s+(?:два|две|2|одн[оу])?\s*(?:заняти\w*|пар\w*|урок\w*)',
    ]
    surname = teacher.split()[0].replace(',', '').replace('.', '') if teacher else ""
    surname_stem = surname[:-1] if len(surname) > 5 and surname[-1].lower() in 'аяоеыи' else surname
    if surname_stem:
        remove_date_patterns.append(rf'(?:у\s+)?\b{re.escape(surname_stem)}[а-яА-ЯёЁ]*\s*[:\-—]?')
    for word_num in sorted(RU_NUMBERS.keys(), key=len, reverse=True):
        remove_date_patterns.append(rf'(?:к|на|до)\s+{re.escape(word_num)}(?:\s+числ\w*)?')
        remove_date_patterns.append(rf'{re.escape(word_num)}\s+числ\w*')

    for pat in remove_date_patterns:
        task_body = re.sub(pat, ' ', task_body, flags=re.IGNORECASE)

    # Повторно зачищаем возможные остатки названия предмета после удаления фамилии/даты
    task_body = _strip_subject_and_teacher_prefix(task_body, subject=subject, teacher=teacher)
    task_body = re.sub(r'^\s*по\s+[а-яА-ЯёЁ\-]+\s*', '', task_body, flags=re.IGNORECASE)
    task_body = re.sub(r'^\s*(?:нужно|надо|задал[аи]?)\s+', '', task_body, flags=re.IGNORECASE)
    task_body = re.sub(r'\s+', ' ', task_body).strip(' ,.-:;!—')

    if len(task_body) < 3:
        return None

    task_text = task_body[0].upper() + task_body[1:]

    # 4. Вычисляем дату сдачи
    if date_type == "OPEN":
        target_date = "Без срока"
    elif date_type == "EXPLICIT" and explicit_raw:
        target_date = normalize_target_date(explicit_raw, base_date=today_date)
    else:
        target_date = calculate_next_practice_date(
            practice_lessons=practice_lessons,
            target_subject=subject,
            target_teacher=teacher,
            start_from=today_date,
            skip_practices=skip_practices
        ) or "Без срока"

    return {
        "teacher": teacher,
        "subject": subject,
        "taskText": task_text,
        "dateType": date_type,
        "skipPractices": skip_practices,
        "targetDate": target_date,
        "zeroToken": True
    }



def parse_homework_locally(
    text: str,
    user_schedule: List[Dict[str, Any]],
    today_date: Optional[date] = None
) -> Optional[Dict[str, Any]]:
    """
    Локальный Zero-Token анализатор текста или расшифровки ВК (тратит 0 токенов Gemini!):
    Поддерживает как ОДНО, так и НЕСКОЛЬКО домашних заданий (разным преподавателям)
    в одном голосовом или текстовом сообщении!
    """
    if not text or len(text.strip()) < 5:
        return None
    if today_date is None:
        today_date = date.today()

    segments = split_into_homework_segments(text, user_schedule)
    parsed_tasks: List[Dict[str, Any]] = []

    for seg in segments:
        item = parse_single_homework_locally(seg, user_schedule, today_date=today_date)
        if item:
            parsed_tasks.append(item)

    if not parsed_tasks:
        return None

    first = dict(parsed_tasks[0])
    first["tasks"] = parsed_tasks
    first["isMultiple"] = len(parsed_tasks) > 1
    logger.info(f"⚡ ДЗ распознано ЛОКАЛЬНО за 0 токенов (заданий: {len(parsed_tasks)})")
    return first


def finalize_single_homework_task(
    task_obj: Dict[str, Any],
    practice_lessons: List[Dict[str, Any]],
    today_date: date
) -> Dict[str, Any]:
    """Нормализует дату и поля одного распознанного задания."""
    date_type = str(task_obj.get("dateType", "NEXT_PRACTICE")).upper()
    raw_target_date = str(task_obj.get("targetDate", "")).strip()
    skip_practices = int(task_obj.get("skipPractices", 0) or 0)

    if date_type == "SKIP_PRACTICE" and skip_practices == 0:
        skip_practices = 1

    if date_type == "OPEN" or any(w in raw_target_date.lower() for w in ["без", "открыт", "бессроч", "нет"]):
        task_obj["targetDate"] = "Без срока"
    elif date_type == "EXPLICIT" and raw_target_date and raw_target_date not in ("NEXT_PRACTICE", "SKIP_PRACTICE"):
        task_obj["targetDate"] = normalize_target_date(raw_target_date, base_date=today_date)
    else:
        calc_date = calculate_next_practice_date(
            practice_lessons=practice_lessons,
            target_subject=task_obj.get("subject", ""),
            target_teacher=task_obj.get("teacher", ""),
            start_from=today_date,
            skip_practices=skip_practices
        )
        if calc_date:
            task_obj["targetDate"] = calc_date
        elif raw_target_date and raw_target_date not in ("Ближайшая пара", "NEXT_PRACTICE", "SKIP_PRACTICE"):
            task_obj["targetDate"] = normalize_target_date(raw_target_date, base_date=today_date)
        else:
            task_obj["targetDate"] = "Без срока"

    return task_obj


def process_voice_homework(
    audio_bytes: Optional[bytes] = None,
    mime_type: str = "audio/ogg",
    user_schedule: Optional[List[Dict[str, Any]]] = None,
    today_date: Optional[date] = None,
    transcript_text: Optional[str] = None
) -> Dict[str, Any]:
    """
    Принимает голосовое сообщение студента (или готовую текстовую расшифровку от ВК / Vosk / текст):
    1. Если нет transcript_text от ВК, пробует локальный офлайн-движок Vosk (voice_recognizer.transcribe_audio_locally).
    2. Если текст получен — пробует разобрать 1 или несколько заданий локально за 0 токенов (parse_homework_locally)!
    3. Если нужен ИИ — отправляет запрос в Gemini с поддержкой нескольких заданий в одном сообщении.
    """
    if user_schedule is None:
        user_schedule = []
    if today_date is None:
        today_date = date.today()

    # Шаг 0: Если ВК не прислал готовую расшифровку, пробуем локальное офлайн-распознавание (Vosk)
    if (not transcript_text or not transcript_text.strip()) and audio_bytes:
        try:
            from voice_recognizer import transcribe_audio_locally
            offline_text = transcribe_audio_locally(audio_bytes)
            if offline_text:
                transcript_text = offline_text
        except Exception as vosk_err:
            logger.debug(f"Локальное распознавание голоса пропущено: {vosk_err}")

    # Шаг 1: Если есть текстовая расшифровка (от ВК, Vosk или текстового ввода) — пробуем Zero-Token локальный парсер
    if transcript_text and transcript_text.strip():
        local_res = parse_homework_locally(transcript_text, user_schedule, today_date=today_date)
        if local_res:
            return local_res

    # Шаг 2: Проверяем SHA-256 кэш для аудио или текста
    cache_key = None
    if transcript_text and transcript_text.strip():
        cache_key = f"hw_txt_{hashlib.sha256(transcript_text.strip().lower().encode('utf-8')).hexdigest()}_{today_date.isoformat()}"
    elif audio_bytes:
        cache_key = f"hw_aud_{hashlib.sha256(audio_bytes).hexdigest()}_{today_date.isoformat()}"

    if cache_key and cache_key in _media_cache:
        logger.info("⚡ Домашнее задание мгновенно получено из кэша (0 токенов потрачено)!")
        return _media_cache[cache_key]

    today_dow = today_date.weekday()
    today_dow_name = format_day_name(today_dow)
    curr_week_type = get_week_type(today_date)
    
    practice_lessons = [l for l in user_schedule if l.get("lesson_type") == "PRACTICE"]
    compact_schedule = build_compact_schedule_context(practice_lessons or user_schedule)

    input_instruction = (
        f'Сообщение студента: "{transcript_text.strip()}"'
        if (transcript_text and transcript_text.strip())
        else "Прослушай голосовое сообщение студента с домашними заданиями и извлеки все задания."
    )

    prompt = f"""Ты — ассистент студента. {input_instruction}
Сегодня: {today_date.strftime('%d.%m.%Y')} ({today_dow_name}), неделя: {curr_week_type}.
Предметы и преподаватели из расписания студента:
{compact_schedule}

ВАЖНО: Студент может назвать в одном сообщении СРАЗУ НЕСКОЛЬКО домашних заданий разным преподавателям или по разным предметам! Раздели их в массив "tasks".

ПРАВИЛА ДАТЫ для каждого задания (dateType, skipPractices, targetDate):
1. ЯВНАЯ ДАТА ("к 25 числу", "на 25.11") -> dateType="EXPLICIT", skipPractices=0, targetDate="DD.MM (Дн)".
2. БЕЗ СРОКА ("с открытой датой", "без дедлайна", "без срока") -> dateType="OPEN", skipPractices=0, targetDate="Без срока".
3. ЧЕРЕЗ ЗАНЯТИЕ ("через занятие", "через занятия", "через пару") -> dateType="SKIP_PRACTICE", skipPractices=1, targetDate="SKIP_PRACTICE".
4. НА ЭТУ НЕДЕЛЮ / К СЛЕДУЮЩЕЙ ПАРЕ / ПО УМОЛЧАНИЮ -> dateType="NEXT_PRACTICE", skipPractices=0, targetDate="NEXT_PRACTICE".

Верни ТОЛЬКО компактный JSON:
{{"tasks":[{{"teacher":"...","subject":"...","taskText":"...","dateType":"EXPLICIT|OPEN|NEXT_PRACTICE|SKIP_PRACTICE","skipPractices":0,"targetDate":"..."}}]}}"""

    try:
        use_media = None if (transcript_text and transcript_text.strip()) else audio_bytes
        use_mime = None if (transcript_text and transcript_text.strip()) else mime_type

        raw_text = generate_content_with_fallback(
            prompt_text=prompt,
            media_bytes=use_media,
            mime_type=use_mime
        )
        
        cleaned_json = clean_json_response(raw_text)
        parsed_data = json.loads(cleaned_json)

        raw_tasks: List[Dict[str, Any]] = []
        if isinstance(parsed_data, list):
            raw_tasks = parsed_data
        elif isinstance(parsed_data, dict):
            if isinstance(parsed_data.get("tasks"), list) and len(parsed_data["tasks"]) > 0:
                raw_tasks = parsed_data["tasks"]
            elif "subject" in parsed_data or "taskText" in parsed_data:
                raw_tasks = [parsed_data]

        if not raw_tasks:
            return parsed_data if isinstance(parsed_data, dict) else {"error": "Не удалось распознать задание"}

        final_tasks = [
            finalize_single_homework_task(dict(t), practice_lessons, today_date)
            for t in raw_tasks
        ]

        result = dict(final_tasks[0])
        result["tasks"] = final_tasks
        result["isMultiple"] = len(final_tasks) > 1

        if cache_key:
            _media_cache[cache_key] = result
                
        return result
    except Exception as e:
        err_msg = str(e)
        if "User location is not supported" in err_msg or "400" in err_msg:
            logger.info("⚡ Обнаружено региональное ограничение Google Gemini (User location is not supported). Переключение на облачный европейский шлюз...")
            try:
                payload_data: Dict[str, Any] = {"vk_id": 1001}
                if transcript_text and transcript_text.strip():
                    payload_data["voiceText"] = transcript_text.strip()
                elif audio_bytes:
                    payload_data["audioBase64"] = base64.b64encode(audio_bytes).decode("utf-8")
                    payload_data["mimeType"] = mime_type
                relay_res = call_relay_api("process-voice", payload_data)
                if relay_res and "result" in relay_res:
                    logger.info("✅ Задание успешно обработано через реле-шлюз!")
                    return relay_res["result"]
            except Exception as relay_err:
                logger.error(f"Ошибка при вызове реле-шлюза для аудио: {relay_err}")

        logger.error(f"Ошибка при обработке голосового сообщения: {e}")
        raise RuntimeError(f"Не удалось распознать голосовое сообщение: {e}")


