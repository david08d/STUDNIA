"""Модуль «Оголошення» для Shafa.

Що робить:
  1. Шукає товар на складі за артикулом або назвою, збирає розміри в наявності і найкращий закуп.
  2. Зберігає фото в папку ads/<артикул>/ (їх потім завантажуєш на Shafa).
  3. Просить Gemini заповнити поля оголошення (за фото і назвою) і шукає ринкові ціни в Google.
  4. Показує кожне поле окремим повідомленням (тап по тексту копіює його).
  5. Після вибору ціни записує її в колонку «Продаж_Ц» для всіх рядків цього артикула.

Модуль підключається до bot.py функцією init(). Власних налаштувань у коді немає,
усе береться з config.py (див. cfg(...) нижче).
"""
import asyncio
import base64
import html
import json
import logging
import os
import re
import statistics
import threading
import time
from datetime import datetime

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import settings as config

try:  # у новіших версіях бібліотеки прев'ю посилань вимикається так
    from telegram import LinkPreviewOptions
    NOPREVIEW = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
except ImportError:  # у старіших так
    NOPREVIEW = {"disable_web_page_preview": True}

B = None  # модуль bot.py, його передає init()

MARKET_SHEET = "Ринок"
MARKET_HEADERS = ["Артикул", "Медіана", "Мін", "Макс", "Кількість цін", "Лінки", "Дата"]
DATE_FMT = "%d.%m.%y %H:%M"
MAX_PHOTOS = 10          # стільки Shafa дозволяє у вільному оголошенні
PHOTOS_TO_AI = 4         # стільки фото бачить Gemini (більше не потрібно)
LETTER_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "XXXL", "3XL", "4XL"]

SHAFA_SECTIONS = ("Жіночий одяг, Дитячий одяг, Чоловічий одяг, Для дому, Краса і здоров'я, "
                  "Хобі та спорт, Дитячі товари, Аксесуари, Електроніка, Зоотовари, Дача та сад, Інтимні товари")

STYLE_EXAMPLE = """Назва: Кросівки жіночі nike air max bliss dh5128-001 сірі 38, 40
Опис:
Nike Air Max Bliss, артикул DH5128-001, колір Photon Dust/Black/Lilac (сірий)
Сезонність: демісезон / всесезон
Матеріал верху: текстиль (сітка), синтетика
Розміри:
38 EU / 7 US / 37.5 UA, устілка 24 см
40 EU / 8.5 US / 39.5 UA, устілка 25.5 см
Стан: нові
Оригінал
Без коробки
Доставка 1–3 дні"""


class AdError(Exception):
    """Помилка зі зрозумілим для людини текстом. code: HTTP-код відповіді Gemini, якщо він є."""
    code = None


def init(bot_module):
    global B
    B = bot_module


def cfg(name, default):
    return getattr(config, name, default)


def esc(s):
    return html.escape(str(s), quote=False)


# ====================================================================
#  Gemini
# ====================================================================

def has_key():
    key = str(cfg("GEMINI_API_KEY", "")).strip()
    return bool(key) and not key.upper().startswith("ВСТАВ")


def _explain_http_error(resp, model):
    try:
        msg = resp.json().get("error", {}).get("message", "") or resp.text
    except ValueError:
        msg = resp.text
    msg = (msg or "").strip().replace("\n", " ")[:220]
    code = resp.status_code
    if code == 429:
        return (f"Gemini (модель «{model}») відмовив через ліміт: запитів на хвилину, на день або на пошук. "
                "Зачекай хвилину і натисни ще раз. Денний ліміт оновлюється опівночі за тихоокеанським часом "
                "(близько 10:00 за Києвом). Свої ліміти по моделях дивись на aistudio.google.com/rate-limit")
    if code == 400 and "API key" in msg:
        return "Gemini не прийняв ключ. Перевір GEMINI_API_KEY у config.py."
    if code == 404:
        return (f"Модель «{model}» не знайдено. Змін GEMINI_MODEL (або GEMINI_SEARCH_MODEL) у config.py. "
                "Актуальні назви: ai.google.dev/gemini-api/docs/models")
    if code in (401, 403):
        return f"Ключ не має доступу до цієї моделі або функції. Відповідь Gemini: {msg}"
    return f"Gemini повернув помилку {code}: {msg}"


USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ads_usage.json")
_usage_lock = threading.Lock()


def _usage_load():
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            u = json.load(f)
    except (OSError, ValueError):
        u = {}
    if u.get("date") != today:
        u = {"date": today, "calls": 0, "searches": 0}
    return u


def usage_take(search):
    """Рахує запити до Gemini за добу і зупиняє бота, якщо перевищено ліміти з config.py.
    Це захист від випадкових витрат: AD_MAX_CALLS_PER_DAY (усі запити) і AD_MAX_SEARCHES_PER_DAY (пошуки цін)."""
    max_calls = int(cfg("AD_MAX_CALLS_PER_DAY", 150))
    max_search = int(cfg("AD_MAX_SEARCHES_PER_DAY", 30))
    with _usage_lock:
        u = _usage_load()
        if u["calls"] >= max_calls:
            raise AdError(f"Досягнуто денний ліміт запитів до Gemini ({max_calls}). "
                          "Він захищає від зайвих витрат. Змінити: AD_MAX_CALLS_PER_DAY у config.py.")
        if search and u["searches"] >= max_search:
            raise AdError(f"Досягнуто денний ліміт пошуків цін ({max_search}). "
                          "Він захищає від зайвих витрат. Змінити: AD_MAX_SEARCHES_PER_DAY у config.py.")
        u["calls"] += 1
        if search:
            u["searches"] += 1
        try:
            with open(USAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(u, f)
        except OSError:
            pass


def usage_today():
    with _usage_lock:
        return _usage_load()


def _retry_delay(resp):
    """Скільки секунд Gemini просить зачекати після помилки 429 (або None)."""
    try:
        for d in resp.json().get("error", {}).get("details", []):
            if str(d.get("@type", "")).endswith("RetryInfo"):
                return float(str(d.get("retryDelay", "")).rstrip("s"))
    except (ValueError, AttributeError, TypeError):
        pass
    return None


def gemini_call(model, parts, json_mode=False, search=False, wait_on_429=True):
    """Один запит до Gemini через REST. Повертає розібрану відповідь (dict)."""
    if not has_key():
        raise AdError("Немає ключа Gemini. Додай у config.py рядок GEMINI_API_KEY = \"...\" "
                      "(ключ береться безкоштовно на aistudio.google.com/apikey).")
    usage_take(search)
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.3,
                                 "maxOutputTokens": int(cfg("AD_MAX_OUTPUT_TOKENS", 8192))}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    if search:
        body["tools"] = [{"google_search": {}}]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": str(cfg("GEMINI_API_KEY", "")).strip()}

    resp = None
    for attempt in range(2):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=120)
        except requests.RequestException as e:
            raise AdError(f"Немає зв'язку з Gemini: {e}")
        if resp.status_code in (500, 503) and attempt == 0:
            time.sleep(6)   # сервер зайнятий, пробуємо ще раз
            continue
        if resp.status_code == 429 and attempt == 0 and wait_on_429:
            wait = _retry_delay(resp)
            if wait is not None and wait <= 40:   # ліміт «на хвилину»: чекаємо і повторюємо
                time.sleep(wait + 1)
                continue
        break
    if resp.status_code != 200:
        logging.warning("Gemini %s -> %s: %s", model, resp.status_code, resp.text[:1500].replace("\n", " "))
        err = AdError(_explain_http_error(resp, model))
        err.code = resp.status_code
        raise err
    try:
        return resp.json()
    except ValueError:
        raise AdError("Gemini повернув не JSON. Спробуй ще раз.")


