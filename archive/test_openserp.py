"""Тест OpenSERP: чи знаходить він магазини й оголошення з цінами саме на твій артикул.

Спершу запусти сам OpenSERP, потім у папці з ботом:
    python test_openserp.py DH5128-001
    python test_openserp.py "adidas samba IG7601"
Артикул має бути ОСТАННІМ словом запиту.

Додатково:
    --url http://127.0.0.1:8080   інша адреса сервера
    --bing                        додати окремий запуск через Bing (у тестах він часто дає сміття)
    --google                      додати Google (з домашньої IP зазвичай капча)
    --file openserp_extract.json  не запускати пошук, а розібрати вже збережену відповідь (сервер не потрібен)

Як рахується збіг:
    точний артикул     DH5128-001 = DH5128 001 = DH5128001 (але не DH5128-0010 і не IG7601-010 для IG7601)
    та сама модель     DH5128-101 для запиту DH5128-001: та сама модель, але ІНШИЙ колір. Ціни йдуть окремо
Ціни беруться лише поруч з артикулом (до 400 символів), на сторінці береться найближча.
Ключі не потрібні. Відповідь запиту з відкриттям сторінок зберігається в openserp_extract.json."""
import json
import re
import statistics
import sys

import requests

args = list(sys.argv[1:])
URL = "http://127.0.0.1:7000"
FILE = None
if "--url" in args:
    i = args.index("--url")
    URL = args[i + 1].rstrip("/")
    del args[i:i + 2]
if "--file" in args:
    i = args.index("--file")
    FILE = args[i + 1]
    del args[i:i + 2]
USE_GOOGLE = "--google" in args
USE_BING = "--bing" in args
args = [a for a in args if a not in ("--google", "--bing")]
query = " ".join(args).strip() or "adidas samba IG7601"
ARTICLE = query.split()[-1]

PRICE = re.compile(r"(\d{1,3}(?:[\s\u00a0\u202f]\d{3})+|\d{3,6})\s?(?:грн|₴|UAH)|(?:₴|UAH)\s?(\d{1,3}(?:[\s\u00a0\u202f]\d{3})+|\d{3,6})", re.I)
SHAFA_INDEX = re.compile(r"Середня\s+ціна\s+продажу", re.I)
found_exact, found_model, shafa_idx = {}, {}, []


def article_regex(art):
    parts = [p for p in re.split(r"[-_\s]+", art) if p]
    body = r"[-_\s]?".join(re.escape(p) for p in parts)
    tail = r"(?![A-Za-z0-9])"
    if len(parts) == 1:   # артикул без дефіса не має бути початком довшого коду типу IG7601-010
        tail = r"(?![A-Za-z0-9]|[-_]\d{3}(?!\d))"
    return re.compile(r"(?<![A-Za-z0-9])" + body + tail, re.I)


PARTS = [p for p in re.split(r"[-_\s]+", ARTICLE) if p]
EXACT = article_regex(ARTICLE)
# «модель» = перша частина артикула Nike-типу (DH5128 з DH5128-001): у ній закодований силует, суфікс це колір
BASE = (re.compile(r"(?<![A-Za-z0-9])" + re.escape(PARTS[0]) + r"(?![A-Za-z0-9])", re.I)
        if len(PARTS) > 1 and len(PARTS[0]) >= 5 else None)


def norm(s):
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def prices_in(text):
    out = []
    for a, b in PRICE.findall(text or ""):
        n = int(re.sub(r"\D", "", a or b))
        if 300 <= n <= 100000:
            out.append(n)
    return out


def prices_near(text, rx, window=400):
    """Ціни в межах window символів від збігу, від найближчої до найдальшої."""
    prices = [(m.start(), m.end(), int(re.sub(r"\D", "", m.group(1) or m.group(2))))
              for m in PRICE.finditer(text)]
    scored = []
    for am in rx.finditer(text):
        for s, e, n in prices:
            if not 300 <= n <= 100000:
                continue
            dist = (s - am.end()) if s >= am.end() else (am.start() - e)
            if dist <= window:
                scored.append((max(dist, 0), n))
    scored.sort(key=lambda x: x[0])
    return list(dict.fromkeys(n for _, n in scored))     # без повторів, найближча перша


def flatten(it):
    parts = [str(it.get(k) or "") for k in ("title", "snippet", "url")]
    ex = it.get("extracted")
    if ex:
        parts.append(ex if isinstance(ex, str) else json.dumps(ex, ensure_ascii=False))
    return "\n".join(parts)


def classify(text):
    if EXACT.search(text):
        return "exact", prices_near(text, EXACT)
    if BASE and BASE.search(text):
        return "model", prices_near(text, BASE)
    if norm(ARTICLE) in norm(text):
        return "similar", []
    return "no", []


LABELS = {"exact": "так", "model": "ТА САМА МОДЕЛЬ, інший колір/варіант",
          "similar": "ні (схожий код, ІНШИЙ товар)", "no": "ні"}


