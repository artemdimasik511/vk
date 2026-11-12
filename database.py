import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from config import (
    DB_PATH,
    FREE_TRIAL_DAYS,
    SUBSCRIPTION_PRICE_RUB,
    SUBSCRIPTION_DAYS,
    REFERRER_BONUS_DAYS,
    INVITED_BONUS_DAYS,
)
from group_schedules import (
    GROUP_SCHEDULES,
    normalize_group_key,
    find_group_schedule,
    format_canonical_group_name,
    persist_group_schedule_to_json,
    decode_isu_group_code,
)

# Пороги ачивок за выполнение домашних заданий (10, 20, 30, 40, 50)
ACHIEVEMENT_MILESTONES = [
    (10, "🥉 Выполнил 10 домашних заданий"),
    (20, "🥈 Выполнил 20 домашних заданий"),
    (30, "🥇 Выполнил 30 домашних заданий"),
    (40, "💎 Выполнил 40 домашних заданий"),
    (50, "👑 Выполнил 50 домашних заданий"),
]


def _parse_iso_dt(val: Optional[str]) -> Optional[datetime]:
    """Безопасно парсит дату/время из SQLite ('YYYY-MM-DD HH:MM:SS' или ISO)."""
    if not val or not str(val).strip():
        return None
    s = str(val).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _fmt_iso_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Создает соединение с базой данных SQLite с защитой от блокировок при 500+ пользователях."""
    target_path = db_path if db_path is not None else DB_PATH
    conn = sqlite3.connect(target_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # Включаем WAL режим, ожидание очереди 5 секунд и оптимальную синхронизацию
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Инициализация таблиц базы данных, миграция колонок профиля/подписки/рефералов и создание индексов."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # 1. Таблица пользователей с личным профилем, группой, шагом регистрации, подпиской и реферальной системой
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vk_id INTEGER UNIQUE NOT NULL,
                last_name TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                nickname TEXT DEFAULT '',
                course TEXT DEFAULT '',
                group_name TEXT DEFAULT '',
                reg_step TEXT DEFAULT 'WAIT_HELLO',
                trial_ends_at TEXT DEFAULT '',
                subscription_until TEXT DEFAULT '',
                expiry_notified INTEGER DEFAULT 0,
                referred_by INTEGER DEFAULT 0,
                referral_rewarded INTEGER DEFAULT 0,
                referrals_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Безопасная миграция для уже существующих баз SQLite
        existing_cols = {
            row["name"] for row in cursor.execute("PRAGMA table_info(users);").fetchall()
        }
        migrations = [
            ("last_name", "TEXT DEFAULT ''"),
            ("first_name", "TEXT DEFAULT ''"),
            ("nickname", "TEXT DEFAULT ''"),
            ("course", "TEXT DEFAULT ''"),
            ("group_name", "TEXT DEFAULT ''"),
            ("reg_step", "TEXT DEFAULT 'REGISTERED'"),
            ("trial_ends_at", "TEXT DEFAULT ''"),
            ("subscription_until", "TEXT DEFAULT ''"),
            ("expiry_notified", "INTEGER DEFAULT 0"),
            ("referred_by", "INTEGER DEFAULT 0"),
            ("referral_rewarded", "INTEGER DEFAULT 0"),
            ("referrals_count", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def};")

        # Заполняем trial_ends_at (+30 дней от created_at) для существующих пользователей, у которых поле пустое
        default_trial_dt = _fmt_iso_dt(datetime.now() + timedelta(days=FREE_TRIAL_DAYS))
        cursor.execute(
            "UPDATE users SET trial_ends_at = ? WHERE trial_ends_at IS NULL OR trial_ends_at = ''",
            (default_trial_dt,)
        )
        
        # 2. Таблица индивидуального расписания пользователя (привязана к личному vk_id)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vk_id INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                time TEXT NOT NULL,
                subject TEXT NOT NULL,
                teacher TEXT NOT NULL,
                room TEXT,
                week_type TEXT NOT NULL,
                lesson_type TEXT NOT NULL,
                FOREIGN KEY (vk_id) REFERENCES users (vk_id)
            );
        """)

        # 3. Таблица общих расписаний групп (предзагруженная и пополняемая база по номеру группы)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name TEXT NOT NULL,
                day_of_week INTEGER NOT NULL,
                time TEXT NOT NULL,
                subject TEXT NOT NULL,
                teacher TEXT NOT NULL,
                room TEXT,
                week_type TEXT NOT NULL,
                lesson_type TEXT NOT NULL
            );
        """)
        
        # 4. Таблица индивидуальных домашних заданий (строго по личному vk_id)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS homework (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vk_id INTEGER NOT NULL,
                teacher TEXT NOT NULL,
                subject TEXT NOT NULL,
                task_text TEXT NOT NULL,
                target_date TEXT NOT NULL,
                is_done INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (vk_id) REFERENCES users (vk_id)
            );
        """)

        # 5. Таблица платежей ЮKassa (30 рублей на 14 дней / 2 недели)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vk_id INTEGER NOT NULL,
                payment_id TEXT UNIQUE NOT NULL,
                amount INTEGER NOT NULL DEFAULT 30,
                days INTEGER NOT NULL DEFAULT 14,
                status TEXT NOT NULL DEFAULT 'pending',
                confirmation_url TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP DEFAULT NULL
            );
        """)

        # 6. Индексы для мгновенной выборки при 500+ пользователях (~10 000+ строк расписания и ДЗ)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_schedule_vk_day ON schedule(vk_id, day_of_week);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_homework_user_active ON homework(vk_id, is_done);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_group_sched_name ON group_schedules(group_name);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_vk_status ON payments(vk_id, status);")
        
        conn.commit()

    # Заполняем общую базу групп предзагруженными расписаниями (например, 206321 ДБ)
    seed_preloaded_group_schedules(db_path)


def seed_preloaded_group_schedules(db_path: Optional[str] = None) -> None:
    """Синхронизирует предзагруженные расписания групп из group_schedules.py в таблицу SQLite."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        for group_name, lessons in GROUP_SCHEDULES.items():
            clean_group = format_canonical_group_name(group_name)
            cursor.execute("SELECT COUNT(*) as cnt FROM group_schedules WHERE group_name = ?", (clean_group,))
            row = cursor.fetchone()
            if not row or row["cnt"] == 0:
                for item in lessons:
                    cursor.execute("""
                        INSERT INTO group_schedules
                        (group_name, day_of_week, time, subject, teacher, room, week_type, lesson_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        clean_group,
                        int(item.get("day_of_week", 0)),
                        str(item.get("time", "")).strip(),
                        str(item.get("subject", "")).strip(),
                        str(item.get("teacher", "")).strip(),
                        str(item.get("room", "")).strip(),
                        str(item.get("week_type", "ALL")).upper().strip(),
                        str(item.get("lesson_type", "PRACTICE")).upper().strip(),
                    ))
        conn.commit()


def save_group_schedule(group_name: str, lessons: List[Dict[str, Any]], db_path: Optional[str] = None) -> int:
    """
    Сохраняет или обновляет общее расписание для указанной учебной группы в таблице SQLite
    и в постоянном JSON-файле, чтобы другие студенты этой группы могли сразу взять его готовым.
    """
    clean_group = format_canonical_group_name(group_name)
    if not clean_group:
        return 0
    target_norm = normalize_group_key(clean_group)
    normalized_lessons: List[Dict[str, Any]] = []

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        # Удаляем старые записи этой группы (с учетом возможных вариантов написания пробелов/регистра)
        existing_groups = cursor.execute("SELECT DISTINCT group_name FROM group_schedules").fetchall()
        for r in existing_groups:
            if normalize_group_key(r["group_name"]) == target_norm:
                cursor.execute("DELETE FROM group_schedules WHERE group_name = ?", (r["group_name"],))

        inserted = 0
        for item in lessons:
            subj = str(item.get("subject", "")).strip()
            if not subj:
                continue
            lesson_obj = {
                "day_of_week": int(item.get("day_of_week", 0)),
                "time": str(item.get("time", "")).strip(),
                "subject": subj,
                "teacher": str(item.get("teacher", "")).strip(),
                "room": str(item.get("room", "")).strip(),
                "week_type": str(item.get("week_type", "ALL")).upper().strip(),
                "lesson_type": str(item.get("lesson_type", "PRACTICE")).upper().strip(),
            }
            normalized_lessons.append(lesson_obj)
            cursor.execute("""
                INSERT INTO group_schedules
                (group_name, day_of_week, time, subject, teacher, room, week_type, lesson_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                clean_group,
                lesson_obj["day_of_week"],
                lesson_obj["time"],
                lesson_obj["subject"],
                lesson_obj["teacher"],
                lesson_obj["room"],
                lesson_obj["week_type"],
                lesson_obj["lesson_type"],
            ))
            inserted += 1
        conn.commit()

    if normalized_lessons and db_path is None:
        persist_group_schedule_to_json(clean_group, normalized_lessons)

    return inserted


