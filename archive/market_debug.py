"""Діагностика пошуку цін у магазинах: показує, що саме відбувається з кожним сайтом.
Запусти в папці з config.py і market.py (OpenSERP має працювати):
    python market_debug.py IG7601
    python market_debug.py IG7601 1500                    (друге число: закуп, ціни нижче 80% відкинуться)
    python market_debug.py IG7601 1500 "Adidas Samba"     (третє: модель, щоб браузер не відкривав сторонні сторінки)
Браузерний режим береться з config.py (USE_BROWSER = True)."""
import logging
import sys

import market

try:
    import config
    base = getattr(config, "OPENSERP_URL", "http://127.0.0.1:7000")
    priority = getattr(config, "PRIORITY_SITES", market.PRIORITY_SITES)
    use_browser = bool(getattr(config, "USE_BROWSER", False))
    max_browser = int(getattr(config, "BROWSER_MAX_PAGES", 8))
    channel = getattr(config, "BROWSER_CHANNEL", None)
except ImportError:
    base, priority, use_browser, max_browser, channel = "http://127.0.0.1:7000", market.PRIORITY_SITES, False, 8, None

if len(sys.argv) < 2:
    raise SystemExit("Вкажи артикул: python market_debug.py IG7601")
article = sys.argv[1]
floor = int(int(sys.argv[2]) * 0.8) if len(sys.argv) > 2 else None
model = sys.argv[3] if len(sys.argv) > 3 else ""
logging.basicConfig(level=logging.WARNING)
print(f"Артикул {article}, магазинів у списку: {len(priority)}, браузер: {'увімкнено' if use_browser else 'вимкнено'}")
try:
    res = market.find_prices(base, article, priority=priority, floor=floor, browser=use_browser, model=model,
                             max_browser=max_browser, browser_channel=channel)
except market.SearchError as e:
    raise SystemExit(f"Пошук не вдався: {e}")
print("\nЩО СТАЛОСЯ:")
for line in res["debug"]:
    print("  •", line)
print("\nЦІНИ:", sorted(res["prices"]) or "немає")
for title, url in res["links"]:
    print("  ", title, url)
print("\nПІДСУМОК:", res["note"])
