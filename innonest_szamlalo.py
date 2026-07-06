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
   VÉGIGLAPOZUNK 100-as offsetekkel mindkét listán - de a két lista eltérő
   URL-mintát használ (felhasználó által megadott, élőben ellenőrzött URL-ek):
     - Ajánlatok: https://app.innonest.hu/bids (0. oldal), majd
       .../bids/index/100/, .../bids/index/200/, ...
     - Megrendelőlapok: https://app.innonest.hu/ordersheets/index/0/all (0.
       oldal is "/index/{offset}/all" formátumú), majd .../index/100/all, ...
   (lásd _bids_page_url / _ordersheets_page_url), és megszámoljuk, hány sor
   azonosítója kezdődik a tárgyévvel. A lista alapból legújabb elöl rendezett,
   ezért amint egy korábbi évre eső sort találunk, onnan leállhatunk (a
   további, még régebbi sorok közt sem lesz több idei).
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

EUR-s tételek (2026.07.07-i javítás): korábban a nettó Ft érték 0 maradt
minden olyan sornál, ahol az Innonestben az összeg EUR-ban van megadva (csak
"HUF" végződésű összegeket kerestünk a sor szövegében). Most _parse_netto_huf
EUR összeget is felismer, és a sor kiállítási dátumán (ajánlat/megrendelőlap
dátuma) érvényes EUR/HUF árfolyammal (ECB referencia-árfolyam, Frankfurter API,
lásd _eur_huf_rate_for_date) váltja Ft-ra - hétvégén/ünnepnapon automatikusan
az utolsó érvényes üzleti napi árfolyamot használva.
"""

import re
import json
import logging
import urllib.request
from datetime import datetime

from playwright.async_api import async_playwright

from innonest_core import login, load_session, make_browser_args, run_in_loop

log = logging.getLogger(__name__)

ARAJANLATOK_URL = "https://app.innonest.hu/bids"
MEGRENDELOLAPOK_URL = "https://app.innonest.hu/ordersheets"

LISTA_OLDALMERET = 100  # mindkét lista lapozási lépésköze


def _bids_page_url(offset: int) -> str:
    """Ajánlatok lista lapozási URL-je: /bids (0. oldal), majd
    /bids/index/100/, /bids/index/200/, ..."""
    return ARAJANLATOK_URL if offset == 0 else f"{ARAJANLATOK_URL}/index/{offset}/"


def _ordersheets_page_url(offset: int) -> str:
    """Megrendelőlapok lista lapozási URL-je: /ordersheets/index/0/all,
    /ordersheets/index/100/all, /ordersheets/index/200/all, ... - MINDEN oldal
    (a 0. is) "/index/{offset}/all" formátumú, ez eltér a bids mintától, ahol
    a 0. oldalnak nincs "/index/0/" előtagja. Felhasználó által megadott,
    élőben ellenőrzött URL-minta (2026.07.07)."""
    return f"{MEGRENDELOLAPOK_URL}/index/{offset}/all"


BID_PATTERN = re.compile(r"BID-(\d{4})-(\d+)")
ORDER_PATTERN = re.compile(r"(\d{4})-(\d+)")

# A megrendelőlap "Megnevezés" mezőjének végén automatikusan megjelenő
# BID-hivatkozás mintája, pl.: "... [Árajánlat KIV #BID-2026-155]"
BID_REF_PATTERN = re.compile(r"#(BID-\d{4}-\d+)\]")

# Összeg-minta a sor teljes szövegében, pl. "1 234 567 HUF"
HUF_AMOUNT_PATTERN = re.compile(r"([0-9][0-9 ]{2,}[0-9])\s*HUF")

# EUR-ban kiállított ajánlatok/megrendelések összege, pl. "1 760 EUR" vagy
# "1 760,50 EUR" - 2026.07.07-i javítás: korábban ezekre a sorokra a nettó Ft
# érték 0 maradt a Sheetben, mert csak a HUF_AMOUNT_PATTERN-t néztük.
EUR_AMOUNT_PATTERN = re.compile(r"([0-9][0-9 ]{2,}[0-9](?:[.,]\d{1,2})?)\s*EUR")

DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")

STATUS_WORDS = [
    "Piszkozat", "Elküldve", "Elfogadva", "Megrendelt",
    "Teljesítve", "Számlázva", "Törölve", "Lejárt", "Visszautasítva",
]


async def _paginated_year_count(page, url_for_offset, pattern: "re.Pattern", current_year: int, max_pages: int = 50) -> int:
    """Végiglapozza a listaoldalt LISTA_OLDALMERET-es offsetekkel
    (url_for_offset(0), url_for_offset(100), url_for_offset(200), ...), és
    megszámolja, hány sor azonosítója ("td.left.bold a" szövege) kezdődik a
    tárgyévvel. url_for_offset egy függvény (offset -> URL), mert a bids és az
    ordersheets lista eltérő URL-mintát használ (lásd _bids_page_url /
    _ordersheets_page_url).

    A lista alapból legújabb elöl rendezett, ezért amint egy korábbi évre eső
    sort találunk, tudjuk, hogy onnantól (ezen az oldalon és minden további
    oldalon) már csak régebbi évek jönnek - ott leállhatunk. Biztonsági korlát:
    max_pages (végtelen ciklus elleni védelem, ha a szerkezet váratlanul
    megváltozna).
    """
    count = 0
    offset = 0

    for _ in range(max_pages):
        url = url_for_offset(offset)
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

        bid_count = await _paginated_year_count(page, _bids_page_url, BID_PATTERN, current_year)
        order_count = await _paginated_year_count(page, _ordersheets_page_url, ORDER_PATTERN, current_year)

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


def _parse_eur_amounts(text: str):
    amounts = []
    for m in EUR_AMOUNT_PATTERN.finditer(text):
        raw = m.group(1).replace(" ", "").replace(",", ".")
        try:
            amounts.append(float(raw))
        except ValueError:
            continue
    return amounts


_EUR_HUF_RATE_CACHE = {}  # { "2026-06-30": 356.3, ... } - egy futáson belüli gyorsítótár


def _eur_huf_rate_for_date(date_str: str):
    """Az EUR/HUF árfolyam a megadott napon (YYYY-MM-DD), az Európai Központi
    Bank (ECB) napi referencia-árfolyamai alapján, a Frankfurter API-n keresztül
    (https://api.frankfurter.dev - ingyenes, kulcs nélküli szolgáltatás).
    Hétvégén/ünnepnapon nincs jegyzés - ilyenkor az API automatikusan az utolsó
    érvényes (megelőző) üzleti napi árfolyamot adja vissza, ami pontosan a
    kívánt "az adott napon érvényes árfolyam" viselkedés. Napi szinten
    gyorsítótárazva, hogy egy futás alatt ne kérdezzük le ugyanazt a napot
    többször. Hiba esetén None-t ad vissza (a hívó ilyenkor loggol és 0-t ír).
    """
    if not date_str:
        return None
    if date_str in _EUR_HUF_RATE_CACHE:
        return _EUR_HUF_RATE_CACHE[date_str]

    rate = None
    try:
        url = f"https://api.frankfurter.dev/v1/{date_str}?from=EUR&to=HUF"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw_rate = (data.get("rates") or {}).get("HUF")
        if raw_rate is not None:
            rate = float(raw_rate)
    except Exception as e:  # noqa: BLE001
        log.error(f"[SZAMLALO] EUR/HUF árfolyam lekérés hiba ({date_str}): {e}")
        rate = None

    _EUR_HUF_RATE_CACHE[date_str] = rate
    return rate


def _parse_netto_huf(text: str, datum: str) -> int:
    """A sor teljes szövegéből kiolvassa a nettó összeget, Ft-ban.
    Ha a sor HUF-ban van (a megszokott eset), a két érték (nettó/bruttó) közül
    a KISEBBET vesszük (ez a bevált logika, lásd a modul-docstringet). Ha a
    sorban nincs HUF összeg, csak EUR (pl. osztrák/külföldi ügyfelek egyes
    ajánlatai), akkor az EUR nettó összeget átváltjuk Ft-ra a sor kiállítási
    dátumán (ajánlat/megrendelőlap dátuma) érvényes EUR/HUF árfolyammal - ha a
    dátum nem olvasható ki a sorból, a mai napi árfolyammal (közelítés,
    figyelmeztetéssel naplózva).
    """
    huf_amounts = _parse_huf_amounts(text)
    if huf_amounts:
        return min(huf_amounts)

    eur_amounts = _parse_eur_amounts(text)
    if not eur_amounts:
        return 0

    eur_netto = min(eur_amounts)
    rate_date = datum or datetime.now().strftime("%Y-%m-%d")
    if not datum:
        log.error(f"[SZAMLALO] EUR-s sorban nem található dátum, mai árfolyammal közelítve. Sor: {text[:200]!r}")

    rate = _eur_huf_rate_for_date(rate_date)
    if not rate:
        log.error(
            f"[SZAMLALO] Nem sikerült EUR/HUF árfolyamot lekérni ehhez a naphoz: {rate_date!r} - "
            f"a sor nettó Ft értéke 0 marad, kézi ellenőrzés szükséges. EUR összeg: {eur_netto}"
        )
        return 0

    return round(eur_netto * rate)


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


async def _scrape_rows_paginated(page, url_for_offset, id_pattern: "re.Pattern", current_year: int, max_pages: int = 50):
    """A _scrape_rows lapozós változata: végigmegy a listaoldalakon
    (url_for_offset(0), url_for_offset(100), ... LISTA_OLDALMERET-es
    offsetekkel - lásd _bids_page_url / _ordersheets_page_url), és összegyűjti
    az összes sort, amíg a tárgyévbe eső azonosítókat talál. A lista legújabb
    elöl rendezett, ezért amint egy korábbi évre eső azonosítót talál, az adott
    oldal feldolgozása után megáll - a további oldalakon már csak régebbi évek
    lennének.
    """
    all_rows = []
    offset = 0

    for _ in range(max_pages):
        url = url_for_offset(offset)
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
    raw_rows = await _scrape_rows_paginated(page, _bids_page_url, BID_PATTERN, current_year)
    out = []
    for r in raw_rows:
        bid = r["id"]
        bid_m = BID_PATTERN.match(bid)
        if not bid_m or int(bid_m.group(1)) != current_year:
            continue
        text = r["text"]
        date_m = DATE_PATTERN.search(text)
        datum = date_m.group(1) if date_m else ""
        netto = _parse_netto_huf(text, datum)
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
    raw_rows = await _scrape_rows_paginated(page, _ordersheets_page_url, ORDER_PATTERN, current_year)
    out = []
    for r in raw_rows:
        rendelesszam = r["id"]
        order_m = ORDER_PATTERN.match(rendelesszam)
        if not order_m or int(order_m.group(1)) != current_year:
            continue
        text = r["text"]
        bid_ref = BID_REF_PATTERN.search(text)
        bid = bid_ref.group(1) if bid_ref else ""
        date_m = DATE_PATTERN.search(text)
        datum = date_m.group(1) if date_m else ""
        netto = _parse_netto_huf(text, datum)
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
