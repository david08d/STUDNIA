"""Заповнення форми оголошення на Shafa у видимому браузері на твоєму комп'ютері.

Бот відкриває вікно браузера, заходить на сторінку нового оголошення, підставляє фото, назву, опис, розділ,
ціну та характеристики. Кнопку «Опублікувати» бот НЕ натискає: ти перевіряєш форму й тиснеш її сам.

Вхід в акаунт: перший раз увійди в Shafa у вікні, яке відкриє бот. Сесія збережеться в папці shafa_profile
(поруч із ботом), і наступного разу входити не треба. Логін і пароль бот не бачить і не зберігає.
Папку shafa_profile нікому не показуй: у ній збережений вхід у твій акаунт.

Запуск окремо, без Telegram:
    python shafa.py login      відкрити Shafa, увійти в акаунт (один раз)
    python shafa.py inspect    зберегти опис полів форми в shafa_form.json і shafa_form.png (надішли їх Claude)
    python shafa.py demo       заповнити форму тестовими даними

Налаштування в config.py (усе необов'язково):
    SHAFA_ADD_URL = "https://shafa.ua/..."   адреса сторінки нового оголошення, якщо бот сам її не знаходить
    SHAFA_BROWSER_CHANNEL = "chrome"         "chrome" або "msedge": браузер, що вже встановлений у системі
    SHAFA_FIELDS = {"title": "input[name=title]"}   свій селектор для поля, якщо за назвою воно не знаходиться
"""
import json
import os
import re
import sys
import threading
import time

try:
    import settings as config
except ImportError:      # запуск окремо без config.py
    config = None

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "shafa_profile")
HOME_URL = "https://shafa.ua/uk/"
DEFAULT_ADD_URL = "https://shafa.ua/uk/new"
# Поля, які на Shafa знаходяться за name (з shafa_form.json). Решту шукаємо за підписом.
DEFAULT_FIELDS = {"title": "input[name=titleUk]", "description": "textarea[name=descriptionUk]",
                  "price": "input[name=price]"}
ADD_LINK_TEXT = re.compile(r"(Додати|Створити|Подати|Розмістити)\s+(оголошення|товар)|Продати|Продавати", re.I)
LOGIN_URL_PART = re.compile(r"login|signin|sign-in|auth|account/enter|enter", re.I)
LOGIN_WAIT_SECONDS = 300
HOLD_SECONDS = 2 * 3600          # скільки тримаємо вікно відкритим, поки ти перевіряєш і публікуєш

# Підписи полів у формі Shafa. Шукаємо по тексту поруч із полем, по placeholder, aria-label або name.
LABELS = {
    "title": r"^\s*(назва|заголовок)",
    "description": r"^\s*(опис|description)",
    "price": r"^\s*(ціна|вартість|price)",
    "brand": r"^\s*(бренд|brand|виробник)",
    "condition": r"^\s*(стан|condition)(?!\p{L})",
    "color": r"^\s*(колір|color)",
    "material": r"^\s*(матеріал|material)",
    "season": r"^\s*(сезон|season)",
    "membrane": r"^\s*(мембрана)",
    "keywords": r"(ключов\p{L}+ слов|теги|tags|keywords)",
    "size": r"^\s*(розмір|size)(?!\p{L})",
    "insole": r"^\s*довжина\s+устілки",
    "category": r"^\s*(розділ|категорія|category)",
}

CONTROLS = ("input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]):not([type=submit])"
            ",textarea,select,[role=combobox],[contenteditable=true],button[aria-haspopup],[aria-haspopup=listbox]")

