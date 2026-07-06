"""
innonest_szamlalo.py
---------------------
Az innonest_core.py meglévő async Playwright-mintájára épül (login/session,
run_in_loop, make_browser_args) - ugyanúgy hívható a szerverből, mint az
innonest_adatok_leker(bid) a core modulban.

Két végpontot ad:

1) GET /innonest-counters
   2026.07.07-ig a két listaoldal LEGFELSŐ sorának BID/rendelésszámát vettük
   darabszámnak (pl. legfelső sor "BID-2026-246" -> 246) - ez HIBÁS logika volt,
   mert feltételezte, hogy a sorszámozás lyukmentes. Helyette most ténylegesen
   VÉGIGLAPOZUNK 100-as offsetekkel (https://app.innonest.hu/bids,
   .../bids/index/100/, .../bids/index/200/, ... - és ugyanígy /ordersheets-en),
   és megszámoljuk, hány sor azonosítója kezdődik a tárgyévvel. A lista alapból
   legújabb elöl rendezett, ezért amint egy korábbi évre eső sort találunk, onnan
   leállhatunk (a további, még régebbi sorok közt sem lesz több idei).
   Ezt hívja a Google Sheets sync script (frissitsTolcser_) a Tölcsér "Ajánlat"
   és "Megrendelés" szakaszainak darabszámához.

2) GET /innonest-full-data
   2026.07.07-ig csak az ELSŐ oldalt olvasta ki mindkét listából ("nem szükséges
   lapozni, mert az első oldal mindig a legfrissebbet mutatja" feltételezés
   alapján) - ez HIBÁS volt, mert 100-nál több idei tétel esetén a régebbi (de
   még idei) ajánlatok/megrendelések kimaradtak a Sheet-ből (pl. a legrégebbi
   idei BID a Sheetben BID-2026-145 maradt, holott van korábbi is). Most - a
   /innonest-counters mintájára - VÉGIGLAPOZUNK 100-as offsetekkel mindkét
   listán, amíg idei sorokat találunk, és mindet összegyűjtjük.
   Ezt hívja a Sheets sync script (frissitsAjanlatokEsProjektek_) az Ajánlatok
   és Projektek táblák feltöltéséhez. A Pipedrive-ot ide MÁR NEM enumerálásra
   használjuk (a felhasználó jelezte: "ott nem minden ajánlatot viszek fel"),
   csak az Innonest a teljes/megbízható forrás. A Pipedrive-ot a Sheets script
   külön, csak az "Értékesítő" mező kiegészítésére (BID_CUSTOM_FIELD_KEY szerinti
   egyeztetéssel) használja tovább.

Használat a Flask szerverben (server.py-ban regisztrálva, a többi modul
mintájára):
    from innonest_szamlalo import register_innonest_szamlalo_routes
    register_innonest_szamlalo_routes(app)

Szelektor DevTools-szal ellenőrizve (2026.07.05-06, bids és ordersheets oldal is):
a sorazonosító mindig a "td.left.bold a" linkben van, pl.
<td class="left bold"><a ...>2026-69</a></td>, és mindkét lista ugyanazt a
"table.table-softservice" táblázat-szerkezetet használja.
"""

import re
import logging
from datetime import datetime

from playwright.async_api import async_playwright

from innonest_core import login, load_session, make_browser_args, run_in_loop

log = logging.getLogger(__name__)

ARAJANLATOK_URL = "https://app.innonest.hu/bids"
MEGRENDELOLAPOK_URL = "https://app.innonest.hu/ordersheets"

BID_PATTERN = re.compile(r"BID-(\d{4})-(\d+)")
ORDER_PATTERN = re.compile(r"(\d{4})-(\d+)")

# A megrendelőlap "Megnevezés" mezőjének végén automatikusan megjelenő
# BID-hivatkozás mintája, pl.: "... [Árajánlat KIV #BID-2026-155]"
BID_REF_PATTERN = re.compile(r"#(BID-\d{4}-\d+)\]")

# Összeg-minta a sor teljes szövegében, pl. "1 234 567 HUF"
HUF_AMOUNT_PATTERN = re.compile(r"([0-9][0-9 ]{2,}[0-9])\s*HUF")

DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")

STATUS_WORDS = [
    "Piszkozat", "Elküldve", "Elfogadva", "Megrendelt",
    "Teljesítve", "Számlázva", "Törölve", "Lejárt", "Visszautasítva",
]


LISTA_OLDALMERET = 100  # a /bids, /bids/index/100/, /bids/index/200/ ... offset-lépésköze