def get_group_schedule_by_query(group_query: str, db_path: Optional[str] = None) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """
    Ищет расписание группы сначала в таблице SQLite group_schedules, затем в модуле group_schedules.py.
    Возвращает (название_группы, список_пар) или None.
    """
    norm_q = normalize_group_key(group_query)
    if not norm_q:
        return None

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        rows = cursor.execute("SELECT DISTINCT group_name FROM group_schedules").fetchall()
        for r in rows:
            g_name = r["group_name"]
            norm_g = normalize_group_key(g_name)
            if norm_q == norm_g or norm_q in norm_g or norm_g in norm_q:
                lessons_rows = cursor.execute("""
                    SELECT day_of_week, time, subject, teacher, room, week_type, lesson_type
                    FROM group_schedules
                    WHERE group_name = ?
                    ORDER BY day_of_week ASC, time ASC
                """, (g_name,)).fetchall()
                return format_canonical_group_name(g_name), [dict(lr) for lr in lessons_rows]

    return find_group_schedule(group_query)


def get_available_group_names(db_path: Optional[str] = None) -> List[str]:
    """Возвращает список всех групп, для которых загружено общее расписание."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        rows = cursor.execute("SELECT DISTINCT group_name FROM group_schedules ORDER BY group_name ASC").fetchall()
        db_groups = [format_canonical_group_name(r["group_name"]) for r in rows]
    for k in GROUP_SCHEDULES.keys():
        canon_k = format_canonical_group_name(k)
        if canon_k not in db_groups:
            db_groups.append(canon_k)
    return db_groups


def register_user(vk_id: int, default_reg_step: str = "REGISTERED") -> None:
    """Создает запись пользователя в БД с бесплатным пробным периодом 30 дней, если его еще нет."""
    trial_dt = _fmt_iso_dt(datetime.now() + timedelta(days=FREE_TRIAL_DAYS))
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO users (vk_id, reg_step, trial_ends_at, expiry_notified) VALUES (?, ?, ?, 0)",
            (vk_id, default_reg_step, trial_dt)
        )
        conn.commit()


def get_user_profile(vk_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает профиль пользователя по его личному vk_id или None, если пользователь еще не начинал диалог."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE vk_id = ?", (vk_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def upsert_user_profile(
    vk_id: int,
    last_name: Optional[str] = None,
    first_name: Optional[str] = None,
    nickname: Optional[str] = None,
    course: Optional[str] = None,
    group_name: Optional[str] = None,
    reg_step: Optional[str] = None
) -> Dict[str, Any]:
    """Создает или обновляет поля профиля пользователя (автоматически нормализует шифр группы и выдает 30 дней пробного периода при первом входе)."""
    norm_group = format_canonical_group_name(group_name) if group_name is not None else None
    decoded_course = None
    if norm_group:
        decoded_info = decode_isu_group_code(norm_group)
        if decoded_info.get("course"):
            decoded_course = decoded_info["course"]

    final_course = course if course is not None else decoded_course

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE vk_id = ?", (vk_id,))
        existing = cursor.fetchone()
        if not existing:
            trial_dt = _fmt_iso_dt(datetime.now() + timedelta(days=FREE_TRIAL_DAYS))
            cursor.execute(
                """
                INSERT INTO users (vk_id, last_name, first_name, nickname, course, group_name, reg_step, trial_ends_at, expiry_notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    vk_id,
                    (last_name or "").strip(),
                    (first_name or "").strip(),
                    (nickname or "").strip(),
                    (final_course or "").strip(),
                    (norm_group or "").strip(),
                    reg_step or "WAIT_HELLO",
                    trial_dt
                )
            )
        else:
            updates = []
            params = []
            if last_name is not None:
                updates.append("last_name = ?")
                params.append(last_name.strip())
            if first_name is not None:
                updates.append("first_name = ?")
                params.append(first_name.strip())
            if nickname is not None:
                updates.append("nickname = ?")
                params.append(nickname.strip())
            if final_course is not None:
                updates.append("course = ?")
                params.append(final_course.strip())
            if norm_group is not None:
                updates.append("group_name = ?")
                params.append(norm_group.strip())
            if reg_step is not None:
                updates.append("reg_step = ?")
                params.append(reg_step.strip())
            if not existing["trial_ends_at"]:
                updates.append("trial_ends_at = ?")
                params.append(_fmt_iso_dt(datetime.now() + timedelta(days=FREE_TRIAL_DAYS)))
            if updates:
                params.append(vk_id)
                cursor.execute(f"UPDATE users SET {', '.join(updates)} WHERE vk_id = ?", tuple(params))
        conn.commit()
    return get_user_profile(vk_id) or {}


