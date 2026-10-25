import asyncio
import io
import csv
import re
import uuid
import logging
import sys
from datetime import datetime

import gspread
from openpyxl import load_workbook
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
import settings as config
import ads

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx пише в журнал кожен запит до Telegram разом із токеном бота в адресі: вимикаємо це
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

ORDER_HEADERS = [
    "№", "Дата", "Площадка", "Від", "Ініціали", "Телефон", "Місто",
    "Відділення", "Модель", "Артикул", "Розмір EU", "Розмір см", "Закуп",
    "Ціна продажу", "Комісія", "Передоплата", "ТТН", "Прибуток", "Статус",
]
COL_TTN = ORDER_HEADERS.index("ТТН") + 1      # номер колонки в таблиці (з 1)
COL_STATUS = ORDER_HEADERS.index("Статус") + 1

_book = None


def get_book():
    """Підключення до таблиці (один раз)."""
    global _book
    if _book is None:
        gc = gspread.service_account(filename=config.CREDENTIALS_FILE)
        _book = gc.open_by_key(config.SHEET_ID)
    return _book


# ---------- Допоміжні функції ----------

def norm_article(s):
    return re.sub(r"[\s\-_]", "", str(s)).lower()


def norm_size(s):
    return str(s).strip().replace(",", ".").upper()


def to_int(s, default=0):
    try:
        return int(float(str(s).replace(",", ".").replace(" ", "")))
    except (ValueError, TypeError):
        return default


def money(s):
    """Дістає суму з тексту: '7100₴' -> 7100. Якщо цифр немає -> 0."""
    m = re.search(r"\d+", str(s).replace(" ", ""))
    return int(m.group(0)) if m else 0


# ---------- Склад ----------

def stock_cols(header):
    cols = {}
    for i, h in enumerate(header):
        h = re.sub(r"\s", "", h).lower()
        if h == "модель":
            cols["model"] = i
        elif h == "артикул":
            cols["article"] = i
        elif h == "розмір":
            cols["size"] = i
        elif h.startswith("кільк"):
            cols["qty"] = i
        elif h == "закуп":
            cols["buy"] = i
        elif h.startswith("продаж"):
            cols["sell"] = i
        elif h.startswith("постач"):
            cols["supplier"] = i
    for need in ("model", "article", "size", "qty"):
        if need not in cols:
            raise RuntimeError(f"У листі «{config.STOCK_SHEET}» не знайшов колонку: {need}")
    return cols


def load_stock():
    ws = get_book().worksheet(config.STOCK_SHEET)
    values = ws.get_all_values()
    if not values:
        return [], {}
    header, rows = values[0], values[1:]
    cols = stock_cols(header)
    items = []
    for n, row in enumerate(rows, start=2):  # рядок 1 це заголовки
        row = row + [""] * (len(header) - len(row))
        items.append({
            "row": n,
            "model": row[cols["model"]].strip(),
            "article": row[cols["article"]].strip(),
            "size": row[cols["size"]].strip(),
            "qty": to_int(row[cols["qty"]]),
            "sell": row[cols["sell"]].strip() if "sell" in cols else "",
            "buy": row[cols["buy"]].strip() if "buy" in cols else "",
            "supplier": row[cols["supplier"]].strip() if "supplier" in cols else "",
        })
    return items, cols


def fmt_price(x):
    m = money(x)
    return f"{m}₴" if m else ""


def size_key(s):
    try:
        return float(norm_size(s))
    except ValueError:
        return 999.0


def fmt_row(r, with_size=True):
    s = f"{norm_size(r['size'])} — {r['qty']} шт" if with_size else f"{r['qty']} шт"
    if fmt_price(r["buy"]):
        s += f" · закуп {fmt_price(r['buy'])}"
    if r["supplier"]:
        s += f" · {r['supplier']}"
    if fmt_price(r["sell"]):
        s += f" · продаж {fmt_price(r['sell'])}"
    return s


def search_stock(query, sort="buy"):
    """sort='buy': від найдешевшого закупу, sort='size': за розміром."""
    tokens = query.split()
    size = None
    if len(tokens) > 1 and re.fullmatch(r"((3[4-9]|4\d|50)([.,]5)?|[1-4]?X{0,3}[SML])", tokens[-1], re.I):
        size = norm_size(tokens.pop())
    text = " ".join(tokens)
    qa = norm_article(text)
    words = text.lower().split()

    items, _ = load_stock()
    groups = {}
    for it in items:
        art = norm_article(it["article"])
        by_article = len(art) >= 4 and (qa == art or qa in art or art in qa)
        hay = (it["model"] + " " + it["article"]).lower()
        by_name = bool(words) and all(w in hay for w in words)
        if by_article or by_name:
            groups.setdefault((it["model"], it["article"]), []).append(it)

    if not groups:
        return "Нічого не знайшов за цим запитом."

    no_price = 10**9
    if sort == "buy":
        row_key = lambda r: (money(r["buy"]) or no_price, size_key(r["size"]))
    else:
        row_key = lambda r: (size_key(r["size"]), money(r["buy"]) or no_price)

    def best_buy(rows):
        return min((money(r["buy"]) or no_price for r in rows if r["qty"] > 0), default=no_price + 1)

    ordered = list(groups.items())
    if sort == "buy":
        ordered.sort(key=lambda kv: best_buy(kv[1]))   # спершу модель з найвигіднішим закупом

    def lines_for(rows, with_size, bullet=""):
        out = []
        for n, r in enumerate(rows):
            medal = "🥇 " if (sort == "buy" and n == 0 and len(rows) > 1 and money(r["buy"])) else ""
            out.append(bullet + medal + fmt_row(r, with_size=with_size))
        return "\n".join(out)

    parts = []
    for (model, article), rows in ordered:
        in_stock = sorted([r for r in rows if r["qty"] > 0], key=row_key)
        title = f"{model} {article}".strip()
        if size:
            exact = [r for r in in_stock if norm_size(r["size"]) == size]
            if exact:
                parts.append(f"✅ {title}\nРозмір {size}:\n{lines_for(exact, False, '• ')}")
            elif in_stock:
                sizes = ", ".join(sorted({norm_size(r["size"]) for r in in_stock}, key=size_key))
                parts.append(f"❌ {title}\nРозміру {size} немає. Є розміри: {sizes}")
            else:
                parts.append(f"❌ {title}\nНемає в наявності")
        else:
            if in_stock:
                parts.append(f"📦 {title}\n{lines_for(in_stock, True)}")
            else:
                parts.append(f"❌ {title}\nНемає в наявності")
    head = "Сортування: від найдешевшого закупу\n\n" if sort == "buy" else "Сортування: за розміром\n\n"
    out = head + "\n\n".join(parts)
    if len(out) > 3800:
        out = out[:3800].rsplit("\n", 1)[0] + "\n…\nБагато результатів, уточни запит (додай розмір)."
    return out


# ---------- Розбір замовлення (за твоїм шаблоном, без ШІ) ----------

ARTICLE_TOKEN = re.compile(r"(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9\-]+")


