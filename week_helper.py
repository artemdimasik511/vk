from datetime import date, timedelta

# Опорный понедельник: неделя с 26.10.2026 по 01.11.2026 является НЕЧЕТНОЙ (ODD)
ANCHOR_MONDAY = date(2026, 10, 26)

WEEK_TYPE_ODD = "ODD"    # Нечетная неделя (▲)
WEEK_TYPE_EVEN = "EVEN"  # Четная неделя (■)
WEEK_TYPE_ALL = "ALL"    # Каждую неделю

WEEK_ICONS = {
    WEEK_TYPE_ODD: "▲",
    WEEK_TYPE_EVEN: "■",
    WEEK_TYPE_ALL: "◆"
}

WEEK_NAMES = {
    WEEK_TYPE_ODD: "Нечетная (▲)",
    WEEK_TYPE_EVEN: "Четная (■)",
    WEEK_TYPE_ALL: "Каждая неделя"
}

DAY_NAMES_RU = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье"
]

DAY_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def get_monday(target_date: date) -> date:
    """Возвращает дату понедельника для переданной даты."""
    return target_date - timedelta(days=target_date.weekday())


def get_week_type(target_date: date) -> str:
    """
    Вычисляет тип недели (ODD или EVEN) относительно опорного понедельника 26.10.2026.
    26.10.2026 - 01.11.2026 -> ODD (разница 0 недель).
    """
    monday = get_monday(target_date)
    delta_days = (monday - ANCHOR_MONDAY).days
    delta_weeks = delta_days // 7
    
    if delta_weeks % 2 == 0:
        return WEEK_TYPE_ODD
    else:
        return WEEK_TYPE_EVEN


def format_day_name(day_index: int, short: bool = False) -> str:
    """Возвращает название дня недели на русском (0 = Понедельник)."""
    if 0 <= day_index < len(DAY_NAMES_RU):
        return DAY_SHORT_RU[day_index] if short else DAY_NAMES_RU[day_index]
    return "Неизвестно"


def normalize_target_date(date_str: str, base_date=None) -> str:
    """
    Нормализует дедлайн:
    - 'Без срока', 'Открытая дата', 'бессрочно' -> 'Без срока'
    - '25.11 (Ср)' -> возвращает как есть
    - '25.11' -> '25.11 (Ср)' с вычисленным днем недели
    - '25' или '25 число' -> '25.MM (Дн)'
    """
    if not date_str:
        return "Без срока"
    s = str(date_str).strip()
    s_lower = s.lower()
    if any(w in s_lower for w in ["без", "открыт", "бессроч", "нет", "open"]):
        return "Без срока"

    if base_date is None:
        base_date = date.today()

    import re
    # 1. Формат DD.MM (Дн)
    m_full = re.search(r"(\d{1,2})\.(\d{1,2})\s*\(([А-Яа-яA-Za-z]{2})\)", s)
    if m_full:
        day_n = int(m_full.group(1))
        month_n = int(m_full.group(2))
        return f"{day_n:02d}.{month_n:02d} ({m_full.group(3).capitalize()})"

    # 2. Формат DD.MM
    m_dm = re.search(r"(\d{1,2})\.(\d{1,2})", s)
    if m_dm:
        day_n = int(m_dm.group(1))
        month_n = int(m_dm.group(2))
        year_n = base_date.year
        if month_n < base_date.month or (month_n == base_date.month and day_n < base_date.day):
            year_n += 1
        try:
            d = date(year_n, month_n, day_n)
            dow_short = format_day_name(d.weekday(), short=True)
            return f"{day_n:02d}.{month_n:02d} ({dow_short})"
        except ValueError:
            pass

    # 2b. Формат '25 ноября' / '16 декабря'
    months_ru = {
        "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
        "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12
    }
    for stem, m_num in months_ru.items():
        m_ru = re.search(rf"\b(\d{{1,2}})\s+{stem}[а-я]*\b", s_lower)
        if m_ru:
            day_n = int(m_ru.group(1))
            year_n = base_date.year
            if m_num < base_date.month or (m_num == base_date.month and day_n < base_date.day):
                year_n += 1
            try:
                d = date(year_n, m_num, day_n)
                dow_short = format_day_name(d.weekday(), short=True)
                return f"{day_n:02d}.{m_num:02d} ({dow_short})"
            except ValueError:
                pass

    # 3. Число месяца (например, "25 число" или "25")
    m_day = re.search(r"\b(\d{1,2})\b", s)
    if m_day:
        day_n = int(m_day.group(1))
        if 1 <= day_n <= 31:
            month_n = base_date.month
            year_n = base_date.year
            if day_n < base_date.day:
                month_n += 1
                if month_n > 12:
                    month_n = 1
                    year_n += 1
            try:
                d = date(year_n, month_n, day_n)
                dow_short = format_day_name(d.weekday(), short=True)
                return f"{day_n:02d}.{month_n:02d} ({dow_short})"
            except ValueError:
                pass

    return s

