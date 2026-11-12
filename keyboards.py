import json
from typing import Optional


def get_main_keyboard() -> str:
    """
    Возвращает постоянную Reply-клавиатуру (Keyboard) быстрого доступа внизу чата VK.
    Кнопки:
    1. 📅 Расписание на сегодня | 📆 Расписание на завтра
    2. 📌 Мои задания          | 👤 Мой профиль
    3. 🏫 Выбрать группу / Фото | ℹ️ Помощь
    """
    keyboard = {
        "one_time": False,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "📅 Расписание на сегодня",
                        "payload": json.dumps({"command": "schedule_today"})
                    },
                    "color": "primary"
                },
                {
                    "action": {
                        "type": "text",
                        "label": "📆 Расписание на завтра",
                        "payload": json.dumps({"command": "schedule_tomorrow"})
                    },
                    "color": "primary"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "📌 Мои задания",
                        "payload": json.dumps({"command": "my_homework"})
                    },
                    "color": "positive"
                },
                {
                    "action": {
                        "type": "text",
                        "label": "👤 Мой профиль",
                        "payload": json.dumps({"command": "my_profile"})
                    },
                    "color": "positive"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "🏫 Выбрать группу / Фото",
                        "payload": json.dumps({"command": "choose_schedule_mode"})
                    },
                    "color": "secondary"
                },
                {
                    "action": {
                        "type": "text",
                        "label": "ℹ️ Помощь",
                        "payload": json.dumps({"command": "help"})
                    },
                    "color": "secondary"
                }
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


def get_welcome_hello_keyboard() -> str:
    """
    Приветственная горячая клавиша «👋 Привет», которая появляется,
    когда студент впервые заходит в диалог с маскотом Тёмой (Байкальской нерпой).
    """
    keyboard = {
        "one_time": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "👋 Привет",
                        "payload": json.dumps({"command": "greet_tema"})
                    },
                    "color": "positive"
                }
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


def get_registration_name_keyboard() -> str:
    """
    Клавиатура шага 1 регистрации: выбор способа ввода Фамилии, Имени и Никнейма.
    Две кнопки:
    1. Взять Фамилию и Имя со страницы ВКонтакте
    2. Самостоятельный ввод (Фамилия, Имя, Никнейм)
    """
    keyboard = {
        "one_time": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "📱 Взять Фамилию и Имя из ВК",
                        "payload": json.dumps({"command": "reg_vk_name"})
                    },
                    "color": "primary"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "✍️ Самостоятельный ввод (ФИ и Никнейм)",
                        "payload": json.dumps({"command": "reg_manual_name"})
                    },
                    "color": "secondary"
                }
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


def get_schedule_choice_keyboard() -> str:
    """
    Клавиатура выбора способа загрузки расписания (2 варианта):
    1. Взять расписание из общей базы по номеру группы
    2. Загрузить собственное расписание (скинуть фотографию)
    """
    keyboard = {
        "one_time": False,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "🏫 Взять расписание с общей базы",
                        "payload": json.dumps({"command": "sched_from_group"})
                    },
                    "color": "positive"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "📸 Своё расписание (скинуть фото)",
                        "payload": json.dumps({"command": "sched_custom_photo"})
                    },
                    "color": "primary"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "👤 Мой профиль",
                        "payload": json.dumps({"command": "my_profile"})
                    },
                    "color": "secondary"
                }
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


def get_profile_inline_keyboard() -> str:
    """
    Inline-кнопки управления в карточке «Мой профиль».
    """
    keyboard = {
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "🤝 Пригласить одногруппника (+7 дн.)",
                        "payload": json.dumps({"command": "referral_info"})
                    },
                    "color": "positive"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "✏️ Изменить ФИО / Ник / Группу",
                        "payload": json.dumps({"command": "restart_registration"})
                    },
                    "color": "secondary"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "🏫 Сменить расписание (Группа / Фото)",
                        "payload": json.dumps({"command": "choose_schedule_mode"})
                    },
                    "color": "primary"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "💳 Продлить подписку (30 ₽ / 2 нед.)",
                        "payload": json.dumps({"command": "buy_subscription"})
                    },
                    "color": "positive"
                }
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


def get_subscription_pay_keyboard(confirmation_url: str = "", payment_id: str = "") -> str:
    """
    Клавиатура оплаты подписки через ЮKassa (30 рублей на 2 недели / 14 дней):
    - Если есть ссылка confirmation_url — добавляет кнопку прямого перехода в ЮKassa (open_link без поля color по стандарту VK API)
    - Кнопка «🔄 Проверить оплату» для мгновенной проверки статуса платежа и активации +14 дней
    - Кнопка «💳 Создать новый счёт (30 ₽)»
    - Кнопка «👤 Мой профиль»
    """
    rows = []
    if confirmation_url and confirmation_url.startswith("http"):
        rows.append([
            {
                "action": {
                    "type": "open_link",
                    "link": confirmation_url,
                    "label": "💳 Оплатить 30 ₽ в ЮKassa (2 недели)",
                    "payload": json.dumps({"command": "open_yookassa_link", "payment_id": payment_id})
                }
            }
        ])

    rows.append([
        {
            "action": {
                "type": "text",
                "label": "🔄 Проверить оплату",
                "payload": json.dumps({"command": "check_payment", "payment_id": payment_id})
            },
            "color": "positive"
        }
    ])
    rows.append([
        {
            "action": {
                "type": "text",
                "label": "💳 Оплатить 30 ₽ (на 2 недели)",
                "payload": json.dumps({"command": "buy_subscription"})
            },
            "color": "primary"
        },
        {
            "action": {
                "type": "text",
                "label": "👤 Мой профиль",
                "payload": json.dumps({"command": "my_profile"})
            },
            "color": "secondary"
        }
    ])

    keyboard = {
        "one_time": False,
        "buttons": rows
    }
    return json.dumps(keyboard, ensure_ascii=False)