def analyze(data):
    meta = data.get("meta") or {}
    failed = meta.get("engines_failed") or meta.get("failed_engines") or meta.get("failed")
    if failed:
        print("Рушії з помилкою:", failed)
    items = data.get("results") or []
    count = {"exact": 0, "model": 0, "similar": 0, "no": 0}
    priced = 0
    for it in items:
        text = flatten(it)
        kind, pr = classify(text)
        count[kind] += 1
        priced += bool(pr)
        if kind in ("exact", "model") and it.get("url"):
            store = found_exact if kind == "exact" else found_model
            store.setdefault(it["url"], [])
            for p in pr:
                if p not in store[it["url"]]:
                    store[it["url"]].append(p)
        extra = ""
        if it.get("extracted") and SHAFA_INDEX.search(text):
            m = SHAFA_INDEX.search(text)
            nums = prices_in(text[m.start(): m.start() + 300])
            shafa_idx.append(nums[:3])
            extra = f"\n  ★ є блок «Середня ціна продажу»: {nums[:3] if nums else 'цифр не видно'}"
        got = "є" if it.get("extracted") else "—"
        print(f"- {str(it.get('title') or '')[:70]}  [{it.get('engine') or ''}]\n  {it.get('url')}\n"
              f"  артикул збігся: {LABELS[kind]} | ціни біля нього: {pr[:4] if pr else '—'} | вміст сторінки: {got}{extra}")
    print(f"\nПідсумок: результатів {len(items)}; точний артикул {count['exact']}, та сама модель {count['model']}, "
          f"схожий код іншого товару {count['similar']}, не по темі {count['no']}; з цінами {priced}")


def check_server():
    try:
        r = requests.get(URL + "/health", timeout=5)
        print(f"Сервер відповідає: {URL}/health -> HTTP {r.status_code}")
        return True
    except requests.RequestException as e:
        print(f"Не можу підключитися до {URL}.\nПеревір, що OpenSERP запущений (вікно з ним не закривай).\n({e})")
        return False


def search(label, path, params, save_as=None):
    print(f"\n===== {label} =====")
    try:
        r = requests.get(URL + path, params=params, timeout=170)
    except requests.RequestException as e:
        print("Помилка запиту:", e)
        return
    print(f"HTTP {r.status_code}")
    if save_as:
        try:
            with open(save_as, "w", encoding="utf-8") as f:
                f.write(r.text)
        except OSError:
            pass
    if r.status_code != 200:
        print(r.text[:500])
        if "captcha" in r.text.lower():
            print("-> цей рушій показує капчу, з цієї IP він не підходить")
        return
    try:
        analyze(r.json())
    except ValueError:
        print("Відповідь не JSON:", r.text[:300])


def price_block(title, pages, note=""):
    priced = {u: p for u, p in pages.items() if p}
    print(f"{title}: сторінок {len(pages)}, з цінами {len(priced)}{note}")
    for u, p in list(priced.items())[:8]:
        print(f"  {p[:3]}  {u}")
    vals = [p[0] for p in priced.values()]    # на кожній сторінці беремо найближчу до артикула ціну
    if len(vals) >= 2:
        print(f"  медіана {int(statistics.median(vals))}₴ (від {min(vals)} до {max(vals)}) за {len(vals)} цінами")
    elif vals:
        print(f"  ціна лише одна: {vals[0]}₴")
    else:
        print("  цін не знайдено")


if FILE:
    print(f"Розбираю збережену відповідь: {FILE}\nАртикул: {ARTICLE}")
    try:
        with open(FILE, encoding="utf-8") as f:
            analyze(json.load(f))
    except (OSError, ValueError) as e:
        sys.exit(f"Не вдалося прочитати файл: {e}")
else:
    if not check_server():
        sys.exit(1)
    base = {"lang": "UK", "region": "UA", "limit": 10, "engines": "duckduckgo", "mode": "balanced"}
    search("SHAFA (DuckDuckGo) + вміст сторінок у режимі rendered", "/mega/search",
           {**base, "text": query, "site": "shafa.ua", "limit": 6, "extract": 2, "extract_mode": "rendered"})
    search("OLX (DuckDuckGo)", "/mega/search", {**base, "text": query, "site": "olx.ua"})
    search("БЕЗ ФІЛЬТРА САЙТУ (DuckDuckGo)", "/mega/search", {**base, "text": f"{query} ціна грн"})
    search("БЕЗ ФІЛЬТРА + ВМІСТ СТОРІНОК rendered (DuckDuckGo)", "/mega/search",
           {**base, "text": f"{query} ціна грн", "limit": 6, "extract": 3, "extract_mode": "rendered"},
           save_as="openserp_extract.json")
    if USE_BING:
        search("BING", "/mega/search", {**base, "engines": "bing", "text": f"{query} ціна грн"})
    if USE_GOOGLE:
        search("GOOGLE", "/google/search", {"lang": "UK", "region": "UA", "text": f"{query} ціна грн", "limit": 5})

print("\n" + "=" * 60)
price_block("ТОЧНИЙ АРТИКУЛ", found_exact)
if BASE:
    price_block("ТА САМА МОДЕЛЬ, ІНШІ КОЛЬОРИ (орієнтир, колір інший)", found_model)
if shafa_idx:
    print(f"БЛОК «Середня ціна продажу» з Shafa знайдено на сторінках: {len(shafa_idx)}; цифри: {shafa_idx[:3]}")
else:
    print("Блок «Середня ціна продажу» з Shafa в отриманому вмісті не знайдено.")
