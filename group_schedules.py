"""
Общая база расписаний учебных групп и декодер шифра групп ПИ ИГУ.
Сюда заранее выгружаются расписания групп, а также автоматически сохраняются
расписания, загруженные студентами по фотографии для своей группы.
"""

import os
import re
import json
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

SAVED_GROUPS_JSON_PATH = Path(__file__).resolve().parent / "saved_group_schedules.json"

# Предзагруженные расписания по номерам групп (хранятся прямо в коде)
GROUP_SCHEDULES: Dict[str, List[Dict[str, Any]]] = {
    "206321 ДБ": [
        # Понедельник (0)
        {
            "day_of_week": 0,
            "time": "08:30 - 10:00",
            "subject": "Аксиологические основы психолого-педагогического образования (с практикумом)",
            "teacher": "Гордин А.И.",
            "room": "10, 205",
            "week_type": "ODD",
            "lesson_type": "LECTURE"
        },
        {
            "day_of_week": 0,
            "time": "08:30 - 10:00",
            "subject": "Аксиологические основы психолого-педагогического образования (с практикумом)",
            "teacher": "Гордин А.И.",
            "room": "10, 205",
            "week_type": "EVEN",
            "lesson_type": "PRACTICE"
        },
        {
            "day_of_week": 0,
            "time": "10:10 - 11:40",
            "subject": "Аксиологические основы психолого-педагогического образования (с практикумом)",
            "teacher": "Гордин А.И.",
            "room": "10, 205",
            "week_type": "ALL",
            "lesson_type": "PRACTICE"
        },
        {
            "day_of_week": 0,
            "time": "11:50 - 13:20",
            "subject": "Нормативно-правовые и этические основы деятельности социального педагога",
            "teacher": "Матафонова С.И.",
            "room": "10, 205",
            "week_type": "ODD",
            "lesson_type": "LECTURE"
        },
        {
            "day_of_week": 0,
            "time": "11:50 - 13:20",
            "subject": "Нормативно-правовые и этические основы деятельности социального педагога",
            "teacher": "Матафонова С.И.",
            "room": "10, 205",
            "week_type": "EVEN",
            "lesson_type": "PRACTICE"
        },
        # Вторник (1)
        {
            "day_of_week": 1,
            "time": "08:30 - 10:00",
            "subject": "Нормативно-правовые основы профессиональной деятельности",
            "teacher": "Айдаров К.А.",
            "room": "10, 124а",
            "week_type": "ODD",
            "lesson_type": "PRACTICE"
        },
        {
            "day_of_week": 1,
            "time": "08:30 - 10:00",
            "subject": "Обучение лиц с ОВЗ и особыми образовательными потребностями",
            "teacher": "Заиграева Н.В.",
            "room": "10, 126",
            "week_type": "EVEN",
            "lesson_type": "PRACTICE"
        },
        {
            "day_of_week": 1,
            "time": "10:10 - 11:40",
            "subject": "Методики и технологии работы социального педагога",
            "teacher": "Кибальник А.В.",
            "room": "10, 208",
            "week_type": "ODD",
            "lesson_type": "LECTURE"
        },
        {
            "day_of_week": 1,
            "time": "10:10 - 11:40",
            "subject": "Основы математической обработки информации",
            "teacher": "Лапшина Е.С.",
            "room": "11, 304",
            "week_type": "EVEN",
            "lesson_type": "LECTURE"
        },
        {
            "day_of_week": 1,
            "time": "11:50 - 13:20",
            "subject": "Методики и технологии работы социального педагога",
            "teacher": "Кибальник А.В.",
            "room": "10, 208",
            "week_type": "ODD",
            "lesson_type": "PRACTICE"
        },
        # Среда (2)
        {
            "day_of_week": 2,
            "time": "08:30 - 10:00",
            "subject": "Психолого-педагогические технологии в профессиональной деятельности",
            "teacher": "Матафонова С.И.",
            "room": "10, 204",
            "week_type": "ODD",
            "lesson_type": "LECTURE"
        },
        {
            "day_of_week": 2,
            "time": "08:30 - 10:00",
            "subject": "Психолого-педагогические технологии в профессиональной деятельности",
            "teacher": "Матафонова С.И.",
            "room": "10, 204",
            "week_type": "EVEN",
            "lesson_type": "PRACTICE"
        },
        {
            "day_of_week": 2,
            "time": "10:10 - 11:40",
            "subject": "Психолого-педагогические технологии в профессиональной деятельности",
            "teacher": "Матафонова С.И.",
            "room": "10, 204",
            "week_type": "ALL",
            "lesson_type": "PRACTICE"
        },
        {
            "day_of_week": 2,
            "time": "11:50 - 13:20",
            "subject": "Учебная практика. Научно-исследовательская работа (получение первичных навыков научно-исследовательской работы)",
            "teacher": "Костин А.К.",
            "room": "10, 208",
            "week_type": "ALL",
            "lesson_type": "PRACTICE"
        },
        # Четверг (3)
        {
            "day_of_week": 3,
            "time": "10:10 - 11:40",
            "subject": "Психолого-педагогическое консультирование",
            "teacher": "Рерке В.И.",
            "room": "10, 104",
            "week_type": "ALL",
            "lesson_type": "LECTURE"
        },
        {
            "day_of_week": 3,
            "time": "11:50 - 13:20",
            "subject": "Психолого-педагогическое консультирование",
            "teacher": "Рерке В.И.",
            "room": "10, 104",
            "week_type": "ALL",
            "lesson_type": "PRACTICE"
        },
        {
            "day_of_week": 3,
            "time": "13:50 - 15:20",
            "subject": "Основы математической обработки информации",
            "teacher": "Бычкова О.И.",
            "room": "11, 221",
            "week_type": "EVEN",
            "lesson_type": "PRACTICE"
        },
        # Пятница (4)
        {
            "day_of_week": 4,
            "time": "10:10 - 11:40",
            "subject": "Обучение лиц с ОВЗ и особыми образовательными потребностями",
            "teacher": "Гостар А.А.",
            "room": "Дистант",
            "week_type": "EVEN",
            "lesson_type": "PRACTICE"
        },
        {
            "day_of_week": 4,
            "time": "10:10 - 11:40",
            "subject": "Нормативно-правовые основы профессиональной деятельности",
            "teacher": "Айдаров К.А.",
            "room": "Дистант",
            "week_type": "EVEN",
            "lesson_type": "LECTURE"
        }
    ]
}