def get_homework_inline_keyboard(homework_id: int) -> str:
    """
    Возвращает Inline-кнопку «✅ Выполнено» для конкретного домашнего задания.
    """
    keyboard = {
        "inline": True,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "✅ Выполнено",
                        "payload": json.dumps({"command": "done_hw", "id": homework_id})
                    },
                    "color": "positive"
                }
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


def get_my_homework_actions_keyboard() -> str:
    """
    Горячая клавиатура при переходе в «📌 Мои задания»:
    1. Большая клавиша «✍️ Записать ДЗ текстом»
    2. Большая клавиша «✏️ Редактировать задание»
    3. Большая клавиша «✅ Отметить задания выполненными»
    4. Клавиша возврата «🔙 В главное меню»
    """
    keyboard = {
        "one_time": False,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "✍️ Записать ДЗ текстом",
                        "payload": json.dumps({"command": "hw_add_text_cmd"})
                    },
                    "color": "primary"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "✏️ Редактировать задание",
                        "payload": json.dumps({"command": "hw_menu_edit"})
                    },
                    "color": "primary"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "✅ Отметить задания выполненными",
                        "payload": json.dumps({"command": "hw_menu_done"})
                    },
                    "color": "positive"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "🔙 В главное меню",
                        "payload": json.dumps({"command": "main_menu"})
                    },
                    "color": "secondary"
                }
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


def get_homework_numbers_keyboard(active_items: list, mode: str = "done") -> str:
    """
    Генерирует горячие клавиши с циферками активных заданий (1, 2, 3...),
    чтобы при нажатии на цифру (например, «2») задание сразу закрывалось (mode='done')
    или открывалось для редактирования даты/текста (mode='edit').
    """
    rows = []
    current_row = []
    cmd_name = "hw_done_num" if mode == "done" else "hw_edit_num"
    btn_color = "positive" if mode == "done" else "primary"

    # Ограничение VK Keyboard: максимум 10 строк, до 4 кнопок в строке
    max_items = min(len(active_items), 20)
    for idx in range(1, max_items + 1):
        hw = active_items[idx - 1]
        current_row.append({
            "action": {
                "type": "text",
                "label": str(idx),
                "payload": json.dumps({
                    "command": cmd_name,
                    "num": idx,
                    "id": hw.get("id")
                })
            },
            "color": btn_color
        })
        if len(current_row) == 4:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    rows.append([
        {
            "action": {
                "type": "text",
                "label": "🔙 Назад к заданиям",
                "payload": json.dumps({"command": "my_homework"})
            },
            "color": "secondary"
        },
        {
            "action": {
                "type": "text",
                "label": "🏠 Главное меню",
                "payload": json.dumps({"command": "main_menu"})
            },
            "color": "secondary"
        }
    ])

    keyboard = {
        "one_time": False,
        "buttons": rows
    }
    return json.dumps(keyboard, ensure_ascii=False)


def get_homework_edit_field_keyboard(homework_id: int, num: int = 1) -> str:
    """
    Клавиатура выбора, что именно поправить в выбранном задании:
    - 📅 Поправить дату сдачи
    - 📝 Поправить само задание (текст)
    - 📚 Поправить предмет
    - 👨‍🏫 Поправить преподавателя
    """
    keyboard = {
        "one_time": False,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "📅 Поправить дату сдачи",
                        "payload": json.dumps({
                            "command": "hw_edit_date",
                            "id": homework_id,
                            "num": num
                        })
                    },
                    "color": "primary"
                },
                {
                    "action": {
                        "type": "text",
                        "label": "📝 Поправить само задание (текст)",
                        "payload": json.dumps({
                            "command": "hw_edit_text",
                            "id": homework_id,
                            "num": num
                        })
                    },
                    "color": "primary"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "📚 Поправить предмет",
                        "payload": json.dumps({
                            "command": "hw_edit_subject",
                            "id": homework_id,
                            "num": num
                        })
                    },
                    "color": "primary"
                },
                {
                    "action": {
                        "type": "text",
                        "label": "👨‍🏫 Поправить преподавателя",
                        "payload": json.dumps({
                            "command": "hw_edit_teacher",
                            "id": homework_id,
                            "num": num
                        })
                    },
                    "color": "primary"
                }
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "🔙 Назад к заданиям",
                        "payload": json.dumps({"command": "my_homework"})
                    },
                    "color": "secondary"
                }
            ]
        ]
    }
    return json.dumps(keyboard, ensure_ascii=False)