# Шукає поле за підписом. Повертає опис знайденого й ставить на нього атрибут data-cl, щоб його можна було вибрати.
FIND_JS = """
(arg) => {
  const rx = new RegExp(arg.rx, 'iu');
  const CTRL = arg.ctrl;
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  document.querySelectorAll('[data-cl]').forEach(e => e.removeAttribute('data-cl'));
  const done = (el, how) => { el.setAttribute('data-cl', '1');
    return {tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '', role: el.getAttribute('role') || '',
            readonly: !!el.readOnly, how: how}; };
  const all = Array.from(document.querySelectorAll(CTRL)).filter(vis);
  for (const el of all) {                                   // 1) за атрибутами самого поля
    const t = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('name'), el.id]
      .filter(Boolean).join(' ');
    if (t && rx.test(t)) return done(el, 'атрибут');
  }
  const labels = Array.from(document.querySelectorAll('label, span, div, p, h2, h3, h4, legend, dt, li'))
    .filter(el => { if (!vis(el)) return false;
      const own = Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim();
      return own.length > 0 && own.length < 130 && rx.test(own); });
  for (const lab of (arg.last ? labels.slice().reverse() : labels)) {                               // 2) поле після підпису, у тому самому блоці
    if (lab.tagName === 'LABEL' && lab.htmlFor) {
      const t = document.getElementById(lab.htmlFor); if (t && vis(t)) return done(t, 'label for');
    }
    let box = lab;
    for (let up = 0; up < 4 && box.parentElement; up++) {
      box = box.parentElement;
      const found = Array.from(box.querySelectorAll(CTRL)).filter(el => vis(el) &&
        (lab.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) && !lab.contains(el));
      if (found.length) return done(found[0], 'підпис');
    }
  }
  return null;
}
"""

DUMP_JS = """
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const labelOf = el => {
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return l.innerText.trim(); }
    const l = el.closest('label'); if (l) return l.innerText.trim().slice(0, 80);
    let box = el;
    for (let up = 0; up < 4 && box.parentElement; up++) {
      box = box.parentElement;
      const t = Array.from(box.querySelectorAll('label, span, div, p, legend'))
        .filter(x => vis(x) && !x.contains(el) && (x.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))
        .map(x => Array.from(x.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim())
        .filter(s => s && s.length < 70);
      if (t.length) return t[t.length - 1];
    }
    return '';
  };
  const out = {url: location.href, title: document.title, controls: [], files: [], buttons: [], headings: []};
  document.querySelectorAll('input, textarea, select, [role=combobox], [contenteditable=true], [aria-haspopup]').forEach(el => {
    if (el.type === 'hidden') return;
    if (el.type === 'file') { out.files.push({name: el.name, accept: el.accept, multiple: el.multiple}); return; }
    if (!vis(el)) return;
    const c = {tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
      id: el.id || '', placeholder: el.getAttribute('placeholder') || '', aria: el.getAttribute('aria-label') || '',
      role: el.getAttribute('role') || '', readonly: !!el.readOnly, label: labelOf(el),
      class: (el.className && el.className.toString ? el.className.toString() : '').slice(0, 80)};
    if (el.tagName === 'SELECT') c.options = Array.from(el.options).map(o => o.text.trim()).slice(0, 60);
    out.controls.push(c);
  });
  document.querySelectorAll('button, [role=button], a.button, input[type=submit]').forEach(el => {
    if (vis(el)) { const t = (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ');
      if (t && t.length < 60) out.buttons.push(t); } });
  document.querySelectorAll('h1, h2, h3').forEach(el => { if (vis(el)) out.headings.push(el.innerText.trim().slice(0, 80)); });
  return out;
}
"""

_lock = threading.Lock()


def _cfg(name, default=None):
    return getattr(config, name, default) if config else default


def playwright_ok():
    try:
        import importlib.util
        return importlib.util.find_spec("playwright") is not None
    except Exception:
        return False


def _vis_count(page, selector):
    try:
        return page.locator(selector).count()
    except Exception:
        return 0


def form_ready(page):
    """Форма нового оголошення відкрита: є поле завантаження фото і хоча б одне текстове поле."""
    try:
        return page.locator("input[type=file]").count() > 0 and page.locator("textarea, input[type=text]").count() > 0
    except Exception:
        return False