def model_chain(key, default):
    """Основна модель і запасні. Ліміти Gemini рахуються окремо для кожної моделі."""
    first = str(cfg(key, default)).strip()
    extra = cfg("GEMINI_FALLBACK_MODELS", [])   # за замовчуванням порожньо: моделі не міняються самі
    return [first] + [m for m in extra if m and m != first]


def gemini_try(models, parts, **kw):
    """Пробує моделі по черзі, якщо попередня не знайдена (404) або впёрлась у ліміт (429)."""
    last = None
    for i, m in enumerate(models):
        try:
            return gemini_call(m, parts, wait_on_429=(i == len(models) - 1), **kw)
        except AdError as e:
            last = e
            if e.code not in (404, 429):
                raise
    raise last


def reply_text(data):
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise AdError("Gemini не дав відповіді" + (f" (причина: {reason})." if reason else "."))
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise AdError("Gemini повернув порожню відповідь. Спробуй ще раз.")
    return text


def parse_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except ValueError:
                pass
    raise AdError("Gemini відповів не в тому форматі. Натисни «Спробувати ще».")


# ====================================================================
#  Товар зі складу
# ====================================================================

def sort_sizes(sizes):
    def key(s):
        try:
            return (0, float(s.replace(",", ".")), "")
        except ValueError:
            up = s.upper()
            return (1, LETTER_ORDER.index(up) if up in LETTER_ORDER else 99, up)
    return sorted(sizes, key=key)


def find_candidates(query):
    """Групи товарів зі складу, які збігаються із запитом. Тільки те, що є в наявності."""
    items, _ = B.load_stock()
    q = B.norm_article(query)
    words = query.lower().split()
    groups, out_of_stock = {}, set()
    for it in items:
        art = B.norm_article(it["article"])
        if not art:
            continue
        by_article = len(art) >= 4 and (q == art or q in art or art in q)
        hay = (it["model"] + " " + it["article"]).lower()
        by_name = bool(words) and all(w in hay for w in words)
        if not (by_article or by_name):
            continue
        if it["qty"] > 0:
            g = groups.setdefault(art, {"article": it["article"], "model": "", "rows": []})
            g["rows"].append(it)
            if not g["model"] and it["model"]:
                g["model"] = it["model"]
        else:
            out_of_stock.add(art)
    found = list(groups.values())
    for g in found:
        out_of_stock.discard(B.norm_article(g["article"]))
    return found, bool(out_of_stock)


def build_product(group):
    rows = group["rows"]
    sizes = sort_sizes(list({B.norm_size(r["size"]) for r in rows if r["size"].strip()}))
    buys = [B.money(r["buy"]) for r in rows if B.money(r["buy"])]
    return {
        "article": group["article"],
        "model": group["model"],
        "sizes": sizes,
        "buy_min": min(buys) if buys else 0,
        "buy_max": max(buys) if buys else 0,
    }


def folder_for(article):
    base = cfg("AD_PHOTOS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ads"))
    return os.path.join(base, re.sub(r"[^\w\-]+", "_", article).strip("_") or "bez_artykula")


# ====================================================================
#  Поля оголошення від Gemini
# ====================================================================

def listing_prompt(p, box, n_photos):
    photo_line = (f"До повідомлення додано фото товару ({n_photos} шт). Колір і матеріал бери з фото."
                  if n_photos else "Фото немає, тому опирайся на назву й артикул.")
    return f"""Ти помічник продавця кросівок та одягу на українському маркетплейсі Shafa. Підготуй поля оголошення українською мовою.

Товар зі складу: «{p['model'] or 'назва невідома'}», артикул {p['article']}.
Розміри в наявності: {', '.join(p['sizes']) or 'не вказані'}.
{photo_line}

Правила:
- Нічого не вигадуй. Якщо не певен у полі, залиш "" і додай назву поля в "uncertain".
- brand: бренд латиницею (Nike, Adidas, New Balance). model_name: тільки назва моделі латиницею, без бренду й артикула.
- type_gender: тип товару і стать у правильному роді та числі, з великої літери (приклади: "Кросівки жіночі", "Футболка чоловіча", "Куртка жіноча"). Стать визнач за назвою, артикулом і фото. Якщо не зрозуміло, пиши "унісекс".
- color_title: колір одним словом, узгоджений з type_gender (приклади: "сірі", "білі", "чорна"). color_ua: колір українською в називному відмінку чоловічого роду (приклад: "сірий"). color_official: офіційна назва кольору від бренду, якщо знаєш (приклад: "Photon Dust/Black/Lilac"), інакше "".
- season: приклад "демісезон / всесезон". material: матеріал верху (для одягу просто матеріал), приклад "текстиль (сітка), синтетика". membrane: "Немає" або "Є (назва)".
- kind: "shoes" для взуття, "clothes" для одягу, "accessory" для аксесуарів, інакше "other".
- category_path: імовірний шлях розділу на Shafa. Перший пункт обери з: {SHAFA_SECTIONS}. Наприклад: ["Жіночий одяг", "Взуття", "Кросівки"].
- sizes: для взуття поверни по одному запису на кожен розмір з наявних: eu (як у списку), us (за розмірною сіткою цього бренду для цієї статі), cm (довжина устілки в сантиметрах). Для одягу та аксесуарів sizes = [].
- keywords: 6-10 ключових слів для пошуку на Shafa, українською і латиницею, без повторів (приклад: "кросівки жіночі", "nike air max", "dh5128-001").

Приклад хорошого оголошення, на який треба рівнятися за змістом (форму збере програма, ти лише заповнюєш поля):
{STYLE_EXAMPLE}

Поверни ТІЛЬКИ один JSON без пояснень і без markdown, у такій формі:
{{"brand": "", "model_name": "", "kind": "", "type_gender": "", "gender": "", "category_path": ["", "", ""], "color_title": "", "color_ua": "", "color_official": "", "season": "", "material": "", "membrane": "", "sizes": [{{"eu": "", "us": "", "cm": ""}}], "keywords": [""], "uncertain": [""]}}"""