def parse_order(text):
    """Повертає (замовлення, список_чого_бракує). Якщо це не замовлення -> (None, [])."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or "замовлення" not in lines[0].lower():
        return None, []

    o = {
        "date": "", "platform": "", "from": "", "buy": 0, "prepay": 0,
        "model": "", "article": "", "size_eu": "", "size_cm": "",
        "price": 0, "commission": 0, "name": "", "phone": "",
        "city": "", "branch": "", "ttn": "",
    }

    head = lines[0]
    m = re.search(r"\d{1,2}\.\d{1,2}\.\d{2,4}", head)
    if m:
        o["date"] = m.group(0)
        rest = head.split(m.group(0), 1)[1]
    else:
        rest = head.split(":", 1)[-1]
    o["platform"] = re.sub(r"[✅☑✔️]", "", rest).strip(" :-")

    body = lines[1:]
    used = set()
    for i, l in enumerate(body):
        low = l.lower()
        if low.startswith("від"):
            o["from"] = l.split(":", 1)[-1].strip()
            used.add(i)
        elif low.startswith("закуп"):
            o["buy"] = money(l)
            used.add(i)
        elif "передоплат" in low:
            o["prepay"] = money(l)
            used.add(i)
        elif low.startswith("ттн") or low.startswith("тнн"):
            o["ttn"] = re.sub(r"\D", "", l)
            used.add(i)
        elif "комісі" in low:
            o["commission"] = money(l)
            used.add(i)
        elif "|" in l and "см" in low:
            eu, cm = l.split("|", 1)
            o["size_eu"] = norm_size(eu)
            o["size_cm"] = norm_size(re.sub(r"см", "", cm, flags=re.I))
            used.add(i)
        elif re.fullmatch(r"\d[\d\s]*\s*(₴|грн)", l, re.I) and not o["price"]:
            o["price"] = money(l)
            used.add(i)

    # рядок з моделлю і артикулом: останнє слово має і літери, і цифри
    for i, l in enumerate(body):
        if i in used:
            continue
        parts = l.split()
        if len(parts) >= 2 and ARTICLE_TOKEN.fullmatch(parts[-1]):
            o["article"] = parts[-1]
            o["model"] = " ".join(parts[:-1])
            used.add(i)
            break

    # решта: ініціали, телефон, місто, відділення
    rest_lines = [body[i] for i in range(len(body)) if i not in used]
    phone_idx = None
    for i, l in enumerate(rest_lines):
        if len(re.sub(r"\D", "", l)) >= 7:
            phone_idx = i
            o["phone"] = re.sub(r"[^\d+]", "", l)
            break
    if phone_idx is not None:
        o["name"] = " ".join(rest_lines[:phone_idx])
        after = rest_lines[phone_idx + 1:]
        if after:
            o["city"] = after[0]
            o["branch"] = " ".join(after[1:])
    else:
        o["name"] = " ".join(rest_lines)

    missing = []
    if not o["model"] or not o["article"]:
        missing.append("модель і артикул (в одному рядку, артикул останнім словом)")
    if not o["size_eu"]:
        missing.append("розмір у вигляді «42 | 26,5 см»")
    if not o["price"]:
        missing.append("ціна продажу (число зі знаком ₴, окремим рядком)")
    return o, missing


def profit(o):
    return o["price"] - o["commission"] - o["buy"]


def order_text(o, number=None):
    head = "Замовлення" + (f" №{number}" if number else "")
    lines = [
        f"{head}: {o['date']} {o['platform']} ✅",
        f"Від: {o['from']}",
        f"Закуп: {o['buy']}₴",
    ]
    if o["prepay"]:
        lines.append(f"Передоплата: {o['prepay']}₴")
    lines += [
        f"{o['model']} {o['article']}",
        f"{o['size_eu']} | {o['size_cm']} см",
        f"{o['price']}₴ (комісія {o['commission']}₴)",
        o["name"],
        f"номер {o['phone']}",
        o["city"],
        o["branch"],
        f"ТТН: {o['ttn'] or '—'}",
        f"💰 Прибуток: {profit(o)}₴",
    ]
    return "\n".join(l for l in lines if l.strip())


def find_stock_row(article, size, buy=None):
    """Шукає рядок складу. Якщо рядків кілька (різні постачальники) бере з тим самим закупом, інакше найдешевший."""
    items, cols = load_stock()
    cands = [it for it in items
             if norm_article(it["article"]) == norm_article(article)
             and norm_size(it["size"]) == norm_size(size) and it["qty"] > 0]
    if not cands:
        return None, cols
    if buy:
        same = [c for c in cands if money(c["buy"]) == buy]
        if same:
            return same[0], cols
    cands.sort(key=lambda r: money(r["buy"]) or 10**9)
    return cands[0], cols


def save_order(o):
    """Записує замовлення в таблицю і зменшує залишок. Повертає (номер, примітка)."""
    book = get_book()
    ws = book.worksheet(config.ORDERS_SHEET)
    values = ws.get_all_values()
    if not values or not any(values[0]):
        ws.append_row(ORDER_HEADERS, value_input_option="RAW")
        number = 1
    else:
        numbers = [to_int(r[0]) for r in values[1:] if r and str(r[0]).strip()]
        number = (max(numbers) if numbers else 0) + 1

    status = "відправлено" if o["ttn"] else "чекає ТТН"
    row = [
        number, o["date"], o["platform"], o["from"], o["name"], o["phone"],
        o["city"], o["branch"], o["model"], o["article"], o["size_eu"],
        o["size_cm"], o["buy"], o["price"], o["commission"], o["prepay"],
        o["ttn"], profit(o), status,
    ]
    ws.append_row(row, value_input_option="RAW")

    try:
        stock_item, cols = find_stock_row(o["article"], o["size_eu"], o["buy"])
        if stock_item:
            new_qty = stock_item["qty"] - 1
            book.worksheet(config.STOCK_SHEET).update_cell(
                stock_item["row"], cols["qty"] + 1, new_qty)
            note = f"Склад: {o['article']} розмір {o['size_eu']} — залишилось {new_qty} шт"
        else:
            note = ("⚠️ На складі не знайшов цей артикул з таким розміром. "
                    "Залишок не змінено, перевір таблицю.")
    except Exception:
        logging.exception("stock update failed")
        note = "⚠️ Замовлення записано, але залишок на складі НЕ змінено. Зміни його вручну."
    return number, note


# ---------- Імпорт прайсів постачальників ----------

def norm_h(s):
    return re.sub(r"\s", "", str(s or "")).lower()


def clean_cell(v):
    """Значення клітинки -> текст без зайвого. 42.0 -> 42."""
    if v is None:
        return ""
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s


def map_header(row):
    """Шукає в рядку назви колонок. Повертає {поле: номер колонки}."""
    cols = {}
    for i, raw in enumerate(row):
        h = norm_h(raw)
        if not h:
            continue
        if h in ("eur", "eu", "euro", "ею"):
            cols.setdefault("size_eu", i)
        elif h in ("us", "usa"):
            cols.setdefault("size_us", i)
        elif h == "uk":
            cols.setdefault("size_uk", i)
        elif h in ("cm", "см"):
            cols.setdefault("size_cm", i)
        elif h.startswith("артикул") or h.startswith("sku") or h in ("арт", "арт."):
            cols.setdefault("article", i)
        elif ("назв" in h or "наимен" in h) and "категор" not in h:
            cols.setdefault("name", i)
        elif "модел" in h:
            cols.setdefault("model_col", i)
        elif "розмір" in h or "размер" in h or h == "size":
            if "eu" in h or "ею" in h:
                cols.setdefault("size_eu", i)
            elif "us" in h or "сша" in h:
                cols.setdefault("size_us", i)
            elif "uk" in h:
                cols.setdefault("size_uk", i)
            elif "см" in h or "cm" in h:
                cols.setdefault("size_cm", i)
            else:
                cols.setdefault("size", i)
        elif "кільк" in h or "колич" in h or "кол-во" in h or "к-сть" in h or h in ("qty", "шт"):
            cols.setdefault("qty", i)
        elif "дроп" in h or "drop" in h:
            cols.setdefault("drop", i)
        elif "закуп" in h:
            cols.setdefault("buy", i)
        elif "цін" in h or "цен" in h or "price" in h:
            cols.setdefault("price", i)
    return cols


def find_header(rows):
    """Знаходить рядок заголовків серед перших 20 рядків."""
    best = (0, None, {})
    for idx, row in enumerate(rows[:20]):
        cols = map_header(row)
        if len(cols) > best[0]:
            best = (len(cols), idx, cols)
    return best[1], best[2]


SIZE_RE = re.compile(r"\d{1,2}(\.5)?( \d/\d)?|\d{1,2}-\d{1,2}|[1-4]?X{0,3}[SML]|[SML]/[SML]|OS|MISC|ONESIZE")
EU_RE = re.compile(r"(3[4-9]|4[0-9]|50)(\.5)?( \d/\d)?")
PRICE_RE = re.compile(r"\d[\d\s.,]*\s*(₴|грн)?", re.I)


def looks_like_size(s):
    return bool(SIZE_RE.fullmatch(norm_size(s)))


def looks_like_eu(s):
    return bool(EU_RE.fullmatch(norm_size(s)))


def looks_like_price(s):
    return bool(PRICE_RE.fullmatch(str(s).strip())) and money(s) >= 100


def column_values(rows, hi, i, limit=40):
    vals = []
    for row in rows[hi + 1:]:
        if i < len(row):
            v = clean_cell(row[i])
            if v:
                vals.append(v)
        if len(vals) >= limit:
            break
    return vals


def guess_column(rows, hi, used, width, test):
    """Шукає колонку за значеннями (коли назва колонки незрозуміла або порожня)."""
    best, best_share = None, 0.0
    for i in range(width):
        if i in used:
            continue
        vals = column_values(rows, hi, i)
        if len(vals) < 3:
            continue
        share = sum(1 for v in vals if test(v)) / len(vals)
        if share >= 0.7 and share > best_share:
            best, best_share = i, share
    return best


def col_letter(i):
    return chr(65 + i) if i < 26 else str(i + 1)


def parse_price_rows(rows):
    """Повертає (позиції, інфо). Кидає ValueError з поясненням, якщо щось не знайдено."""
    hi, cols = find_header(rows)
    if hi is None:
        raise ValueError("Не знайшов рядок із заголовками (Артикул, Розмір, Кількість, Ціна).")
    header = rows[hi]
    width = max((len(r) for r in rows[hi:hi + 50]), default=0)

    # «Модель» без колонки «Артикул» це і є артикул (формат: Назва | Модель | Розмір EU ...)
    if "article" not in cols and "model_col" in cols:
        cols["article"] = cols.pop("model_col")
    model_key = "name" if "name" in cols else ("model_col" if "model_col" in cols else None)

    guessed = set()
    size_key_name = "size_eu" if "size_eu" in cols else ("size" if "size" in cols else None)
    if size_key_name is None:
        g = guess_column(rows, hi, set(cols.values()), width, looks_like_eu)
        if g is not None:
            cols["size_eu"], size_key_name = g, "size_eu"
            guessed.add("size_eu")
    price_key = next((k for k in ("drop", "buy", "price") if k in cols), None)
    if price_key is None:
        g = guess_column(rows, hi, set(cols.values()), width, looks_like_price)
        if g is not None:
            cols["price"], price_key = g, "price"
            guessed.add("price")

    missing = []
    if "article" not in cols:
        missing.append("Артикул")
    if size_key_name is None:
        missing.append("Розмір (EU)")
    if price_key is None:
        missing.append("Ціна")
    if missing:
        found = ", ".join(f"«{clean_cell(c)}»" for c in header if clean_cell(c))
        raise ValueError(
            "Не знайшов колонки: " + ", ".join(missing) + f".\nЗаголовки у файлі: {found}\n"
            "Перейменуй потрібні колонки (наприклад «Артикул», «Розмір EU», «Кількість», «Ціна») і надішли ще раз.")

    def cell(row, key):
        i = cols.get(key)
        return clean_cell(row[i]) if i is not None and i < len(row) else ""

    items, skipped_rows = [], []
    last_art, last_model = "", ""

    def skip(ridx, row, reason):
        preview = " | ".join(clean_cell(c) for c in row if clean_cell(c))
        skipped_rows.append({"row": ridx + 1, "reason": reason, "preview": preview[:90]})

    for ridx in range(hi + 1, len(rows)):
        row = rows[ridx]
        if not any(clean_cell(c) for c in row):
            continue
        art_raw = cell(row, "article")
        model = cell(row, model_key) if model_key else ""
        size = norm_size(cell(row, size_key_name))
        if not art_raw and size:            # об'єднані клітинки: беремо з попереднього рядка
            art_raw, model = last_art, model or last_model
        parts = art_raw.split()
        art = art_raw
        if len(parts) == 2 and parts[1].upper() in ("W", "M", "U"):  # «BQ6472-051 W» -> артикул без літери статі
            art = parts[0]
        elif len(parts) >= 2 and ARTICLE_TOKEN.fullmatch(parts[-1]):   # «Adidas Superstar B42621»
            art = parts[-1]
            model = model or " ".join(parts[:-1])
        if not art or not size:
            lack = [w for w, bad in (("артикулу", not art), ("розміру", not size)) if bad]
            skip(ridx, row, "немає " + " і ".join(lack))
            continue
        last_art, last_model = art_raw, model
        qty_raw = cell(row, "qty") if "qty" in cols else ""
        qty = to_int(qty_raw, default=0) if "qty" in cols else 1
        if qty <= 0:
            skip(ridx, row, f"кількість «{qty_raw or 'порожньо'}» → 0, у наявності немає")
            continue
        buy = money(cell(row, price_key))
        items.append({"model": model, "article": art, "size": size, "qty": qty, "buy": buy or ""})

    if not items:
        raise ValueError("Не знайшов жодної позиції під заголовками.")

    def name(key):
        i = cols[key]
        h = clean_cell(header[i]) if i < len(header) else ""
        if key in guessed:
            return f"колонка {col_letter(i)}" + (f" («{h}»)" if h else "") + ", вгадав за значеннями"
        return f"«{h}»"
    used = [f"артикул ← {name('article')}", f"розмір ← {name(size_key_name)}", f"закуп ← {name(price_key)}"]
    used.append(f"кількість ← {name('qty')}" if "qty" in cols else "кількість не знайшов, ставлю 1")
    if model_key:
        used.append(f"модель ← {name(model_key)}")

    warnings = []
    bad = [it["size"] for it in items if not looks_like_size(it["size"])]
    if len(bad) / len(items) > 0.3:
        ex = ", ".join(sorted(set(bad))[:3])
        warnings.append(f"у колонці розміру багато значень, які не схожі на розмір (напр.: {ex}). Перевір приклади нижче")
    noprice = sum(1 for it in items if not it["buy"])
    if noprice / len(items) > 0.3:
        warnings.append(f"у {noprice} із {len(items)} позицій немає ціни")
    return items, {"skipped_rows": skipped_rows, "used": used, "warnings": warnings}


def read_tables(filename, data):
    """Читає xlsx або csv. Повертає список (назва аркуша, рядки)."""
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext == "xlsx":
        # read_only: файл читається потоком, пам'яті треба в рази менше
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        try:
            result = []
            for ws in wb.worksheets:
                try:
                    ws.reset_dimensions()   # деякі прайси мають хибний розмір аркуша
                except Exception:
                    pass
                result.append((ws.title, [list(r) for r in ws.iter_rows(values_only=True)]))
            return result
        finally:
            wb.close()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251")
    first = text.splitlines()[0] if text.strip() else ""
    delim = ";" if first.count(";") >= first.count(",") and ";" in first else ("\t" if "\t" in first else ",")
    return [(filename, list(csv.reader(io.StringIO(text), delimiter=delim)))]


def parse_price_file(filename, data):
    """Читає всі аркуші. Повертає список аркушів: кожен зі своїми позиціями та пропущеними рядками."""
    sheets = []
    for title, table in read_tables(filename, data):
        if not any(clean_cell(c) for row in table for c in row):
            continue  # порожній аркуш не показуємо
        try:
            it, inf = parse_price_rows(table)
        except ValueError as e:
            text = str(e)
            sheets.append({"name": title, "items": [], "count": 0, "skipped_rows": [], "used": [],
                           "warnings": [], "error": text.split("\n")[0], "full_error": text})
            continue
        sheets.append({"name": title, "items": it, "count": len(it), "skipped_rows": inf["skipped_rows"],
                       "used": inf["used"], "warnings": inf["warnings"], "error": "", "full_error": ""})
    if not sheets:
        raise ValueError("Файл порожній.")
    if not any(s["count"] for s in sheets):
        if len(sheets) == 1:
            raise ValueError(sheets[0]["full_error"])
        raise ValueError("Жоден аркуш не підійшов:\n" +
                         "\n".join(f"• «{s['name']}»: {s['full_error']}" for s in sheets))
    return sheets


def row_blocks(rows):
    """[5,6,7,10] -> [(5,7),(10,10)]"""
    blocks = []
    for r in sorted(rows):
        if blocks and r == blocks[-1][1] + 1:
            blocks[-1][1] = r
        else:
            blocks.append([r, r])
    return [tuple(b) for b in blocks]


def build_new_rows(items, supplier, header, cols, sell_map=None):
    sell_map = sell_map or {}
    rows = []
    for it in items:
        row = [""] * len(header)
        if "sell" in cols and norm_article(it["article"]) in sell_map:
            row[cols["sell"]] = sell_map[norm_article(it["article"])]
        row[cols["model"]] = it["model"]
        row[cols["article"]] = it["article"]
        row[cols["size"]] = it["size"]
        row[cols["qty"]] = it["qty"]
        if "buy" in cols:
            row[cols["buy"]] = it["buy"]
        row[cols["supplier"]] = supplier
        rows.append(row)
    return rows

ARCHIVE_SHEET = "Архів складу"


def _pad(row, n):
    return list(row) + [""] * (n - len(row))


def archive_rows(book, header, rows, supplier, reason):
    """Копія видалених рядків у окремий лист. Якщо копію зробити не вдалось, нічого не видаляємо."""
    if not rows:
        return
    try:
        ws = book.worksheet(ARCHIVE_SHEET)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=ARCHIVE_SHEET, rows=1000, cols=len(header) + 3)
        ws.append_row(["Коли видалено", "Чому", "Постачальник"] + list(header), value_input_option="RAW")
    stamp = datetime.now().strftime("%d.%m.%y %H:%M")
    ws.append_rows([[stamp, reason, supplier] + _pad(r, len(header)) for r in rows],
                   value_input_option="RAW")


def count_supplier_rows(supplier):
    items, cols = load_stock()
    if items and "supplier" not in cols:
        raise RuntimeError(f"У листі «{config.STOCK_SHEET}» немає колонки «Постачальник». Додай її в заголовки.")
    return sum(1 for it in items if it["supplier"].lower() == supplier.lower())


def apply_import(supplier, items, mode="replace"):
    """mode='replace': видаляє всі старі рядки постачальника і додає нові.
    mode='add': лишає старі, прибирає тільки дублі (той самий артикул + розмір) і додає нові.
    Видалені рядки спершу копіюються в лист «Архів складу».
    Повертає (видалено, додано, скільки_збережено_цін_продажу)."""
    book = get_book()
    ws = book.worksheet(config.STOCK_SHEET)
    values = ws.get_all_values()
    header = values[0]
    cols = stock_cols(header)
    if "supplier" not in cols:
        raise RuntimeError(f"У листі «{config.STOCK_SHEET}» немає колонки «Постачальник». Додай її в заголовки.")
    width = len(header)

    def cell_of(row, key):
        i = cols[key]
        return row[i].strip() if i < len(row) else ""

    # ціни продажу, які ти вже виставив, переносимо на нові рядки з тим самим артикулом
    sell_map = {}
    if "sell" in cols:
        for row in values[1:]:
            if cell_of(row, "sell"):
                sell_map[norm_article(cell_of(row, "article"))] = cell_of(row, "sell")

    mine = [n for n, row in enumerate(values[1:], start=2)
            if cell_of(row, "supplier").lower() == supplier.lower()]
    if mode == "replace":
        old = mine
        reason = "заміна прайсу"
    else:
        new_keys = {(norm_article(it["article"]), norm_size(it["size"])) for it in items}
        old = [n for n in mine
               if (norm_article(cell_of(values[n - 1], "article")),
                   norm_size(cell_of(values[n - 1], "size"))) in new_keys]
        reason = "оновлення дублів при додаванні прайсу"

    if old:
        archive_rows(book, header, [values[n - 1] for n in old], supplier, reason)  # спершу копія
        requests = [{"deleteDimension": {"range": {
            "sheetId": ws.id, "dimension": "ROWS", "startIndex": s - 1, "endIndex": e}}}
            for s, e in reversed(row_blocks(old))]
        book.batch_update({"requests": requests})

    new_rows = build_new_rows(items, supplier, header, cols, sell_map)
    # пишемо в конкретний рядок одразу під останніми даними, щоб нічого не перезаписати
    start = len(values) - len(old) + 1
    ws = book.worksheet(config.STOCK_SHEET)          # свіжі розміри аркуша після видалення
    need = start + len(new_rows) - 1
    if need > ws.row_count:
        ws.add_rows(need - ws.row_count)
    book.values_update(f"'{config.STOCK_SHEET}'!A{start}",
                       params={"valueInputOption": "RAW"},
                       body={"values": [_pad(r, width) for r in new_rows]})
    kept = sum(1 for r in new_rows if "sell" in cols and r[cols["sell"]])
    return len(old), len(new_rows), kept


# ---------- Замовлення в таблиці ----------

OI = {name: i for i, name in enumerate(ORDER_HEADERS)}


def load_orders():
    ws = get_book().worksheet(config.ORDERS_SHEET)
    values = ws.get_all_values()
    orders = []
    for row in values[1:]:
        row = row + [""] * (len(ORDER_HEADERS) - len(row))
        if not row[0].strip():
            continue
        orders.append({h: row[i] for h, i in OI.items()})
    return orders


def clean_ttn(s):
    digits = re.sub(r"\D", "", str(s))
    return digits if 8 <= len(digits) <= 20 else None


def set_ttn(number, ttn):
    ws = get_book().worksheet(config.ORDERS_SHEET)
    values = ws.get_all_values()
    for i, row in enumerate(values[1:], start=2):
        if row and row[0].strip() == str(number):
            ws.update_cell(i, COL_TTN, ttn)
            ws.update_cell(i, COL_STATUS, "відправлено")
            return True
    return False

def list_suppliers():
    """[(назва, кількість рядків)] без урахування регістру."""
    items, cols = load_stock()
    if "supplier" not in cols:
        return []
    found = {}
    for it in items:
        if it["supplier"]:
            k = it["supplier"].lower()
            name, n = found.get(k, (it["supplier"], 0))
            found[k] = (name, n + 1)
    return sorted(found.values(), key=lambda x: x[0].lower())


# ---------- Telegram: меню ----------

BTN_SEARCH = "🔎 Наявність"
BTN_ORDER = "📝 Нове замовлення"
BTN_TTN = "🚚 Додати ТТН"
BTN_LIST = "📋 Останні замовлення"
BTN_IMPORT = "📥 Імпорт прайсу"
BTN_ADS = "🛍 Оголошення"
MENU_BUTTONS = {BTN_SEARCH, BTN_ORDER, BTN_TTN, BTN_LIST, BTN_IMPORT, BTN_ADS}

MENU = ReplyKeyboardMarkup(
    [[BTN_SEARCH, BTN_ORDER], [BTN_TTN, BTN_LIST], [BTN_IMPORT, BTN_ADS]],
    resize_keyboard=True, is_persistent=True,
    input_field_placeholder="Артикул і розмір, наприклад CN8490-100 43",
)

CANCEL_BTN = InlineKeyboardButton("❌ Скасувати", callback_data="cancel")
CANCEL_KB = InlineKeyboardMarkup([[CANCEL_BTN]])


def sort_kb(current):
    other, label = ("size", "↕ Показати за розміром") if current == "buy" else ("buy", "↕ Показати від найдешевшого закупу")
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"srt:{other}")]])


TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024   # Telegram дозволяє ботам скачувати файли до 20 МБ
FLASH_SECONDS = 10


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != config.OWNER_ID:
            return
        return await func(update, context)
    return wrapper


def reset_mode(context):
    for k in ("mode", "ttn_order", "supplier", "suppliers", "ad"):
        context.user_data.pop(k, None)


# ---------- Telegram: чистий чат ----------
# tmp    : службові повідомлення поточної дії (питання, шаблони, прев'ю, твої відповіді).
#          Видаляються, коли дія завершена або ти натиснув іншу кнопку меню.
# sticky : результат (наявність, список замовлень, підсумок імпорту).
#          Лишається, поки не почнеш наступну дію.
# flash  : коротке підтвердження, зникає саме через кілька секунд.
# Повідомлення в чаті замовлень (ORDERS_CHAT_ID) бот ніколи не видаляє.

_bg = set()


async def safe_answer(q, text=None, alert=False):
    """Відповідь на натискання кнопки. Якщо натискання вже застаріло (бот був зайнятий) чи зник інтернет,
    це не помилка: просто пропускаємо."""
    try:
        await q.answer(text, show_alert=alert) if text else await q.answer()
    except Exception as e:
        logging.info("answer пропущено: %s", str(e)[:80])


def spawn(coro):
    task = asyncio.create_task(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)


async def safe_delete(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def delete_later(bot, chat_id, message_id, seconds):
    await asyncio.sleep(seconds)
    await safe_delete(bot, chat_id, message_id)


def track(context, *msgs, kind="tmp"):
    store = context.user_data.setdefault(kind, [])
    for m in msgs:
        if m is not None:
            store.append((m.chat_id, m.message_id))


async def clear(context, *kinds):
    for kind in (kinds or ("tmp",)):
        for chat_id, mid in context.user_data.pop(kind, []):
            await safe_delete(context.bot, chat_id, mid)


async def say(context, chat_id, text, kind="tmp", seconds=FLASH_SECONDS, **kw):
    m = await context.bot.send_message(chat_id, text, **kw)
    if kind in ("tmp", "sticky"):
        track(context, m, kind=kind)
    elif kind == "flash":
        spawn(delete_later(context.bot, chat_id, m.message_id, seconds))
    return m


async def abandon(context):
    """Нова дія з меню: скидаємо режим і прибираємо все, що лишилось від попередньої."""
    reset_mode(context)
    await clear(context, "tmp", "sticky")


WELCOME = (
    "Привіт! Користуйся кнопками внизу:\n\n"
    "🔎 Наявність: перевірити артикул або модель\n"
    "📝 Нове замовлення: дам шаблон, ти заповниш\n"
    "🚚 Додати ТТН: оберу замовлення без ТТН\n"
    "📋 Останні замовлення\n"
    "📥 Імпорт прайсу: файл .xlsx або .csv від постачальника\n"
    "🛍 Оголошення: поля для Shafa, ціна й фото\n\n"
    "Можна й просто написати артикул або модель з розміром без кнопок, "
    "я одразу покажу наявність.\n\n"
    "Чат чистий: службові повідомлення зникають після завершення дії."
)


@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await abandon(context)
    await safe_delete(context.bot, chat_id, update.message.message_id)
    await context.bot.send_message(chat_id, WELCOME, reply_markup=MENU)


@owner_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await safe_delete(context.bot, chat_id, update.message.message_id)
    context.bot_data.get("pending_import", {}).clear()
    reset_mode(context)
    await clear(context)
    await say(context, chat_id, "❌ Скасовано", kind="flash", seconds=4)


@owner_only
async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat_id цього чату: {update.effective_chat.id}")


@owner_only
async def cmd_ttn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await safe_delete(context.bot, chat_id, update.message.message_id)
    if len(context.args) != 2:
        await say(context, chat_id, "Простіше через кнопку «🚚 Додати ТТН».\nАбо командою: /ttn НОМЕР ТТН",
                  kind="flash")
        return
    number, ttn = context.args[0], clean_ttn(context.args[1])
    if not ttn:
        await say(context, chat_id, "ТТН має складатися з цифр (від 8 до 20).", kind="flash")
        return
    await save_ttn(context, chat_id, number, ttn)


async def save_ttn(context, chat_id, number, ttn):
    try:
        ok = await asyncio.to_thread(set_ttn, number, ttn)
        await clear(context)
        if ok:
            await say(context, chat_id, f"✅ Замовлення №{number}: ТТН {ttn} записано", kind="flash")
            if config.ORDERS_CHAT_ID:
                await context.bot.send_message(config.ORDERS_CHAT_ID, f"🚚 Замовлення №{number}: ТТН {ttn}")
        else:
            await say(context, chat_id, f"Не знайшов замовлення №{number}", kind="flash")
    except Exception as e:
        logging.exception("ttn error")
        await clear(context)
        await say(context, chat_id, f"Помилка: {e}", kind="flash", seconds=20)


async def show_order_template(update, context):
    today = datetime.now().strftime("%d.%m.%y")
    template = (
        f"Замовлення: {today} Shafa ✅\n"
        "Від: ім'я клієнта\n"
        "Закуп: 0₴\n"
        "модель АРТИКУЛ\n"
        "42 | 26,5 см\n"
        "0₴\n"
        "(300₴ комісія для шафи)\n"
        "Прізвище Ім'я По батькові\n"
        "номер 0500000000\n"
        "Місто\n"
        "Нова пошта відділення 1"
    )
    await say(
        context, update.effective_chat.id,
        "Натисни на шаблон, він скопіюється. Заміни дані й надішли мені.\n"
        "Артикул пиши останнім словом у рядку з моделлю, ціну продажу з ₴ окремим рядком. "
        "Рядок «[Передоплата (500₴)]» додай після закупу, якщо вона є, ТТН можна додати пізніше.\n\n"
        f"<code>{template}</code>",
        parse_mode="HTML", reply_markup=CANCEL_KB)


async def show_recent_orders(update, context):
    chat_id = update.effective_chat.id
    orders = await asyncio.to_thread(load_orders)
    if not orders:
        await say(context, chat_id, "Замовлень поки немає.", kind="sticky")
        return
    waiting = sum(1 for o in orders if not o["ТТН"].strip())
    blocks = []
    for o in orders[-5:][::-1]:
        icon = "✅" if o["ТТН"].strip() else "⏳"
        who = o["Ініціали"] or "—"
        profit_txt = f" · прибуток {o['Прибуток']}₴" if o["Прибуток"] else ""
        blocks.append(
            f"{icon} №{o['№']} · {o['Дата']} · {o['Площадка']}\n"
            f"{o['Модель']} {o['Артикул']} · {o['Розмір EU']} · {who}\n"
            f"{o['Ціна продажу']}₴{profit_txt} · {o['Статус']}")
    head = f"Останні замовлення (⏳ без ТТН: {waiting})\n\n"
    await say(context, chat_id, head + "\n\n".join(blocks), kind="sticky")


async def show_ttn_picker(update, context):
    chat_id = update.effective_chat.id
    orders = [o for o in await asyncio.to_thread(load_orders) if not o["ТТН"].strip()]
    if not orders:
        await say(context, chat_id, "Усі замовлення мають ТТН ✅", kind="flash")
        return
    rows = []
    for o in orders[-10:][::-1]:
        who = (o["Ініціали"].split() or ["—"])[0]
        label = f"№{o['№']} · {who} · {o['Артикул']} {o['Розмір EU']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ttn:{o['№']}")])
    rows.append([CANCEL_BTN])
    await say(context, chat_id,
              "Для якого замовлення ТТН? Обери зі списку (показані замовлення без ТТН):",
              reply_markup=InlineKeyboardMarkup(rows))


def supplier_picker(context, suppliers):
    context.user_data["suppliers"] = [name for name, _ in suppliers]
    rows = [[InlineKeyboardButton(f"{name} · {n} поз.", callback_data=f"sup:{i}")]
            for i, (name, n) in enumerate(suppliers[:40])]
    rows.append([InlineKeyboardButton("➕ Новий постачальник", callback_data="supnew")])
    rows.append([CANCEL_BTN])
    text = "Від якого постачальника прайс? Обери зі списку (поруч кількість рядків, які вже є у складі):"
    return text, InlineKeyboardMarkup(rows)


async def show_supplier_picker(update, context):
    chat_id = update.effective_chat.id
    try:
        suppliers = await asyncio.to_thread(list_suppliers)
    except Exception as e:
        logging.exception("suppliers error")
        await say(context, chat_id, f"Не зміг прочитати склад: {e}", kind="flash", seconds=20)
        return
    if not suppliers:
        context.user_data["mode"] = "import_name"
        await say(context, chat_id, "Напиши назву постачальника (так, як вона буде в таблиці):",
                  reply_markup=CANCEL_KB)
        return
    text, kb = supplier_picker(context, suppliers)
    await say(context, chat_id, text, reply_markup=kb)


async def handle_order_text(update, context, text):
    chat_id = update.effective_chat.id
    track(context, update.message)
    order, missing = parse_order(text)
    if order is None:
        await say(context, chat_id,
                  "Не схоже на замовлення. Перший рядок має починатися зі слова «Замовлення». "
                  "Натисни «📝 Нове замовлення», щоб отримати шаблон.")
        return
    if missing:
        await say(context, chat_id, "Не зміг розібрати замовлення. Не вистачає:\n- " + "\n- ".join(missing))
        return

    stock_item, _ = await asyncio.to_thread(find_stock_row, order["article"], order["size_eu"], order["buy"])
    if stock_item:
        who = f", {stock_item['supplier']}" if stock_item["supplier"] else ""
        stock_line = f"\n\n📦 На складі є: {stock_item['qty']} шт{who} (після запису буде {stock_item['qty'] - 1})"
    else:
        stock_line = "\n\n⚠️ На складі такого артикулу з цим розміром не знайшов"

    key = uuid.uuid4().hex[:8]
    context.bot_data.setdefault("pending", {})[key] = order
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Записати", callback_data=f"ok:{key}"),
        InlineKeyboardButton("❌ Скасувати", callback_data=f"no:{key}"),
    ]])
    reset_mode(context)
    await say(context, chat_id, order_text(order) + stock_line, reply_markup=kb)


@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    try:
        # 1. кнопки меню: прибираємо їхній текст з чату, скидаємо попередній режим і сміття
        if text in MENU_BUTTONS:
            await safe_delete(context.bot, chat_id, update.message.message_id)
            await abandon(context)
            if text == BTN_SEARCH:
                await say(context, chat_id,
                          "Напиши артикул або модель і розмір.\nНаприклад: CN8490-100 43 або air max 90 42",
                          reply_markup=CANCEL_KB)
            elif text == BTN_ORDER:
                context.user_data["mode"] = "order"
                await show_order_template(update, context)
            elif text == BTN_TTN:
                await show_ttn_picker(update, context)
            elif text == BTN_LIST:
                await show_recent_orders(update, context)
            elif text == BTN_IMPORT:
                await show_supplier_picker(update, context)
            elif text == BTN_ADS:
                await ads.start(update, context)
            return

        if (context.user_data.get("mode") or "").startswith("ad_"):   # розмова «Оголошення»
            await ads.on_text(update, context, text)
            return

        await clear(context, "sticky")   # будь-який новий запит прибирає попередній результат
        mode = context.user_data.get("mode")

        # 2. ми чекаємо ТТН
        if mode == "ttn":
            track(context, update.message)
            ttn = clean_ttn(text)
            if not ttn:
                await say(context, chat_id,
                          "ТТН має складатися з цифр (від 8 до 20). Спробуй ще раз або натисни кнопку в меню.")
                return
            number = context.user_data.get("ttn_order")
            reset_mode(context)
            await save_ttn(context, chat_id, number, ttn)
            return

        # 3. ми чекаємо назву нового постачальника
        if mode == "import_name":
            track(context, update.message)
            name = text[:40]
            context.user_data["supplier"] = name
            context.user_data["mode"] = "import"
            await say(context, chat_id, f"Постачальник: «{name}».\nТепер надішли файл прайсу (.xlsx або .csv).",
                      reply_markup=CANCEL_KB)
            return

        if mode == "import":
            track(context, update.message)
            await say(context, chat_id,
                      f"Чекаю файл прайсу від «{context.user_data.get('supplier', '')}» (.xlsx або .csv). "
                      "Або обери іншу дію в меню.", reply_markup=CANCEL_KB)
            return

        # 4. замовлення (у режимі «Нове замовлення» або якщо текст починається зі слова «Замовлення»)
        first_line = text.splitlines()[0].lower() if text else ""
        if mode == "order" or "замовлення" in first_line:
            await handle_order_text(update, context, text)
            return

        # 5. решта це пошук наявності: питання і відповідь лишаються, поки не почнеш щось інше
        await clear(context, "tmp")
        track(context, update.message, kind="sticky")
        context.user_data["last_query"] = text
        answer = await asyncio.to_thread(search_stock, text, "buy")
        await say(context, chat_id, answer, kind="sticky", reply_markup=sort_kb("buy"))
    except Exception as e:
        logging.exception("text error")
        await say(context, chat_id, f"Помилка: {e}", kind="flash", seconds=20)


# ---------- Telegram: імпорт прайсу ----------

def selected_items(job):
    return [it for i, s in enumerate(job["sheets"]) if i in job["selected"] for it in s["items"]]


def skipped_of(job):
    """[(назва аркуша або '', рядок)] для вибраних аркушів."""
    many = len(job["sheets"]) > 1
    out = []
    for i, s in enumerate(job["sheets"]):
        if i in job["selected"]:
            out += [(s["name"] if many else "", r) for r in s["skipped_rows"]]
    return out


def import_panel(key, job):
    sheets, sel = job["sheets"], job["selected"]
    many = len(sheets) > 1
    lines = [f"📥 Прайс: {job['supplier']}", f"Файл: {job['filename']}", ""]
    if many:
        lines += ["Обери аркуші для імпорту (натисни на аркуш, щоб увімкнути чи вимкнути):", ""]
    total = skipped = 0
    for i, s in enumerate(sheets):
        label = f"«{s['name']}»" if many else "Таблиця"
        if s["error"]:
            lines.append(f"⚠️ {label}: не підходить, {s['error']}")
            continue
        on = i in sel
        sk = f", пропущено рядків: {len(s['skipped_rows'])}" if s["skipped_rows"] else ""
        lines.append(f"{'✅' if on else '⬜'} {label}: {s['count']} позицій{sk}")
        if on:
            total += s["count"]
            skipped += len(s["skipped_rows"])
            lines.append("   колонки: " + "; ".join(s["used"]))
            lines += [f"   ⚠️ {w}" for w in s["warnings"]]
    lines += ["", f"Разом до імпорту: {total} позицій"]
    chosen = selected_items(job)
    if chosen:
        lines += ["", "Приклад:"] + [
            f"{n}) {it['model']} {it['article']}, розмір {it['size']}, {it['qty']} шт, закуп {it['buy'] or '—'}"
            for n, it in enumerate(chosen[:3], start=1)]
    if job["existing"]:
        lines += ["", f"⚠️ У складі вже є {job['existing']} рядків постачальника «{job['supplier']}».",
                  "🔄 Замінити: видалити їх і поставити цей прайс (копія видалених піде в лист «Архів складу»).",
                  "➕ Додати: старі лишаються, оновляться тільки однакові артикул + розмір."]
    else:
        lines += ["", f"У складі ще немає рядків постачальника «{job['supplier']}»."]
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900].rsplit("\n", 1)[0] + "\n…"

    rows = []
    if many:
        for i, s in enumerate(sheets[:40]):
            nm = s["name"][:28]
            if s["error"]:
                rows.append([InlineKeyboardButton(f"⚠️ {nm}", callback_data=f"iew:{key}:{i}")])
            else:
                mark = "✅" if i in sel else "⬜"
                rows.append([InlineKeyboardButton(f"{mark} {nm} · {s['count']}", callback_data=f"ist:{key}:{i}")])
    if skipped:
        rows.append([InlineKeyboardButton(f"👁 Пропущені рядки ({skipped})", callback_data=f"isk:{key}")])
    if total:
        if job["existing"]:
            rows.append([
                InlineKeyboardButton(f"🔄 Замінити (−{job['existing']})", callback_data=f"iok:{key}:r"),
                InlineKeyboardButton("➕ Додати", callback_data=f"iok:{key}:a"),
            ])
        else:
            rows.append([InlineKeyboardButton("✅ Імпортувати", callback_data=f"iok:{key}:r")])
    rows.append([InlineKeyboardButton("❌ Скасувати", callback_data=f"ino:{key}")])
    return text, InlineKeyboardMarkup(rows)


def skipped_page(key, job, page, per=10):
    rows = skipped_of(job)
    total = len(rows)
    pages = max(1, -(-total // per))
    page = max(0, min(page, pages - 1))
    lines = [f"👁 Пропущені рядки: {total} (сторінка {page + 1}/{pages})", ""]
    for name, r in rows[page * per:(page + 1) * per]:
        where = f"«{name}» · " if name else ""
        lines.append(f"{where}рядок {r['row']}: {r['reason']}\n   {r['preview'] or '(порожньо)'}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"ikp:{key}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"ikp:{key}:{page + 1}"))
    kb = [nav] if nav else []
    kb.append([InlineKeyboardButton("✖ Закрити", callback_data=f"ikx:{key}")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


@owner_only
async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    doc = update.message.document
    track(context, update.message)
    await clear(context, "sticky")
    supplier = (update.message.caption or "").strip()
    if not supplier and context.user_data.get("mode") == "import":
        supplier = context.user_data.get("supplier", "")
    if not supplier:
        await say(context, chat_id,
                  "Спочатку натисни «📥 Імпорт прайсу» і обери постачальника. "
                  "Або надішли файл ще раз з назвою постачальника в підписі.")
        return
    name = doc.file_name or ""
    if not name.lower().endswith((".xlsx", ".csv")):
        await say(context, chat_id,
                  "Приймаю тільки .xlsx або .csv. Якщо в тебе .xls, PDF або Google-таблиця, "
                  "збережи її як .xlsx (у Google Таблицях: Файл → Завантажити → Microsoft Excel).")
        return
    too_big = (
        "❌ Файл завеликий{size}. Telegram дозволяє ботам скачувати файли лише до 20 МБ.\n\n"
        "Що можна зробити:\n"
        "1) Прибрати з файлу картинки (саме вони зазвичай роблять xlsx таким важким).\n"
        "2) Залишити тільки потрібні аркуші й надіслати частинами.\n"
        "3) Зберегти потрібний аркуш як .csv (він в рази легший).")
    if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
        await say(context, chat_id, too_big.format(size=f" ({doc.file_size / 1024 / 1024:.0f} МБ)"))
        return

    wait = await say(context, chat_id, "⏳ Читаю файл...")
    try:
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        sheets = await asyncio.to_thread(parse_price_file, name, data)
        existing = await asyncio.to_thread(count_supplier_rows, supplier)
    except ValueError as e:
        await safe_delete(context.bot, chat_id, wait.message_id)
        await say(context, chat_id, f"❌ {e}")
        return
    except Exception as e:
        await safe_delete(context.bot, chat_id, wait.message_id)
        if "too big" in str(e).lower():
            await say(context, chat_id, too_big.format(size=""))
        else:
            logging.exception("import read error")
            await say(context, chat_id, f"Не зміг прочитати файл: {e}")
        return
    await safe_delete(context.bot, chat_id, wait.message_id)

    key = uuid.uuid4().hex[:8]
    pend = context.bot_data.setdefault("pending_import", {})
    pend.clear()   # лишається тільки останній імпорт
    job = {"supplier": supplier, "filename": name, "sheets": sheets, "existing": existing,
           "selected": {i for i, s in enumerate(sheets) if not s["error"]}}
    pend[key] = job
    reset_mode(context)
    text, kb = import_panel(key, job)
    await say(context, chat_id, text, reply_markup=kb)


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    parts = q.data.split(":")
    action = parts[0]
    key = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    pend = context.bot_data.get("pending_import", {})
    job = pend.get(key) if action.startswith("i") else None

    # --- скасування будь-якої дії ---
    if action == "cancel":
        await safe_answer(q)
        context.bot_data.get("pending_import", {}).clear()
        reset_mode(context)
        await clear(context)
        await safe_delete(context.bot, chat_id, q.message.message_id)   # якщо повідомлення не відстежувалось
        await say(context, chat_id, "❌ Скасовано", kind="flash", seconds=4)
        return

    if action == "ad":
        await ads.on_button(update, context, parts)
        return

    # відповіді-сповіщення (спливаюче вікно)
    if action == "iew":
        text = "Аркуш не підійшов"
        if job:
            try:
                text = job["sheets"][int(arg)]["full_error"][:190]
            except (ValueError, IndexError):
                pass
        await safe_answer(q, text, alert=True)
        return
    if action == "iok" and job is not None and not job["selected"]:
        await safe_answer(q, "Обери хоча б один аркуш", alert=True)
        return
    await safe_answer(q)

    # --- вибір замовлення для ТТН ---
    if action == "ttn":
        context.user_data["mode"] = "ttn"
        context.user_data["ttn_order"] = key
        await q.edit_message_text(f"Замовлення №{key}. Напиши номер ТТН:", reply_markup=CANCEL_KB)
        return

    # --- вибір постачальника для імпорту ---
    if action == "sup":
        suppliers = context.user_data.get("suppliers", [])
        try:
            name = suppliers[int(key)]
        except (ValueError, IndexError):
            await q.edit_message_text("Список застарів. Натисни «📥 Імпорт прайсу» ще раз.")
            return
        context.user_data["supplier"] = name
        context.user_data["mode"] = "import"
        await q.edit_message_text(
            f"Постачальник: «{name}».\nТепер надішли файл прайсу (.xlsx або .csv).",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Інший постачальник", callback_data="supback"), CANCEL_BTN]]))
        return
    if action == "supback":
        context.user_data.pop("mode", None)
        context.user_data.pop("supplier", None)
        try:
            suppliers = await asyncio.to_thread(list_suppliers)
            text, kb = supplier_picker(context, suppliers)
            await q.edit_message_text(text, reply_markup=kb)
        except Exception as e:
            logging.exception("suppliers error")
            await q.edit_message_text(f"Не зміг прочитати склад: {e}", reply_markup=CANCEL_KB)
        return
    if action == "supnew":
        context.user_data["mode"] = "import_name"
        await q.edit_message_text("Напиши назву нового постачальника (так, як вона буде в таблиці):",
                                  reply_markup=CANCEL_KB)
        return

    # --- перемикання сортування в результатах пошуку ---
    if action == "srt":
        query = context.user_data.get("last_query")
        if not query:
            await q.edit_message_text("Запит застарів. Напиши артикул ще раз.")
            return
        sort = "size" if key == "size" else "buy"
        answer = await asyncio.to_thread(search_stock, query, sort)
        await q.edit_message_text(answer, reply_markup=sort_kb(sort))
        return

    # --- імпорт: панель аркушів, пропущені рядки, підтвердження ---
    if action in ("ist", "isk", "ikp", "ikx", "iok", "ino"):
        if action == "ikx":
            await safe_delete(context.bot, chat_id, q.message.message_id)
            return
        if job is None:
            await q.edit_message_text("Цей імпорт вже оброблений або застарів. Надішли файл ще раз.")
            return
        if action == "ist":
            try:
                i = int(arg)
                if not job["sheets"][i]["error"]:
                    job["selected"] ^= {i}
            except (ValueError, IndexError):
                pass
            text, kb = import_panel(key, job)
            await q.edit_message_text(text, reply_markup=kb)
            return
        if action == "isk":
            text, kb = skipped_page(key, job, 0)
            await say(context, chat_id, text, reply_markup=kb)
            return
        if action == "ikp":
            try:
                page = int(arg)
            except ValueError:
                page = 0
            text, kb = skipped_page(key, job, page)
            await q.edit_message_text(text, reply_markup=kb)
            return
        if action == "ino":
            pend.pop(key, None)
            await clear(context)
            await say(context, chat_id, "❌ Скасовано. Склад не змінено.", kind="flash")
            return
        # iok
        pend.pop(key, None)
        mode = "add" if arg == "a" else "replace"
        items = selected_items(job)
        try:
            await q.edit_message_text("⏳ Імпортую, зачекай...")
            deleted, added, kept = await asyncio.to_thread(apply_import, job["supplier"], items, mode)
            await clear(context)
            how = "додано до наявних (дублі оновлено)" if mode == "add" else "старі рядки замінено"
            archive = f"\nКопія видалених рядків: лист «{ARCHIVE_SHEET}»" if deleted else ""
            await say(context, chat_id,
                      f"✅ Готово: «{job['supplier']}», {how}\n"
                      f"Додано рядків: {added}\nВидалено старих рядків: {deleted}\n"
                      f"Ціни продажу збережено для рядків: {kept}{archive}",
                      kind="sticky")
        except Exception as e:
            logging.exception("import error")
            await clear(context)
            await say(context, chat_id,
                      f"Помилка імпорту: {e}\nПеревір таблицю складу. Якщо рядки зникли, "
                      f"їхня копія лежить у листі «{ARCHIVE_SHEET}».", kind="sticky")
        return

    # --- підтвердження замовлення ---
    order = context.bot_data.get("pending", {}).pop(key, None)
    if order is None:
        await q.edit_message_text("Це замовлення вже оброблене або застаріло. Надішли його ще раз.")
        return
    if action == "no":
        await clear(context)
        await say(context, chat_id, "❌ Скасовано. У таблицю нічого не записано.", kind="flash")
        return
    try:
        number, note = await asyncio.to_thread(save_order, order)
        await clear(context)
        await say(context, chat_id,
                  f"✅ Записано як замовлення №{number}\n{note}\n\nТТН додаси кнопкою «🚚 Додати ТТН».",
                  kind="flash", seconds=15)
        if config.ORDERS_CHAT_ID:
            await context.bot.send_message(config.ORDERS_CHAT_ID, order_text(order, number))
    except Exception as e:
        logging.exception("save error")
        await clear(context)
        await say(context, chat_id, f"Помилка запису: {e}\nУ таблицю нічого не записано.",
                  kind="flash", seconds=20)


@owner_only
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ads.on_photo(update, context)


async def post_init(app):
    await app.bot.set_my_commands([
        ("menu", "Показати меню з кнопками"),
        ("cancel", "Скасувати поточну дію"),
        ("ttn", "Додати ТТН: /ttn НОМЕР ТТН"),
        ("chatid", "Дізнатись chat_id чату"),
    ])


async def on_error(update, context):
    """Короткі повідомлення замість довгих трасувань для дрібних помилок мережі."""
    from telegram.error import BadRequest, NetworkError
    err = context.error
    if isinstance(err, NetworkError):       # у тому числі TimedOut: короткий збій зв'язку з Telegram
        logging.warning("Зв'язок із Telegram: %s. Не критично, бот продовжує працювати.", str(err)[:80])
        return
    if isinstance(err, BadRequest) and "too old" in str(err).lower():
        logging.info("Застаріле натискання кнопки проігноровано.")
        return
    logging.error("Помилка в боті", exc_info=err)


def main():
    ads.init(sys.modules[__name__])
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("ttn", cmd_ttn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.PRIVATE, on_document))
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.ChatType.PRIVATE, on_photo))
    app.add_error_handler(on_error)
    print("Бот запущено. Щоб зупинити, натисни Ctrl+C.")
    app.run_polling()


if __name__ == "__main__":
    main()