def _open_add_form(page, status):
    url = _cfg("SHAFA_ADD_URL") or DEFAULT_ADD_URL
    if url:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        return
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    for finder in (lambda: page.get_by_role("link", name=ADD_LINK_TEXT).first,
                   lambda: page.get_by_role("button", name=ADD_LINK_TEXT).first,
                   lambda: page.get_by_text(ADD_LINK_TEXT).first):
        try:
            el = finder()
            if el.count() and el.is_visible():
                el.click(timeout=4000)
                page.wait_for_timeout(2500)
                return
        except Exception:
            continue
    status("Не знайшов кнопку «Додати оголошення» на сайті. Відкрий форму нового оголошення сам, я зачекаю.")


def _wait_for_form(page, status):
    """Чекає, поки відкриється форма (у цей час можна увійти в акаунт у вікні)."""
    if form_ready(page):
        return True
    status("Увійди в акаунт Shafa у вікні браузера і відкрий форму нового оголошення. Я зачекаю до 5 хвилин.")
    end = time.time() + LOGIN_WAIT_SECONDS
    tried_open = False
    while time.time() < end:
        if page.is_closed():
            return False
        if form_ready(page):
            page.wait_for_timeout(1500)
            return True
        if not tried_open and not LOGIN_URL_PART.search(page.url) and time.time() > end - LOGIN_WAIT_SECONDS + 20:
            tried_open = True          # увійшов, але форма не відкрилась сама: пробуємо знайти кнопку ще раз
            try:
                _open_add_form(page, lambda t: None)
            except Exception:
                pass
        page.wait_for_timeout(2000)
    return False


# ---------------------------------------------------------------- заповнення полів

def _find(page, key):
    """Знаходить поле за ключем із LABELS або за селектором із config.SHAFA_FIELDS. Повертає locator або None."""
    pinned = {**DEFAULT_FIELDS, **(_cfg("SHAFA_FIELDS", {}) or {})}.get(key)
    if pinned:
        loc = page.locator(pinned).first
        if loc.count():
            info = loc.evaluate("e => ({tag: e.tagName.toLowerCase(), type: e.getAttribute('type') || '', "
                                "role: e.getAttribute('role') || '', readonly: !!e.readOnly, how: 'name'})")
            return loc, info
        if key in DEFAULT_FIELDS and not (_cfg("SHAFA_FIELDS", {}) or {}).get(key):
            pass                      # name змінився: пробуємо знайти за підписом
        else:
            return None, None
    info = page.evaluate(FIND_JS, {"rx": LABELS[key], "ctrl": CONTROLS, "last": key == "insole"})
    if not info:
        return None, None
    return page.locator('[data-cl="1"]').first, info


def _click_option(page, text, exact_only=False):
    """Клікає пункт випадного списку з потрібним текстом."""
    esc = re.escape(text.strip())
    exact = re.compile(rf"^\s*{esc}\s*$", re.I)
    part = re.compile(esc, re.I)
    for rx in ((exact,) if exact_only else (exact, part)):
        for loc in (page.get_by_role("option", name=rx), page.get_by_role("button", name=rx),
                    page.locator("li, [role=option], [class*=option], [class*=item], [class*=menu] div, [class*=list] div")
                        .filter(has_text=rx)):
            try:
                el = loc.first
                if el.count() and el.is_visible():
                    el.click(timeout=3000)
                    return True
            except Exception:
                continue
    return False