# =========================================================================
# Функции управления подпиской и монетизацией (1 месяц бесплатно, далее 30 ₽ / 14 дней)
# =========================================================================

def get_subscription_status(vk_id: int, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Проверяет статус подписки пользователя:
    - Первые 30 дней с момента входа в бота (trial_ends_at) — бесплатный пробный период (TRIAL).
    - После оплаты 30 рублей — платная подписка на 14 дней (PAID).
    - Если оба срока прошли — подписка истекла (EXPIRED), нужно оплатить 30 ₽ на следующие 2 недели.
    """
    now = now_dt or datetime.now()
    profile = get_user_profile(vk_id)
    if not profile:
        trial_end = now + timedelta(days=FREE_TRIAL_DAYS)
        return {
            "is_active": True,
            "status_type": "TRIAL",
            "expires_at": trial_end.strftime("%d.%m.%Y"),
            "expires_iso": _fmt_iso_dt(trial_end),
            "days_left": FREE_TRIAL_DAYS,
            "price_rub": SUBSCRIPTION_PRICE_RUB,
            "period_days": SUBSCRIPTION_DAYS,
            "label": f"🎁 Бесплатный пробный месяц (осталось {FREE_TRIAL_DAYS} дн., до {trial_end.strftime('%d.%m.%Y')})"
        }

    trial_dt = _parse_iso_dt(profile.get("trial_ends_at"))
    if not trial_dt:
        created_dt = _parse_iso_dt(profile.get("created_at")) or now
        trial_dt = created_dt + timedelta(days=FREE_TRIAL_DAYS)

    paid_dt = _parse_iso_dt(profile.get("subscription_until"))

    # Определяем самую позднюю дату окончания доступа
    if paid_dt and paid_dt > now and (paid_dt >= trial_dt):
        delta_seconds = (paid_dt - now).total_seconds()
        days_left = max(1, int((delta_seconds + 86399) // 86400))
        return {
            "is_active": True,
            "status_type": "PAID",
            "expires_at": paid_dt.strftime("%d.%m.%Y"),
            "expires_iso": _fmt_iso_dt(paid_dt),
            "days_left": days_left,
            "price_rub": SUBSCRIPTION_PRICE_RUB,
            "period_days": SUBSCRIPTION_DAYS,
            "label": f"💎 Подписка активна (осталось {days_left} дн., до {paid_dt.strftime('%d.%m.%Y')})"
        }

    if trial_dt > now:
        delta_seconds = (trial_dt - now).total_seconds()
        days_left = max(1, int((delta_seconds + 86399) // 86400))
        return {
            "is_active": True,
            "status_type": "TRIAL",
            "expires_at": trial_dt.strftime("%d.%m.%Y"),
            "expires_iso": _fmt_iso_dt(trial_dt),
            "days_left": days_left,
            "price_rub": SUBSCRIPTION_PRICE_RUB,
            "period_days": SUBSCRIPTION_DAYS,
            "label": f"🎁 Бесплатный пробный месяц (осталось {days_left} дн., до {trial_dt.strftime('%d.%m.%Y')})"
        }

    expired_dt = paid_dt if (paid_dt and paid_dt > trial_dt) else trial_dt
    return {
        "is_active": False,
        "status_type": "EXPIRED",
        "expires_at": expired_dt.strftime("%d.%m.%Y"),
        "expires_iso": _fmt_iso_dt(expired_dt),
        "days_left": 0,
        "price_rub": SUBSCRIPTION_PRICE_RUB,
        "period_days": SUBSCRIPTION_DAYS,
        "label": f"⚠️ Пробный период закончился ({expired_dt.strftime('%d.%m.%Y')}). Требуется продление: {SUBSCRIPTION_PRICE_RUB} ₽ на 2 недели"
    }


def activate_paid_subscription(
    vk_id: int,
    days: int = SUBSCRIPTION_DAYS,
    payment_id: Optional[str] = None,
    now_dt: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Продлевает подписку пользователя на `days` дней (по умолчанию 14 дней / 2 недели за 30 рублей).
    Если у пользователя еще действовал пробный период или подписка — прибавляет 14 дней к текущей дате окончания.
    Если срок уже истек — прибавляет 14 дней от текущего момента.
    """
    now = now_dt or datetime.now()
    register_user(vk_id)
    profile = get_user_profile(vk_id) or {}

    trial_dt = _parse_iso_dt(profile.get("trial_ends_at")) or now
    paid_dt = _parse_iso_dt(profile.get("subscription_until"))

    base_dt = now
    if paid_dt and paid_dt > base_dt:
        base_dt = paid_dt
    if trial_dt and trial_dt > base_dt:
        base_dt = trial_dt

    new_until = base_dt + timedelta(days=days)
    new_until_str = _fmt_iso_dt(new_until)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET subscription_until = ?, expiry_notified = 0 WHERE vk_id = ?",
            (new_until_str, vk_id)
        )
        if payment_id:
            cursor.execute(
                "UPDATE payments SET status = 'succeeded', paid_at = ? WHERE payment_id = ?",
                (_fmt_iso_dt(now), payment_id)
            )
        conn.commit()

    return get_subscription_status(vk_id, now_dt=now)