def _s(v):
    return str(v).strip() if v is not None else ""


def normalize_ai(raw):
    if not isinstance(raw, dict):
        raise AdError("Gemini відповів не в тому форматі. Натисни «Спробувати ще».")
    sizes = {}
    for it in raw.get("sizes") or []:
        if isinstance(it, dict) and _s(it.get("eu")):
            sizes[B.norm_size(_s(it["eu"]))] = {"us": _s(it.get("us")), "cm": _s(it.get("cm"))}
    kw = []
    for k in raw.get("keywords") or []:
        k = _s(k).lower()
        if k and k not in kw:
            kw.append(k)
    path = [_s(x) for x in (raw.get("category_path") or []) if _s(x)]
    return {
        "brand": _s(raw.get("brand")), "model_name": _s(raw.get("model_name")),
        "kind": _s(raw.get("kind")).lower(), "type_gender": _s(raw.get("type_gender")),
        "color_title": _s(raw.get("color_title")), "color_ua": _s(raw.get("color_ua")),
        "color_official": _s(raw.get("color_official")), "season": _s(raw.get("season")),
        "material": _s(raw.get("material")), "membrane": _s(raw.get("membrane")),
        "path": path, "sizes": sizes, "keywords": kw[:10],
        "uncertain": [_s(x) for x in (raw.get("uncertain") or []) if _s(x)],
    }


def is_shoes(p, ai):
    if ai["kind"]:
        return ai["kind"] == "shoes"
    nums = []
    for s in p["sizes"]:
        try:
            nums.append(float(s))
        except ValueError:
            return False
    return bool(nums) and all(34 <= n <= 50 for n in nums)


def ua_size(eu):
    """Український розмір = EU мінус 0.5 (так у твоїх оголошеннях: 38 EU це 37.5 UA)."""
    try:
        v = float(eu.replace(",", "."))
    except ValueError:
        return ""
    return f"{v - 0.5:g}" if 34 <= v <= 50 else ""


def size_lines(p, ai, shoes):
    if not shoes:
        return ["Розміри: " + ", ".join(p["sizes"])] if p["sizes"] else []
    lines = ["Розміри:"]
    for eu in p["sizes"]:
        info = ai["sizes"].get(eu, {})
        parts = [f"{eu} EU"]
        if info.get("us"):
            parts.append(f"{info['us']} US")
        ua = ua_size(eu)
        if ua:
            parts.append(f"{ua} UA")
        line = " / ".join(parts)
        if info.get("cm"):
            line += f", устілка {info['cm']} см"
        lines.append(line)
    return lines


def build_title(p, ai):
    base = ai["type_gender"] or "Товар"
    base = base[0].upper() + base[1:]
    tail = " ".join(x for x in (ai["brand"], ai["model_name"], p["article"], ai["color_title"]) if x).lower()
    sizes = ", ".join(p["sizes"])
    title = " ".join(x for x in (base, tail, sizes) if x)
    if len(title) > 100:   # довгі заголовки Shafa можуть не прийняти, тому прибираємо розміри
        title = " ".join(x for x in (base, tail) if x)
    return title


def build_description(p, ai, box, shoes):
    head = " ".join(x for x in (ai["brand"], ai["model_name"]) if x) or p["model"]
    head += f", артикул {p['article']}"
    if ai["color_official"] and ai["color_ua"]:
        head += f", колір {ai['color_official']} ({ai['color_ua']})"
    elif ai["color_ua"] or ai["color_official"]:
        head += f", колір {ai['color_ua'] or ai['color_official']}"
    lines = [head]
    if ai["season"]:
        lines.append(f"Сезонність: {ai['season']}")
    if ai["material"]:
        lines.append(("Матеріал верху: " if shoes else "Матеріал: ") + ai["material"])
    lines += size_lines(p, ai, shoes)
    lines.append("Стан: нові")
    original = cfg("AD_ORIGINAL_LINE", "Оригінал")
    if original:
        lines.append(original)
    if box == "with":
        lines.append("З коробкою")
    elif box == "without":
        lines.append("Без коробки")
    delivery = cfg("AD_DELIVERY_LINE", "Доставка 1–3 дні")
    if delivery:
        lines.append(delivery)
    return "\n".join(lines)


def build_info(p, ai, shoes):
    cond = cfg("AD_CONDITION", "Новий")
    lines = []
    if ai["path"]:
        lines.append("Розділ: " + " → ".join(ai["path"]))
    lines.append(f"Стан: {cond}")
    if ai["brand"]:
        lines.append(f"Бренд: {ai['brand']}")
    lines.append("")
    lines.append("Характеристики у формі:")
    if ai["color_ua"]:
        lines.append("Колір: " + ai["color_ua"].capitalize())
    if ai["material"]:
        lines.append("Матеріал: " + ai["material"])
    if ai["season"]:
        lines.append("Сезон: " + ai["season"])
    if ai["membrane"]:
        lines.append("Мембрана: " + ai["membrane"])
    if shoes:
        cms = [ai["sizes"].get(eu, {}).get("cm", "") for eu in p["sizes"]]
        cms = [c for c in cms if c]
        if cms:
            lines.append("Довжина устілки: " + ", ".join(cms))
    if p["sizes"]:
        lines.append("Розмір: " + ", ".join(p["sizes"]))
    return "\n".join(lines)