def _set_tags(page, value):
    """Ключові слова: вводимо по одному й тиснемо Enter."""
    words = [w.strip() for w in re.split(r"[,;]", str(value)) if w.strip()]
    loc, info = _find(page, "keywords")
    if loc is None:
        return False, "поле не знайдено"
    try:
        loc.click(timeout=4000)
        for w in words[:10]:
            loc.fill(w)
            loc.press("Enter")
            page.wait_for_timeout(200)
        return True, f"{len(words[:10])} шт."
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _set_value(page, key, value):
    """Записує значення в поле. Повертає (успіх, примітка)."""
    value = str(value).strip()
    if not value:
        return False, "порожнє значення"
    if key == "keywords":
        return _set_tags(page, value)
    if key == "condition":                      # на Shafa стан це ряд кнопок
        if _click_option(page, value):
            return True, ""
    loc, info = _find(page, key)
    if loc is None:
        return False, "поле не знайдено"
    tag, role = info["tag"], info["role"]
    try:
        if tag == "select":
            opts = loc.locator("option").all_inner_texts()
            pick = next((o for o in opts if o.strip().lower() == value.lower()), None) \
                or next((o for o in opts if value.lower() in o.strip().lower()), None)
            if not pick:
                return False, "у списку немає «" + value + "»"
            loc.select_option(label=pick.strip())
            return True, ""
        typed = tag in ("input", "textarea") and not info["readonly"] and role != "combobox"
        if typed:
            loc.click(timeout=4000)
            loc.fill(value)
            return True, ""
        # випадний список або поле з підказками: відкриваємо, вводимо для пошуку, вибираємо пункт
        loc.click(timeout=4000)
        page.wait_for_timeout(500)
        if tag == "input":
            try:
                loc.fill(value)
                page.wait_for_timeout(900)
            except Exception:
                pass
        if _click_option(page, value):
            return True, ""
        if tag == "input":
            loc.press("Enter")
            return True, "підставив без вибору зі списку, перевір"
        return False, "пункт «" + value + "» не знайдено у списку"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _set_many(page, key, values):
    """Кілька значень (наприклад, кілька розмірів) у полі зі списком."""
    done, missed = [], []
    loc, info = _find(page, key)
    if loc is None:
        return False, "поле не знайдено"
    try:
        loc.click(timeout=4000)
        page.wait_for_timeout(500)
        for v in values:
            if _click_option(page, v):
                done.append(v)
            else:
                missed.append(v)
            page.wait_for_timeout(250)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"
    if missed and not done:
        return False, "не знайшов у списку: " + ", ".join(missed)
    return True, ("не знайшов: " + ", ".join(missed)) if missed else ""


def _set_category(page, path):
    """Вибирає розділ послідовними клацаннями: Жіночий одяг → Взуття → Кросівки."""
    clicked = []
    for i, seg in enumerate(path):
        ok = _click_option(page, seg)
        if not ok and i == 0:                  # плитки не видно: пробуємо відкрити список розділів
            loc, info = _find(page, "category")
            if loc is not None:
                try:
                    loc.click(timeout=4000)
                    page.wait_for_timeout(700)
                except Exception:
                    pass
                ok = _click_option(page, seg)
        if ok:
            clicked.append(seg)
            page.wait_for_timeout(800)
        else:
            break
    if len(clicked) == len(path):
        return True, ""
    if clicked:
        return False, "вибрав лише " + " → ".join(clicked) + ", далі обери сам"
    return False, "не знайшов розділ, обери сам"


def _upload_photos(page, photos):
    paths = [p for p in photos if os.path.exists(p)]
    if not paths:
        return False, "фото немає"
    try:
        page.locator("input[type=file]").first.set_input_files(paths)
        page.wait_for_timeout(1500)
        return True, f"{len(paths)} шт."
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _parts(value, sep=r"[/,;]"):
    """Розбиває «демісезон / всесезон» на окремі значення без дужок."""
    out = []
    for x in re.split(sep, str(value)):
        x = re.sub(r"\(.*?\)", "", x).strip()
        if x:
            out.append(x)
    return out