async def _paginated_year_count(page, base_url: str, pattern: "re.Pattern", current_year: int, max_pages: int = 50) -> int:
    """Végiglapozza a listaoldalt LISTA_OLDALMERET-es offsetekkel
    (base_url, base_url/index/100/, base_url/index/200/, ...), és megszámolja,
    hány sor azonosítója ("td.left.bold a" szövege) kezdődik a tárgyévvel.

    A lista alapból legújabb elöl rendezett, ezért amint egy korábbi évre eső
    sort találunk, tudjuk, hogy onnantól (ezen az oldalon és minden további
    oldalon) már csak régebbi évek jönnek - ott leállhatunk. Biztonsági korlát:
    max_pages (végtelen ciklus elleni védelem, ha a szerkezet váratlanul
    megváltozna).
    """
    count = 0
    offset = 0

    for _ in range(max_pages):
        url = base_url if offset == 0 else f"{base_url.rstrip('/')}/index/{offset}/"
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(1000)

        table = page.locator("table.table-softservice")
        if await table.count() == 0:
            table = page.locator("table")

        rows = table.first.locator("tbody tr")
        row_count = await rows.count()
        if row_count == 0:
            break

        reached_previous_year = False
        for i in range(row_count):
            link = rows.nth(i).locator("td.left.bold a").first
            try:
                text = (await link.inner_text(timeout=5000)).strip()
            except Exception:
                continue

            match = pattern.match(text)
            if not match:
                continue

            row_year = int(match.group(1))
            if row_year == current_year:
                count += 1
            else:
                reached_previous_year = True
                break

        if reached_previous_year or row_count < LISTA_OLDALMERET:
            break

        offset += LISTA_OLDALMERET

    return count


async def _innonest_counters_async() -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        await page.goto(ARAJANLATOK_URL, wait_until="networkidle")
        await page.wait_for_timeout(1000)

        if "login" in page.url:
            await login(page)

        current_year = datetime.now().year

        bid_count = await _paginated_year_count(page, ARAJANLATOK_URL, BID_PATTERN, current_year)
        order_count = await _paginated_year_count(page, MEGRENDELOLAPOK_URL, ORDER_PATTERN, current_year)

        await browser.close()

        return {
            "ajanlatok_db": bid_count,
            "megrendelesek_db": order_count,
            "ev": current_year,
        }


def innonest_counters() -> dict:
    """Szinkron belépési pont, az innonest_adatok_leker(bid) mintájára."""
    try:
        return run_in_loop(_innonest_counters_async())
    except Exception as e:  # noqa: BLE001
        log.error(f"[SZAMLALO] Hiba: {e}")
        return {
            "ajanlatok_db": 0,
            "megrendelesek_db": 0,
            "ev": datetime.now().year,
            "error": str(e),
        }


# ── Teljes lista lekaparás (Ajánlatok + Projektek Power BI táblákhoz) ─────────

def _parse_huf_amounts(text: str):
    amounts = []
    for m in HUF_AMOUNT_PATTERN.finditer(text):
        try:
            amounts.append(int(m.group(1).replace(" ", "")))
        except ValueError:
            continue
    return amounts


def _parse_status(text: str) -> str:
    for word in STATUS_WORDS:
        if word in text:
            return word
    return ""


def _meaningful_lines(text: str):
    """A megrendeles_figyelő.py-ban már bevált szűrés: kiszedi a dátum/összeg/
    státusz/pusztán-numerikus sorokat, hogy a cégnév/tárgy sorok maradjanak."""
    out = []
    for s in text.splitlines():
        s = s.strip()
        if not s:
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            continue
        if re.match(r"^\d{4}-\d+$", s):
            continue
        if re.match(r"^BID-\d{4}-\d+$", s):
            continue
        if re.search(r"HUF|EUR|USD|GBP|CHF", s):
            continue
        if s in STATUS_WORDS:
            continue
        if re.match(r"^[\d\s.,]+$", s):
            continue
        if len(s) <= 5 and s.isupper():
            continue
        out.append(s)
    return out


async def _scrape_rows(page, url: str):
    """Egy listaoldal (bids vagy ordersheets) EGY oldalának sorait olvassa ki:
    az azonosítót ("td.left.bold a" szövege) és a teljes sor szövegét
    (innerText), a megrendeles_figyelő.py-ban bevált teljes-sor-regex mintát
    követve. Lapozáshoz lásd _scrape_rows_paginated."""
    await page.goto(url, wait_until="networkidle")
    await page.wait_for_timeout(1500)

    raw_rows = await page.evaluate(
        """
        () => {
            const trs = document.querySelectorAll('table.table-softservice tbody tr');
            const out = [];
            trs.forEach(tr => {
                const link = tr.querySelector('td.left.bold a');
                if (!link) return;
                out.push({ id: link.innerText.trim(), text: tr.innerText });
            });
            return out;
        }
        """
    )
    return raw_rows


