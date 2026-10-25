"""Ціни на сайтах магазинів: OpenSERP (DuckDuckGo) шукає посилання, бот сам читає ціну зі сторінок.
Ключі не потрібні. Потрібен запущений OpenSERP (адреса у config.py: OPENSERP_URL)."""
import importlib.util
import json
import re
import time
from urllib.parse import quote, unquote, urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor

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
PRIORITY_SITES = ["tcross.shop", "yesoriginal.com.ua", "idealsport.com.ua", "footballmall.com.ua",
                  "solidniyshop.com", "lishop.store", "krosshouse.com.ua", "intertop.ua",
                  "deltasport.ua", "ultrasport.in.ua"]
MIN_PRICES = 3      # скільки цін досить; якщо у твоїх магазинах менше, добираємо з інших сайтів
SKIP_DOMAINS = ("ebay.", "aliexpress.", "amazon.", "pinterest.", "facebook.", "instagram.", "youtube.")


class SearchError(Exception):
    pass


def to_num(v):
    if v is None:
        return None
    s = re.sub(r"[^0-9.]", "", str(v).replace(",", "."))
    try:
        n = float(s)
    except ValueError:
        return None
    return int(round(n)) if 200 <= n <= 200000 else None


def _walk(o, found):
    if isinstance(o, dict):
        offers = o.get("offers")
        if offers is not None:
            for of in (offers if isinstance(offers, list) else [offers]):
                if isinstance(of, dict):
                    cur = str(of.get("priceCurrency") or "UAH").upper()
                    if cur not in ("UAH", "ГРН"):       # ціни в доларах і євро нам не підходять
                        continue
                    for k in ("price", "lowPrice"):
                        n = to_num(of.get(k))
                        if n:
                            found.append(n)
        for v in o.values():
            _walk(v, found)
    elif isinstance(o, list):
        for v in o:
            _walk(v, found)


def _from_jsonld(html):
    found = []
    for raw in LD.findall(html):
        try:
            _walk(json.loads(raw.strip()), found)
        except ValueError:
            continue
    return min(found) if found else None


def _from_meta(html):
    m = {}
    for tag in META.findall(html):
        a = {x.group(1).lower(): (x.group(2) if x.group(2) is not None else x.group(3)) for x in ATTR.finditer(tag)}
        key = a.get("property") or a.get("name") or a.get("itemprop")
        if key and "content" in a:
            m.setdefault(key.lower(), a["content"])
    # у product:price:amount при знижці лежить СТАРА ціна, тому sale_price першим
    for k in ("product:sale_price:amount", "product:price:amount", "og:price:amount", "price"):
        n = to_num(m.get(k))
        if n:
            return n
    return None


STRIKE_TAG = re.compile(r"(?is)<(s|del|strike)\b[^>]*>.{0,300}?</\1>")
STRIKE_CLASS = re.compile(r"""(?is)<(\w+)\b[^>]*(?:class|style)\s*=\s*["'][^"']*(?<![a-z])"""
                          r"""(?:old|prev|previous|was|crossed|strike|strikethrough|line-through|discount-old)"""
                          r"""(?![a-z])[^"']*["'][^>]*>.{0,300}?</\1>""")


def _from_text(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", html)
    html = STRIKE_TAG.sub(" ", html)        # закреслена СТАРА ціна не потрібна
    html = STRIKE_CLASS.sub(" ", html)
    h1 = re.search(r"(?is)<h1\b.*?>", html)
    if h1:
        html = html[h1.start():]
    text = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))[:4000]
    nums = [to_num(x) for x in TEXT_PRICE.findall(text)]
    nums = [n for n in nums if n]
    return nums[0] if nums else None


def parse_html(html, rx=None):
    """(ціна, джерело) зі сторінки. Якщо JSON-LD сильно дешевший за ціну в тексті, довіряємо тексту."""
    if rx is not None and not rx.search(html):
        return None, "на сторінці немає цього артикула"
    ld, meta, txt = _from_jsonld(html), _from_meta(html), _from_text(html)
    if ld and txt and ld < 0.4 * txt:
        return txt, "текст (JSON-LD дає підозріло малу ціну)"
    if ld:
        return ld, "JSON-LD"
    if meta:
        return meta, "мета-теги"
    if txt:
        return txt, "текст"
    return None, ""