def _click_buttons(page, values, limit=None):
    """Натискає кнопки з потрібними підписами (розмір, колір, сезон на Shafa це кнопки)."""
    done, missed = [], []
    for v in values:
        if limit and len(done) >= limit:
            break
        if _click_option(page, v, exact_only=True):
            done.append(v)
            page.wait_for_timeout(250)
        else:
            missed.append(v)
    if not done:
        return False, "немає кнопки: " + ", ".join(missed)
    return True, ("немає кнопки: " + ", ".join(missed)) if missed else ""


def _set_dropdown_any(page, key, values):
    """Поле зі списком (матеріал, довжина устілки): пробуємо значення по черзі, поки не виберемо хоч одне."""
    loc, info = _find(page, key)
    if loc is None:
        return False, "поле не знайдено"
    for v in values:
        try:
            loc.click(timeout=4000)
            page.wait_for_timeout(400)
            loc.fill(v)
            page.wait_for_timeout(800)
            if _click_option(page, v):
                return True, ""
            loc.fill("")
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:80]}"
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False, "у списку немає: " + ", ".join(values)


def fill_form(page, data, report):
    """Заповнює форму. report: список (назва поля, ок?, примітка)."""
    def add(name, res):
        report.append((name, res[0], res[1]))
        
    if data.get("photos"):
        add("Фото", _upload_photos(page, data["photos"]))
    if data.get("path"):
        add("Розділ", _set_category(page, data["path"]))
        page.wait_for_timeout(2000)         # після вибору розділу форма перебудовується
        
    for key, name in (("title", "Назва"), ("description", "Опис"), ("brand", "Бренд")):
        if data.get(key) not in (None, ""):
            add(name, _set_value(page, key, data[key]))
            page.wait_for_timeout(300)
            
    if data.get("condition"):
        add("Стан", _click_buttons(page, [data["condition"]]))
    if data.get("sizes"):
        add("Розмір", _click_buttons(page, [str(x) for x in data["sizes"]]))
    if data.get("color"):
        add("Колір", _click_buttons(page, _parts(data["color"], r"[/,;]| і "), limit=2))
    if data.get("season"):
        add("Сезон", _click_buttons(page, [x.capitalize() for x in _parts(data["season"])]))
    if data.get("membrane"):
        m = str(data["membrane"]).lower()
        pick = "Gore-Tex" if "gore" in m else ("Немає" if m.startswith(("нем", "ні", "нема")) else "Є")
        add("Мембрана", _click_buttons(page, [pick]))
    if data.get("material"):
        add("Матеріал", _set_dropdown_any(page, "material", _parts(data["material"])))
    if data.get("insoles"):
        vals = []
        for c in data["insoles"][:1]:
            vals += [str(c), str(c).replace(".", ","), str(c) + " см"]
        add("Довжина устілки", _set_dropdown_any(page, "insole", vals))
    if data.get("keywords"):
        add("Ключові слова", _set_tags(page, data["keywords"]))

    # Введення ціни перенесено в кінець
    if data.get("price") not in (None, ""):
        add("Ціна", _set_value(page, "price", data["price"]))
        page.wait_for_timeout(300)


# ---------------------------------------------------------------- браузер

def _launch(pw, headless):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    kw = dict(user_data_dir=PROFILE_DIR, headless=headless, no_viewport=True,
              args=["--disable-blink-features=AutomationControlled", "--start-maximized"], locale="uk-UA")
    channel = _cfg("SHAFA_BROWSER_CHANNEL")
    if channel:
        kw["channel"] = channel
    return pw.chromium.launch_persistent_context(**kw)