def make_listing(p, box, photo_paths):
    """Запит до Gemini з фото. Повертає готові тексти полів."""
    parts = [{"text": listing_prompt(p, box, len(photo_paths[:PHOTOS_TO_AI]))}]
    for path in photo_paths[:PHOTOS_TO_AI]:
        with open(path, "rb") as f:
            parts.append({"inline_data": {"mime_type": "image/jpeg",
                                          "data": base64.b64encode(f.read()).decode()}})
    data = gemini_try(model_chain("GEMINI_MODEL", "gemini-3.8-flash"), parts, json_mode=True)
    ai = normalize_ai(parse_json(reply_text(data)))
    shoes = is_shoes(p, ai)
    notes = []
    if ai["uncertain"]:
        notes.append("Gemini не певен у полях: " + ", ".join(ai["uncertain"]))
    if shoes and ai["sizes"]:
        notes.append("US і довжину устілки Gemini бере з пам'яті. Звір їх із розмірною таблицею бренду")
    return {
        "title": build_title(p, ai),
        "description": build_description(p, ai, box, shoes),
        "keywords": ", ".join(ai["keywords"]),
        "info": build_info(p, ai, shoes),
        "notes": notes,
        "brand": ai["brand"],
        "ai": ai,
        "shoes": shoes,
    }


# ====================================================================
#  Ціни магазинів (OpenSERP + читання сторінок) і кеш у листі «Ринок»
# ====================================================================

def summarize_prices(nums):
    nums = [n for n in nums if 200 <= n <= 200000]
    if len(nums) >= 3:
        med = statistics.median(nums)
        nums = [n for n in nums if 0.4 * med <= n <= 2.5 * med]   # прибираємо явні викиди
    elif len(nums) == 2 and min(nums) < 0.4 * max(nums):
        nums = [max(nums)]          # дві ціни, одна менш ніж 40% від іншої: схоже на помилку читання
    if not nums:
        return None
    return {"median": int(round(statistics.median(nums))), "min": min(nums),
            "max": max(nums), "count": len(nums)}


def _cell(v):
    if isinstance(v, bool):
        return {"userEnteredValue": {"stringValue": str(v)}}
    if isinstance(v, (int, float)):
        return {"userEnteredValue": {"numberValue": v}}
    return {"userEnteredValue": {"stringValue": str(v)}}


def _market_sheet():
    book = B.get_book()
    try:
        return book.worksheet(MARKET_SHEET)
    except B.gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=MARKET_SHEET, rows=500, cols=len(MARKET_HEADERS))
        ws.append_row(MARKET_HEADERS, value_input_option="RAW")
        return ws


def read_market_cache(article):
    ws = _market_sheet()
    values = ws.get_all_values()
    key = B.norm_article(article)
    for n, row in enumerate(values[1:], start=2):
        if row and B.norm_article(row[0]) == key:
            row = row + [""] * (len(MARKET_HEADERS) - len(row))
            try:
                when = datetime.strptime(row[6], DATE_FMT)
            except ValueError:
                when = None
            links = []
            for line in row[5].split("\n"):
                if "\t" in line:
                    t, u = line.split("\t", 1)
                    links.append((t, u))
            return {"row": n, "median": B.to_int(row[1]), "min": B.to_int(row[2]),
                    "max": B.to_int(row[3]), "count": B.to_int(row[4]),
                    "links": links, "date": row[6], "when": when, "note": "", "cached": True}
    return None


def save_market(article, m, row=None):
    ws = _market_sheet()
    links = "\n".join(f"{t}\t{u}" for t, u in m["links"])
    vals = [article, m["median"], m["min"], m["max"], m["count"], links, m["date"]]
    if row:
        ws.spreadsheet.batch_update({"requests": [{"updateCells": {
            "range": {"sheetId": ws.id, "startRowIndex": row - 1, "endRowIndex": row,
                      "startColumnIndex": 0, "endColumnIndex": len(vals)},
            "rows": [{"values": [_cell(v) for v in vals]}],
            "fields": "userEnteredValue"}}]})
    else:
        ws.append_row(vals, value_input_option="RAW")


def get_market(p, force=False, log=None):
    """Ціни в магазинах: з кешу, якщо свіжі, інакше новий пошук. None, якщо пошук вимкнено."""
    if not cfg("AD_PRICE_SEARCH", True):
        return None
        
    try:
        cached = read_market_cache(p["article"])
    except Exception:
        logging.warning("кеш цін недоступний", exc_info=True)
        cached = None
        
    days = cfg("MARKET_CACHE_DAYS", 10)
    if cached and not force and cached["when"] and (datetime.now() - cached["when"]).days < days:
        return cached

    import market

    def _log(s):                    # у термінал і, якщо треба, у показ прогресу в Telegram
        logging.getLogger("ads").info("ціни: %s", s)
        if log:
            log(s)
    try:
        res = market.find_prices(cfg("OPENSERP_URL", "http://127.0.0.1:7000"), p["article"], model=p.get("model") or "", use_browser=cfg("AD_PRICE_BROWSER", None),
                                 priority=cfg("PRIORITY_SITES", market.PRIORITY_SITES),
                                 log=_log)
    except market.SearchError as e:
        raise AdError(str(e))
    summary = summarize_prices(res["prices"])
    if not summary:
        return {"empty": True, "note": res["note"] or "Цін на цей артикул не знайшов.", "links": []}
    m = dict(summary, links=res["links"], note=res["note"], cached=False,
             date=datetime.now().strftime(DATE_FMT))
             
    try:
        save_market(p["article"], m, row=cached["row"] if cached else None)
    except Exception:
        logging.warning("не вдалося зберегти кеш цін", exc_info=True)
        
    return m


# ====================================================================
#  Ціна й запис у «Продаж_Ц»
# ====================================================================

def round_price(x, step=10):
    return int(round(x / step) * step)


def fee_of(price):
    """Комісія Shafa: SHAFA_FEE_PCT (у відсотках від ціни) плюс SHAFA_FEE_FIXED (у гривнях)."""
    return price * float(cfg("SHAFA_FEE_PCT", 0)) / 100 + float(cfg("SHAFA_FEE_FIXED", 0))


def margin_of(price, buy):
    m = round(price - fee_of(price) - buy)
    pct = round(m / buy * 100) if buy else 0
    return m, pct


def price_options(p, market):
    opts, seen = [], set()

    def add(label, price):
        if price > 0 and price not in seen:
            seen.add(price)
            opts.append((label, price))
    if market and market.get("median"):
        med, box = market["median"], p.get("box")
        if box == "without":        # без коробки: на 500₴ дешевше, ніж у магазинах
            cut = int(cfg("AD_NOBOX_DISCOUNT", 500))
            add(f"без коробки −{cut}", round_price(med - cut))
        elif box == "with":         # з коробкою: на 100-200₴ дешевше
            lo, hi = cfg("AD_BOX_DISCOUNTS", (100, 200))
            add(f"з коробкою −{lo}", round_price(med - lo))
            add(f"з коробкою −{hi}", round_price(med - hi))
        else:
            add("−5% до магазинів", round_price(med * 0.95))
        add("як у магазинах", round_price(med))
    if p["buy_min"]:
        mk = cfg("AD_DEFAULT_MARKUP_PCT", 25)
        add(f"закуп +{mk}%", round_price(p["buy_min"] * (1 + mk / 100)))
    return opts


