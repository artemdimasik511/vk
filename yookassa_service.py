"""
====================================================================
Модуль интеграции с платежной системой ЮKassa (API v3)
====================================================================
Правила монетизации бота Тёмы (Байкальской нерпы):
- Первые 30 дней (1 месяц) с момента первого входа в бот — полностью БЕСПЛАТНО (пробный период).
- После окончания пробного месяца — 30 рублей на 2 недели (14 дней).
- Оплата через официальный REST API ЮKassa (https://api.yookassa.ru/v3/payments)
  с использованием Basic-аутентификации (YOOKASSA_SHOP_ID : YOOKASSA_SECRET_KEY) и Idempotence-Key.
"""

import uuid
import logging
from typing import Dict, Any, Optional
import requests

from config import (
    YOOKASSA_SHOP_ID,
    YOOKASSA_SECRET_KEY,
    SUBSCRIPTION_PRICE_RUB,
    SUBSCRIPTION_DAYS,
)

logger = logging.getLogger("VKStudentBot.YooKassa")

YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"


def create_yookassa_payment(
    vk_id: int,
    amount_rub: int = SUBSCRIPTION_PRICE_RUB,
    days: int = SUBSCRIPTION_DAYS,
    return_url: str = "https://vk.com",
    user_identity: str = "",
) -> Dict[str, Any]:
    """
    Создает платеж в ЮKassa на сумму amount_rub (по умолчанию 30.00 RUB) на 14 дней (2 недели).
    Возвращает словарь:
    {
        "success": True/False,
        "payment_id": str,
        "status": str,
        "confirmation_url": str,
        "amount": int,
        "days": int,
        "error": Optional[str]
    }
    """
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return {
            "success": False,
            "error": "Не заданы YOOKASSA_SHOP_ID или YOOKASSA_SECRET_KEY в настройках."
        }

    amount_str = f"{float(amount_rub):.2f}"
    description = f"Подписка на бота Тёма (на 2 недели / {days} дн.) — {user_identity or f'VK ID {vk_id}'}"[:128]

    base_payload: Dict[str, Any] = {
        "amount": {
            "value": amount_str,
            "currency": "RUB"
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url
        },
        "description": description,
        "metadata": {
            "vk_id": str(vk_id),
            "days": str(days),
            "bot": "tema_student_assistant"
        }
    }

    # Запросы к российскому шлюзу ЮKassa выполняем напрямую без зарубежного прокси для Gemini
    no_proxy = {"http": None, "https": None}

    try:
        idempotence_key = str(uuid.uuid4())
        resp = requests.post(
            f"{YOOKASSA_API_BASE}/payments",
            json=base_payload,
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            headers={
                "Idempotence-Key": idempotence_key,
                "Content-Type": "application/json"
            },
            proxies=no_proxy,
            timeout=15
        )

        # Если в личном кабинете ЮKassa включена обязательная отправка чеков по 54-ФЗ (receipt),
        # повторяем запрос с корректным блоком чека:
        if resp.status_code == 400 and "receipt" in resp.text.lower():
            logger.info("Магазин ЮKassa требует передачу чека (receipt). Повторное создание платежа с чеком 54-ФЗ...")
            payload_with_receipt = dict(base_payload)
            payload_with_receipt["receipt"] = {
                "customer": {
                    "email": f"vk{vk_id}@student-bot.ru"
                },
                "items": [
                    {
                        "description": description[:128],
                        "quantity": "1.00",
                        "amount": {
                            "value": amount_str,
                            "currency": "RUB"
                        },
                        "vat_code": 1,
                        "payment_mode": "full_payment",
                        "payment_subject": "service"
                    }
                ]
            }
            resp = requests.post(
                f"{YOOKASSA_API_BASE}/payments",
                json=payload_with_receipt,
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
                headers={
                    "Idempotence-Key": str(uuid.uuid4()),
                    "Content-Type": "application/json"
                },
                proxies=no_proxy,
                timeout=15
            )

        if resp.status_code in (200, 201):
            data = resp.json()
            payment_id = data.get("id", "")
            status = data.get("status", "pending")
            confirmation = data.get("confirmation") or {}
            confirmation_url = confirmation.get("confirmation_url", "")
            logger.info(f"✅ Создан платеж ЮKassa id={payment_id} для vk_id={vk_id} на {amount_str} RUB ({days} дн.)")
            return {
                "success": True,
                "payment_id": payment_id,
                "status": status,
                "confirmation_url": confirmation_url,
                "amount": amount_rub,
                "days": days
            }
        else:
            logger.error(f"Ошибка ЮKassa API ({resp.status_code}): {resp.text}")
            return {
                "success": False,
                "error": f"HTTP {resp.status_code}: {resp.text}"
            }
    except Exception as e:
        logger.error(f"Сетевая ошибка при обращении к ЮKassa: {e}")
        return {
            "success": False,
            "error": str(e)
        }


def check_yookassa_payment(payment_id: str) -> Dict[str, Any]:
    """
    Проверяет актуальный статус платежа в ЮKassa по его payment_id.
    Статусы ЮKassa: 'pending', 'waiting_for_capture', 'succeeded', 'canceled'.
    """
    if not payment_id:
        return {"success": False, "status": "unknown", "error": "Не указан ID платежа"}

    no_proxy = {"http": None, "https": None}

    try:
        resp = requests.get(
            f"{YOOKASSA_API_BASE}/payments/{payment_id}",
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            headers={"Content-Type": "application/json"},
            proxies=no_proxy,
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "pending")
            paid = bool(data.get("paid", False)) or status == "succeeded"
            confirmation = data.get("confirmation") or {}
            return {
                "success": True,
                "payment_id": data.get("id", payment_id),
                "status": "succeeded" if paid else status,
                "paid": paid,
                "confirmation_url": confirmation.get("confirmation_url", ""),
                "metadata": data.get("metadata") or {}
            }
        else:
            logger.warning(f"Не удалось проверить платеж {payment_id} ({resp.status_code}): {resp.text}")
            return {
                "success": False,
                "status": "error",
                "error": f"HTTP {resp.status_code}: {resp.text}"
            }
    except Exception as e:
        logger.error(f"Ошибка при проверке статуса платежа {payment_id}: {e}")
        return {
            "success": False,
            "status": "error",
            "error": str(e)
        }