def set_user_trial_expiration(
    vk_id: int,
    expires_dt: datetime,
    reset_notified: bool = True
) -> Dict[str, Any]:
    """
    Устанавливает дату окончания пробного периода (и очищает платную подписку) —
    используется для тестирования истечения пробного месяца.
    """
    register_user(vk_id)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET trial_ends_at = ?, subscription_until = '', expiry_notified = ? WHERE vk_id = ?",
            (_fmt_iso_dt(expires_dt), 0 if reset_notified else 1, vk_id)
        )
        conn.commit()
    return get_subscription_status(vk_id)


def save_pending_payment(
    vk_id: int,
    payment_id: str,
    amount: int = SUBSCRIPTION_PRICE_RUB,
    days: int = SUBSCRIPTION_DAYS,
    confirmation_url: str = ""
) -> None:
    """Сохраняет созданный платеж ЮKassa со статусом 'pending'."""
    register_user(vk_id)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO payments (vk_id, payment_id, amount, days, status, confirmation_url)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (vk_id, payment_id, amount, days, confirmation_url)
        )
        conn.commit()


def get_latest_pending_payment(vk_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает последний ожидающий оплаты платеж пользователя."""
    with get_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT * FROM payments WHERE vk_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
            (vk_id,)
        ).fetchone()
        return dict(row) if row else None


def get_all_pending_payments() -> List[Dict[str, Any]]:
    """Возвращает все ожидающие платежи для фоновой автоматической проверки оплаты через ЮKassa."""
    with get_connection() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM payments WHERE status = 'pending' ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_payment_status(payment_id: str, status: str) -> None:
    """Обновляет статус платежа в БД (например, 'canceled' или 'succeeded')."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE payments SET status = ? WHERE payment_id = ?",
            (status, payment_id)
        )
        conn.commit()


