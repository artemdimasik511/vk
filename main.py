"""
====================================================================
VK Student Assistant Bot (Расписание, Профили, Группы и ДЗ)
====================================================================
Модульный многопользовательский LongPoll-бот ВКонтакте:
- Личные профили студентов: "Фамилия Имя (Никнейм)", курс, номер группы и личный vk_id.
- Пошаговая регистрация: ФИ из ВК или ручной ввод -> курс и группа -> выбор расписания.
- Общая база расписаний по номеру группы (0 токенов) + индивидуальная загрузка по фото.
- Индивидуальный учет домашних заданий для каждого студента без путаницы.
- Счетчик выполненных / активных заданий и система ачивок за 10, 20, 30, 40 и 50 выполненных ДЗ.
- Локальное (офлайн) распознавание голосовых через Vosk + встроенный transcript ВК + Gemini API.
"""

import sys
import os
import re
import time
import json
import logging
import threading
import concurrent.futures
from logging.handlers import RotatingFileHandler
from datetime import date, timedelta
from typing import Optional, Dict, Any, List, Tuple

import requests

from config import (
    VK_TOKEN,
    VK_GROUP_ID,
    GEMINI_API_KEY,
    DB_PATH,
    GEMINI_PROXY,
    FREE_TRIAL_DAYS,
    SUBSCRIPTION_PRICE_RUB,
    SUBSCRIPTION_DAYS,
    REFERRER_BONUS_DAYS,
    INVITED_BONUS_DAYS,
)
import database
from week_helper import (
    get_week_type,
    format_day_name,
    normalize_target_date,
    WEEK_TYPE_ODD,
    WEEK_TYPE_EVEN,
    WEEK_ICONS,
    WEEK_NAMES
)
from keyboards import (
    get_main_keyboard,
    get_welcome_hello_keyboard,
    get_homework_inline_keyboard,
    get_registration_name_keyboard,
    get_schedule_choice_keyboard,
    get_profile_inline_keyboard,
    get_my_homework_actions_keyboard,
    get_homework_numbers_keyboard,
    get_homework_edit_field_keyboard,
    get_subscription_pay_keyboard,
)
from group_schedules import decode_isu_group_code, format_canonical_group_name
import gemini_service
import yookassa_service

# Безопасная ротация логов (максимум 5 файлов по 5 МБ, чтобы диск сервера не забивался)
file_handler = RotatingFileHandler(
    os.path.join(os.path.dirname(__file__), "bot.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        file_handler
    ]
)
logger = logging.getLogger("VKStudentBot")

# Попытка импорта vk_api
try:
    import vk_api
    from vk_api.longpoll import VkLongPoll, VkEventType
    from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
    from vk_api.utils import get_random_id
except ImportError:
    logger.error("Библиотека vk_api не установлена! Выполните: pip install -r requirements.txt")
    sys.exit(1)


def parse_course_and_group(raw_text: str) -> Tuple[str, str]:
    """
    Извлекает курс и номер группы из строки вида:
    '3 курс, 206321 ДБ' или '3 206321 ДБ' или '2 курс ИСиТ-24'.
    """
    cleaned = raw_text.strip()
    if "," in cleaned:
        parts = [p.strip() for p in cleaned.split(",", 1)]
        course_part, group_part = parts[0], parts[1]
        if course_part.isdigit():
            course_part = f"{course_part} курс"
        return course_part, group_part

    m = re.match(r"^(\d)\s*(?:курс|к\.?)?\s+(.+)$", cleaned, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1)} курс", m.group(2).strip()

    return "Не указан", cleaned


def extract_referrer_id(message: dict, text: str = "") -> Optional[int]:
    """
    Извлекает ID пригласившего студента из реферальной ссылки ВКонтакте (message['ref'])
    или из текста приветствия ('Начать ref_1001', '/start ref_1001', 'ref_1001').
    """
    candidates: List[str] = []
    if isinstance(message, dict):
        if message.get("ref"):
            candidates.append(str(message.get("ref")))
        if message.get("ref_source"):
            candidates.append(str(message.get("ref_source")))
        payload_raw = message.get("payload")
        if payload_raw:
            try:
                p = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
                if isinstance(p, dict) and p.get("ref"):
                    candidates.append(str(p.get("ref")))
            except Exception:
                pass
    if text:
        candidates.append(text)

    for raw in candidates:
        m = re.search(r"(?:ref[=_-]?|invite[=_-]?)(\d+)\b", raw, flags=re.IGNORECASE)
        if m:
            try:
                val = int(m.group(1))
                if val > 0:
                    return val
            except ValueError:
                continue
        if raw.strip().isdigit():
            try:
                val = int(raw.strip())
                if val > 0:
                    return val
            except ValueError:
                continue
    return None



