"""Показує по кроках, як бот шукає ціни. Запуск (OpenSERP має працювати):
    python test_market.py IG7601
    python test_market.py IG7601 "adidas samba"      (з назвою моделі, як бот її бере зі складу)
Надішли мені весь вивід."""
import sys
import market

art = sys.argv[1] if len(sys.argv) > 1 else "IG7601"
model = sys.argv[2] if len(sys.argv) > 2 else ""
url = "http://127.0.0.1:7000"
try:
    r = market.find_prices(url, art, log=print, model=model)
except market.SearchError as e:
    sys.exit(str(e))
print("\nПідсумок:", r["prices"], "|", r["note"])
for t, u in r["links"]:
    print("  ", t, u)