async def _scrape_rows_paginated(page, base_url: str, id_pattern: "re.Pattern", current_year: int, max_pages: int = 50):
    """A _scrape_rows lapozós változata: végigmegy a listaoldalakon
    (base_url, base_url/index/100/, base_url/index/200/, ... LISTA_OLDALMERET-es
    offsetekkel), és összegyűjti az összes sort, amíg a tárgyévbe eső
    azonosítókat talál. A lista legújabb elöl rendezett, ezért amint egy
    korábbi évre eső azonosítót talál, az adott oldal feldolgozása után megáll
    - a további oldalakon már csak régebbi évek lennének.
    """
    all_rows = []
    offset = 0

    for _ in range(max_pages):
        url = base_url if offset == 0 else f"{base_url.rstrip('/')}/index/{offset}/"
        raw_rows = await _scrape_rows(page, url)

        if not raw_rows:
            break

        reached_previous_year = False
        for r in raw_rows:
            match = id_pattern.match(r["id"])
            if match and int(match.group(1)) != current_year:
                reached_previous_year = True
                break
            all_rows.append(r)

        if reached_previous_year or len(raw_rows) < LISTA_OLDALMERET:
            break

        offset += LISTA_OLDALMERET

    return all_rows


async def _scrape_ajanlatok(page, current_year: int):
    raw_rows = await _scrape_rows_paginated(page, ARAJANLATOK_URL, BID_PATTERN, current_year)
    out = []
    for r in raw_rows:
        bid = r["id"]
        bid_m = BID_PATTERN.match(bid)
        if not bid_m or int(bid_m.group(1)) != current_year:
            continue
        text = r["text"]
        amounts = _parse_huf_amounts(text)
        netto = min(amounts) if amounts else 0
        date_m = DATE_PATTERN.search(text)
        datum = date_m.group(1) if date_m else ""
        allapot = _parse_status(text)
        lines = _meaningful_lines(text)
        targy = lines[0] if len(lines) >= 1 else ""
        ugyfel = lines[1] if len(lines) >= 2 else ""
        out.append({
            "bid": bid,
            "datum": datum,
            "ugyfel": ugyfel,
            "targy": targy,
            "netto": netto,
            "allapot": allapot,
        })
    return out


async def _scrape_megrendelesek(page, current_year: int):
    raw_rows = await _scrape_rows_paginated(page, MEGRENDELOLAPOK_URL, ORDER_PATTERN, current_year)
    out = []
    for r in raw_rows:
        rendelesszam = r["id"]
        order_m = ORDER_PATTERN.match(rendelesszam)
        if not order_m or int(order_m.group(1)) != current_year:
            continue
        text = r["text"]
        bid_ref = BID_REF_PATTERN.search(text)
        bid = bid_ref.group(1) if bid_ref else ""
        amounts = _parse_huf_amounts(text)
        netto = min(amounts) if amounts else 0
        date_m = DATE_PATTERN.search(text)
        datum = date_m.group(1) if date_m else ""
        allapot = _parse_status(text)
        lines = _meaningful_lines(text)
        targy = lines[0] if len(lines) >= 1 else ""
        ugyfel = lines[1] if len(lines) >= 2 else ""
        out.append({
            "rendelesszam": rendelesszam,
            "bid": bid,
            "datum": datum,
            "ugyfel": ugyfel,
            "targy": targy,
            "netto": netto,
            "allapot": allapot,
        })
    return out


async def _innonest_full_data_async() -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        await page.goto(ARAJANLATOK_URL, wait_until="networkidle")
        await page.wait_for_timeout(1000)
        if "login" in page.url:
            await login(page)

        current_year = datetime.now().year

        ajanlatok = await _scrape_ajanlatok(page, current_year)
        megrendelesek = await _scrape_megrendelesek(page, current_year)

        await browser.close()

        return {
            "ev": current_year,
            "ajanlatok": ajanlatok,
            "megrendelesek": megrendelesek,
        }


def innonest_full_data() -> dict:
    try:
        return run_in_loop(_innonest_full_data_async())
    except Exception as e:  # noqa: BLE001
        log.error(f"[SZAMLALO] Teljes lista hiba: {e}")
        return {
            "ev": datetime.now().year,
            "ajanlatok": [],
            "megrendelesek": [],
            "error": str(e),
        }


def register_innonest_szamlalo_routes(app):
    """A meglévő server.py modulregisztrációs mintája szerint hívandó
    (lásd register_arajanlat_routes, register_pipedrive_routes stb.)."""

    @app.route("/innonest-counters", methods=["GET"])
    def innonest_counters_route():
        data = innonest_counters()
        status = 500 if data.get("error") else 200
        return data, status

    @app.route("/innonest-full-data", methods=["GET"])
    def innonest_full_data_route():
        data = innonest_full_data()
        status = 500 if data.get("error") else 200
        return data, status
