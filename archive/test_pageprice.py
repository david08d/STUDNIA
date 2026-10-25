"""Тест: чи може бот сам дістати ціну зі сторінки магазину (OpenSERP лише дає посилання).

Три способи, від надійного до запасного:
  1) JSON-LD (schema.org Product → offers → price)
  2) мета-теги (product:sale_price:amount, product:price:amount, og:price:amount)
  3) перша ціна у видимому тексті після заголовка h1 («4 790 ₴»)
Якщо є і поточна, і стара (закреслена) ціна, береться ПОТОЧНА.

Запуск:
    python test_pageprice.py https://site1/... https://site2/...
    python test_pageprice.py --file openserp_extract.json --article DH5128-001
Ключі не потрібні. Сторінки відкриваються з твого ПК так само, як у браузері без входу в акаунт."""
import json
import re
import statistics
import sys
import time

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.5",
}
LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
META = re.compile(r"<meta\b[^>]*>", re.I)
ATTR = re.compile(r'([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
TEXT_PRICE = re.compile(r"(?<!\d[\s\u00a0\u202f])(?<!\d)(\d{1,3}(?:[\s\u00a0\u202f]\d{3})+|\d{3,6})\s?(?:грн|₴|UAH)", re.I)
BLOCK_WORDS = ("just a moment", "captcha", "cf-chl", "attention required", "access denied", "enable javascript")


def to_num(v):
    if v is None:
        return None
    s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^0-9.]", "", s)
    try:
        n = float(s)
    except ValueError:
        return None
    return int(round(n)) if 100 <= n <= 500000 else None


def walk_offers(o, found):
    if isinstance(o, dict):
        offers = o.get("offers")
        if offers is not None:
            for of in (offers if isinstance(offers, list) else [offers]):
                if isinstance(of, dict):
                    for k in ("price", "lowPrice"):
                        n = to_num(of.get(k))
                        if n:
                            found.append(n)
                    spec = of.get("priceSpecification")
                    for sp in (spec if isinstance(spec, list) else [spec]):
                        if isinstance(sp, dict) and to_num(sp.get("price")):
                            found.append(to_num(sp["price"]))
        for v in o.values():
            walk_offers(v, found)
    elif isinstance(o, list):
        for v in o:
            walk_offers(v, found)


def from_jsonld(html):
    found = []
    for raw in LD.findall(html):
        try:
            walk_offers(json.loads(raw.strip()), found)
        except ValueError:
            continue
    found = list(dict.fromkeys(found))
    if not found:
        return None
    return {"price": min(found), "old": None, "note": f"у JSON-LD цін: {found[:4]}"}


def metas(html):
    out = {}
    for tag in META.findall(html):
        attrs = {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
                 for m in ATTR.finditer(tag)}
        key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        if key and "content" in attrs:
            out.setdefault(key.lower(), attrs["content"])
    return out


def from_meta(html):
    m = metas(html)
    sale = to_num(m.get("product:sale_price:amount"))
    regular = to_num(m.get("product:price:amount")) or to_num(m.get("og:price:amount")) \
        or to_num(m.get("price"))
    if sale:    # у product:price:amount тоді лежить СТАРА ціна
        return {"price": sale, "old": regular if regular and regular != sale else None, "note": "мета-тег sale_price"}
    if regular:
        return {"price": regular, "old": None, "note": "мета-тег price"}
    return None


def visible_text(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", html)
    h1 = re.search(r"(?is)<h1\b.*?>", html)
    if h1:
        html = html[h1.start():]
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


def from_text(html):
    text = visible_text(html)[:4000]
    nums = [to_num(x) for x in TEXT_PRICE.findall(text)]
    nums = [n for n in nums if n]
    if not nums:
        return None
    return {"price": nums[0], "old": nums[1] if len(nums) > 1 and nums[1] > nums[0] else None,
            "note": "перша ціна після заголовка (перевір на сайті)"}


def get_price(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
    except requests.RequestException as e:
        return {"status": "помилка", "detail": str(e)[:120]}
    html = r.text
    low = html[:6000].lower()
    if r.status_code in (403, 429, 503) or (len(html) < 15000 and any(w in low for w in BLOCK_WORDS)):
        return {"status": f"ЗАБЛОКОВАНО (HTTP {r.status_code})", "detail": "сайт не пустив бота"}
    if r.status_code != 200:
        return {"status": f"HTTP {r.status_code}", "detail": ""}
    for source, fn in (("JSON-LD", from_jsonld), ("мета-теги", from_meta), ("текст", from_text)):
        res = fn(html)
        if res:
            return {"status": "ok", "source": source, **res}
    return {"status": "ціну не знайдено", "detail": f"розмір сторінки {len(html)} символів; можливо, ціна підвантажується скриптом"}


def urls_from_file(path, article):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    norm = lambda s: re.sub(r"[^A-Za-z0-9]", "", s or "").upper()
    full = norm(article)
    parts = [p for p in re.split(r"[-_\s]+", article) if p]
    base = norm(parts[0]) if len(parts) > 1 and len(parts[0]) >= 5 else None
    out = []
    for it in data.get("results") or []:
        blob = norm(" ".join(str(it.get(k) or "") for k in ("title", "snippet", "url")))
        if full in blob:
            out.append((it["url"], "точний артикул"))
        elif base and base in blob:
            out.append((it["url"], "та сама модель, інший колір"))
    return out


args = sys.argv[1:]
targets = []
if "--file" in args:
    i = args.index("--file")
    path = args[i + 1]
    del args[i:i + 2]
    art = ""
    if "--article" in args:
        j = args.index("--article")
        art = args[j + 1]
        del args[j:j + 2]
    if not art:
        sys.exit("Додай артикул: --article DH5128-001")
    targets += urls_from_file(path, art)
targets += [(a, "") for a in args if a.startswith("http")]
if not targets:
    sys.exit(__doc__)

exact_prices, model_prices = [], []
for n, (url, kind) in enumerate(targets[:12], start=1):
    print(f"\n[{n}] {url}" + (f"\n    {kind}" if kind else ""))
    res = get_price(url)
    if res["status"] != "ok":
        print(f"    {res['status']} {res.get('detail', '')}")
    else:
        old = f" (стара ціна {res['old']}₴)" if res.get("old") else ""
        print(f"    ціна: {res['price']}₴{old} | джерело: {res['source']} ({res['note']})")
        (model_prices if kind.startswith("та сама") else exact_prices).append(res["price"])
    time.sleep(1)

print("\n" + "=" * 60)
for label, vals in (("ТОЧНИЙ АРТИКУЛ", exact_prices), ("ТА САМА МОДЕЛЬ, ІНШИЙ КОЛІР", model_prices)):
    if vals:
        mid = f", медіана {int(statistics.median(vals))}₴" if len(vals) > 1 else ""
        print(f"{label}: цін {len(vals)}: {sorted(vals)}{mid}")
    elif label == "ТОЧНИЙ АРТИКУЛ":
        print(f"{label}: цін не знайдено")
