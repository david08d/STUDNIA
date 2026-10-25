import os
import json
import ast
from dotenv import load_dotenv

load_dotenv()

def env_str(name, default=""):
    return os.getenv(name, default).strip()

def env_int(name, default=0):
    value = env_str(name)
    return int(value) if value else default

def env_float(name, default=0.0):
    value = env_str(name)
    return float(value) if value else default

def env_bool(name, default=False):
    value = env_str(name, str(default)).lower()
    return value in ("1", "true", "yes", "on")

def env_struct(name, default):
    value = env_str(name)
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return ast.literal_eval(value)

BOT_TOKEN = env_str("BOT_TOKEN")
OWNER_ID = env_int("OWNER_ID")
SHEET_ID = env_str("SHEET_ID")

_orders_chat = env_str("ORDERS_CHAT_ID")
ORDERS_CHAT_ID = int(_orders_chat) if _orders_chat.lstrip("-").isdigit() else (_orders_chat or None)

CREDENTIALS_FILE = env_str("CREDENTIALS_FILE", "credentials.json")
STOCK_SHEET = env_str("STOCK_SHEET", "Наявність")
ORDERS_SHEET = env_str("ORDERS_SHEET", "Замовлення")

GEMINI_API_KEY = env_str("GEMINI_API_KEY")
GEMINI_MODEL = env_str("GEMINI_MODEL", "gemini-3-flash-preview")
GEMINI_SEARCH_MODEL = env_str("GEMINI_SEARCH_MODEL", GEMINI_MODEL)

SHAFA_FEE_PCT = env_float("SHAFA_FEE_PCT", 0)
SHAFA_FEE_FIXED = env_float("SHAFA_FEE_FIXED", 0)

AD_PRICE_AUTO = env_bool("AD_PRICE_AUTO", False)
AD_NOBOX_DISCOUNT = env_int("AD_NOBOX_DISCOUNT", 500)
AD_BOX_DISCOUNTS = tuple(env_struct("AD_BOX_DISCOUNTS", [100, 200]))

USE_BROWSER = env_bool("USE_BROWSER", True)
PRIORITY_SEARCH = env_struct("PRIORITY_SEARCH", {})

# Додаткові параметри, які ads.py та shafa.py можуть використовувати.
AD_MAX_CALLS_PER_DAY = env_int("AD_MAX_CALLS_PER_DAY", 150)
AD_MAX_SEARCHES_PER_DAY = env_int("AD_MAX_SEARCHES_PER_DAY", 30)
AD_MAX_OUTPUT_TOKENS = env_int("AD_MAX_OUTPUT_TOKENS", 2048)
GEMINI_FALLBACK_MODELS = env_struct("GEMINI_FALLBACK_MODELS", [])
AD_PHOTOS_DIR = env_str("AD_PHOTOS_DIR", "ads")
AD_PRICE_SEARCH = env_bool("AD_PRICE_SEARCH", True)
MARKET_CACHE_DAYS = env_int("MARKET_CACHE_DAYS", 10)
OPENSERP_URL = env_str("OPENSERP_URL", "http://127.0.0.1:7000")
PRIORITY_SITES = env_struct("PRIORITY_SITES", [])
AD_DEFAULT_MARKUP_PCT = env_float("AD_DEFAULT_MARKUP_PCT", 25)
SHAFA_ENABLED = env_bool("SHAFA_ENABLED", True)
AD_CONDITION = env_str("AD_CONDITION", "Новий")
SHAFA_FIELDS = env_struct("SHAFA_FIELDS", {})
SHAFA_ADD_URL = env_str("SHAFA_ADD_URL", "")
SHAFA_BROWSER_CHANNEL = env_str("SHAFA_BROWSER_CHANNEL", "")