def format_canonical_group_name(group_str: str) -> str:
    """
    Приводит введенный пользователем номер группы к единому стандарту:
    например, '206321дб' или '206321-дб' -> '206321 ДБ'.
    Если пользователь случайно написал '3 курс, 206321дб', извлекает чистый номер группы.
    """
    if not group_str:
        return ""
    cleaned = group_str.strip()
    # Убираем случайные слова вроде "3 курс", "группа" и т.п., если кто-то написал их по привычке
    cleaned = re.sub(r"\b\d\s*(?:курс|к\.?)\b[,;\s]*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:группа|гр\.?)\b[:\s]*", "", cleaned, flags=re.IGNORECASE).strip()

    m = re.match(r"^(\d{6})\s*[-_]*\s*([а-яА-ЯёЁa-zA-Z]{1,4})$", cleaned)
    if m:
        return f"{m.group(1)} {m.group(2).upper()}"

    return re.sub(r"\s+", " ", cleaned).upper()


def decode_isu_group_code(group_str: str) -> Dict[str, Any]:
    """
    Полная расшифровка шифра учебной группы Педагогического института ИГУ
    на примере '206321 ДБ':
    • 206 — внутренний код факультета, института и направления подготовки в БД университета
    • 3   — текущий курс (3-й курс)
    • 2   — код профиля или специализации внутри направления
    • 1   — порядковый номер группы на потоке (1-я группа)
    • Д   — форма обучения (Д — Дневная/очная, З — Заочная, ОЗ — Очно-заочная)
    • Б   — уровень образования (Б — Бакалавриат, М — Магистратура, С — Специалитет, А — Аспирантура)
    """
    canonical = format_canonical_group_name(group_str)
    m = re.match(r"^(\d{3})(\d)(\d)(\d)(?:\s+([А-ЯЁA-Z]{1,4}))?$", canonical)
    if not m:
        return {
            "is_decoded": False,
            "canonical_group": canonical,
            "course": "",
            "short_info": "",
            "full_explanation": ""
        }

    faculty_code = m.group(1)      # 206
    course_num = m.group(2)        # 3
    profile_code = m.group(3)      # 2
    stream_group_num = m.group(4)  # 1
    suffix = (m.group(5) or "").upper()  # ДБ

    form_map = {
        "Д": "Дневная (очная)",
        "О": "Очная",
        "З": "Заочная",
        "В": "Вечерняя (очно-заочная)",
    }
    level_map = {
        "Б": "Бакалавриат",
        "М": "Магистратура",
        "С": "Специалитет",
        "А": "Аспирантура",
    }

    form_letter = suffix[0] if len(suffix) >= 1 else ""
    level_letter = suffix[1] if len(suffix) >= 2 else ""

    form_desc = form_map.get(form_letter, "Дневная (очная)" if not form_letter else form_letter)
    level_desc = level_map.get(level_letter, "Бакалавриат" if not level_letter else level_letter)

    course_label = f"{course_num}-й курс"
    short_info = f"{course_label}, {stream_group_num}-я группа, профиль {profile_code}, {form_desc.lower()}, {level_desc.lower()}"

    lines = [
        f"🔍 Расшифровка шифра вашей группы «{canonical}»:",
        f"• {faculty_code} — код факультета/института и направления подготовки в БД университета",
        f"• {course_num} — ваш текущий курс ({course_label})",
        f"• {profile_code} — код профиля / специализации внутри направления",
        f"• {stream_group_num} — порядковый номер вашей группы на потоке ({stream_group_num}-я группа)",
    ]
    if form_letter:
        lines.append(f"• {form_letter} — форма обучения ({form_desc})")
    if level_letter:
        lines.append(f"• {level_letter} — уровень образования ({level_desc})")

    return {
        "is_decoded": True,
        "canonical_group": canonical,
        "faculty_code": faculty_code,
        "course_num": int(course_num),
        "course": course_label,
        "profile_code": profile_code,
        "stream_group_num": stream_group_num,
        "form_letter": form_letter,
        "form_desc": form_desc,
        "level_letter": level_letter,
        "level_desc": level_desc,
        "short_info": short_info,
        "full_explanation": "\n".join(lines),
    }