def page_price_ex(url, rx=None):
    """(ціна, причина невдачі або джерело ціни)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        return None, f"не відкрилась ({type(e).__name__})"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}" + (" (сайт не пускає бота)" if r.status_code in (403, 429, 503) else "")
    html = r.text
    if len(html) < 15000 and any(w in html[:6000].lower() for w in BLOCK_WORDS):
        return None, "сторінка-заглушка (капча/перевірка)"
    n, src = parse_html(html, rx)
    if n:
        return n, src
    if src:
        return None, src
    return None, f"ціни немає в коді сторінки ({len(html)} символів), її малює скрипт"


def _goto(page, url, wait=2500):
    """Відкриває сторінку; якщо вона довго вантажиться, працюємо з тим, що встигло завантажитись."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        if "Timeout" not in type(e).__name__ and "Timeout" not in str(e)[:40]:
            raise
    page.wait_for_timeout(wait)


def browser_pages(urls, log=lambda *a: None):
    """Відкриває сторінки справжнім браузером (Playwright). {url: html або None}."""
    out = {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("Браузер для сторінок не встановлено (pip install playwright, потім playwright install chromium)")
        return {u: None for u in urls}
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=HEADERS["User-Agent"], locale="uk-UA")
            for u in urls:
                page = None
                try:
                    page = ctx.new_page()
                    _goto(page, u)
                    out[u] = page.content()
                except Exception as e:
                    log(f"   браузер не відкрив {u[:70]}: {type(e).__name__}")
                    out[u] = None
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
            br.close()
    except Exception as e:
        log(f"Браузер не запустився: {type(e).__name__}: {str(e)[:100]}")
    return {u: out.get(u) for u in urls}


def browser_prices(urls, log=lambda *a: None, rx=None):
    """Запасний шлях: {url: (ціна, джерело)} зі сторінок, відкритих браузером."""
    res = {}
    for u, html in browser_pages(urls, log).items():
        if html is None:
            res[u] = (None, "браузер не відкрив сторінку")
            continue
        n, src = parse_html(html, rx)
        res[u] = (n, f"браузер, {src}") if n else (None, "браузер: " + (src or "ціни не видно"))
    return res


HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)


def product_links(html, base_url, rx, domain):
    """Посилання зі сторінки пошуку магазину, в адресі яких є потрібний артикул."""
    out = []
    for h in HREF.findall(html):
        url = urljoin(base_url, h.replace("&amp;", "&"))
        path = unquote(urlparse(url).path)      # артикул шукаємо лише в шляху, а не в ?search=...
        if rx.search(path) and not re.search(r"search|/page-\d+", path, re.I) \
                and _prio_of(_site(url), [domain]) and url not in out:
            out.append(url)
    return out


SEARCH_INPUTS = ['input[type="search"]', 'input[name="q"]', 'input[name="s"]', 'input[name="search"]',
                 'input[name="query"]', 'input[name="keyword"]', 'input[placeholder*="ошук" i]',
                 'input[placeholder*="earch" i]', 'input[class*="search" i]', 'input[id*="search" i]']
SEARCH_OPENERS = ['[class*="search-icon" i]', '[class*="search" i] button', 'button[class*="search" i]',
                  'a[class*="search" i]', '[class*="search" i]', 'img[src*="search" i]']


def _find_input(page):
    for sel in SEARCH_INPUTS:
        try:
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    return el
        except Exception:
            continue
    return None


def search_by_typing(domains, article, model, rx, log, want=3):
    """Відкриває головну сторінку кожного сайту справжнім браузером, вводить у його пошук
    «модель + артикул» і збирає адреси карток з цим артикулом. {domain: url}.
    Це працює без знання адрес пошуку: бот поводиться як людина."""
    found = {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("Браузер не встановлено (pip install playwright, потім playwright install chromium)")
        return found
    queries = list(dict.fromkeys([f"{model} {article}".strip(), article]))
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=HEADERS["User-Agent"], locale="uk-UA")
            for dom in domains:
                if len(found) >= want:
                    break
                home = f"https://{dom}/"
                page = None
                try:
                    page = ctx.new_page()
                    _goto(page, home, 1500)
                    for query in queries:
                        box = _find_input(page)
                        if box is None:       # поле пошуку може ховатись за значком лупи
                            for sel in SEARCH_OPENERS:
                                try:
                                    for el in page.query_selector_all(sel):
                                        if el.is_visible():
                                            el.click(timeout=2000)
                                            page.wait_for_timeout(600)
                                            break
                                except Exception:
                                    continue
                                box = _find_input(page)
                                if box:
                                    break
                        if box is None:
                            log(f"{dom}: не знайшов поле пошуку на сторінці")
                            break
                        box.click()
                        box.fill(query)
                        box.press("Enter")
                        try:
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                        page.wait_for_timeout(2000)
                        links = product_links(page.content(), page.url, rx, dom)
                        log(f"{dom}: ввів у пошук «{query}», карток з артикулом {len(links)}")
                        if links:
                            found[dom] = links[0]
                            break
                        _goto(page, home, 1000)
                except Exception as e:
                    log(f"{dom}: не вдалося ({type(e).__name__}: {str(e)[:80]})")
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
            br.close()
    except Exception as e:
        log(f"Браузер не запустився: {type(e).__name__}: {str(e)[:100]}")
    return found