def fmt_uah(n):
    return f"{int(n):,}".replace(",", "\u00a0") + " ₴"


def _stores(market):
    """[(магазин, ціна, посилання)] за зростанням ціни; з міток виду «shop.ua · 3429₴»."""
    out = []
    for label, url in market.get("links") or []:
        m = re.match(r"^(.*?)\s*·\s*(\d[\d\s]*)₴\s*$", label)
        if m:
            out.append((m.group(1).strip(), int(re.sub(r"\D", "", m.group(2))), url))
    return sorted(out, key=lambda x: x[1])


def price_text(p, market, market_err):
    lines = ["💰 <b>Ціна</b>"]
    if p["buy_min"]:
        extra = f" (в інших постачальників до {p['buy_max']}₴)" if p["buy_max"] > p["buy_min"] else ""
        lines.append(f"Закуп: {p['buy_min']}₴{extra}")
    if market and market.get("median"):
        cache = f", перевірено {market['date']}" if market.get("cached") else ""
        lines += ["", f"🏬 <b>Ціни в магазинах</b> ({market['count']} шт.{cache})"]
        for site, price, url in _stores(market):
            counted = market["min"] <= price <= market["max"]
            mark = "" if counted else " <i>(не враховано: схоже на помилку)</i>"
            name = f'<a href="{html.escape(url, quote=True)}">{esc(site)}</a>' if url else esc(site)
            lines.append(f"• {name} — <b>{fmt_uah(price)}</b>{mark}")
        lines.append(f"Медіана <b>{fmt_uah(market['median'])}</b> · від {fmt_uah(market['min'])} "
                     f"до {fmt_uah(market['max'])}")
        note = market.get("note") or ""
        if note and not note.startswith("Ціни з"):      # загальне «ціни з N магазинів» уже видно зі списку
            lines.append("⚠️ " + esc(note))
        lines.append("")
    elif market and market.get("empty"):
        lines.append("Магазини: " + esc(market["note"]))
    elif market_err:
        lines.append("Магазини: " + esc(market_err))
    elif not cfg("AD_PRICE_SEARCH", True):
        lines.append("Ціни магазинів: пошук вимкнено в config.py")
    else:
        lines.append("Ціни магазинів: ще не шукав. Натисни «🔎 Знайти ціни».")
    try:
        u = usage_today()
        lines.append(f"<i>Запитів до Gemini сьогодні: {u['calls']}</i>")
    except Exception:
        pass
    pct, fixed = float(cfg("SHAFA_FEE_PCT", 0)), float(cfg("SHAFA_FEE_FIXED", 0))
    if pct > 30:
        lines.append(f"⚠️ У config.py SHAFA_FEE_PCT = {pct:g}%. Це відсотки, а не гривні, маржа порахована хибно. "
                     "Фіксовану комісію в гривнях став у SHAFA_FEE_FIXED.")
    elif not pct and not fixed:
        lines.append("Маржа нижче без комісії Shafa. Додай у config.py SHAFA_FEE_PCT (у %) "
                     "або SHAFA_FEE_FIXED (у ₴).")
    lines.append("Ціни магазинів це орієнтир, а не точна цифра. Обери ціну або впиши свою:")
    return "\n".join(lines)


def price_keyboard(p, market):
    rows = []
    for label, price in price_options(p, market):
        if p["buy_min"]:
            m, pct = margin_of(price, p["buy_min"])
            sign = "+" if m >= 0 else ""
            text = f"{price}₴ ({sign}{m}₴, {pct}%) · {label}"
        else:
            text = f"{price}₴ · {label}"
        rows.append([InlineKeyboardButton(text, callback_data=f"ad:price:{price}")])
    rows.append([InlineKeyboardButton("✏️ Своя ціна", callback_data="ad:custom"),
                 InlineKeyboardButton("🔄 Оновити ціни" if (market and market.get("median")) else "🔎 Знайти ціни",
                                      callback_data="ad:refresh")])
    rows.append([B.CANCEL_BTN])
    return InlineKeyboardMarkup(rows)


def write_sell_price(article, price):
    """Ставить ціну в «Продаж_Ц» для всіх рядків артикула. Інші клітинки не чіпає."""
    ws = B.get_book().worksheet(config.STOCK_SHEET)
    items, cols = B.load_stock()
    if "sell" not in cols:
        raise AdError(f"У листі «{config.STOCK_SHEET}» немає колонки «Продаж_Ц». Додай її в заголовки.")
    key = B.norm_article(article)
    rows = [it["row"] for it in items if B.norm_article(it["article"]) == key]
    if not rows:
        raise AdError("Не знайшов рядків цього артикула у складі.")
    col = cols["sell"]
    requests_ = [{"updateCells": {
        "range": {"sheetId": ws.id, "startRowIndex": r - 1, "endRowIndex": r,
                  "startColumnIndex": col, "endColumnIndex": col + 1},
        "rows": [{"values": [{"userEnteredValue": {"numberValue": price}}]}],
        "fields": "userEnteredValue"}} for r in rows]
    ws.spreadsheet.batch_update({"requests": requests_})
    return len(rows)


# ====================================================================
#  Telegram: кроки розмови
# ====================================================================

def _state(context):
    return context.user_data.setdefault("ad", {})


def _kb(*rows):
    return InlineKeyboardMarkup([list(r) for r in rows])


async def start(update, context):
    """Кнопка «🛍 Оголошення»."""
    chat_id = update.effective_chat.id
    context.user_data["ad"] = {}
    if not has_key():
        await B.say(context, chat_id,
                    "Щоб робити оголошення, потрібен ключ Gemini.\n\n"
                    "1. Відкрий aistudio.google.com/apikey і створи ключ.\n"
                    "2. У config.py додай рядок: GEMINI_API_KEY = \"твій ключ\"\n"
                    "3. Перезапусти бота.", kind="flash", seconds=30)
        return
    context.user_data["mode"] = "ad_article"
    await B.say(context, chat_id,
                "🛍 Оголошення\nНапиши артикул або назву товару зі складу.\nНаприклад: DH5128-001",
                reply_markup=B.CANCEL_KB)


