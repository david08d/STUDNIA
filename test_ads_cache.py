"""Тести кешу відповіді Gemini (ads.py). Без мережі й без справжнього Gemini: виклик підміняється.
Запуск:  python -m unittest test_ads_cache -v
Папка з фото і кешем створюється тимчасова, реальна ads/ не чіпається."""
import importlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

import bot
import ads


def reply(**over):
    ai = {"brand": "Nike", "model_name": "Air Max Bliss", "kind": "shoes", "type_gender": "Кросівки жіночі",
          "gender": "жіноча", "category_path": ["Жіночий одяг", "Взуття", "Кросівки"],
          "color_title": "сірі", "color_ua": "сірий", "color_official": "Photon Dust", "season": "демісезон",
          "material": "текстиль", "membrane": "Немає",
          "sizes": [{"eu": "38", "us": "7", "cm": "24"}, {"eu": "40", "us": "8.5", "cm": "25.5"}],
          "keywords": ["кросівки жіночі", "nike air max"], "uncertain": []}
    ai.update(over)
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(ai, ensure_ascii=False)}]},
                            "finishReason": "STOP"}], "modelVersion": "fake-1"}


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        ads.init(bot)
        self.old_dir = getattr(ads.config, "AD_PHOTOS_DIR", None)
        self.old_model = getattr(ads.config, "GEMINI_MODEL", None)
        self.old_version = ads.PROMPT_VERSION
        ads.config.AD_PHOTOS_DIR = self.tmp
        ads.config.GEMINI_MODEL = "model-a"
        self.calls = 0
        self.response = reply()
        self.lock = threading.Lock()
        self.delay = 0
        ads.gemini_try = self.fake_gemini
        self.p = {"article": "DH5128-001", "model": "Nike Air Max Bliss", "sizes": ["38", "40"],
                  "buy_min": 1000, "buy_max": 1200}
        self.photos = [self.photo("01.jpg", b"photo-one"), self.photo("02.jpg", b"photo-two")]

    def tearDown(self):
        ads.config.AD_PHOTOS_DIR = self.old_dir
        ads.config.GEMINI_MODEL = self.old_model
        ads.PROMPT_VERSION = self.old_version
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_gemini(self, models, parts, **kw):
        with self.lock:
            self.calls += 1
        time.sleep(self.delay)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def photo(self, name, data):
        d = os.path.join(self.tmp, "src")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def cache_file(self):
        return os.path.join(ads.folder_for(self.p["article"]), ads.AI_CACHE_FILE)

    def make(self, box=None, photos=None, p=None):
        return ads.make_listing(p or self.p, box, self.photos if photos is None else photos)

    # --- основне ---
    def test_first_call_asks_gemini_and_saves(self):
        self.make()
        self.assertEqual(self.calls, 1)
        self.assertTrue(os.path.exists(self.cache_file()))

    def test_repeat_does_not_call_gemini_and_gives_same_text(self):
        a = self.make()
        b = self.make()
        self.assertEqual(self.calls, 1)
        self.assertEqual((a["title"], a["description"], a["info"], a["keywords"]),
                         (b["title"], b["description"], b["info"], b["keywords"]))
        self.assertEqual(a["ai"], b["ai"])

    def test_works_after_restart(self):
        self.make()
        importlib.reload(ads)                 # новий «процес»: пам'ять порожня, файл лишився
        ads.init(bot)
        ads.gemini_try = self.fake_gemini
        self.make(p=dict(self.p))
        self.assertEqual(self.calls, 1)

    def test_changed_inputs_make_new_request(self):
        self.make()
        self.photos[1] = self.photo("02.jpg", b"other-photo")
        self.make()
        self.assertEqual(self.calls, 2)                     # інше фото
        self.make(photos=self.photos[::-1])
        self.assertEqual(self.calls, 3)                     # інший порядок фото
        self.make(p=dict(self.p, sizes=["38", "40", "41"]))
        self.assertEqual(self.calls, 4)                     # інші розміри
        ads.config.GEMINI_MODEL = "model-b"
        self.make()
        self.assertEqual(self.calls, 5)                     # інша модель
        ads.config.GEMINI_MODEL = "model-a"
        ads.PROMPT_VERSION += 1
        self.make()
        self.assertEqual(self.calls, 6)                     # інша версія промпту
        self.make(p=dict(self.p, model="Інша назва"))
        self.assertEqual(self.calls, 7)                     # інша назва у промпті

    def test_no_photos_has_own_key(self):
        self.make()
        self.make(photos=[])
        self.assertEqual(self.calls, 2)
        self.make(photos=[])
        self.assertEqual(self.calls, 2)

    def test_only_photos_sent_to_model_count(self):
        extra = [self.photo(f"x{i}.jpg", f"x{i}".encode()) for i in range(3)]
        base = self.photos + extra[:2]                       # 4 фото = PHOTOS_TO_AI
        self.make(photos=base)
        self.make(photos=base + [self.photo("late.jpg", b"late")])   # п'яте фото до моделі не йде
        self.assertEqual(self.calls, 1)

    def test_box_change_does_not_call_gemini(self):
        a = self.make(box="with")
        b = self.make(box="without")
        self.assertEqual(self.calls, 1)
        self.assertNotEqual(a["description"], b["description"])   # а текст усе ж перебудовується
        self.assertEqual(ads.listing_prompt(self.p, "with", 2), ads.listing_prompt(self.p, "without", 2))

    # --- що не кешуємо ---
    def test_errors_and_bad_answers_not_cached(self):
        self.response = ads.AdError("Gemini недоступний")
        with self.assertRaises(ads.AdError):
            self.make()
        self.response = {"candidates": [{"content": {"parts": [{"text": "це не json"}]}, "finishReason": "STOP"}]}
        with self.assertRaises(ads.AdError):
            self.make()
        self.response = {"candidates": [{"content": {"parts": [{"text": ""}]}}]}
        with self.assertRaises(ads.AdError):
            self.make()
        self.assertFalse(os.path.exists(self.cache_file()))

    def test_truncated_or_empty_answer_not_cached(self):
        self.response = reply()
        self.response["candidates"][0]["finishReason"] = "MAX_TOKENS"
        self.make()
        self.assertFalse(os.path.exists(self.cache_file()))
        self.response = reply(type_gender="", keywords=[])
        self.make()
        self.assertFalse(os.path.exists(self.cache_file()))
        self.response = reply(kind="щось")
        self.make()
        self.assertFalse(os.path.exists(self.cache_file()))

    def test_corrupt_cache_does_not_break(self):
        self.make()
        for junk in ("{не json", "[]", '{"entries": 5}', ""):
            with open(self.cache_file(), "w", encoding="utf-8") as f:
                f.write(junk)
            before = self.calls
            self.make()
            self.assertEqual(self.calls, before + 1)
            self.make()                                      # а тепер знову з кешу
            self.assertEqual(self.calls, before + 1)

    def test_bad_entry_inside_cache_is_ignored(self):
        self.make()
        with open(self.cache_file(), encoding="utf-8") as f:
            data = json.load(f)
        for e in data["entries"].values():
            e["ai"] = {"brand": "x"}
        with open(self.cache_file(), "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.make()
        self.assertEqual(self.calls, 2)

    # --- запис і конкуренція ---
    def test_atomic_write_leaves_no_temp_files_and_limits_entries(self):
        for i in range(ads.AI_CACHE_MAX_ENTRIES + 3):
            self.make(photos=[self.photo(f"n{i}.jpg", f"n{i}".encode())])
        names = os.listdir(os.path.dirname(self.cache_file()))
        self.assertEqual(names, [ads.AI_CACHE_FILE])
        with open(self.cache_file(), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["entries"]), ads.AI_CACHE_MAX_ENTRIES)

    def test_write_failure_does_not_break_generation(self):
        real = ads.os.replace
        ads.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("диск зайнятий"))
        try:
            listing = self.make()
        finally:
            ads.os.replace = real
        self.assertTrue(listing["title"])
        self.assertEqual(os.listdir(os.path.dirname(self.cache_file())), [])   # тимчасовий файл прибрано

    def test_concurrent_same_key_calls_gemini_once(self):
        self.delay = 0.3
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.make())) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(results), 8)

    def test_concurrent_different_keys_do_not_lose_entries(self):
        threads = [threading.Thread(target=self.make, kwargs={"photos": [self.photo(f"c{i}.jpg", f"c{i}".encode())]})
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(self.cache_file(), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["entries"]), 5)

    # --- автоматичне оголошення за артикулом ---
    def test_saved_photos_sorted_by_number_and_filtered(self):
        folder = ads.folder_for(self.p["article"])
        os.makedirs(folder)
        for name in ("10.jpg", "02.jpg", "01.jpg", "notes.txt", "03.png", "x.jpg"):
            with open(os.path.join(folder, name), "wb") as f:
                f.write(b"x")
        self.assertEqual([os.path.basename(x) for x in ads.saved_photos(self.p["article"])],
                         ["01.jpg", "02.jpg", "10.jpg"])
        self.assertEqual(ads.saved_photos("NO-SUCH"), [])

    def test_reuse_saved_ignores_photo_set_but_default_does_not(self):
        self.make()
        other = [self.photo("other.jpg", b"other")]
        ads.make_listing(self.p, None, other)                       # звичайний шлях: інші фото = новий запит
        self.assertEqual(self.calls, 2)
        third = [self.photo("third.jpg", b"third")]
        ads.make_listing(self.p, None, third, True)                 # автоматичний: бере збережену відповідь
        self.assertEqual(self.calls, 2)

    def test_reuse_saved_rules(self):
        self.make()
        self.assertIsNone(ads.ai_cache_latest(dict(self.p, article="OTHER-1")))
        self.assertIsNone(ads.ai_cache_latest(dict(self.p, sizes=["38", "40", "41"])))
        self.assertIsNotNone(ads.ai_cache_latest(dict(self.p, sizes=["40"])))
        ads.PROMPT_VERSION += 1
        self.assertIsNone(ads.ai_cache_latest(self.p))
        new_photo = [self.photo("v.jpg", b"v")]
        ads.make_listing(self.p, None, new_photo, True)             # відповідь неактуальна: новий запит
        self.assertEqual(self.calls, 2)

    def test_last_box_remembered(self):
        self.make(box="without")
        self.assertEqual(ads.ai_cache_last_box(self.p["article"]), "without")
        self.make(box="with")
        self.assertEqual(ads.ai_cache_last_box(self.p["article"]), "with")
        self.make(box=None)
        self.assertEqual(ads.ai_cache_last_box(self.p["article"]), "with")
        self.make(photos=[self.photo("z.jpg", b"z")])
        self.assertEqual(ads.ai_cache_last_box(self.p["article"]), "with")   # новий запис не губить коробку

    def test_box_not_saved_without_cache_file(self):
        self.response = reply(type_gender="")
        self.make(box="with")
        self.assertFalse(os.path.exists(self.cache_file()))
        self.assertIsNone(ads.ai_cache_last_box(self.p["article"]))

    def _flow(self, stock):
        """Фальшивий bot-модуль, щоб проганяти текст і вибір товару без Telegram."""
        import asyncio
        from types import SimpleNamespace
        out = SimpleNamespace(spawned=[], said=[], ctx=SimpleNamespace(user_data={"mode": "ad_article"}))

        async def noop(*a, **k):
            return None

        async def say(ctx, chat, text, **k):
            out.said.append((text, k))
            return SimpleNamespace(message_id=1)

        def spawn(coro):
            out.spawned.append(coro)
            coro.close()
        out.B = SimpleNamespace(
            safe_answer=noop, say=say, clear=noop, safe_delete=noop, spawn=spawn, track=lambda *a: None,
            CANCEL_BTN=None, CANCEL_KB=None, norm_article=bot.norm_article, norm_size=bot.norm_size,
            money=bot.money, load_stock=lambda: (stock, None))
        out.update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), message=SimpleNamespace(message_id=5))
        out.send = lambda text: asyncio.run(ads.on_text(out.update, out.ctx, text))
        return out

    @staticmethod
    def _row(article, model="Nike Air Max Bliss", size="38", qty=1):
        return {"article": article, "model": model, "size": size, "qty": qty, "buy": "1000"}

    def test_text_with_saved_photos_starts_generation_at_once(self):
        flow = self._flow([self._row("DH5128-001"), self._row("DH5128-001", size="40")])
        folder = ads.folder_for("DH5128-001")
        os.makedirs(folder)
        for n in ("01.jpg", "02.jpg"):
            with open(os.path.join(folder, n), "wb") as f:
                f.write(b"img")
        try:
            ads.B = flow.B
            flow.send("DH5128-001")
        finally:
            ads.B = bot
        ad = flow.ctx.user_data["ad"]
        self.assertEqual(len(flow.spawned), 1)
        self.assertTrue(ad["auto"] and ad["busy"])
        self.assertEqual(len(ad["photos"]), 2)
        self.assertEqual(len(flow.said), 1)                     # лише коротке повідомлення, питань нема
        self.assertNotIn("Коробка", flow.said[0][0])

    def test_text_with_saved_answer_but_no_photos_uses_remembered_box(self):
        self.make(box="with", photos=[])                        # відповідь і коробка збережені
        flow = self._flow([self._row("DH5128-001"), self._row("DH5128-001", size="40")])
        try:
            ads.B = flow.B
            flow.send("dh5128")
        finally:
            ads.B = bot
        ad = flow.ctx.user_data["ad"]
        self.assertEqual(len(flow.spawned), 1)
        self.assertEqual((ad["box"], ad["photos"], ad["auto"]), ("with", [], True))

    def test_new_product_keeps_old_questions(self):
        flow = self._flow([self._row("NEW-123")])
        try:
            ads.B = flow.B
            flow.send("NEW-123")
        finally:
            ads.B = bot
        ad = flow.ctx.user_data["ad"]
        self.assertEqual(flow.spawned, [])
        self.assertFalse(ad["auto"])
        self.assertIn("Коробка", flow.said[0][0])

    def test_several_matches_ask_to_choose(self):
        flow = self._flow([self._row("DH5128-001"), self._row("DH5128-002", model="Nike Air Max Bliss 2")])
        folder = ads.folder_for("DH5128-001")
        os.makedirs(folder)
        with open(os.path.join(folder, "01.jpg"), "wb") as f:
            f.write(b"img")
        try:
            ads.B = flow.B
            flow.send("Air Max Bliss")
        finally:
            ads.B = bot
        self.assertEqual(flow.spawned, [])                      # автоматично нічого не запущено
        self.assertIn("кілька", flow.said[0][0])
        self.assertEqual(len(flow.ctx.user_data["ad"]["cands"]), 2)

    def test_busy_generation_falls_back_to_old_flow(self):
        self.make()
        flow = self._flow([self._row("DH5128-001"), self._row("DH5128-001", size="40")])
        flow.ctx.user_data["ad"] = {"busy": True}
        try:
            ads.B = flow.B
            flow.send("DH5128-001")
        finally:
            ads.B = bot
        self.assertEqual(flow.spawned, [])
        self.assertIn("Коробка", flow.said[0][0])

    def test_generate_passes_auto_flag(self):
        import asyncio
        from types import SimpleNamespace
        seen = {}

        def fake_make(p, box, photos, reuse):
            seen["args"] = (box, list(photos), reuse)
            return {"title": "T", "description": "D", "keywords": "k", "info": "i", "notes": [],
                    "brand": "Nike", "ai": {}, "shoes": True}

        class Tk:
            log = staticmethod(lambda s: None)

            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        flow = self._flow([])
        flow.ctx.user_data["ad"] = {"product": dict(self.p), "photos": ["/x/01.jpg"], "box": "with", "auto": True}
        flow.ctx.bot = SimpleNamespace()
        old = (ads.make_listing, ads._Ticker)
        try:
            ads.B = flow.B
            ads.make_listing, ads._Ticker = fake_make, Tk
            asyncio.run(ads._generate(flow.ctx, 1))
        finally:
            ads.B = bot
            ads.make_listing, ads._Ticker = old
        self.assertEqual(seen["args"], ("with", ["/x/01.jpg"], True))


if __name__ == "__main__":
    unittest.main()