def store_search(template, article, model, rx, domain, log):
    """Пошук на самому сайті магазину за адресою-шаблоном. У шаблоні {q} це «модель + артикул»
    (наприклад «adidas samba IG7601»), {article} лише артикул, {model} лише модель.
    Спершу пробуємо «модель + артикул», потім лише артикул. Повертає адресу товару або None."""
    full = f"{model} {article}".strip()
    for query in dict.fromkeys([full, article]):
        url = (template.replace("{q}", quote(query)).replace("{article}", quote(article))
               .replace("{model}", quote(model)))
        html = None
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                html = r.text
        except requests.RequestException:
            pass
        links = product_links(html, url, rx, domain) if html else []
        how = "запит"
        if not links:
            page = browser_pages([url], log).get(url)
            links = product_links(page, url, rx, domain) if page else []
            how = "браузер"
        log(f"{domain}: пошук «{query}» ({how}): карток з артикулом {len(links)}")
        if links:
            return links[0]
    return None


def article_regex(art):
    parts = [p for p in re.split(r"[-_\s]+", art) if p]
    body = r"[-_\s]?".join(re.escape(p) for p in parts)
    tail = r"(?![A-Za-z0-9])"
    if len(parts) == 1:   # IG7601 не має збігатися з IG7601-010 (інший товар)
        tail = r"(?![A-Za-z0-9]|[-_]\d{3}(?!\d))"
    return re.compile(r"(?<![A-Za-z0-9])" + body + tail, re.I)


def search_links(base_url, article, limit=10, site=None, text=None):
    params = {"lang": "UK", "region": "UA", "limit": limit, "engines": "duckduckgo", "mode": "balanced",
              "text": text or (f"{article} ціна грн" if not site else article)}
    if site:
        params["site"] = site
    r = None
    for attempt in range(2):
        try:
            r = requests.get(base_url.rstrip("/") + "/mega/search", timeout=120, params=params)
        except requests.RequestException:
            raise SearchError("Не можу підключитися до OpenSERP. Перевір, що він запущений "
                              "(вікно з ним не закривай) і що OPENSERP_URL у config.py правильний.")
        if r.status_code != 502:
            break
        time.sleep(4)          # 502: DuckDuckGo притримав запит, пробуємо ще раз
    time.sleep(3)              # невелика пауза між пошуками, щоб не отримати блок
    if r.status_code != 200:
        raise SearchError(f"OpenSERP відповів помилкою {r.status_code}.")
    try:
        return r.json().get("results") or []
    except ValueError:
        raise SearchError("OpenSERP повернув не те, що очікувалось.")


def _site(url):
    return re.sub(r"^www\.", "", re.sub(r"^https?://", "", url).split("/")[0]).lower()


def _prio_of(site, priority):
    return any(site == d or site.endswith("." + d) for d in priority)