def _load_saved_groups_from_json() -> None:
    """Подгружает сохраненные пользователями расписания групп из JSON-файла в память."""
    if not SAVED_GROUPS_JSON_PATH.exists():
        return
    try:
        data = json.loads(SAVED_GROUPS_JSON_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for grp, lessons in data.items():
                if isinstance(lessons, list) and lessons:
                    canon = format_canonical_group_name(grp)
                    GROUP_SCHEDULES[canon] = lessons
    except Exception:
        pass


def persist_group_schedule_to_json(group_name: str, lessons: List[Dict[str, Any]]) -> str:
    """
    Сохраняет расписание группы в память GROUP_SCHEDULES и на диск в saved_group_schedules.json,
    чтобы оно навсегда оставалось в базе и было доступно другим студентам этой группы.
    """
    canon = format_canonical_group_name(group_name)
    if not canon or not lessons:
        return canon
    GROUP_SCHEDULES[canon] = lessons
    try:
        existing: Dict[str, Any] = {}
        if SAVED_GROUPS_JSON_PATH.exists():
            existing = json.loads(SAVED_GROUPS_JSON_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        existing[canon] = lessons
        SAVED_GROUPS_JSON_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass
    return canon


_load_saved_groups_from_json()


def normalize_group_key(group_str: str) -> str:
    """Нормализует название группы для нечувствительного к регистру и пробелам поиска."""
    if not group_str:
        return ""
    canon = format_canonical_group_name(group_str)
    return re.sub(r"[\s\-_]+", "", canon.upper())


def find_group_schedule(group_query: str) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """
    Ищет расписание группы в предзагруженной и сохраненной базе GROUP_SCHEDULES.
    Поддерживает поиск по полному номеру ('206321 ДБ', '206321дб') или по цифрам ('206321').
    Возвращает кортеж (каноническое_название_группы, список_пар) или None.
    """
    _load_saved_groups_from_json()
    norm_q = normalize_group_key(group_query)
    if not norm_q:
        return None

    for canonical_name, lessons in GROUP_SCHEDULES.items():
        norm_canon = normalize_group_key(canonical_name)
        if norm_q == norm_canon or norm_q in norm_canon or norm_canon in norm_q:
            return canonical_name, lessons

    return None


def list_available_groups() -> List[str]:
    """Возвращает список всех предзагруженных и сохраненных групп."""
    _load_saved_groups_from_json()
    return list(GROUP_SCHEDULES.keys())