def get_newly_expired_users(now_dt: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Находит пользователей, у которых закончился 30-дневный бесплатный пробный период
    (или платная подписка) и которым еще НЕ было отправлено уведомление об окончании (expiry_notified == 0).
    """
    now = now_dt or datetime.now()
    with get_connection() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(
            "SELECT * FROM users WHERE expiry_notified = 0"
        ).fetchall()

    expired_users: List[Dict[str, Any]] = []
    for r in rows:
        user_dict = dict(r)
        vk_id = int(user_dict["vk_id"])
        sub = get_subscription_status(vk_id, now_dt=now)
        if not sub["is_active"]:
            expired_users.append(user_dict)
    return expired_users


def mark_user_expiry_notified(vk_id: int) -> None:
    """Помечает, что пользователю отправлено уведомление об окончании пробного периода / подписки."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET expiry_notified = 1 WHERE vk_id = ?",
            (vk_id,)
        )
        conn.commit()


# =========================================================================
# Реферальная система (Защита экономики: 3 правила + двойной бонус +7 / +3 дня)
# =========================================================================

def register_new_user_with_referral(
    vk_id: int,
    referrer_vk_id: Optional[int] = None,
    reg_step: str = "WAIT_HELLO",
    now_dt: Optional[datetime] = None
) -> Tuple[bool, Optional[int]]:
    """
    Правило 1 (Учет только новых пользователей и защита от самоприглашения):
    - Привязка реферала срабатывает ТОЛЬКО если приглашенного (`vk_id`) еще ни разу не записывали в таблицу `users`.
    - Нельзя приглашать самого себя (`referrer_vk_id != vk_id`).
    - Возвращает кортеж `(привязан_ли_реферер, id_реферера)`.
    """
    now = now_dt or datetime.now()
    trial_dt = _fmt_iso_dt(now + timedelta(days=FREE_TRIAL_DAYS))

    valid_referrer = 0
    if referrer_vk_id is not None:
        try:
            cand = int(referrer_vk_id)
            if cand > 0 and cand != int(vk_id):
                valid_referrer = cand
        except (ValueError, TypeError):
            valid_referrer = 0

    with get_connection() as conn:
        cursor = conn.cursor()
        existing = cursor.execute(
            "SELECT vk_id, referred_by FROM users WHERE vk_id = ?",
            (vk_id,)
        ).fetchone()

        if existing:
            # Пользователь уже есть в таблице users — по Правилу 1 реферальная привязка для старых пользователей не применяется
            if reg_step:
                cursor.execute(
                    "UPDATE users SET reg_step = ? WHERE vk_id = ?",
                    (reg_step, vk_id)
                )
                conn.commit()
            return False, None

        # Новый пользователь впервые попадает в таблицу users
        cursor.execute(
            """
            INSERT INTO users (
                vk_id, reg_step, trial_ends_at, expiry_notified,
                referred_by, referral_rewarded, referrals_count
            )
            VALUES (?, ?, ?, 0, ?, 0, 0)
            """,
            (vk_id, reg_step or "WAIT_HELLO", trial_dt, valid_referrer)
        )
        conn.commit()

    if valid_referrer > 0:
        return True, valid_referrer
    return False, None


