import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def _load_env_file_manually(env_path: Path) -> None:
    """Надежное чтение .env файла даже если пакет python-dotenv не установлен."""
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass


# 1. Сначала пробуем python-dotenv, затем встроенный парсер .env
try:
    from dotenv import load_dotenv
    if ENV_FILE.exists():
        load_dotenv(dotenv_path=ENV_FILE, override=False)
    else:
        load_dotenv(override=False)
except ImportError:
    pass

_load_env_file_manually(ENV_FILE)
_load_env_file_manually(Path.cwd() / ".env")

# 2. Читаем переменные из .env
VK_TOKEN = os.getenv("VK_TOKEN", "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

GEMINI_API_KEY_BACKUP = os.getenv("GEMINI_API_KEY_BACKUP", "").strip()

GEMINI_API_KEYS = list(
    dict.fromkeys([k.strip() for k in [GEMINI_API_KEY, GEMINI_API_KEY_BACKUP] if k and k.strip()])
)

VK_GROUP_ID = os.getenv("VK_GROUP_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "student_bot.sqlite"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

DEFAULT_PROXY = "http://Aa3vE2:2HhPfA@170.83.176.112:8000"
GEMINI_PROXY = (
    os.getenv("GEMINI_PROXY")
    or os.getenv("HTTPS_PROXY")
    or os.getenv("HTTP_PROXY")
    or DEFAULT_PROXY
)

# Защита от перехвата трафика VK API и ЮKassa сторонними прокси
# GEMINI_PROXY передается точечно только в запросы Gemini API (gemini_service.py)
for var in ("NO_PROXY", "no_proxy"):
    current = os.environ.get(var, "")
    bypass = "api.vk.com,vk.com,oauth.vk.com,api.yookassa.ru,yookassa.ru,127.0.0.1,localhost"
    os.environ[var] = f"{current},{bypass}" if current else bypass

GEMINI_RELAY_URL = os.getenv(
    "GEMINI_RELAY_URL",
    "https://ais-dev-43mzptri5aziktxjqjxpri-564032992741.europe-west3.run.app"
)

# 3. Настройки монетизации и ЮKassa (1 месяц бесплатно, далее 30 ₽ на 2 недели / 14 дней)
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()

FREE_TRIAL_DAYS = int(os.getenv("FREE_TRIAL_DAYS", "30"))
SUBSCRIPTION_PRICE_RUB = int(os.getenv("SUBSCRIPTION_PRICE_RUB", "30"))
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "14"))

# 4. Настройки реферальной системы (+7 дней пригласившему, +3 дня приглашенному другу после выбора ФИ и группы)
REFERRER_BONUS_DAYS = int(os.getenv("REFERRER_BONUS_DAYS", "7"))
INVITED_BONUS_DAYS = int(os.getenv("INVITED_BONUS_DAYS", "3"))

# Автоматическое создание локального .env из .env.example при первом запуске
if not ENV_FILE.exists():
    example_path = BASE_DIR / ".env.example"
    if example_path.exists():
        try:
            ENV_FILE.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass


