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


if __name__ == "__main__":
    unittest.main()