def complete_referral_if_eligible(
    invited_vk_id: int,
    now_dt: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """
    Правило 2 (Награда за целевое действие, а не за клик) и Правило 3 (Умное суммирование времени + двойной бонус):
    - Вызывается после того, как приглашенный студент выбрал своё ФИ (ник) и учебную группу в меню.
    - Проверяет, что у приглашенного задан `referred_by > 0`, награда еще не выдавалась (`referral_rewarded == 0`),
      и заполнены ФИ/ник + номер учебной группы (`group_name`).
    - Начисляет пригласившему (`referrer_id`) +7 дней (`REFERRER_BONUS_DAYS`):
      * Если у пригласившего активен триал или подписка — прибавляет +7 дней к текущей дате окончания.
      * Если подписка уже закончилась — устанавливает новую дату (`now + 7 дней`) и сбрасывает `expiry_notified = 0`.
    - Начисляет приглашенному другу (`invited_vk_id`) +3 бонусных дня (`INVITED_BONUS_DAYS`) к его стартовому периоду.
    """
    now = now_dt or datetime.now()

    with get_connection() as conn:
        cursor = conn.cursor()
        invited_row = cursor.execute(
            "SELECT * FROM users WHERE vk_id = ?",
            (invited_vk_id,)
        ).fetchone()

        if not invited_row:
            return None

        invited = dict(invited_row)
        referrer_id = int(invited.get("referred_by") or 0)
        already_rewarded = int(invited.get("referral_rewarded") or 0)

        # Защита от самоприглашения, повторного начисления или отсутствия реферера
        if referrer_id <= 0 or referrer_id == int(invited_vk_id) or already_rewarded != 0:
            return None

        # Правило 2: Проверяем, что целевое действие выполнено (выбраны ФИ/ник И учебная группа)
        has_name = bool(
            (invited.get("first_name") or "").strip()
            or (invited.get("last_name") or "").strip()
            or (invited.get("nickname") or "").strip()
        )
        has_group = bool((invited.get("group_name") or "").strip())
        if not (has_name and has_group):
            return None

        # Гарантируем, что профиль пригласившего существует в БД
        ref_row = cursor.execute(
            "SELECT * FROM users WHERE vk_id = ?",
            (referrer_id,)
        ).fetchone()
        if not ref_row:
            default_ref_trial = _fmt_iso_dt(now + timedelta(days=FREE_TRIAL_DAYS))
            cursor.execute(
                """
                INSERT INTO users (vk_id, reg_step, trial_ends_at, expiry_notified, referred_by, referral_rewarded, referrals_count)
                VALUES (?, 'REGISTERED', ?, 0, 0, 0, 0)
                """,
                (referrer_id, default_ref_trial)
            )
            ref_row = cursor.execute(
                "SELECT * FROM users WHERE vk_id = ?",
                (referrer_id,)
            ).fetchone()

        referrer = dict(ref_row)

        # 1) Начисляем +3 бонусных дня приглашенному другу (к его стартовому триалу)
        inv_trial_dt = _parse_iso_dt(invited.get("trial_ends_at")) or (now + timedelta(days=FREE_TRIAL_DAYS))
        inv_paid_dt = _parse_iso_dt(invited.get("subscription_until"))
        inv_base_dt = inv_trial_dt if inv_trial_dt > now else now
        if inv_paid_dt and inv_paid_dt > inv_base_dt:
            inv_base_dt = inv_paid_dt
        new_inv_until_dt = inv_base_dt + timedelta(days=INVITED_BONUS_DAYS)
        new_inv_until_str = _fmt_iso_dt(new_inv_until_dt)

        cursor.execute(
            """
            UPDATE users
            SET trial_ends_at = ?, referral_rewarded = 1, expiry_notified = 0
            WHERE vk_id = ?
            """,
            (new_inv_until_str, invited_vk_id)
        )

        # 2) Правило 3: Умное суммирование времени (+7 дней пригласившему)
        ref_trial_dt = _parse_iso_dt(referrer.get("trial_ends_at"))
        ref_paid_dt = _parse_iso_dt(referrer.get("subscription_until"))

        active_candidates = [dt for dt in (ref_trial_dt, ref_paid_dt) if dt is not None and dt > now]
        if active_candidates:
            was_expired_before = False
            ref_base_dt = max(active_candidates)
        else:
            was_expired_before = True
            ref_base_dt = now

        new_ref_until_dt = ref_base_dt + timedelta(days=REFERRER_BONUS_DAYS)
        new_ref_until_str = _fmt_iso_dt(new_ref_until_dt)

        # Если у пригласившего уже была платная подписка или подписка истекла — обновляем subscription_until и trial_ends_at
        if ref_paid_dt and (not ref_trial_dt or ref_paid_dt >= ref_trial_dt):
            cursor.execute(
                """
                UPDATE users
                SET subscription_until = ?,
                    referrals_count = COALESCE(referrals_count, 0) + 1,
                    expiry_notified = 0
                WHERE vk_id = ?
                """,
                (new_ref_until_str, referrer_id)
            )
        elif was_expired_before:
            cursor.execute(
                """
                UPDATE users
                SET subscription_until = ?,
                    trial_ends_at = ?,
                    referrals_count = COALESCE(referrals_count, 0) + 1,
                    expiry_notified = 0
                WHERE vk_id = ?
                """,
                (new_ref_until_str, new_ref_until_str, referrer_id)
            )
        else:
            cursor.execute(
                """
                UPDATE users
                SET trial_ends_at = ?,
                    referrals_count = COALESCE(referrals_count, 0) + 1,
                    expiry_notified = 0
                WHERE vk_id = ?
                """,
                (new_ref_until_str, referrer_id)
            )

        conn.commit()

    updated_invited = get_user_profile(invited_vk_id) or invited
    return {
        "referrer_id": referrer_id,
        "invited_id": invited_vk_id,
        "invited_identity": format_user_identity(updated_invited, invited_vk_id),
        "invited_group": (updated_invited.get("group_name") or "").strip(),
        "referrer_bonus_days": REFERRER_BONUS_DAYS,
        "invited_bonus_days": INVITED_BONUS_DAYS,
        "was_expired_before": was_expired_before,
        "referrer_subscription": get_subscription_status(referrer_id, now_dt=now),
        "invited_subscription": get_subscription_status(invited_vk_id, now_dt=now),
    }


def get_referral_stats(vk_id: int) -> Dict[str, Any]:
    """Возвращает статистику рефералов пользователя (сколько друзей пригласил и сколько дней бонуса получил)."""
    profile = get_user_profile(vk_id) or {}
    referrals_count = int(profile.get("referrals_count") or 0)
    referred_by = int(profile.get("referred_by") or 0)
    referral_rewarded = int(profile.get("referral_rewarded") or 0)
    return {
        "vk_id": vk_id,
        "referral_code": f"ref_{vk_id}",
        "referrals_count": referrals_count,
        "bonus_days_earned": referrals_count * REFERRER_BONUS_DAYS,
        "referred_by": referred_by if referred_by > 0 else None,
        "referral_rewarded": bool(referral_rewarded),
        "referrer_bonus_days": REFERRER_BONUS_DAYS,
        "invited_bonus_days": INVITED_BONUS_DAYS,
    }



def format_user_identity(profile: Optional[Dict[str, Any]], vk_id: Optional[int] = None) -> str:
    """
    Форматирует идентификатор пользователя строго по шаблону: 'Фамилия Имя (Никнейм)'.
    Например: 'Иванов Артём (artem_pro)'.
    """
    if not profile:
        return f"Студент (ID {vk_id})" if vk_id else "Студент"
    last_name = (profile.get("last_name") or "").strip()
    first_name = (profile.get("first_name") or "").strip()
    nickname = (profile.get("nickname") or "").strip()
    full_name = f"{last_name} {first_name}".strip()
    if not full_name:
        full_name = f"Студент #{profile.get('vk_id', vk_id or '')}".strip()
    if nickname:
        return f"{full_name} ({nickname})"
    return full_name


def get_user_homework_stats(vk_id: int) -> Dict[str, Any]:
    """
    Возвращает индивидуальную статистику домашних заданий пользователя и его ачивки:
    - done_count: сколько заданий выполнено (is_done = 1)
    - pending_count: сколько заданий еще нужно выполнить (is_done = 0)
    - achievements: список открытых ачивок (за 10, 20, 30, 40, 50 выполненных заданий)
    - next_milestone: ближайшая следующая цель для ачивки
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        done_row = cursor.execute(
            "SELECT COUNT(*) as cnt FROM homework WHERE vk_id = ? AND is_done = 1",
            (vk_id,)
        ).fetchone()
        pending_row = cursor.execute(
            "SELECT COUNT(*) as cnt FROM homework WHERE vk_id = ? AND is_done = 0",
            (vk_id,)
        ).fetchone()

        done_count = int(done_row["cnt"]) if done_row else 0
        pending_count = int(pending_row["cnt"]) if pending_row else 0

    unlocked = []
    next_milestone = None
    for threshold, title in ACHIEVEMENT_MILESTONES:
        if done_count >= threshold:
            unlocked.append(title)
        elif next_milestone is None:
            next_milestone = threshold

    return {
        "done_count": done_count,
        "pending_count": pending_count,
        "total_count": done_count + pending_count,
        "achievements": unlocked,
        "next_milestone": next_milestone
    }


def check_newly_unlocked_achievement(done_count: int) -> Optional[str]:
    """Если текущее число выполненных заданий ровно достигло порога 10, 20, 30, 40 или 50 — возвращает название ачивки."""
    for threshold, title in ACHIEVEMENT_MILESTONES:
        if done_count == threshold:
            return title
    return None


def apply_group_schedule_to_user(vk_id: int, group_query: str) -> Optional[Tuple[str, int]]:
    """
    Находит расписание группы в общей базе и копирует его в индивидуальное расписание студента.
    Возвращает (каноническое_название_группы, количество_пар) или None, если группа не найдена.
    """
    found = get_group_schedule_by_query(group_query)
    if not found:
        return None
    canonical_group, lessons = found
    count = save_schedule(vk_id, lessons)
    upsert_user_profile(vk_id, group_name=canonical_group)
    return canonical_group, count


def save_schedule(vk_id: int, lessons: List[Dict[str, Any]]) -> int:
    """
    Очищает старое расписание пользователя и записывает новые уроки.
    Возвращает количество успешно сохраненных записей.
    """
    register_user(vk_id)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM schedule WHERE vk_id = ?", (vk_id,))
        
        inserted = 0
        for item in lessons:
            day = int(item.get("day_of_week", 0))
            time_val = str(item.get("time", "")).strip()
            subject = str(item.get("subject", "")).strip()
            teacher = str(item.get("teacher", "")).strip()
            room = str(item.get("room", "")).strip()
            week_type = str(item.get("week_type", "ALL")).upper().strip()
            if week_type not in ("ODD", "EVEN", "ALL"):
                week_type = "ALL"
            lesson_type = str(item.get("lesson_type", "PRACTICE")).upper().strip()
            if lesson_type not in ("PRACTICE", "LECTURE"):
                lesson_type = "PRACTICE"
            
            if subject:
                cursor.execute("""
                    INSERT INTO schedule 
                    (vk_id, day_of_week, time, subject, teacher, room, week_type, lesson_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (vk_id, day, time_val, subject, teacher, room, week_type, lesson_type))
                inserted += 1
                
        conn.commit()
        return inserted


def get_schedule_for_day(vk_id: int, day_of_week: int, current_week_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Выбирает пары на указанный день недели с учетом четности/нечетности недели.
    Если current_week_type указан, возвращает пары с week_type == current_week_type или 'ALL'.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        if current_week_type:
            cursor.execute("""
                SELECT * FROM schedule 
                WHERE vk_id = ? AND day_of_week = ? AND (week_type = ? OR week_type = 'ALL')
                ORDER BY time ASC
            """, (vk_id, day_of_week, current_week_type))
        else:
            cursor.execute("""
                SELECT * FROM schedule 
                WHERE vk_id = ? AND day_of_week = ?
                ORDER BY time ASC
            """, (vk_id, day_of_week))
            
        return [dict(row) for row in cursor.fetchall()]


def get_full_schedule(vk_id: int) -> List[Dict[str, Any]]:
    """Возвращает всё расписание пользователя по дням."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM schedule 
            WHERE vk_id = ? 
            ORDER BY day_of_week ASC, time ASC
        """, (vk_id,))
        return [dict(row) for row in cursor.fetchall()]


def get_practice_lessons(vk_id: int) -> List[Dict[str, Any]]:
    """Возвращает только практические занятия пользователя."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM schedule 
            WHERE vk_id = ? AND lesson_type = 'PRACTICE'
            ORDER BY day_of_week ASC, time ASC
        """, (vk_id,))
        return [dict(row) for row in cursor.fetchall()]