def find_prices(base_url, article, priority=None, max_pages=8, min_prices=MIN_PRICES, log=None, model="", use_browser=None, search_urls=None):
    """Спершу ціни з магазинів зі списку priority, потім, якщо їх мало, з інших сайтів.
    Повертає {"prices": [...], "links": [(назва, url)], "note": str, "kind": "exact"|"model"|None}."""
    priority = [d.lower() for d in (priority if priority is not None else PRIORITY_SITES)]
    log = log if log is not None else (lambda *a: None)
    exact = article_regex(article)
    parts = [p for p in re.split(r"[-_\s]+", article) if p]
    base = (re.compile(r"(?<![A-Za-z0-9])" + re.escape(parts[0]) + r"(?![A-Za-z0-9])", re.I)
            if len(parts) > 1 and len(parts[0]) >= 5 else None)

    def classify(items):
        ex, mod, seen = [], [], set()
        for it in items:
            url = it.get("url") or ""
            if not url or any(d in url for d in SKIP_DOMAINS):
                continue
            site = _site(url)
            if site in seen:        # один магазин рахуємо один раз
                continue
            if re.search(r"/page-\d+|[?&]page=\d+|/category/|/catalog/?$", url):
                continue            # сторінки списків товарів, а не картка одного товару
            text = " ".join(str(it.get(k) or "") for k in ("title", "url"))   # у сніпеті артикул може бути з іншого товару
            row = ((it.get("title") or site)[:60], url, site)
            if exact.search(text):
                ex.append(row); seen.add(site)
            elif base and base.search(text):
                mod.append(row); seen.add(site)
        return ex, mod

    def fetch(rows, rx=None):
        rx = rx or exact
        with ThreadPoolExecutor(max_workers=4) as pool:
            res = list(pool.map(lambda t: (t, page_price_ex(t[1], rx)), rows))
        if use_browser is not False:
            retry = [t for t, (p, why) in res if not p and not why.startswith("HTTP 404")]
            if retry:
                got_b = browser_prices([t[1] for t in retry], log, rx)
                res = [(t, got_b.get(t[1], (p, why)) if not p and t in retry else (p, why)) for t, (p, why) in res]
        for t, (p, why) in res:
            log(f"   сторінка {t[2]}: " + (f"{p}₴ ({why})" if p else f"НЕ ПРОЧИТАВ: {why}"))
        return [(t, p) for t, (p, _) in res if p]

    items, seen_u = [], set()
    queries = [None, f"{article} купити", f"{model} {article}".strip() if model else None]
    for q in queries:
        try:
            part = search_links(base_url, article, text=q)
        except SearchError:
            if not items:
                raise
            continue
        for it in part:
            if it.get("url") not in seen_u:
                seen_u.add(it.get("url")); items.append(it)
    ex, mod = classify(items)
    log(f"Загальний пошук: {len(items)} результатів, з точним артикулом {len(ex)}, тієї ж моделі {len(mod)}")
    for t in ex:
        log(f"   {'★' if _prio_of(t[2], priority) else ' '} {t[1]}")
    prio_ex = [t for t in ex if _prio_of(t[2], priority)]
    other_ex = [t for t in ex if not _prio_of(t[2], priority)]

    # 1) магазини зі списку, що знайшлися в загальному пошуку
    got = fetch(prio_ex[:max_pages])
    have = {t[2] for t, _ in got} | {t[2] for t in prio_ex}
    
    # 2) решта магазинів зі списку: окремий пошук по кожному сайту, поки не набереться досить цін
    for dom, tpl in (search_urls or {}).items():
        if any(h == dom or h.endswith("." + dom) for h in have):
            continue
        found = store_search(tpl, article, model, exact, dom, log)
        have.add(dom)
        if found:
            got += fetch([(dom, found, dom)])
            
    # пошук «як людина»: бот сам вводить назву й артикул у поле пошуку на сайті
    if use_browser is not False:
        todo = [d for d in priority if d not in (search_urls or {})
                and not any(h == d or h.endswith("." + d) for h in have)]
        typed = search_by_typing(todo, article, model, exact, log, want=len(todo))
        if importlib.util.find_spec("playwright"):
            have.update(todo)          # їх уже пробували в самому магазині; DuckDuckGo там нічого не знаходив
            
        got += fetch([(dom, url, dom) for dom, url in typed.items()])
        have.update(typed.keys())
        
    fails = 0
    for dom in priority:
        if dom in (search_urls or {}) or any(h == dom or h.endswith("." + dom) for h in have):
            continue
        if fails >= 3:
            break
        if any(h == dom or h.endswith("." + dom) for h in have):
            continue
        try:
            sub_items = []
            for q in [f"{model} {article}".strip()]:        # один запит без лапок: лапки викликали 502
                sub_items += search_links(base_url, article, limit=5, site=dom, text=q)
            sub_ex, _ = classify(sub_items)
        except SearchError as e:
            log(f"{dom}: помилка пошуку ({e})")
            fails += 1
            if fails >= 3:
                log("Пошук по окремих сайтах зупинено: DuckDuckGo відповідає помилками. Спробуй пізніше.")
            continue
        sub_ex = [t for t in sub_ex if _prio_of(t[2], [dom])][:1]
        have.add(dom)
        res = fetch(sub_ex)
        log(f"{dom}: у пошуку {len(sub_items)} результатів, з артикулом {len(sub_ex)}"
            + ("" if sub_ex else "; приклад: " + (sub_items[0].get("url", "—") if sub_items else "—")))
        got += res
    n_prio = len(got)
    # 3) додаткові сайти, якщо цін мало
    if len(got) < min_prices:
        extra = fetch(other_ex[:max_pages])
        got += extra
    kind = "exact"
    if not got and mod:
        got, kind, n_prio = fetch(mod[:max_pages], base), "model", 0
    if not got:
        return {"prices": [], "links": [], "kind": None,
                "note": f"Знайшов сторінок: {len(ex)} з точним артикулом, {len(mod)} тієї ж моделі, але цін на них не прочитав."}
    prices = [p for _, p in got]
    links = [(f"{t[2]} · {p}₴", t[1]) for t, p in got][:6]
    if kind == "exact":
        note = f"Ціни з {len(got)} магазинів, артикул збігається"
        note += f" (з твого списку: {n_prio})." if priority else "."
    else:
        note = f"Точного артикула не знайшов; це ціни тієї ж моделі в ІНШОМУ кольорі ({len(got)} шт.)."
    return {"prices": prices, "links": links, "kind": kind, "note": note}