async def on_text(update, context, text):
    chat_id = update.effective_chat.id
    mode = context.user_data.get("mode")
    B.track(context, update.message)       # твоє повідомлення зникне разом із кроком
    if mode == "ad_article":
        groups, only_zero = await asyncio.to_thread(find_candidates, text)
        if not groups:
            msg = ("Цей товар є в таблиці, але кількість 0. Оголошення робимо тільки для того, що є в наявності."
                   if only_zero else "Не знайшов такого товару на складі. Спробуй інший артикул або назву.")
            await B.say(context, chat_id, msg, kind="flash", seconds=8)
            return
        if len(groups) == 1:
            await _choose(context, chat_id, groups[0])
            return
        ad = _state(context)
        ad["cands"] = groups[:8]
        rows = [[InlineKeyboardButton(f"{g['model'] or '—'} {g['article']}"[:60], callback_data=f"ad:pick:{i}")]
                for i, g in enumerate(ad["cands"])]
        rows.append([B.CANCEL_BTN])
        await B.say(context, chat_id, "Знайшов кілька товарів. Обери потрібний:",
                    reply_markup=InlineKeyboardMarkup(rows))
    elif mode == "ad_price":
        try:
            price = round(float(re.sub(r"[^\d.]", "", text.replace(",", ".").replace(" ", ""))))
        except ValueError:
            price = 0
            
        if not 100 <= price <= 500000:
            await B.say(context, chat_id, "Напиши ціну числом від 100 до 500000, наприклад 3300", kind="flash", seconds=6)
            return
        await _set_price(context, chat_id, price)
    else:
        await B.say(context, chat_id, "Зараз чекаю фото або натискання кнопки під повідомленням.",
                    kind="flash", seconds=6)


async def _choose(context, chat_id, group):
    ad = _state(context)
    p = build_product(group)
    ad["product"] = p
    ad["photos"] = []
    ad["status_id"] = None
    await B.clear(context, "tmp")
    buy = f"\nЗакуп: від {p['buy_min']}₴" if p["buy_min"] else ""
    await B.say(context, chat_id,
                f"✅ {esc(p['model'] or '—')} {esc(p['article'])}\n"
                f"Розміри в наявності: {esc(', '.join(p['sizes']) or '—')}{buy}\n\n"
                "Коробка у товару є?",
                parse_mode="HTML",
                reply_markup=_kb([InlineKeyboardButton("📦 З коробкою", callback_data="ad:box:with"),
                                  InlineKeyboardButton("Без коробки", callback_data="ad:box:without")],
                                 [InlineKeyboardButton("Не вказувати", callback_data="ad:box:skip")],
                                 [B.CANCEL_BTN]))


async def on_photo(update, context):
    chat_id = update.effective_chat.id
    if context.user_data.get("mode") != "ad_photos":
        await B.say(context, chat_id, "Фото приймаю після кнопки «🛍 Оголошення».", kind="flash", seconds=5)
        return
    ad = _state(context)
    if len(ad["photos"]) >= MAX_PHOTOS:
        await B.safe_delete(context.bot, chat_id, update.message.message_id)
        return
    folder = folder_for(ad["product"]["article"])
    os.makedirs(folder, exist_ok=True)
    used = [int(m.group(1)) for f in os.listdir(folder) if (m := re.match(r"(\d+)\.jpg$", f))]
    path = os.path.join(folder, f"{(max(used) + 1) if used else 1:02d}.jpg")
    tg_file = await update.message.photo[-1].get_file()
    await tg_file.download_to_drive(path)
    ad["photos"].append(path)
    await B.safe_delete(context.bot, chat_id, update.message.message_id)   # фото вже на диску
    task = ad.get("debounce")
    if task:
        task.cancel()
    ad["debounce"] = asyncio.create_task(_photo_status(context, chat_id, ad))


async def _photo_status(context, chat_id, ad):
    """Альбом приходить кількома повідомленнями, тому оновлюємо лічильник із невеликою паузою."""
    try:
        await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        return
    if context.user_data.get("ad") is not ad:
        return
    text = (f"📷 Фото збережено: {len(ad['photos'])} з {MAX_PHOTOS}.\n"
            "Можеш докинути ще або натиснути «Готово».")
    kb = _kb([InlineKeyboardButton("✅ Готово, генерувати", callback_data="ad:go")], [B.CANCEL_BTN])
    if ad.get("status_id"):
        try:
            await context.bot.edit_message_text(text, chat_id, ad["status_id"], reply_markup=kb)
            return
        except Exception:
            pass
    m = await B.say(context, chat_id, text, reply_markup=kb)
    ad["status_id"] = m.message_id


async def on_button(update, context, parts):
    q = update.callback_query
    chat_id = q.message.chat_id
    op = parts[1] if len(parts) > 1 else ""
    await B.safe_answer(q, "⏳ Ще шукаю, зачекай трохи." if op == "busy" else None)
    if op == "busy":
        return
    arg = parts[2] if len(parts) > 2 else ""
    ad = context.user_data.get("ad")
    if not ad:
        await B.safe_delete(context.bot, chat_id, q.message.message_id)
        await B.say(context, chat_id, "Ця дія застаріла. Натисни «🛍 Оголошення» ще раз.",
                    kind="flash", seconds=6)
        return

    if op == "pick":
        try:
            group = ad["cands"][int(arg)]
        except (ValueError, IndexError, KeyError):
            await B.say(context, chat_id, "Список застарів. Почни ще раз.", kind="flash", seconds=5)
            return
        await _choose(context, chat_id, group)

    elif op == "box":
        ad["box"] = arg
        context.user_data["mode"] = "ad_photos"
        await B.clear(context, "tmp")
        await B.say(context, chat_id,
                    "📷 Кинь фото товару (до 10, можна одним альбомом).\n"
                    "Головне фото першим. Gemini за ними визначить колір і матеріал.",
                    reply_markup=_kb([InlineKeyboardButton("⏭ Без фото", callback_data="ad:go")],
                                     [B.CANCEL_BTN]))

    elif op == "go":
        if ad.get("busy"):
            return
        ad["busy"] = True
        B.spawn(_generate_job(context, chat_id, ad))

    elif op == "price":
        await _set_price(context, chat_id, int(arg))

    elif op == "custom":
        context.user_data["mode"] = "ad_price"
        await B.say(context, chat_id, "Напиши ціну продажу числом, наприклад 3300",
                    reply_markup=B.CANCEL_KB)

    elif op == "refresh":
        await _refresh_market(context, chat_id, q)

    elif op == "shafa":
        await _shafa_start(context, chat_id, ad)