def add_homework(vk_id: int, teacher: str, subject: str, task_text: str, target_date: str) -> int:
    """Добавляет индивидуальное домашнее задание пользователя в базу данных."""
    register_user(vk_id)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO homework (vk_id, teacher, subject, task_text, target_date, is_done)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (vk_id, teacher.strip(), subject.strip(), task_text.strip(), target_date.strip()))
        conn.commit()
        return cursor.lastrowid


def get_active_homework(vk_id: int) -> List[Dict[str, Any]]:
    """Возвращает активные (невыполненные) домашние задания конкретного пользователя."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM homework 
            WHERE vk_id = ? AND is_done = 0
            ORDER BY id ASC
        """, (vk_id,))
        return [dict(row) for row in cursor.fetchall()]


def get_homework_by_id(vk_id: int, homework_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает конкретное домашнее задание пользователя по его ID в БД."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM homework WHERE id = ? AND vk_id = ?",
            (homework_id, vk_id)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def resolve_active_homework_by_number(vk_id: int, num_or_id: int) -> Optional[Tuple[int, Dict[str, Any]]]:
    """
    Находит активное задание по порядковому номеру в списке (1, 2, 3...)
    либо по внутреннему ID задания в базе.
    Возвращает кортеж (порядковый_номер_от_1, словарь_задания) или None.
    """
    items = get_active_homework(vk_id)
    if not items:
        return None
    if 1 <= num_or_id <= len(items):
        return num_or_id, items[num_or_id - 1]
    for idx, item in enumerate(items, start=1):
        if int(item["id"]) == num_or_id:
            return idx, item
    return None


def update_homework(
    vk_id: int,
    homework_id: int,
    task_text: Optional[str] = None,
    target_date: Optional[str] = None,
    subject: Optional[str] = None,
    teacher: Optional[str] = None
) -> bool:
    """
    Вручную обновляет текст домашнего задания (task_text), дату сдачи (target_date),
    предмет (subject) и/или преподавателя (teacher) для конкретного задания пользователя.
    """
    updates = []
    params: List[Any] = []
    if task_text is not None and task_text.strip():
        updates.append("task_text = ?")
        params.append(task_text.strip())
    if target_date is not None and target_date.strip():
        updates.append("target_date = ?")
        params.append(target_date.strip())
    if subject is not None and subject.strip():
        updates.append("subject = ?")
        params.append(subject.strip())
    if teacher is not None and teacher.strip():
        updates.append("teacher = ?")
        params.append(teacher.strip())

    if not updates:
        return False

    params.extend([homework_id, vk_id])
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE homework SET {', '.join(updates)} WHERE id = ? AND vk_id = ?",
            tuple(params)
        )
        conn.commit()
        return cursor.rowcount > 0


def mark_homework_done(vk_id: int, homework_id: int) -> bool:
    """Отмечает индивидуальное домашнее задание пользователя выполненным."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE homework SET is_done = 1 
            WHERE id = ? AND vk_id = ? AND is_done = 0
        """, (homework_id, vk_id))
        conn.commit()
        return cursor.rowcount > 0


def delete_homework(vk_id: int, homework_id: int) -> bool:
    """Удаляет домашнее задание пользователя из базы данных."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM homework WHERE id = ? AND vk_id = ?", (homework_id, vk_id))
        conn.commit()
        return cursor.rowcount > 0


def clear_schedule(vk_id: int) -> int:
    """Очищает всё расписание пользователя."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM schedule WHERE vk_id = ?", (vk_id,))
        conn.commit()
        return cursor.rowcount