def _run(data, status, report_cb, headless=False, hold=True):
    from playwright.sync_api import sync_playwright
    report = []
    try:
        with sync_playwright() as pw:
            ctx = _launch(pw, headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                _open_add_form(page, status)
                if not _wait_for_form(page, status):
                    report_cb({"error": "Форма нового оголошення не відкрилась. Спробуй ще раз або вкажи "
                                        "SHAFA_ADD_URL у config.py.", "report": []})
                    return
                fill_form(page, data, report)
                report_cb({"error": "", "report": report})
                if hold:
                    end = time.time() + HOLD_SECONDS
                    while time.time() < end and not page.is_closed():
                        try:
                            page.wait_for_timeout(1500)
                        except Exception:
                            break
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        report_cb({"error": f"{type(e).__name__}: {str(e)[:200]}", "report": report})


def start(data, status, report_cb):
    """Запускає заповнення у фоновому потоці. Вікно лишається відкритим, поки ти його не закриєш."""
    if not _lock.acquire(blocking=False):
        report_cb({"error": "Вікно Shafa вже відкрите. Закрий його або доопублікуй попереднє оголошення.", "report": []})
        return None

    def job():
        try:
            _run(data, status, report_cb)
        finally:
            _lock.release()
    t = threading.Thread(target=job, daemon=True)
    t.start()
    return t


def report_text(rep):
    """Текст для Telegram."""
    if rep.get("error"):
        return "❌ " + rep["error"]
    lines = ["🚀 <b>Форму Shafa заповнено</b>. Перевір її у вікні браузера й натисни «Опублікувати» сам.", ""]
    bad = 0
    for name, ok, note in rep["report"]:
        if ok:
            lines.append(f"✅ {name}" + (f" ({note})" if note else ""))
        else:
            bad += 1
            lines.append(f"⚠️ {name}: {note}")
    if bad:
        lines += ["", f"Не все вдалося ({bad}). Те, що позначено ⚠️, дозаповни у формі вручну."]
    return "\n".join(lines)


# ---------------------------------------------------------------- запуск з командного рядка

def _cli():
    from playwright.sync_api import sync_playwright
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("login", "inspect", "demo"):
        print(__doc__)
        return
    say = print
    with sync_playwright() as pw:
        ctx = _launch(pw, False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if cmd == "login":
                page.goto(HOME_URL)
                print("Увійди в акаунт у вікні браузера, потім закрий вікно. Вхід збережеться.")
                while not page.is_closed():
                    page.wait_for_timeout(1000)
                return
            _open_add_form(page, say)
            if not _wait_for_form(page, say):
                print("Форма не відкрилась.")
                return
            page.wait_for_timeout(1500)
            if cmd == "inspect":
                dump = page.evaluate(DUMP_JS)
                with open(os.path.join(HERE, "shafa_form.json"), "w", encoding="utf-8") as f:
                    json.dump(dump, f, ensure_ascii=False, indent=1)
                page.screenshot(path=os.path.join(HERE, "shafa_form.png"), full_page=True)
                print(f"Зберіг shafa_form.json ({len(dump['controls'])} полів) і shafa_form.png.")
                input("\nТепер у вікні сам обери розділ до кінця (наприклад Жіночий одяг, Взуття, Кросівки), "
                      "дочекайся, поки з'являться нові поля, і натисни Enter тут: ")
                dump = page.evaluate(DUMP_JS)
                with open(os.path.join(HERE, "shafa_form2.json"), "w", encoding="utf-8") as f:
                    json.dump(dump, f, ensure_ascii=False, indent=1)
                page.screenshot(path=os.path.join(HERE, "shafa_form2.png"), full_page=True)
                print("Зберіг shafa_form2.json і shafa_form2.png. Надішли Claude усі чотири файли.")
            else:
                rep = []
                fill_form(page, {
                    "title": "Тест. Кросівки жіночі nike air max bliss dh5128-001 сірі 38", "description": "Тестовий опис",
                    "price": 3000, "brand": "Nike", "condition": "Новий", "color": "Сірий", "sizes": ["38"],
                    "path": ["Жіночий одяг", "Взуття", "Кросівки"]}, rep)
                print(report_text({"error": "", "report": rep}).replace("<b>", "").replace("</b>", ""))
                print("Закрий вікно, коли подивишся. Нічого не опубліковано.")
                while not page.is_closed():
                    page.wait_for_timeout(1000)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    _cli()