def _progress_text(header, st, hint="Зазвичай це 1-3 хвилини. Повторно натискати не треба."):
    sec = int(time.time() - st["t0"])
    lines = [header, f"⏱ {sec // 60}:{sec % 60:02d}"]
    found, last = 0, ""
    for s in st["log"]:
        if re.search(r"сторінка \S+: \d+₴", s):
            found += 1
    for s in reversed(st["log"]):
        m = re.search(r"сторінка (\S+): (\d+)₴", s)
        if m:
            last = f"✅ {m.group(1)}: {m.group(2)} ₴"
        elif re.search(r"сторінка (\S+): НЕ ПРОЧИТАВ", s):
            last = "⚠️ " + re.search(r"сторінка (\S+): НЕ ПРОЧИТАВ", s).group(1) + ": не вдалося прочитати"
        elif re.search(r"^(\S+): ввів у пошук", s):
            last = "🔎 Шукаю в магазині " + re.search(r"^(\S+): ввів у пошук", s).group(1)
        elif s.startswith("Загальний пошук"):
            last = "🔎 Загальний пошук готовий, читаю сторінки"
        else:
            last = s.strip()[:70]
        break
    if last:
        lines.append(last)
    if st["log"]:
        lines.append(f"Знайдено цін: {found}")
    lines.append(hint)
    return "\n".join(lines)


class _Ticker:
    """Показує, що бот працює: таймер і останній крок, оновлюється кожні кілька секунд."""
    EVERY = 3

    def __init__(self, context, chat_id, msg_id, header,
                 hint="Зазвичай це 1-3 хвилини. Повторно натискати не треба."):
        self.context, self.chat_id, self.msg_id, self.header, self.hint = context, chat_id, msg_id, header, hint
        self.st = {"t0": time.time(), "log": []}

    def log(self, s):              # викликається з робочого потоку
        self.st["log"].append(str(s))

    async def _run(self):
        while True:
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.EVERY)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.context.bot.edit_message_text(_progress_text(self.header, self.st, self.hint),
                                                         chat_id=self.chat_id, message_id=self.msg_id)
            except Exception:
                pass               # «не змінилось» або тимчасовий збій: нічого страшного

    async def __aenter__(self):
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc):
        self.stop.set()
        await self.task


async def _generate_job(context, chat_id, ad):
    """Генерація у фоні, щоб бот не «замерзав» і відповідав на кнопки."""
    try:
        await _generate(context, chat_id)
    except Exception as e:
        logging.exception("generate error")
        await B.say(context, chat_id, f"Помилка: {str(e)[:300]}", kind="flash", seconds=20)
    finally:
        ad["busy"] = False


async def _generate(context, chat_id):
    ad = _state(context)
    p = ad["product"]
    await B.clear(context, "tmp")
    p["box"] = ad.get("box")        # від коробки залежить пропонована ціна
    ad["status_id"] = None
    context.user_data["mode"] = "ad_photos"     # щоб «Спробувати ще» працювало після помилки
    auto = bool(cfg("AD_PRICE_AUTO", True))
    header = "⏳ Готую оголошення" + (" і шукаю ціни" if auto else " (10-40 секунд)")
    wait = await B.say(context, chat_id, header + "\n⏱ 0:00")

    hint = ("Разом із пошуком цін це 1-3 хвилини. Повторно натискати не треба." if auto
            else "Повторно натискати не треба.")
    async with _Ticker(context, chat_id, wait.message_id, header, hint) as tk:
        async def market_job():
            if not auto:      # ринок тільки за кнопкою
                return None, ""
            try:
                return await asyncio.to_thread(get_market, p, False, tk.log), ""
            except AdError as e:
                return None, str(e)
            except Exception as e:
                logging.exception("market error")
                return None, f"Ціни не вдалося отримати ({type(e).__name__}). Натисни «Знайти ціни» ще раз."

        async def listing_job():
            try:
                return await asyncio.to_thread(make_listing, p, ad.get("box"), ad.get("photos", [])), ""
            except AdError as e:
                return None, str(e)

        (market, market_err), (listing, listing_err) = await asyncio.gather(market_job(), listing_job())
    await B.clear(context, "tmp")
    if context.user_data.get("ad") is not ad:    # поки йшла робота, дію скасували
        return

    if not listing:
        await B.say(context, chat_id, f"❌ {listing_err}",
                    reply_markup=_kb([InlineKeyboardButton("🔁 Спробувати ще", callback_data="ad:go")],
                                     [B.CANCEL_BTN]))
        return

    ad["market"], ad["market_err"] = market, market_err
    ad["listing"] = listing
    ad.pop("price", None)
    context.user_data.pop("mode", None)    # далі текст знову працює як звичайний пошук
    parts = [f"<b>Назва</b>\n<code>{esc(listing['title'])}</code>",
             f"<b>Опис</b>\n<code>{esc(listing['description'])}</code>"]
    if listing["keywords"]:
        parts.append(f"<b>Ключові слова</b>\n<code>{esc(listing['keywords'])}</code>")
    folder_note = ""
    if ad.get("photos"):
        folder_note = f"\n\n📁 Фото ({len(ad['photos'])} шт): {esc(folder_for(p['article']))}"
    notes = ("\n\n⚠️ " + "\n⚠️ ".join(esc(n) for n in listing["notes"])) if listing["notes"] else ""
    parts.append(f"<b>Решта полів форми</b>\n{esc(listing['info'])}{folder_note}{notes}")
    fields_text = "\n\n".join(parts)
    full = fields_text + "\n\n" + price_text(p, market, market_err)
    if len(full) <= 4000:       # усе в одному повідомленні, кнопки ціни під ним
        ad["fields_text"] = fields_text
        m = await B.say(context, chat_id, full, kind="sticky", parse_mode="HTML",
                        reply_markup=price_keyboard(p, market), **NOPREVIEW)
        ad["msg_id"] = m.message_id
    else:                       # дуже довгий текст не влізе в одне повідомлення Telegram
        ad["fields_text"], ad["msg_id"] = "", None
        await B.say(context, chat_id, fields_text, kind="sticky", parse_mode="HTML", **NOPREVIEW)
        await B.say(context, chat_id, price_text(p, market, market_err), parse_mode="HTML",
                    reply_markup=price_keyboard(p, market), **NOPREVIEW)