class VKStudentBot:
    def __init__(self, token: str, group_id: Optional[str] = None):
        self.token = token
        self.group_id = int(group_id) if group_id and group_id.isdigit() else None
        
        logger.info("Инициализация соединения с VK API...")
        self.vk_session = vk_api.VkApi(token=self.token)
        self.vk = self.vk_session.get_api()
        
        # Автоматическое определение group_id, если не задан в .env
        if not self.group_id:
            try:
                group_info = self.vk.groups.getById()
                if group_info and len(group_info) > 0:
                    self.group_id = group_info[0]["id"]
                    logger.info(f"Определен ID сообщества: {self.group_id} ({group_info[0].get('name')})")
            except Exception as e:
                logger.warning(f"Не удалось автоматически определить group_id: {e}")
                
        # Инициализируем базу данных (включая таблицы профилей и общих расписаний групп)
        database.init_db()
        logger.info(f"База данных SQLite инициализирована: {DB_PATH}")

        # Пул потоков: держит параллельные запросы (Vosk, Gemini, скачивание фото/аудио) без блокировки LongPoll
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)

        if GEMINI_PROXY:
            proxy_display = GEMINI_PROXY.split('@')[-1] if '@' in GEMINI_PROXY else GEMINI_PROXY
            logger.info(f"🛡️ Прокси для защиты от блокировок активирован: {proxy_display}")

    def send_message(
        self,
        peer_id: int,
        text: str,
        keyboard: Optional[str] = None,
        reply_to: Optional[int] = None
    ) -> None:
        """Безопасная отправка сообщения пользователю с обработкой ошибок API."""
        try:
            params = {
                "peer_id": peer_id,
                "message": text,
                "random_id": get_random_id(),
            }
            if keyboard:
                params["keyboard"] = keyboard
            if reply_to:
                params["reply_to"] = reply_to
                
            self.vk.messages.send(**params)
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения в диалог {peer_id}: {e}")

    def fetch_vk_user_name(self, user_id: int) -> Tuple[str, str]:
        """Получает Фамилию и Имя пользователя со страницы ВКонтакте."""
        try:
            users = self.vk.users.get(user_ids=[user_id])
            if users and len(users) > 0:
                u = users[0]
                first_name = (u.get("first_name") or "Имя").strip()
                last_name = (u.get("last_name") or "Фамилия").strip()
                return last_name, first_name
        except Exception as e:
            logger.warning(f"Не удалось получить имя пользователя {user_id} из VK API: {e}")
        return "Студент", f"ВК-{user_id}"

    # =========================================================================
    # Пошаговая регистрация и управление личным профилем
    # =========================================================================

    def get_user_referral_link(self, user_id: int) -> str:
        """Формирует персональную реферальную ссылку студента для ВКонтакте."""
        if self.group_id:
            return f"https://vk.me/club{self.group_id}?ref=ref_{user_id}"
        return f"https://vk.me/student_assistant_bot?ref=ref_{user_id}"

    def show_initial_hello_prompt(self, user_id: int, referrer_id: Optional[int] = None) -> None:
        """
        Когда человек впервые заходит в чат с ботом, показывает приветствие от маскота Тёмы (Байкальской нерпы)
        и горячую клавишу «👋 Привет» внизу экрана.
        Если переход совершен впервые по реферальной ссылке друга — фиксирует пригласившего в БД.
        """
        is_bound, bound_ref = database.register_new_user_with_referral(
            user_id,
            referrer_vk_id=referrer_id,
            reg_step="WAIT_HELLO"
        )
        ref_note = ""
        if is_bound and bound_ref:
            ref_note = (
                f"\n🎁 Ты зашёл по приглашению одногруппника (ID #{bound_ref})!\n"
                f"Сразу после того, как ты выберешь свои Фамилию/Имя (Ник) и учебную группу, "
                f"тебе начислятся +{INVITED_BONUS_DAYS} бонусных дня доступа, а твоему другу — +{REFERRER_BONUS_DAYS} дней!\n"
            )
        hello_text = (
            "🦭 Привет! Меня зовут Тёма, я Байкальская нерпа — маскот вашей группы и твой умный помощник по учебе!\n"
            f"{ref_note}\n"
            "Нажми на горячую клавишу «👋 Привет» внизу экрана, чтобы познакомиться и подключить расписание твоей группы 👇"
        )
        self.send_message(user_id, hello_text, keyboard=get_welcome_hello_keyboard())

    def start_registration(self, user_id: int, referrer_id: Optional[int] = None) -> None:
        """Запускает шаг 1 регистрации после нажатия клавиши «👋 Привет»."""
        is_bound, bound_ref = database.register_new_user_with_referral(
            user_id,
            referrer_vk_id=referrer_id,
            reg_step="WAIT_NAME_METHOD"
        )
        database.upsert_user_profile(user_id, reg_step="WAIT_NAME_METHOD")
        ref_note = ""
        if is_bound and bound_ref:
            ref_note = (
                f"\n🎁 Приглашение от друга #{bound_ref} активировано! Заполни ФИО (Ник) и группу, "
                f"чтобы получить +{INVITED_BONUS_DAYS} бонусных дня (а друг получит +{REFERRER_BONUS_DAYS} дней)!\n"
            )
        welcome_text = (
            "🦭 Привет-привет! Рад знакомству! Я Тёма — Байкальская нерпа, маскот вашей группы!\n\n"
            "Чтобы я закреплял расписание и твои личные домашние задания строго за тобой (и никогда не путал ребят из группы), "
            "давай создадим твой профиль.\n"
            f"Твой идентификатор будет отображаться как: «Фамилия Имя (Никнейм)».\n{ref_note}\n"
            "👇 Шаг 1 из 3: Выбери способ ввода Фамилии и Имени:"
        )
        self.send_message(user_id, welcome_text, keyboard=get_registration_name_keyboard())

    def handle_reg_vk_name(self, user_id: int) -> None:
        """Берет Фамилию и Имя со страницы ВК и запрашивает только Никнейм."""
        last_name, first_name = self.fetch_vk_user_name(user_id)
        database.upsert_user_profile(
            user_id,
            last_name=last_name,
            first_name=first_name,
            reg_step="WAIT_NICKNAME_ONLY"
        )
        self.send_message(
            user_id,
            f"🦭 Отлично, я взял твои имя и фамилию со страницы ВК: {last_name} {first_name}\n\n"
            "✍️ Теперь напиши свой желаемый Никнейм (одним словом, например: Leader или nerpa_01):"
        )

    def handle_reg_manual_name(self, user_id: int) -> None:
        """Запрашивает ручной ввод Фамилии, Имени и Никнейма."""
        database.upsert_user_profile(user_id, reg_step="WAIT_MANUAL_FULLNAME")
        self.send_message(
            user_id,
            "✍️ Введи свои Фамилию, Имя и желаемый Никнейм через пробел.\n"
            "Пример: Иванов Артём Shadow"
        )

    def handle_choose_schedule_mode(self, user_id: int) -> None:
        """Предлагает 2 варианта загрузки расписания: из общей базы по группе или своим фото."""
        profile = database.get_user_profile(user_id) or {}
        group_name = profile.get("group_name") or "не указана"
        avail_groups = ", ".join(database.get_available_group_names()) or "206321 ДБ"
        database.upsert_user_profile(user_id, reg_step="WAIT_SCHEDULE_SOURCE")
        self.send_message(
            user_id,
            f"🦭 Выбери способ загрузки расписания (твоя группа: {group_name}):\n\n"
            f"1️⃣ «🏫 Взять расписание с общей базы» — мгновенная загрузка готового расписания твоей группы (в базе: {avail_groups}).\n"
            "2️⃣ «📸 Своё расписание (скинуть фото)» — отправь фотографию расписания, и я закреплю его за тобой и сохранлю в общую базу для твоей группы!",
            keyboard=get_schedule_choice_keyboard()
        )

    def handle_schedule_from_group_choice(self, user_id: int) -> None:
        """Проверяет группу пользователя в общей базе или просит ввести номер группы."""
        profile = database.get_user_profile(user_id) or {}
        user_group = (profile.get("group_name") or "").strip()

        if user_group:
            applied = database.apply_group_schedule_to_user(user_id, user_group)
            if applied:
                canonical_group, count = applied
                database.upsert_user_profile(user_id, group_name=canonical_group, reg_step="REGISTERED")
                identity = database.format_user_identity(database.get_user_profile(user_id), user_id)
                self.send_message(
                    user_id,
                    f"🎉 Ура! Регистрация завершена, {identity}!\n\n"
                    f"🦭 Тёма подключил тебе готовое расписание группы «{canonical_group}» из общей базы (пар: {count})!\n"
                    "Теперь ты можешь смотреть расписание, диктовать или писать свои индивидуальные ДЗ и копить ачивки в разделе «👤 Мой профиль»!",
                    keyboard=get_main_keyboard()
                )
                return

        avail = ", ".join(database.get_available_group_names())
        database.upsert_user_profile(user_id, reg_step="WAIT_GROUP_CODE_FOR_SCHED")
        self.send_message(
            user_id,
            f"🏫 Введи номер своей учебной группы для загрузки расписания из общей базы.\n"
            f"Сейчас в базе доступны группы: {avail}\n\n"
            "Либо просто отправь фотографию своего расписания 📸 — я распознаю его и сохраню в общую базу для твоей группы!",
            keyboard=get_schedule_choice_keyboard()
        )

    def handle_registration_text_input(self, user_id: int, text: str, reg_step: str) -> bool:
        """
        Обрабатывает текстовый ответ пользователя, если он находится в процессе регистрации.
        Возвращает True, если сообщение было обработано шагом регистрации.
        """
        if reg_step == "WAIT_HELLO":
            self.start_registration(user_id)
            return True

        if reg_step == "WAIT_NAME_METHOD":
            # Если вместо кнопки пользователь сразу написал Фамилию Имя Никнейм
            parts = text.split()
            if len(parts) >= 2:
                last_name = parts[0]
                first_name = parts[1]
                nickname = " ".join(parts[2:]) if len(parts) >= 3 else first_name
                profile = database.upsert_user_profile(
                    user_id,
                    last_name=last_name,
                    first_name=first_name,
                    nickname=nickname,
                    reg_step="WAIT_COURSE_GROUP"
                )
                identity = database.format_user_identity(profile, user_id)
                self.send_message(
                    user_id,
                    f"✅ Твой идентификатор: {identity}\n\n"
                    "🏫 Шаг 2 из 3: Введи номер твоей учебной группы (например: 206321 ДБ или 206321дб).\n"
                    "💡 Курс отдельно писать не нужно — он уже зашит внутри номера группы!"
                )
                return True
            self.start_registration(user_id)
            return True

        elif reg_step == "WAIT_NICKNAME_ONLY":
            nickname = text.strip(" ()@")
            profile = database.upsert_user_profile(
                user_id,
                nickname=nickname,
                reg_step="WAIT_COURSE_GROUP"
            )
            identity = database.format_user_identity(profile, user_id)
            self.send_message(
                user_id,
                f"✅ Отлично! Твой идентификатор: {identity}\n\n"
                "🏫 Шаг 2 из 3: Введи номер твоей учебной группы (например: 206321 ДБ или 206321дб).\n"
                "💡 Курс отдельно писать не нужно — я сам расшифрую курс, профиль и форму обучения из номера группы!"
            )
            return True

        elif reg_step == "WAIT_MANUAL_FULLNAME":
            parts = text.split()
            if len(parts) < 2:
                self.send_message(
                    user_id,
                    "⚠️ Пожалуйста, введи минимум Фамилию и Имя (и через пробел Никнейм).\n"
                    "Пример: Иванов Артём Shadow"
                )
                return True
            last_name = parts[0]
            first_name = parts[1]
            nickname = " ".join(parts[2:]) if len(parts) >= 3 else first_name
            profile = database.upsert_user_profile(
                user_id,
                last_name=last_name,
                first_name=first_name,
                nickname=nickname,
                reg_step="WAIT_COURSE_GROUP"
            )
            identity = database.format_user_identity(profile, user_id)
            self.send_message(
                user_id,
                f"✅ Отлично! Твой идентификатор: {identity}\n\n"
                "🏫 Шаг 2 из 3: Введи номер твоей учебной группы (например: 206321 ДБ или 206321дб).\n"
                "💡 Курс отдельно писать не нужно — я сам расшифрую курс, профиль и форму обучения из номера группы!"
            )
            return True

        elif reg_step == "WAIT_COURSE_GROUP":
            decoded = decode_isu_group_code(text)
            canonical_group = decoded["canonical_group"] or format_canonical_group_name(text)
            profile = database.upsert_user_profile(
                user_id,
                course=decoded.get("course", ""),
                group_name=canonical_group,
                reg_step="WAIT_SCHEDULE_SOURCE"
            )
            identity = database.format_user_identity(profile, user_id)
            avail = ", ".join(database.get_available_group_names())
            cipher_block = f"\n{decoded['full_explanation']}\n" if decoded.get("is_decoded") else ""

            # ПРАВИЛО 2 и 3: Целевое действие выполнено (выбраны ФИ/ник и учебная группа)!
            # Проверяем и начисляем +7 дней пригласившему и +3 дня приглашенному другу:
            ref_reward = database.complete_referral_if_eligible(user_id)
            invited_bonus_block = ""
            if ref_reward:
                inv_sub = ref_reward["invited_subscription"]
                ref_sub = ref_reward["referrer_subscription"]
                referrer_id = ref_reward["referrer_id"]
                invited_bonus_block = (
                    f"\n🎁 ДВОЙНОЙ РЕФЕРАЛЬНЫЙ БОНУС АКТИВИРОВАН!\n"
                    f"Тебе начислено +{ref_reward['invited_bonus_days']} бонусных дня к бесплатному периоду "
                    f"(доступ активен до {inv_sub['expires_at']}, всего {inv_sub['days_left']} дн.)!\n"
                )
                # Отправляем радостное уведомление пригласившему одногруппнику:
                self.send_message(
                    referrer_id,
                    f"🎉 По твоей ссылке зарегистрировался одногруппник ({identity}, гр. {canonical_group}), "
                    f"зачислили +{ref_reward['referrer_bonus_days']} дней про-доступа!\n\n"
                    f"💎 Твой доступ теперь активен до: {ref_sub['expires_at']} (осталось {ref_sub['days_left']} дн.).\n"
                    "Продолжай делиться реферальной ссылкой с ребятами из группы и пользуйся ботом бесплатно! 🦭",
                    keyboard=get_main_keyboard()
                )

            self.send_message(
                user_id,
                f"✅ Группа сохранена!\n"
                f"👤 Профиль: {identity}\n"
                f"🏫 Группа: {canonical_group}\n"
                f"{cipher_block}"
                f"{invited_bonus_block}\n"
                f"📚 Шаг 3 из 3: Как загрузить твоё расписание?\n"
                f"• Нажми «🏫 Взять расписание с общей базы» (готовые группы в базе: {avail})\n"
                f"• Или нажми «📸 Своё расписание (скинуть фото)» — я распознаю фото и сохраню расписание в общую базу для группы «{canonical_group}»!",
                keyboard=get_schedule_choice_keyboard()
            )
            return True

        elif reg_step in ("WAIT_SCHEDULE_SOURCE", "WAIT_GROUP_CODE_FOR_SCHED"):
            applied = database.apply_group_schedule_to_user(user_id, text)
            if applied:
                canonical_group, count = applied
                database.upsert_user_profile(user_id, group_name=canonical_group, reg_step="REGISTERED")
                identity = database.format_user_identity(database.get_user_profile(user_id), user_id)
                self.send_message(
                    user_id,
                    f"🎉 Готово, {identity}!\n"
                    f"🦭 Тёма подключил расписание группы «{canonical_group}» из общей базы (пар: {count})!\n\n"
                    "Воспользуйся кнопками быстрого доступа внизу экрана 👇",
                    keyboard=get_main_keyboard()
                )
                return True
            else:
                canonical_group = format_canonical_group_name(text)
                if canonical_group:
                    database.upsert_user_profile(user_id, group_name=canonical_group)
                avail = ", ".join(database.get_available_group_names())
                self.send_message(
                    user_id,
                    f"⚠️ Группа «{canonical_group or text}» пока не найдена в готовой базе (сейчас в базе: {avail}).\n\n"
                    "Ты можешь:\n"
                    f"• Ввести один из готовых номеров групп ({avail})\n"
                    f"• Или просто отправить фотографию своего расписания 📸 — я распознаю её и закреплю в общей базе за группой «{canonical_group or text}», чтобы остальные ребята смогли взять её готовой!",
                    keyboard=get_schedule_choice_keyboard()
                )
                return True

        return False

    def handle_referral_info(self, user_id: int) -> None:
        """
        Показывает персональную реферальную ссылку студента, правила защиты экономики и статистику приглашенных.
        """
        ref_stats = database.get_referral_stats(user_id)
        sub_status = database.get_subscription_status(user_id)
        ref_link = self.get_user_referral_link(user_id)
        ref_code = ref_stats["referral_code"]

        msg = (
            "🤝 РЕФЕРАЛЬНАЯ ПРОГРАММА («ПРИГЛАСИ ОДНОГРУППНИКА»)\n"
            f"{'—' * 28}\n"
            f"🎁 Двойной бонус за каждого друга из группы:\n"
            f"• Тебе: +{REFERRER_BONUS_DAYS} дней бесплатного про-доступа\n"
            f"• Твоему другу: +{INVITED_BONUS_DAYS} бонусных дня к стартовому месяцу (33 дня вместо 30)!\n\n"
            f"🔗 Твоя персональная реферальная ссылка:\n"
            f"{ref_link}\n\n"
            f"💬 Или друг может просто отправить боту при первом входе:\n"
            f"Начать {ref_code}\n\n"
            "🛡️ Как начисляется бонус (3 честных правила):\n"
            "1️⃣ Только новые пользователи: друг еще ни разу не должен был запускать нашего бота (и нельзя пригласить самого себя).\n"
            "2️⃣ Награда за целевое действие, а не за клик: дни начисляются сразу после того, как друг выберет своё ФИ (ник) и учебную группу в меню.\n"
            "3️⃣ Умное суммирование времени: если у тебя активен триал или подписка — +7 дней прибавятся к дате окончания. Если доступ уже закончился — он сразу активируется на 7 дней!\n\n"
            "📊 ТВОЯ РЕФЕРАЛЬНАЯ СТАТИСТИКА:\n"
            f"👥 Приглашено одногруппников: {ref_stats['referrals_count']}\n"
            f"⏳ Заработано бонусных дней: +{ref_stats['bonus_days_earned']} дн.\n"
            f"💎 Текущий доступ: {sub_status['label']}"
        )
        self.send_message(user_id, msg, keyboard=get_main_keyboard())

    def handle_my_profile(self, user_id: int) -> None:
        """
        Отображает личный профиль студента («👤 Мой профиль»):
        - Идентификатор: Фамилия Имя (Никнейм) и личный ID
        - Номер группы и автоматическая расшифровка шифра группы ПИ ИГУ (без отдельной плашки курса)
        - Счетчик выполненных домашних заданий и оставшихся активных заданий
        - Список открытых ачивок за 10, 20, 30, 40 и 50 выполненных заданий
        - Реферальная ссылка и статистика приглашенных друзей (+7 дней тебе / +3 дня другу)
        """
        profile = database.get_user_profile(user_id)
        if not profile or (not profile.get("last_name") and not profile.get("first_name")):
            self.start_registration(user_id)
            return

        identity = database.format_user_identity(profile, user_id)
        group_name = profile.get("group_name") or "Не указана"
        decoded = decode_isu_group_code(group_name)
        stats = database.get_user_homework_stats(user_id)
        ref_stats = database.get_referral_stats(user_id)
        ref_link = self.get_user_referral_link(user_id)
        schedule_count = len(database.get_full_schedule(user_id))

        done_cnt = stats["done_count"]
        pending_cnt = stats["pending_count"]
        unlocked = stats["achievements"]
        next_target = stats["next_milestone"]

        if unlocked:
            ach_lines = "\n".join(f"   • {a}" for a in unlocked)
        else:
            ach_lines = "   Пока нет открытых ачивок (первая откроется на 10 выполненных ДЗ!)"

        progress_line = (
            f"🎯 До следующей ачивки: {done_cnt} / {next_target} выполненных ДЗ"
            if next_target
            else "👑 Ты собрал ВСЕ максимальные ачивки (50+ выполненных заданий)!"
        )

        cipher_line = f"🔍 Расшифровка группы: {decoded['short_info']}\n" if decoded.get("is_decoded") else ""
        sub_status = database.get_subscription_status(user_id)

        profile_text = (
            "🦭 МОЙ ПРОФИЛЬ (Маскот Тёма — Байкальская нерпа)\n"
            f"{'—' * 28}\n"
            f"🆔 Идентификатор: {identity}\n"
            f"🔢 Личный ID: #{user_id}\n"
            f"🏫 Группа: {group_name}\n"
            f"{cipher_line}"
            f"📚 Загружено пар в расписании: {schedule_count}\n"
            f"💳 Доступ: {sub_status['label']}\n\n"
            "🤝 РЕФЕРАЛЬНАЯ ПРОГРАММА (+7 ДНЕЙ ЗА ДРУГА):\n"
            f"👥 Приглашено друзей: {ref_stats['referrals_count']} (+{ref_stats['bonus_days_earned']} дн. бонуса)\n"
            f"🔗 Твоя ссылка: {ref_link}\n"
            f"💬 Код для друга: Начать {ref_stats['referral_code']}\n\n"
            "📊 СТАТИСТИКА ДОМАШНИХ ЗАДАНИЙ:\n"
            f"✅ Выполнено заданий: {done_cnt}\n"
            f"⏳ Осталось выполнить: {pending_cnt}\n\n"
            "🏆 МОИ АЧИВКИ (10 / 20 / 30 / 40 / 50 ДЗ):\n"
            f"{ach_lines}\n\n"
            f"{progress_line}"
        )

        self.send_message(user_id, profile_text, keyboard=get_profile_inline_keyboard())


    # =========================================================================
    # Монетизация ЮKassa (1 месяц бесплатно, далее 30 рублей на 2 недели / 14 дней)
    # =========================================================================

    def handle_subscription_expired_prompt(self, user_id: int, is_proactive_notification: bool = False) -> None:
        """
        Отправляет уведомление о том, что бесплатный пробный месяц (или подписка) закончился,
        и генерирует ссылку на оплату 30 рублей на следующие 2 недели через ЮKassa.
        """
        profile = database.get_user_profile(user_id)
        identity = database.format_user_identity(profile, user_id)

        pending = database.get_latest_pending_payment(user_id)
        payment_id = ""
        confirmation_url = ""

        if pending and pending.get("confirmation_url"):
            payment_id = pending["payment_id"]
            confirmation_url = pending["confirmation_url"]
        else:
            pay_res = yookassa_service.create_yookassa_payment(
                vk_id=user_id,
                amount_rub=SUBSCRIPTION_PRICE_RUB,
                days=SUBSCRIPTION_DAYS,
                user_identity=identity
            )
            if pay_res.get("success"):
                payment_id = pay_res["payment_id"]
                confirmation_url = pay_res.get("confirmation_url", "")
                database.save_pending_payment(
                    vk_id=user_id,
                    payment_id=payment_id,
                    amount=SUBSCRIPTION_PRICE_RUB,
                    days=SUBSCRIPTION_DAYS,
                    confirmation_url=confirmation_url
                )

        database.mark_user_expiry_notified(user_id)

        link_block = (
            f"\n🔗 Ссылка для оплаты через ЮKassa ({SUBSCRIPTION_PRICE_RUB} ₽):\n{confirmation_url}\n"
            if confirmation_url
            else "\n👇 Нажми кнопку «💳 Оплатить 30 ₽ (на 2 недели)» ниже для получения ссылки.\n"
        )

        msg = (
            f"🦭 Привет, {identity}!\n\n"
            f"⏰ Твой бесплатный пробный период ({FREE_TRIAL_DAYS} дней) подошёл к концу.\n"
            f"Чтобы продолжить пользоваться всеми функциями бота Тёмы (расписание группы, голосовая запись ДЗ, управление заданиями и ачивки), "
            f"необходимо оплатить подписку:\n\n"
            f"💳 Тариф: {SUBSCRIPTION_PRICE_RUB} рублей на следующие 2 недели ({SUBSCRIPTION_DAYS} дней)."
            f"{link_block}\n"
            "✅ После оплаты нажми на кнопку «🔄 Проверить оплату» внизу экрана — доступ откроется автоматически!"
        )
        self.send_message(
            user_id,
            msg,
            keyboard=get_subscription_pay_keyboard(confirmation_url=confirmation_url, payment_id=payment_id)
        )

    def handle_buy_subscription(self, user_id: int) -> None:
        """Создает новый платеж ЮKassa на 30 рублей (на 2 недели / 14 дней) и отправляет ссылку пользователю."""
        profile = database.get_user_profile(user_id)
        identity = database.format_user_identity(profile, user_id)
        sub_status = database.get_subscription_status(user_id)

        pay_res = yookassa_service.create_yookassa_payment(
            vk_id=user_id,
            amount_rub=SUBSCRIPTION_PRICE_RUB,
            days=SUBSCRIPTION_DAYS,
            user_identity=identity
        )

        if pay_res.get("success"):
            payment_id = pay_res["payment_id"]
            confirmation_url = pay_res.get("confirmation_url", "")
            database.save_pending_payment(
                vk_id=user_id,
                payment_id=payment_id,
                amount=SUBSCRIPTION_PRICE_RUB,
                days=SUBSCRIPTION_DAYS,
                confirmation_url=confirmation_url
            )
            msg = (
                f"💳 Оформление подписки на бота Тёму ({identity})\n"
                f"{'—' * 28}\n"
                f"📌 Текущий статус: {sub_status['label']}\n"
                f"💰 К оплате: {SUBSCRIPTION_PRICE_RUB} рублей\n"
                f"📅 Срок продления: +2 недели ({SUBSCRIPTION_DAYS} дней)\n\n"
                f"🔗 Перейди по ссылке ЮKassa для оплаты:\n{confirmation_url}\n\n"
                "После совершения платежа нажми кнопку «🔄 Проверить оплату» внизу 👇"
            )
            self.send_message(
                user_id,
                msg,
                keyboard=get_subscription_pay_keyboard(confirmation_url=confirmation_url, payment_id=payment_id)
            )
        else:
            err_text = pay_res.get("error", "Неизвестная ошибка платежного шлюза")
            self.send_message(
                user_id,
                f"⚠️ Не удалось сформировать ссылку ЮKassa: {err_text}\nПопробуйте нажать «💳 Оплатить 30 ₽ (на 2 недели)» через пару секунд.",
                keyboard=get_subscription_pay_keyboard()
            )

    def handle_check_payment(self, user_id: int, payment_id: Optional[str] = None) -> None:
        """Проверяет статус платежа в ЮKassa и при успешной оплате активирует подписку на 14 дней."""
        target_payment_id = payment_id
        pending = database.get_latest_pending_payment(user_id)
        if not target_payment_id and pending:
            target_payment_id = pending.get("payment_id")

        if not target_payment_id:
            self.send_message(
                user_id,
                "ℹ️ У тебя пока нет созданного счёта на оплату. Сейчас сформирую новую ссылку на 30 ₽ (на 2 недели)!",
            )
            self.handle_buy_subscription(user_id)
            return

        check_res = yookassa_service.check_yookassa_payment(target_payment_id)
        if check_res.get("paid") or check_res.get("status") == "succeeded":
            new_sub = database.activate_paid_subscription(
                vk_id=user_id,
                days=SUBSCRIPTION_DAYS,
                payment_id=target_payment_id
            )
            identity = database.format_user_identity(database.get_user_profile(user_id), user_id)
            self.send_message(
                user_id,
                f"🎉 Оплата {SUBSCRIPTION_PRICE_RUB} ₽ успешно подтверждена! Спасибо, {identity}!\n\n"
                f"🦭 Тёма продлил твою подписку на 2 недели ({SUBSCRIPTION_DAYS} дней)!\n"
                f"💎 Доступ активен до: {new_sub['expires_at']} (осталось {new_sub['days_left']} дн.).\n\n"
                "Все функции расписания и домашних заданий снова полностью доступны 👇",
                keyboard=get_main_keyboard()
            )
        elif check_res.get("status") == "canceled":
            database.mark_payment_status(target_payment_id, "canceled")
            self.send_message(
                user_id,
                "⚠️ Этот платёж был отменён. Нажми «💳 Оплатить 30 ₽ (на 2 недели)», чтобы получить новую ссылку ЮKassa.",
                keyboard=get_subscription_pay_keyboard()
            )
        else:
            conf_url = check_res.get("confirmation_url") or (pending.get("confirmation_url") if pending else "")
            self.send_message(
                user_id,
                f"⏳ Платёж еще ожидает подтверждения от ЮKassa (статус: {check_res.get('status', 'pending')}).\n\n"
                f"Если ты еще не оплатил — перейди по ссылке:\n{conf_url}\n\n"
                "Как только оплата пройдёт, нажми «🔄 Проверить оплату» ещё раз!",
                keyboard=get_subscription_pay_keyboard(confirmation_url=conf_url, payment_id=target_payment_id)
            )

    def _subscription_monitor_loop(self) -> None:
        """
        Фоновый поток:
        1) Автоматически проверяет ожидающие платежи в ЮKassa и сразу активирует подписку при оплате.
        2) Находит студентов, у которых истёк бесплатный месяц (30 дней) или подписка, и отправляет им уведомление.
        """
        while True:
            try:
                # 1. Проверяем ожидающие платежи
                for p in database.get_all_pending_payments():
                    p_id = p.get("payment_id")
                    u_id = int(p.get("vk_id", 0))
                    if not p_id or not u_id:
                        continue
                    res = yookassa_service.check_yookassa_payment(p_id)
                    if res.get("paid") or res.get("status") == "succeeded":
                        new_sub = database.activate_paid_subscription(
                            vk_id=u_id,
                            days=int(p.get("days") or SUBSCRIPTION_DAYS),
                            payment_id=p_id
                        )
                        identity = database.format_user_identity(database.get_user_profile(u_id), u_id)
                        self.send_message(
                            u_id,
                            f"🎉 Оплата {p.get('amount', SUBSCRIPTION_PRICE_RUB)} ₽ через ЮKassa получена автоматически!\n"
                            f"🦭 {identity}, твоя подписка продлена на 2 недели (до {new_sub['expires_at']})!\n"
                            "Приятной учёбы!",
                            keyboard=get_main_keyboard()
                        )
                    elif res.get("status") == "canceled":
                        database.mark_payment_status(p_id, "canceled")

                # 2. Проверяем пользователей, у которых только что закончился бесплатный месяц или подписка
                for exp_user in database.get_newly_expired_users():
                    u_id = int(exp_user["vk_id"])
                    self.handle_subscription_expired_prompt(u_id, is_proactive_notification=True)

            except Exception as e:
                logger.warning(f"Ошибка в фоновом мониторинге подписок ЮKassa: {e}")

            time.sleep(30)

    # =========================================================================
    # Обработчики расписания и индивидуальных домашних заданий
    # =========================================================================

    def handle_command_help(self, user_id: int) -> None:
        """Вывод справки по командам бота."""
        today = date.today()
        week_type = get_week_type(today)
        week_name = WEEK_NAMES.get(week_type, week_type)
        profile = database.get_user_profile(user_id)
        identity = database.format_user_identity(profile, user_id)
        
        help_text = (
            f"🦭 Привет, {identity}! Меня зовут Тёма, я Байкальская нерпа — маскот вашей группы!\n\n"
            f"📅 Сегодня: {today.strftime('%d.%m.%Y')} ({format_day_name(today.weekday())})\n"
            f"⚡ Текущая неделя: {week_name}\n\n"
            "🔹 Что я умею:\n"
            "1. 👤 «Мой профиль» — твой идентификатор Фамилия Имя (Никнейм), номер группы с расшифровкой шифра ПИ ИГУ, реферальная ссылка (+7 дн.), счетчик ДЗ и ачивки за 10, 20, 30, 40 и 50 заданий!\n"
            "2. 🏫 «Выбрать группу / Фото» — возьми готовое расписание группы из общей базы или скинь фото своего расписания (я сохраню его в базу для всей твоей группы!).\n"
            "3. ✍️ Запись ДЗ текстом или голосом 🎙️ — напиши слово «ДЗ», преподавателя, предмет и само задание (понимаю сразу несколько заданий с разными преподавателями в одном сообщении!).\n"
            "   Пример: «ДЗ Костин Учебная практика: собрать аккумулятор на эту неделю. ДЗ Кибальник Методики: через занятия собрать шесть тапочек.»\n"
            "4. 📌 «Мои задания» — кнопка «✍️ Записать ДЗ текстом», полное редактирование ДЗ (дата сдачи, текст задания, предмет, преподаватель) и закрытие заданий по номерам (1, 2, 3...).\n\n"
            "Команды:\n"
            "• ДЗ <Преподаватель> <Предмет>: <Задание> — записать одно или несколько ДЗ текстом\n"
            "• /dz — подсказка по текстовой записи ДЗ\n"
            "• /profile — открыть мой профиль и реферальную ссылку\n"
            "• /done <номер> — отметить задание выполненным\n"
            "• /clear — очистить текущее расписание\n\n"
            "🎁 БОНУС: За нахождение 5 багов выдаются БЕСПЛАТНО 2 недели подписки!\n"
            "📩 Писать сюда: https://vk.ru/moring_mod12 (Администратор)"
        )
        self.send_message(user_id, help_text, keyboard=get_main_keyboard())

    def handle_schedule_for_day(self, user_id: int, target_date: date, day_label: str) -> None:
        """Вывод расписания студента на конкретную дату с учетом четности недели."""
        dow = target_date.weekday()
        week_type = get_week_type(target_date)
        week_str = "нечетная ▲" if week_type == "ODD" else "четная ■"
        profile = database.get_user_profile(user_id) or {}
        group_str = f" (гр. {profile['group_name']})" if profile.get("group_name") else ""
        
        lessons = database.get_schedule_for_day(user_id, dow, current_week_type=week_type)
        
        header = (
            f"📅 Расписание на {day_label}{group_str} ({target_date.strftime('%d.%m')}, {format_day_name(dow)}):\n"
            f"Неделя: {week_str}\n"
            f"{'—' * 28}\n"
        )
        
        if not lessons:
            all_lessons = database.get_full_schedule(user_id)
            if not all_lessons:
                text = (
                    f"{header}\n"
                    "⚠️ У вас еще не подключено расписание!\n"
                    "Нажмите «🏫 Выбрать группу / Фото», чтобы взять расписание вашей группы из общей базы или загрузить своё фото."
                )
                self.send_message(user_id, text, keyboard=get_schedule_choice_keyboard())
                return
            else:
                text = f"{header}\n🎉 В этот день занятий нет! Можно отдыхать."
            self.send_message(user_id, text, keyboard=get_main_keyboard())
            return

        lines = []
        for i, l in enumerate(lessons, 1):
            l_type = "пр" if l["lesson_type"] == "PRACTICE" else "л"
            room_str = f" [ауд. {l['room']}]" if l.get("room") else ""
            teacher_str = f"\n   👨‍🏫 {l['teacher']}" if l.get("teacher") else ""
            
            lines.append(
                f"{i}. ⏰ {l['time']} — {l['subject']} ({l_type}){room_str}{teacher_str}"
            )
            
        full_text = header + "\n\n".join(lines)
        self.send_message(user_id, full_text, keyboard=get_main_keyboard())

    def handle_hw_prompt_add_text(self, user_id: int) -> None:
        """
        Команда и кнопка «✍️ Записать ДЗ текстом»:
        предлагает студенту написать в чат одно или сразу несколько домашних заданий
        в формате: ДЗ <Преподаватель> <Предмет> <Само задание>.
        """
        database.upsert_user_profile(user_id, reg_step="HW_ADD_TEXT")
        prompt_text = (
            "✍️ ЗАПИСЬ ДОМАШНЕГО ЗАДАНИЯ ТЕКСТОМ\n"
            f"{'—' * 28}\n"
            "Напиши слово «ДЗ», затем преподавателя, предмет и само домашнее задание.\n"
            "💡 В одном сообщении можно написать сразу несколько заданий разным преподавателям!\n\n"
            "Примеры:\n"
            "• ДЗ Костин Учебная практика: собрать аккумулятор на эту неделю\n"
            "• ДЗ Костин Учебная практика: собрать аккумулятор на эту неделю. ДЗ Кибальник Методики и технологии: через занятия нужно собрать шесть тапочек.\n"
            "• ДЗ Матафонова Психолого-педагогические технологии подготовить доклад к следующей паре\n\n"
            "👇 Отправь текст с заданием прямо в этот чат:"
        )
        self.send_message(user_id, prompt_text, keyboard=get_my_homework_actions_keyboard())

    def handle_my_homework(self, user_id: int) -> None:
        """
        Вывод индивидуального списка активных домашних заданий студента
        с порядковыми номерами (1, 2, 3...) и большими клавишами:
        - «✍️ Записать ДЗ текстом»
        - «✏️ Редактировать задание»
        - «✅ Отметить задания выполненными»
        """
        database.upsert_user_profile(user_id, reg_step="REGISTERED")
        profile = database.get_user_profile(user_id)
        identity = database.format_user_identity(profile, user_id)
        stats = database.get_user_homework_stats(user_id)
        items = database.get_active_homework(user_id)

        if not items:
            self.send_message(
                user_id,
                f"🎉 {identity}, у вас нет активных домашних заданий! Вы всё сделали.\n"
                f"✅ Всего выполнено заданий: {stats['done_count']}\n\n"
                "💡 Чтобы добавить новое задание текстом, нажмите «✍️ Записать ДЗ текстом» или напишите:\n"
                "«ДЗ Костин Учебная практика: собрать аккумулятор на эту неделю»",
                keyboard=get_my_homework_actions_keyboard()
            )
            return

        lines = [
            f"📌 Индивидуальные задания — {identity}:\n"
            f"(Активных: {stats['pending_count']} | Выполнено: {stats['done_count']})\n"
        ]
        for idx, hw in enumerate(items, start=1):
            t_date = hw['target_date']
            date_label = "⏳ Срок: Без срока (открытая дата)" if t_date == "Без срока" else f"📅 Сдать к: {t_date}"
            lines.append(
                f"🔹 №{idx}. {hw['subject']} (ID #{hw['id']})\n"
                f"   {date_label}\n"
                f"   👨‍🏫 Преподаватель: {hw['teacher']}\n"
                f"   📝 Задание: {hw['task_text']}\n"
            )

        lines.append(
            "👇 Выберите действие на клавиатуре внизу:\n"
            "• Нажмите «✍️ Записать ДЗ текстом», чтобы добавить одно или несколько ДЗ текстом (ДЗ Преподаватель Предмет: задание).\n"
            "• Нажмите «✏️ Редактировать задание», чтобы поправить дату сдачи, текст, предмет или преподавателя.\n"
            "• Нажмите «✅ Отметить задания выполненными», чтобы быстро закрыть задание по его номеру (1, 2, 3...)."
        )
        self.send_message(
            user_id,
            "\n".join(lines),
            keyboard=get_my_homework_actions_keyboard()
        )

    def handle_hw_menu_done(self, user_id: int) -> None:
        """
        При нажатии на «✅ Отметить задания выполненными» показывает активные задания (1, 2, 3...)
        и выводит горячие клавиши с циферками 1, 2, 3...
        """
        items = database.get_active_homework(user_id)
        if not items:
            database.upsert_user_profile(user_id, reg_step="REGISTERED")
            self.send_message(
                user_id,
                "🎉 Все домашние задания уже выполнены!",
                keyboard=get_main_keyboard()
            )
            return

        database.upsert_user_profile(user_id, reg_step="HW_SELECT_DONE")
        lines = ["✅ ОТМЕТИТЬ ЗАДАНИЕ ВЫПОЛНЕННЫМ\nВыберите номер задания на горячих клавишах внизу:\n"]
        for idx, hw in enumerate(items, start=1):
            lines.append(f"{idx}️⃣ {hw['subject']} — {hw['task_text']} (к {hw['target_date']})")

        lines.append("\n👇 Нажмите на цифру выполненного задания (1, 2, 3...):")
        self.send_message(
            user_id,
            "\n".join(lines),
            keyboard=get_homework_numbers_keyboard(items, mode="done")
        )

    def handle_hw_menu_edit(self, user_id: int) -> None:
        """
        При нажатии на «✏️ Редактировать задание» показывает активные задания (1, 2, 3...)
        и выводит горячие клавиши с циферками 1, 2, 3... для выбора задания.
        """
        items = database.get_active_homework(user_id)
        if not items:
            database.upsert_user_profile(user_id, reg_step="REGISTERED")
            self.send_message(
                user_id,
                "⚠️ У вас пока нет активных заданий для редактирования.",
                keyboard=get_main_keyboard()
            )
            return

        database.upsert_user_profile(user_id, reg_step="HW_SELECT_EDIT")
        lines = ["✏️ РЕДАКТИРОВАНИЕ ДОМАШНЕГО ЗАДАНИЯ\nВыберите номер задания, которое нужно исправить:\n"]
        for idx, hw in enumerate(items, start=1):
            lines.append(f"{idx}️⃣ {hw['subject']} — «{hw['task_text']}» (Срок: {hw['target_date']})")

        lines.append("\n👇 Нажмите на цифру нужного задания (1, 2, 3...) внизу:")
        self.send_message(
            user_id,
            "\n".join(lines),
            keyboard=get_homework_numbers_keyboard(items, mode="edit")
        )

    def handle_hw_select_for_edit(self, user_id: int, num_or_id: int) -> None:
        """Открывает меню редактирования конкретного задания (дата сдачи, текст задания, предмет, преподаватель)."""
        resolved = database.resolve_active_homework_by_number(user_id, num_or_id)
        if not resolved:
            self.send_message(
                user_id,
                f"⚠️ Активное задание под номером {num_or_id} не найдено.",
                keyboard=get_my_homework_actions_keyboard()
            )
            return

        num, hw = resolved
        hw_id = int(hw["id"])
        database.upsert_user_profile(user_id, reg_step=f"HW_EDIT_FIELD:{hw_id}:{num}")
        text = (
            f"✏️ Редактирование задания №{num} (ID #{hw_id}):\n"
            f"📚 Предмет: {hw['subject']}\n"
            f"👨‍🏫 Преподаватель: {hw['teacher']}\n"
            f"📝 Текущее задание: {hw['task_text']}\n"
            f"📅 Текущая дата сдачи: {hw['target_date']}\n\n"
            "👇 Выберите, что вы хотите поправить:\n"
            "• «📅 Поправить дату сдачи»\n"
            "• «📝 Поправить само задание (текст)»\n"
            "• «📚 Поправить предмет»\n"
            "• «👨‍🏫 Поправить преподавателя»"
        )
        self.send_message(
            user_id,
            text,
            keyboard=get_homework_edit_field_keyboard(hw_id, num)
        )

    def handle_hw_prompt_edit_date(self, user_id: int, hw_id: int, num: int = 1) -> None:
        """Переводит пользователя в режим ввода новой даты сдачи задания."""
        hw = database.get_homework_by_id(user_id, hw_id)
        if not hw:
            self.handle_my_homework(user_id)
            return
        database.upsert_user_profile(user_id, reg_step=f"HW_EDIT_DATE:{hw_id}:{num}")
        self.send_message(
            user_id,
            f"📅 Введите новую дату сдачи для задания №{num} ({hw['subject']}):\n"
            f"Текущая дата: {hw['target_date']}\n\n"
            "💡 Примеры ввода:\n"
            "• 25.11 или 25 ноября\n"
            "• 18 число\n"
            "• Без срока (если задание с открытой датой)",
            keyboard=get_homework_edit_field_keyboard(hw_id, num)
        )

    def handle_hw_prompt_edit_text(self, user_id: int, hw_id: int, num: int = 1) -> None:
        """Переводит пользователя в режим ручного ввода нового текста задания."""
        hw = database.get_homework_by_id(user_id, hw_id)
        if not hw:
            self.handle_my_homework(user_id)
            return
        database.upsert_user_profile(user_id, reg_step=f"HW_EDIT_TEXT:{hw_id}:{num}")
        self.send_message(
            user_id,
            f"📝 Напишите новый текст для задания №{num} ({hw['subject']}):\n"
            f"Текущий текст: «{hw['task_text']}»\n\n"
            "✍️ Введите исправленный текст задания в чат:",
            keyboard=get_homework_edit_field_keyboard(hw_id, num)
        )

    def handle_hw_prompt_edit_subject(self, user_id: int, hw_id: int, num: int = 1) -> None:
        """Переводит пользователя в режим ручного ввода нового названия предмета."""
        hw = database.get_homework_by_id(user_id, hw_id)
        if not hw:
            self.handle_my_homework(user_id)
            return
        database.upsert_user_profile(user_id, reg_step=f"HW_EDIT_SUBJECT:{hw_id}:{num}")
        self.send_message(
            user_id,
            f"📚 Введите новое название предмета для задания №{num}:\n"
            f"Текущий предмет: «{hw['subject']}»\n\n"
            "✍️ Напишите исправленное название предмета в чат:",
            keyboard=get_homework_edit_field_keyboard(hw_id, num)
        )

    def handle_hw_prompt_edit_teacher(self, user_id: int, hw_id: int, num: int = 1) -> None:
        """Переводит пользователя в режим ручного ввода нового ФИО преподавателя."""
        hw = database.get_homework_by_id(user_id, hw_id)
        if not hw:
            self.handle_my_homework(user_id)
            return
        database.upsert_user_profile(user_id, reg_step=f"HW_EDIT_TEACHER:{hw_id}:{num}")
        self.send_message(
            user_id,
            f"👨‍🏫 Введите новое ФИО преподавателя для задания №{num} ({hw['subject']}):\n"
            f"Текущий преподаватель: «{hw['teacher']}»\n\n"
            "✍️ Напишите исправленную фамилию (и инициалы) преподавателя в чат:",
            keyboard=get_homework_edit_field_keyboard(hw_id, num)
        )

    def handle_mark_done(self, user_id: int, num_or_id: int, by_ordinal: bool = False) -> None:
        """
        Отметка индивидуального задания выполненным (по порядковому номеру 1, 2, 3... или по ID)
        и проверка получения ачивок (10, 20, 30, 40, 50).
        """
        target_hw_id = num_or_id
        ordinal_label = f"#{num_or_id}"
        hw_obj = None

        if by_ordinal:
            resolved = database.resolve_active_homework_by_number(user_id, num_or_id)
            if resolved:
                ord_num, hw_obj = resolved
                target_hw_id = int(hw_obj["id"])
                ordinal_label = f"№{ord_num} ({hw_obj['subject']})"
        else:
            hw_obj = database.get_homework_by_id(user_id, num_or_id)
            if not hw_obj:
                resolved = database.resolve_active_homework_by_number(user_id, num_or_id)
                if resolved:
                    ord_num, hw_obj = resolved
                    target_hw_id = int(hw_obj["id"])
                    ordinal_label = f"№{ord_num} ({hw_obj['subject']})"

        database.upsert_user_profile(user_id, reg_step="REGISTERED")
        success = database.mark_homework_done(user_id, target_hw_id)
        if success:
            stats = database.get_user_homework_stats(user_id)
            done_cnt = stats["done_count"]
            pending_cnt = stats["pending_count"]
            new_ach = database.check_newly_unlocked_achievement(done_cnt)

            msg_lines = [
                f"✅ Задание {ordinal_label} успешно отмечено как выполненное!",
                f"📊 Ваш прогресс: выполнено {done_cnt} | осталось выполнить {pending_cnt}."
            ]
            if new_ach:
                msg_lines.append(
                    f"\n🎉🏆 ПОЗДРАВЛЯЕМ! НОВАЯ АЧИВКА ОТКРЫТА:\n"
                    f"«{new_ach}»!\n"
                    "Она добавлена в ваш раздел «👤 Мой профиль»!"
                )
            self.send_message(user_id, "\n".join(msg_lines), keyboard=get_main_keyboard())
        else:
            self.send_message(
                user_id,
                f"❌ Задание #{num_or_id} не найдено в вашем личном списке или уже было завершено.",
                keyboard=get_main_keyboard()
            )

    def handle_photo_schedule(self, user_id: int, photo_attachment: dict, caption_text: str = "") -> None:
        """
        Сценарий загрузки собственного расписания по фотографии:
        Распознает таблицу, сохраняет в личное расписание пользователя и закрепляет
        это расписание в общей базе за группой пользователя (чтобы одногруппники могли взять его готовым).
        """
        self.send_message(user_id, "🦭 Фотография получена! Тёма внимательно изучает и распознаёт расписание...")
        
        try:
            sizes = photo_attachment.get("sizes", [])
            if not sizes:
                self.send_message(user_id, "❌ Не удалось получить ссылку на фото.")
                return
                
            optimal_size = None
            sorted_sizes = sorted(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0), reverse=True)
            for s in sorted_sizes:
                w = s.get("width", 0)
                h = s.get("height", 0)
                if (0 < w <= 1600 and 0 < h <= 1600) or s.get("type") in ("z", "y", "x"):
                    optimal_size = s
                    break
            if not optimal_size and sorted_sizes:
                optimal_size = sorted_sizes[0]

            photo_url = optimal_size.get("url")
            logger.info(f"Скачивание оптимизированного фото для user_id={user_id} с {photo_url}")
            resp = requests.get(photo_url, timeout=20)
            resp.raise_for_status()
            image_bytes = resp.content
            
            lessons = gemini_service.recognize_schedule_image(image_bytes, mime_type="image/jpeg")
            
            if not lessons:
                self.send_message(
                    user_id,
                    "⚠️ Не удалось обнаружить учебные занятия на этом изображении. "
                    "Пожалуйста, пришли более четкое фото таблицы расписания.",
                    keyboard=get_main_keyboard()
                )
                return

            count = database.save_schedule(user_id, lessons)
            # Если вместе с фото пользователь указал номер группы в тексте сообщения — обновляем группу профиля
            explicit_group = format_canonical_group_name(caption_text) if caption_text and len(caption_text.strip()) <= 20 else ""
            if explicit_group:
                profile = database.upsert_user_profile(user_id, group_name=explicit_group, reg_step="REGISTERED")
            else:
                profile = database.upsert_user_profile(user_id, reg_step="REGISTERED")

            saved_for_group = ""
            if profile.get("group_name"):
                saved_for_group = format_canonical_group_name(profile["group_name"])
                database.save_group_schedule(saved_for_group, lessons)
            
            by_day = {}
            for l in lessons:
                d = int(l.get("day_of_week", 0))
                by_day.setdefault(d, []).append(l)
                
            identity = database.format_user_identity(profile, user_id)
            summary_lines = [
                f"✅ {identity}, твоё расписание успешно распознано! (пар: {count})"
            ]
            if saved_for_group:
                summary_lines.append(
                    f"🏫 Расписание также закреплено в общей базе за группой «{saved_for_group}» — "
                    "теперь твои одногруппники смогут подключить его готовым в один клик!\n"
                )
            else:
                summary_lines.append("")
            
            for d in sorted(by_day.keys()):
                d_name = format_day_name(d)
                summary_lines.append(f"📌 {d_name}: {len(by_day[d])} пар")
                for item in by_day[d][:2]:
                    w_icon = WEEK_ICONS.get(item.get("week_type"), "")
                    l_t = "пр" if item.get("lesson_type") == "PRACTICE" else "л"
                    summary_lines.append(f"   • {item.get('time')} {item.get('subject')} ({l_t}) {w_icon}")
                if len(by_day[d]) > 2:
                    summary_lines.append(f"   • ... и еще {len(by_day[d]) - 2} пары")
                summary_lines.append("")
                
            summary_lines.append("Теперь ты можешь узнавать расписание по кнопкам внизу и записывать свои индивидуальные ДЗ!")
            
            self.send_message(user_id, "\n".join(summary_lines), keyboard=get_main_keyboard())
            
        except Exception as e:
            logger.error(f"Ошибка при обработке фото расписания: {e}", exc_info=True)
            self.send_message(
                user_id,
                f"❌ Произошла ошибка при обработке расписания: {e}\n"
                "Пожалуйста, убедитесь, что фотография четкая и содержит таблицу расписания.",
                keyboard=get_main_keyboard()
            )

    def handle_voice_homework(self, user_id: int, audio_attachment: dict, text_override: Optional[str] = None) -> None:
        """
        Прием индивидуального домашнего задания студента (голосом или текстом):
        1. Проверяет бесплатную расшифровку ВК (transcript), затем офлайн-модель Vosk, затем локальный Zero-Token парсер.
        2. Сохраняет задание строго под личным vk_id студента.
        """
        try:
            user_schedule = database.get_full_schedule(user_id) or []
            profile = database.get_user_profile(user_id)
            identity = database.format_user_identity(profile, user_id)
            vk_transcript = (text_override or audio_attachment.get("transcript") or "").strip()

            audio_bytes = None
            mime_type = "audio/ogg"

            if vk_transcript:
                logger.info(f"⚡ Текст ДЗ для {identity} (id={user_id}): «{vk_transcript}»")
            else:
                self.send_message(user_id, "⏳ Слушаю и анализирую ваше голосовое сообщение...")
                audio_url = audio_attachment.get("link_mp3") or audio_attachment.get("link_ogg")
                if not audio_url:
                    self.send_message(user_id, "❌ Не удалось получить ссылку на аудиофайл.")
                    return
                resp = requests.get(audio_url, timeout=25)
                resp.raise_for_status()
                audio_bytes = resp.content
                mime_type = "audio/mp3" if "mp3" in audio_url else "audio/ogg"
            
            result = gemini_service.process_voice_homework(
                audio_bytes=audio_bytes,
                mime_type=mime_type,
                user_schedule=user_schedule,
                today_date=date.today(),
                transcript_text=vk_transcript if vk_transcript else None
            )
            
            if "error" in result and not result.get("taskText") and not result.get("tasks"):
                transcript = result.get("transcript", vk_transcript or "Не удалось разобрать слова")
                self.send_message(
                    user_id,
                    f"⚠️ Не удалось привязать задание:\n{result['error']}\n\n"
                    f"🎙️ Расшифровка: «{transcript}»\n\n"
                    "💡 Совет: вы можете явно назвать число (например, «к 25 числу») или сказать «с открытой датой».",
                    keyboard=get_main_keyboard()
                )
                return

            tasks_list = result.get("tasks")
            if not tasks_list or not isinstance(tasks_list, list):
                tasks_list = [result]

            saved_items = []
            for t_item in tasks_list:
                teacher = t_item.get("teacher") or "Не указан"
                subject = t_item.get("subject") or "Общее задание"
                task_text = t_item.get("taskText") or "Выполнить задание"
                target_date = t_item.get("targetDate") or "Без срока"

                hw_id = database.add_homework(
                    vk_id=user_id,
                    teacher=teacher,
                    subject=subject,
                    task_text=task_text,
                    target_date=target_date
                )
                saved_items.append({
                    "id": hw_id,
                    "teacher": teacher,
                    "subject": subject,
                    "task_text": task_text,
                    "target_date": target_date
                })

            if len(saved_items) == 1:
                item = saved_items[0]
                date_display = "⏳ Без срока (открытая дата)" if item["target_date"] == "Без срока" else f"📅 Сдать к: {item['target_date']}"
                reply_text = (
                    f"✅ Задание сохранено в личный профиль {identity}!\n\n"
                    f"📝 Задание: {item['task_text']}\n"
                    f"👨‍🏫 Преподаватель: {item['teacher']}\n"
                    f"📚 Предмет: {item['subject']}\n"
                    f"🎯 Срок: {date_display}\n"
                    f"🆔 Номер задания: #{item['id']}\n\n"
                    f"Для отметки выполнения нажмите кнопку ниже или перейдите в «📌 Мои задания»."
                )
                inline_kb = get_homework_inline_keyboard(item["id"])
                self.send_message(user_id, reply_text, keyboard=inline_kb)
            else:
                lines = [f"✅ Сохранено сразу {len(saved_items)} домашних задания в профиль {identity}!\n"]
                for idx, item in enumerate(saved_items, start=1):
                    date_display = "⏳ Без срока (открытая дата)" if item["target_date"] == "Без срока" else f"📅 Сдать к: {item['target_date']}"
                    lines.append(
                        f"{idx}️⃣ Задание #{item['id']}\n"
                        f"   📝 Что сделать: {item['task_text']}\n"
                        f"   👨‍🏫 Преподаватель: {item['teacher']}\n"
                        f"   📚 Предмет: {item['subject']}\n"
                        f"   🎯 Срок: {date_display}"
                    )
                lines.append("\n💡 Управлять заданиями, редактировать дату/текст или отметить их выполненными по номерам (1, 2, 3...) можно в меню «📌 Мои задания»!")
                inline_kb = get_homework_inline_keyboard(saved_items[0]["id"])
                self.send_message(user_id, "\n\n".join(lines), keyboard=inline_kb)
            
        except Exception as e:
            logger.error(f"Ошибка при обработке голосового сообщения: {e}", exc_info=True)
            self.send_message(
                user_id,
                f"❌ Ошибка распознавания задания: {e}",
                keyboard=get_main_keyboard()
            )

    # =========================================================================
    # Главный диспетчер входящих сообщений
    # =========================================================================

    def process_message(self, message: dict) -> None:
        """Маршрутизация сообщения: регистрация, реферальная ссылка, payload, фото, аудио, текст."""
        user_id = message.get("from_id")
        text = (message.get("text") or "").strip()
        payload_raw = message.get("payload")
        attachments = message.get("attachments", [])
        referrer_id = extract_referrer_id(message, text)
        
        # 1. Проверяем Payload от нажатий кнопок
        if payload_raw:
            try:
                payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
                cmd = payload.get("command")
                if cmd == "greet_tema":
                    self.start_registration(user_id, referrer_id=referrer_id)
                    return
                elif cmd == "reg_vk_name":
                    self.handle_reg_vk_name(user_id)
                    return
                elif cmd == "reg_manual_name":
                    self.handle_reg_manual_name(user_id)
                    return
                elif cmd == "restart_registration":
                    self.start_registration(user_id)
                    return
                elif cmd == "referral_info":
                    self.handle_referral_info(user_id)
                    return
                elif cmd == "choose_schedule_mode":
                    self.handle_choose_schedule_mode(user_id)
                    return
                elif cmd == "sched_from_group":
                    self.handle_schedule_from_group_choice(user_id)
                    return
                elif cmd == "sched_custom_photo":
                    database.upsert_user_profile(user_id, reg_step="REGISTERED")
                    self.send_message(
                        user_id,
                        "📸 Отправь мне фотографию или скриншот твоего расписания в этот чат, и я распознаю все пары и закреплю их за твоей группой!",
                        keyboard=get_main_keyboard()
                    )
                    return
                elif cmd == "my_profile":
                    self.handle_my_profile(user_id)
                    return
                elif cmd == "schedule_today":
                    self.handle_schedule_for_day(user_id, date.today(), "сегодня")
                    return
                elif cmd == "schedule_tomorrow":
                    self.handle_schedule_for_day(user_id, date.today() + timedelta(days=1), "завтра")
                    return
                elif cmd == "my_homework":
                    self.handle_my_homework(user_id)
                    return
                elif cmd == "main_menu":
                    database.upsert_user_profile(user_id, reg_step="REGISTERED")
                    self.send_message(
                        user_id,
                        "🏠 Ты вернулся в главное меню. Выбери нужное действие на клавиатуре внизу 👇",
                        keyboard=get_main_keyboard()
                    )
                    return
                elif cmd == "hw_add_text_cmd":
                    self.handle_hw_prompt_add_text(user_id)
                    return
                elif cmd == "hw_menu_done":
                    self.handle_hw_menu_done(user_id)
                    return
                elif cmd == "hw_menu_edit":
                    self.handle_hw_menu_edit(user_id)
                    return
                elif cmd == "hw_done_num":
                    hw_id = payload.get("id")
                    num = payload.get("num")
                    if hw_id:
                        self.handle_mark_done(user_id, int(hw_id), by_ordinal=False)
                    elif num:
                        self.handle_mark_done(user_id, int(num), by_ordinal=True)
                    return
                elif cmd == "hw_edit_num":
                    num = payload.get("num") or payload.get("id")
                    if num:
                        self.handle_hw_select_for_edit(user_id, int(num))
                    return
                elif cmd == "hw_edit_date":
                    hw_id = int(payload.get("id", 0))
                    num = int(payload.get("num", 1))
                    self.handle_hw_prompt_edit_date(user_id, hw_id, num)
                    return
                elif cmd == "hw_edit_text":
                    hw_id = int(payload.get("id", 0))
                    num = int(payload.get("num", 1))
                    self.handle_hw_prompt_edit_text(user_id, hw_id, num)
                    return
                elif cmd == "hw_edit_subject":
                    hw_id = int(payload.get("id", 0))
                    num = int(payload.get("num", 1))
                    self.handle_hw_prompt_edit_subject(user_id, hw_id, num)
                    return
                elif cmd == "hw_edit_teacher":
                    hw_id = int(payload.get("id", 0))
                    num = int(payload.get("num", 1))
                    self.handle_hw_prompt_edit_teacher(user_id, hw_id, num)
                    return
                elif cmd == "help":
                    self.handle_command_help(user_id)
                    return
                elif cmd == "buy_subscription":
                    self.handle_buy_subscription(user_id)
                    return
                elif cmd == "check_payment":
                    self.handle_check_payment(user_id, payment_id=payload.get("payment_id"))
                    return
                elif cmd == "done_hw":
                    hw_id = payload.get("id")
                    if hw_id:
                        self.handle_mark_done(user_id, int(hw_id))
                        return
            except Exception as e:
                logger.warning(f"Ошибка разбора payload: {e}")

        # 2. Проверяем, зарегистрирован ли пользователь и команды регистрации / оплаты
        profile = database.get_user_profile(user_id)
        text_lower = text.lower()

        if text_lower in ("👋 привет", "привет", "приветик", "/register", "регистрация"):
            self.start_registration(user_id, referrer_id=referrer_id)
            return

        if (
            text_lower in ("начать", "start", "/start")
            or text_lower.startswith(("начать ref", "start ref", "/start ref", "ref_"))
        ):
            self.show_initial_hello_prompt(user_id, referrer_id=referrer_id)
            return

        if not profile:
            self.show_initial_hello_prompt(user_id, referrer_id=referrer_id)
            return

        # Команды оплаты, проверки платежа ЮKassa и реферальной программы доступны всегда
        if text_lower in (
            "🤝 пригласить одногруппника (+7 дн.)",
            "пригласить одногруппника",
            "пригласить друга",
            "рефералка",
            "реферальная ссылка",
            "/ref",
            "/referral"
        ):
            self.handle_referral_info(user_id)
            return

        if text_lower in (
            "💳 оплатить 30 ₽ (на 2 недели)",
            "💳 продлить подписку (30 ₽ / 2 нед.)",
            "оплатить",
            "подписка",
            "продлить подписку",
            "/pay"
        ):
            self.handle_buy_subscription(user_id)
            return

        if text_lower in ("🔄 проверить оплату", "проверить оплату", "/check_pay"):
            self.handle_check_payment(user_id)
            return

        reg_step = profile.get("reg_step") or "REGISTERED"

        # Если пользователь еще проходит первичную регистрацию (шаги 1-3) — даем завершить регистрацию
        if reg_step != "REGISTERED" and not reg_step.startswith("HW_"):
            if text_lower in ("📱 взять фамилию и имя из вк", "взять фамилию и имя из вк"):
                self.handle_reg_vk_name(user_id)
                return
            elif text_lower in ("✍️ самостоятельный ввод (фи и никнейм)", "самостоятельный ввод"):
                self.handle_reg_manual_name(user_id)
                return
            elif text_lower in ("🏫 взять расписание с общей базы", "взять расписание с общей базы"):
                self.handle_schedule_from_group_choice(user_id)
                return
            elif text_lower in ("📸 своё расписание (скинуть фото)", "своё расписание"):
                database.upsert_user_profile(user_id, reg_step="REGISTERED")
                self.send_message(
                    user_id,
                    "📸 Отправьте фотографию вашего расписания в чат, и я загружу все пары!",
                    keyboard=get_main_keyboard()
                )
                return
            if self.handle_registration_text_input(user_id, text, reg_step):
                return

        # 3. Профиль и справка доступны всегда
        if text_lower in ("👤 мой профиль", "мой профиль", "профиль", "/profile", "ачивки", "достижения"):
            self.handle_my_profile(user_id)
            return
        elif text_lower in ("ℹ️ помощь", "помощь", "help", "/help"):
            self.handle_command_help(user_id)
            return


        # 4. Проверяем активность подписки (первые 30 дней бесплатно, далее 30 ₽ на 2 недели)
        sub_status = database.get_subscription_status(user_id)
        if not sub_status["is_active"]:
            self.handle_subscription_expired_prompt(user_id)
            return

        # 5. Проверяем наличие вложений (фото расписания или голосовое сообщение ДЗ)
        for att in attachments:
            att_type = att.get("type")
            if att_type == "photo":
                self.handle_photo_schedule(user_id, att.get("photo", {}), caption_text=text)
                return
            elif att_type == "audio_message":
                self.handle_voice_homework(user_id, att.get("audio_message", {}))
                return
            elif att_type == "doc":
                doc = att.get("doc", {})
                ext = doc.get("ext", "").lower()
                if ext in ("jpg", "jpeg", "png", "webp"):
                    self.handle_photo_schedule(user_id, {"sizes": [{"url": doc.get("url"), "width": 1000, "height": 1000}]}, caption_text=text)
                    return

        if text_lower in ("🔙 в главное меню", "🏠 главное меню", "главное меню", "меню"):
            database.upsert_user_profile(user_id, reg_step="REGISTERED")
            self.send_message(user_id, "🏠 Главное меню:", keyboard=get_main_keyboard())
            return
        elif text_lower in ("🔙 назад к заданиям", "📌 мои задания", "мои задания", "задания", "/hw"):
            self.handle_my_homework(user_id)
            return
        elif text_lower in ("✍️ записать дз текстом", "записать дз текстом", "записать дз", "добавить дз", "дз", "домашка", "/dz", "/add_hw"):
            self.handle_hw_prompt_add_text(user_id)
            return
        elif text_lower in ("✏️ редактировать задание", "редактировать задание", "редактировать дз"):
            self.handle_hw_menu_edit(user_id)
            return
        elif text_lower in ("✅ отметить задания выполненными", "отметить задания выполненными", "отметить выполненными"):
            self.handle_hw_menu_done(user_id)
            return
        elif text_lower in ("📱 взять фамилию и имя из вк", "взять фамилию и имя из вк"):
            self.handle_reg_vk_name(user_id)
            return
        elif text_lower in ("✍️ самостоятельный ввод (фи и никнейм)", "самостоятельный ввод"):
            self.handle_reg_manual_name(user_id)
            return
        elif text_lower in ("🏫 взять расписание с общей базы", "взять расписание с общей базы"):
            self.handle_schedule_from_group_choice(user_id)
            return
        elif text_lower in ("📸 своё расписание (скинуть фото)", "своё расписание"):
            database.upsert_user_profile(user_id, reg_step="REGISTERED")
            self.send_message(
                user_id,
                "📸 Отправьте фотографию вашего расписания в чат, и я загружу все пары!",
                keyboard=get_main_keyboard()
            )
            return

        # 5b. Обработка шагов работы с заданиями (запись ДЗ текстом, выбор цифры 1, 2, 3..., правка даты и текста)
        if reg_step == "HW_ADD_TEXT" and len(text) >= 5 and not text.startswith("/"):
            database.upsert_user_profile(user_id, reg_step="REGISTERED")
            self.handle_voice_homework(user_id, {}, text_override=text)
            return

        if reg_step == "HW_SELECT_DONE" and text.isdigit():
            self.handle_mark_done(user_id, int(text), by_ordinal=True)
            return

        if reg_step == "HW_SELECT_EDIT" and text.isdigit():
            self.handle_hw_select_for_edit(user_id, int(text))
            return

        if reg_step.startswith(("HW_EDIT_FIELD:", "HW_EDIT_DATE:", "HW_EDIT_TEXT:", "HW_EDIT_SUBJECT:", "HW_EDIT_TEACHER:")):
            parts = reg_step.split(":")
            hw_id = int(parts[1])
            num = int(parts[2]) if len(parts) > 2 else 1

            # Если пользователь нажал одну из 4 кнопок выбора поля для редактирования:
            if "поправить дату" in text_lower:
                self.handle_hw_prompt_edit_date(user_id, hw_id, num)
                return
            elif "поправить само задание" in text_lower or ("поправить" in text_lower and "текст" in text_lower):
                self.handle_hw_prompt_edit_text(user_id, hw_id, num)
                return
            elif "поправить предмет" in text_lower:
                self.handle_hw_prompt_edit_subject(user_id, hw_id, num)
                return
            elif "поправить преподавателя" in text_lower:
                self.handle_hw_prompt_edit_teacher(user_id, hw_id, num)
                return

            if reg_step.startswith("HW_EDIT_DATE:"):
                new_date = normalize_target_date(text, base_date=date.today())
                database.update_homework(user_id, hw_id, target_date=new_date)
                database.upsert_user_profile(user_id, reg_step="REGISTERED")
                hw = database.get_homework_by_id(user_id, hw_id) or {}
                self.send_message(
                    user_id,
                    f"✅ Дата сдачи для задания №{num} успешно обновлена!\n\n"
                    f"📚 Предмет: {hw.get('subject', '')}\n"
                    f"👨‍🏫 Преподаватель: {hw.get('teacher', '')}\n"
                    f"📝 Задание: {hw.get('task_text', '')}\n"
                    f"📅 Новая дата сдачи: {new_date}",
                    keyboard=get_my_homework_actions_keyboard()
                )
                return

            if reg_step.startswith("HW_EDIT_TEXT:"):
                database.update_homework(user_id, hw_id, task_text=text)
                database.upsert_user_profile(user_id, reg_step="REGISTERED")
                hw = database.get_homework_by_id(user_id, hw_id) or {}
                self.send_message(
                    user_id,
                    f"✅ Текст задания №{num} успешно изменён!\n\n"
                    f"📚 Предмет: {hw.get('subject', '')}\n"
                    f"👨‍🏫 Преподаватель: {hw.get('teacher', '')}\n"
                    f"📝 Новое задание: {hw.get('task_text', '')}\n"
                    f"📅 Дата сдачи: {hw.get('target_date', '')}",
                    keyboard=get_my_homework_actions_keyboard()
                )
                return

            if reg_step.startswith("HW_EDIT_SUBJECT:"):
                database.update_homework(user_id, hw_id, subject=text)
                database.upsert_user_profile(user_id, reg_step="REGISTERED")
                hw = database.get_homework_by_id(user_id, hw_id) or {}
                self.send_message(
                    user_id,
                    f"✅ Предмет для задания №{num} успешно изменён!\n\n"
                    f"📚 Новый предмет: {hw.get('subject', '')}\n"
                    f"👨‍🏫 Преподаватель: {hw.get('teacher', '')}\n"
                    f"📝 Задание: {hw.get('task_text', '')}\n"
                    f"📅 Дата сдачи: {hw.get('target_date', '')}",
                    keyboard=get_my_homework_actions_keyboard()
                )
                return

            if reg_step.startswith("HW_EDIT_TEACHER:"):
                database.update_homework(user_id, hw_id, teacher=text)
                database.upsert_user_profile(user_id, reg_step="REGISTERED")
                hw = database.get_homework_by_id(user_id, hw_id) or {}
                self.send_message(
                    user_id,
                    f"✅ Преподаватель для задания №{num} успешно изменён!\n\n"
                    f"📚 Предмет: {hw.get('subject', '')}\n"
                    f"👨‍🏫 Новый преподаватель: {hw.get('teacher', '')}\n"
                    f"📝 Задание: {hw.get('task_text', '')}\n"
                    f"📅 Дата сдачи: {hw.get('target_date', '')}",
                    keyboard=get_my_homework_actions_keyboard()
                )
                return

        # 6. Стандартные текстовые команды зарегистрированного пользователя
        if text_lower in ("🏫 выбрать группу / фото", "выбрать группу", "сменить группу", "/group"):
            self.handle_choose_schedule_mode(user_id)
        elif text_lower in ("📅 расписание на сегодня", "расписание на сегодня", "сегодня", "/today"):
            self.handle_schedule_for_day(user_id, date.today(), "сегодня")
        elif text_lower in ("📆 расписание на завтра", "расписание на завтра", "завтра", "/tomorrow"):
            self.handle_schedule_for_day(user_id, date.today() + timedelta(days=1), "завтра")
        elif text_lower.startswith("/done") or text_lower.startswith("выполнено"):
            parts = text.split()
            if len(parts) >= 2 and parts[1].isdigit():
                self.handle_mark_done(user_id, int(parts[1]), by_ordinal=True)
            else:
                self.handle_hw_menu_done(user_id)
        elif text_lower == "/clear":
            database.clear_schedule(user_id)
            self.send_message(user_id, "🗑️ Расписание успешно очищено. Выберите группу или отправьте новое фото.")
        elif len(text) >= 6 and not text.startswith("/"):
            # Запись индивидуального ДЗ текстом (обрабатывается локальным Zero-Token парсером или Gemini)
            self.handle_voice_homework(user_id, {}, text_override=text)
        else:
            self.send_message(
                user_id,
                "Я получил ваше сообщение.\n"
                "• Чтобы открыть профиль и ачивки — нажмите «👤 Мой профиль»\n"
                "• Чтобы редактировать или закрыть ДЗ — нажмите «📌 Мои задания»\n"
                "• Чтобы записать домашку — отправьте голосовое 🎙️ или напишите задание текстом ✍️",
                keyboard=get_main_keyboard()
            )

    # =========================================================================
    # Главный бесконечный цикл прослушивания LongPoll
    # =========================================================================

    def run(self):
        """Запуск прослушивания с неблокирующим пулом потоков, фоновой проверкой подписок ЮKassa и автопереподключением."""
        logger.info("Запуск VK LongPoll слушателя (пул потоков: 8 воркеров)...")

        # Запускаем фоновый поток проверки истечения 30-дневного пробного периода и автоподтверждения платежей ЮKassa
        monitor_thread = threading.Thread(target=self._subscription_monitor_loop, daemon=True)
        monitor_thread.start()
        
        while True:
            try:
                longpoll = VkLongPoll(self.vk_session)
                logger.info("✅ Подключение к VK LongPoll успешно установлено! Ожидание сообщений...")
                
                for event in longpoll.listen():
                    if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                        msg_info = self.vk.messages.getById(message_ids=[event.message_id])
                        if msg_info and msg_info.get("items"):
                            msg = msg_info["items"][0]
                            # Передаем обработку в пул потоков, чтобы не блокировать цикл для других студентов:
                            self.executor.submit(self.process_message, msg)
                            
            except (requests.exceptions.RequestException, ConnectionError) as net_err:
                logger.warning(f"Сетевая ошибка VK LongPoll ({net_err}). Повтор через 5 секунд...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Непредвиденная ошибка в основном цикле LongPoll: {e}", exc_info=True)
                time.sleep(5)


if __name__ == "__main__":
    if not VK_TOKEN:
        logger.error("ОШИБКА: VK_TOKEN не найден в .env или config.py!")
        sys.exit(1)
        
    bot = VKStudentBot(token=VK_TOKEN, group_id=VK_GROUP_ID)
    bot.run()