def _compose(ad, p, market, err):
    """Текст повідомлення з ціною: разом із полями, якщо вони в тому самому повідомленні."""
    price = price_text(p, market, err)
    return (ad["fields_text"] + "\n\n" + price) if ad.get("fields_text") else price


async def _refresh_market(context, chat_id, q):
    ad = _state(context)
    p = ad["product"]
    if ad.get("busy"):
        return
    if time.time() - ad.get("market_ts", 0) < 25:    # щойно шукали: не шукаємо вдруге
        await B.say(context, chat_id, "Пошук щойно виконано. Зачекай кілька секунд.", kind="flash", seconds=4)
        return
    ad["busy"] = True
    try:     # одразу видно, що пошук пішов: кнопки замінюються на «⏳»
        await q.edit_message_reply_markup(reply_markup=_kb(
            [InlineKeyboardButton("⏳ Шукаю ціни, зачекай...", callback_data="ad:busy")]))
    except Exception:
        pass
    B.spawn(_refresh_job(context, chat_id, q.message.message_id, ad, p))


async def _refresh_job(context, chat_id, msg_id, ad, p):
    try:
        prev = ad.get("market")
        force = bool(prev and prev.get("median"))   # перший пошук бере свіжий кеш, повторний оновлює
        header = "⏳ Шукаю ціни в магазинах"
        wait = await B.say(context, chat_id, header + "\n⏱ 0:00")
        try:
            async with _Ticker(context, chat_id, wait.message_id, header) as tk:
                market = await asyncio.to_thread(get_market, p, force, tk.log)
            err = ""
        except AdError as e:
            market, err = ad.get("market"), str(e)
        await B.safe_delete(context.bot, chat_id, wait.message_id)
        if context.user_data.get("ad") is not ad:    # поки шукали, дію скасували
            return
        ad["market"], ad["market_err"] = market, err
        ad["market_ts"] = time.time()
        try:
            await context.bot.edit_message_text(_compose(ad, p, market, err), chat_id=chat_id, message_id=msg_id,
                                                parse_mode="HTML", reply_markup=price_keyboard(p, market),
                                                **NOPREVIEW)
        except Exception:
            await B.say(context, chat_id, price_text(p, market, err), parse_mode="HTML",
                        reply_markup=price_keyboard(p, market), **NOPREVIEW)
    except Exception as e:
        logging.exception("refresh error")
        await B.say(context, chat_id, f"Помилка пошуку цін: {str(e)[:300]}", kind="flash", seconds=20)
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id,
                                                        reply_markup=price_keyboard(p, ad.get("market")))
        except Exception:
            pass
    finally:
        ad["busy"] = False


async def _set_price(context, chat_id, price):
    ad = _state(context)
    p = ad["product"]
    try:
        n = await asyncio.to_thread(write_sell_price, p["article"], price)
    except AdError as e:
        await B.say(context, chat_id, f"❌ {e}", kind="flash", seconds=15)
        return
    except Exception as e:
        await B.say(context, chat_id, f"❌ Не вдалося записати ціну: {e}", kind="flash", seconds=15)
        return
    await B.clear(context, "tmp")
    context.user_data.pop("mode", None)
    m, pct = margin_of(price, p["buy_min"]) if p["buy_min"] else (None, None)
    extra = f"\nМаржа від найкращого закупу: {m}₴ ({pct}%)" if m is not None else ""
    result = f"<b>Ціна</b>\n<code>{price}</code>\nЗаписано в «Продаж_Ц» для рядків: {n}.{extra}"
    ad["price"] = price
    kb = _shafa_kb(ad)
    if ad.get("fields_text") and ad.get("msg_id"):
        try:
            await context.bot.edit_message_text(ad["fields_text"] + "\n\n" + result, chat_id, ad["msg_id"],
                                                parse_mode="HTML", reply_markup=kb, **NOPREVIEW)
            return
        except Exception:
            pass
    await B.say(context, chat_id, result, kind="sticky", parse_mode="HTML", reply_markup=kb)


# ====================================================================
#  Завантаження на Shafa (бот заповнює форму, публікуєш ти сам)
# ====================================================================

def _shafa_kb(ad):
    if not cfg("SHAFA_ENABLED", True) or not ad.get("listing"):
        return None
    return _kb([InlineKeyboardButton("🚀 Завантажити на Shafa", callback_data="ad:shafa")])


def shafa_data(ad):
    """Збирає дані форми Shafa з готового оголошення."""
    p, L = ad["product"], ad["listing"]
    ai = L["ai"]
    cms = [ai["sizes"].get(eu, {}).get("cm", "") for eu in p["sizes"]] if L["shoes"] else []
    return {
        "title": L["title"], "description": L["description"], "price": ad.get("price"),
        "brand": ai["brand"], "condition": cfg("AD_CONDITION", "Новий"),
        "color": ai["color_ua"].capitalize() if ai["color_ua"] else "",
        "material": ai["material"], "season": ai["season"], "membrane": ai["membrane"],
        "keywords": L["keywords"], "path": ai["path"], "sizes": list(p["sizes"]),
        "insoles": [c for c in cms if c],
        "photos": [os.path.abspath(x) for x in ad.get("photos", [])],
    }


async def _shafa_start(context, chat_id, ad):
    try:
        import shafa
    except ImportError:
        await B.say(context, chat_id, "Немає файлу shafa.py поруч із ботом.", kind="flash", seconds=10)
        return
    if not shafa.playwright_ok():
        await B.say(context, chat_id, "Не встановлено Playwright (pip install playwright, потім playwright install chromium).",
                    kind="flash", seconds=12)
        return
    if not ad.get("listing") or not ad.get("price"):
        await B.say(context, chat_id, "Спершу вибери ціну.", kind="flash", seconds=6)
        return
    loop = asyncio.get_running_loop()

    def send(text):
        asyncio.run_coroutine_threadsafe(
            B.say(context, chat_id, text, kind="flash", seconds=25, parse_mode="HTML"), loop)

    def final(rep):
        asyncio.run_coroutine_threadsafe(
            B.say(context, chat_id, shafa.report_text(rep), kind="sticky", parse_mode="HTML"), loop)

    await B.say(context, chat_id, "🌐 Відкриваю Shafa у новому вікні на комп'ютері. Якщо просить, увійди в акаунт сам.",
                kind="flash", seconds=15)
    shafa.start(shafa_data(ad), send, final)
