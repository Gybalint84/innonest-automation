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

"Számlázva" állapot / "Futó projektek" KPI (2026.07.07-i módosítás): korábban
a Projektek tábla Státusza (és ebből a Power BI dashboard "Futó projektek"
KPI-ja) kizárólag a megrendelőlap SAJÁT Státusz-szövegéből jött (_parse_status
- pl. "Megrendelt", "Számlázva"), ami a felhasználó szerint nem eléggé
megbízható jelzés arra, hogy egy projekt valóban lezárult-e. Ezért egy külön
lépésben végignéztük az Innonest Számlák listáját is
(https://app.innonest.hu/invoices/index/0/ - lásd _invoices_page_url), és a
benne szereplő BID-szám / megrendelésszám-hivatkozások alapján (lásd
_extract_invoice_refs, _scrape_invoiced_refs) állapítottuk meg, mely
megrendelések vannak ténylegesen kiszámlázva.

*** 2026.08.14-i JAVÍTÁS - BILLINGO-VÁLTÁS (ez a fájl aktuális működése) ***
A cég átállt Számlázz.hu-ról Billingóra, és ezzel az Innonest "Számlázás"
(/invoices) listája KIÜRÜLT - az Innonest ezen a modulján a felület azt írja:
"Ez a modul a Számlázz.hu-val működik!" (ugyanez a megállapítás szerepel a
billingo_teljesitmeny.py 2026.08.14-i docstringjében is, élőben ellenőrizve).
Következmény a dashboardon (a felhasználó jelzése: "az innonestben eltűntek a
számlák, így rossz lett a dashboard"):
  - _scrape_invoiced_refs mostantól MINDIG üres halmazokat ad vissza, tehát
    egyetlen új megrendelés sem kap "Számlázva" állapotot;
  - emiatt a Projektek tábla Státusza a Billingo-váltás óta kiállított
    számláknál "Megrendelt"/üres marad;
  - a dashboard három KPI-ja ezért hibás:
      * "Futó projektek" - a már kiszámlázott (lezárt) projektek is benne
        maradnak, ezért folyamatosan nő (élőben 52 db volt 8-as cél mellett);
      * "Leszámlázott tételek összege" - beragadt a váltás előtti szintre
        (élőben 81,1 M Ft, csak a régi, Innonestben számlázott tételek);
      * "Még nem számlázott megrendelések összege" - ezzel szemben felfújva
        (élőben 114,6 M Ft, mert minden Billingóban számlázott projekt is
        idekerül).
Javítás: a számlázottságot mostantól a BILLINGO-ból állapítjuk meg, nem az
Innonestből. A Billingo számlán nem szerepel a BID szám, ezért ugyanazt a
- már élesben bevált - párosítási logikát használjuk, amit a
billingo_teljesitmeny.py az értékesítői teljesítményhez: Billingo számla ->
Innonest megrendelőlap egyeztetés (nettó összeg + deviza, majd ügyfélnév és
dátumközelség) -> BID (lásd _match_sheet_bid ott; élesben ellenőrzött találat:
SQ-2026-88 / 7 HÁZ BT. / 4 528 428 HUF <-> KIV 2026-84 / BID-2026-257).
A párosításhoz szükséges megrendelőlap-listát NEM kell külön lekaparni: ez a
modul amúgy is végigolvassa a /ordersheets listát (_scrape_megrendelesek), így
a saját, már meglévő adatainkból építjük fel (lásd _sheets_for_matching) -
ezzel egy teljes Playwright-scrapelést takarítunk meg.
Két további, szándékos változás:
  1) Az Innonest /invoices lista lekaparását (_scrape_invoiced_refs) MÁR NEM
     hívjuk a fő folyamatban (INNONEST_INVOICES_ENABLED = False). A függvény
     benne marad, hogy egyetlen kapcsolóval visszakapcsolható legyen, ha az
     Innonest újra tölteni kezdené ezt a listát. Mellékhaszon: a szinkron
     futásideje érezhetően csökken (egy teljes, több oldalas listalapozás
     kimarad), ami a korábban tapasztalt terhelés-eredetű timeoutokon
     (lásd lentebb az EUR/HUF szakaszt) is segít.
  2) A Billingo-lekérés a SZINKRON burkolóban (innonest_full_data) fut, MIUTÁN
     a Playwright-böngésző már bezárult - így a blokkoló HTTP-hívás nem a
     közös, egyszálú asyncio event loopot terheli (pontosan az a hibaforrás,
     ami az EUR/HUF lekérés timeoutjait okozta).
Hibatűrés: ha a BILLINGO_API_KEY nincs beállítva, vagy a Billingo API hibázik,
a szinkron NEM áll le - a megrendelések a saját Innonest-státuszukkal mennek
tovább, és a log egy figyelmeztetést kap (lásd _billingo_invoiced_bids).

EUR/HUF árfolyam-lekérés timeout (2026.08.12-i javítás): éles Railway-logban
sok _eur_huf_rate_for_date hívás "The read operation timed out" hibával halt
el (10 mp-es timeout mellett), annak ellenére, hogy a Frankfurter API
külső, függetlenül végzett teszteléssel PONTOSAN ugyanezekre a napokra
azonnal és hibátlanul válaszolt - tehát nem az API a hibás. A valószínű ok:
ez a hívás ugyanazon az egyetlen, közös asyncio event loopon fut (lásd
innonest_core.py run_in_loop), mint a Playwright-alapú scrapelés, és a
konténer ilyenkor jellemzően erősen terhelt (sok egymást követő oldalbetöltés
a bids/ordersheets/invoices listákon) - emiatt egy egyébként < 1 mp-es
API-hívás is könnyen 10 mp fölé csúszhat. Mivel ez átmeneti, terhelés-függő
jelenség, retry-jal jól kezelhető: mostantól 3 próbálkozás történik, növekvő
timeout-tal (10 / 20 / 30 mp), rövid szünettel közöttük - lásd
_eur_huf_rate_for_date. Csak akkor ad fel véglegesen (rate=None, a sor nettó
Ft értéke 0 marad), ha mind a 3 próbálkozás sikertelen.
"""

import re
import json
import logging
import time
import urllib.request
from datetime import datetime

from playwright.async_api import async_playwright

from innonest_core import login, load_session, make_browser_args, run_in_loop

log = logging.getLogger(__name__)

ARAJANLATOK_URL = "https://app.innonest.hu/bids"
MEGRENDELOLAPOK_URL = "https://app.innonest.hu/ordersheets"
SZAMLAK_URL = "https://app.innonest.hu/invoices"

LISTA_OLDALMERET = 100  # mindkét lista lapozási lépésköze

# 2026.08.14: a Billingo-váltás óta az Innonest /invoices listája üres
# ("Ez a modul a Számlázz.hu-val működik!"), ezért a lekaparását kihagyjuk -
# lásd a modul docstringjének "BILLINGO-VÁLTÁS" szakaszát. Ha az Innonest
# valaha újra tölteni kezdené ezt a listát, elég ezt True-ra állítani.
INNONEST_INVOICES_ENABLED = False


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


def _invoices_page_url(offset: int) -> str:
    """Számlák lista lapozási URL-je: /invoices/index/0/, /invoices/index/100/,
    ... - MINDEN oldal (a 0. is) "/index/{offset}/" formátumú (a megrendelőlap-
    mintához hasonló, de "/all" végződés nélkül). Felhasználó által megadott,
    élőben ellenőrzött URL-minta (2026.07.07).

    2026.08.14: a Billingo-váltás óta ez a lista üres - lásd
    INNONEST_INVOICES_ENABLED."""
    return f"{SZAMLAK_URL}/index/{offset}/"


BID_PATTERN = re.compile(r"BID-(\d{4})-(\d+)")
ORDER_PATTERN = re.compile(r"(\d{4})-(\d+)")

# A megrendelőlap "Megnevezés" mezőjének végén automatikusan megjelenő
# BID-hivatkozás mintája, pl.: "... [Árajánlat KIV #BID-2026-155]". 2026.07.07-i
# javítás: élő Sheet-adatokon (Projektek fül) találtunk 7 olyan sort, ahol a
# hivatkozás "BID-" előtag NÉLKÜL szerepel, pl. "[Árajánlat KIV #2026-12]" -
# a régi minta ("BID-" kötelező) ezekre nem talált egyezést, így bid="" maradt,
# és ezek az idei ajánlatok sosem lettek "Megrendelve: Igen"-re állítva az
# Ajánlatok táblában. Az új minta a "BID-" előtagot opcionálisnak veszi, és a
# hívó kód (lásd _scrape_megrendelesek) mindig egységesen "BID-" előtaggal
# normalizálja, hogy pontosan illeszkedjen az Ajánlatok tábla bid-kulcsaihoz.
BID_REF_PATTERN = re.compile(r"#(?:BID-)?(\d{4}-\d+)\]")

# 2026.07.07-i bővítés: a Számlák lista (/invoices) soraiban a felhasználó
# visszajelzése szerint UGYANÚGY megjelenik mind az eredeti BID-szám, mind a
# hozzá tartozó megrendelésszám (pl. "...#BID-2026-155..." ÉS "...#2026-69...").
# 2026.08.14: a Billingo-váltás óta ez a lista üres, így ez a minta jelenleg
# nem használt (lásd INNONEST_INVOICES_ENABLED).
INVOICE_REF_PATTERN = re.compile(r"#(BID-)?(\d{4}-\d+)")


def _extract_invoice_refs(text: str):
    """Egy számla-sor teljes szövegéből kiszedi AZ ÖSSZES #BID-YYYY-NN és/vagy
    #YYYY-NN hivatkozást (nem csak az elsőt, mint a BID_REF_PATTERN-nél),
    mert egy számla egyszerre hivatkozhat az eredeti ajánlatra ÉS a
    megrendelésre is. Külön halmazba teszi a BID-es és a bare (megrendelés-
    szám) hivatkozásokat."""
    bids, orders = set(), set()
    for m in INVOICE_REF_PATTERN.finditer(text):
        ref = m.group(2)
        if m.group(1):
            bids.add("BID-" + ref)
        else:
            orders.add(ref)
    return bids, orders

# Összeg-minta a sor teljes szövegében, pl. "1 234 567 HUF"
HUF_AMOUNT_PATTERN = re.compile(r"([0-9][0-9 ]{2,}[0-9])\s*HUF")

# EUR-ban kiállított ajánlatok/megrendelések összege, pl. "850 EUR",
# "1 760 EUR" vagy "1 787,50 EUR". 2026.07.07-i javítás (1. kör): korábban
# ezekre a sorokra a nettó Ft érték 0 maradt, mert csak a HUF_AMOUNT_PATTERN-t
# néztük. 2026.07.07-i javítás (2. kör, valós Innonest-képernyőkép alapján):
# az első verzió mintája legalább 4 számjegyet várt (a HUF-mintát másolva),
# de az EUR összegek gyakran 3 jegyűek is (pl. "850 EUR") - ezért NINCS
# minimum-hossz megkötés. Emellett a nagyobb EUR összegeknél az ezres
# elválasztó a weboldalon nem sima szóköz, hanem nem törhető szóköz (NBSP,
#  ) - ezt is elfogadja a minta.
EUR_AMOUNT_PATTERN = re.compile(r"(\d(?:[\d  ]*\d)?(?:[.,]\d{1,2})?)\s*EUR")

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
        raw = m.group(1).replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            amounts.append(float(raw))
        except ValueError:
            continue
    return amounts


_EUR_HUF_RATE_CACHE = {}  # { "2026-06-30": 356.3, ... } - egy futáson belüli gyorsítótár

# 2026.08.12-i javítás: 3 próbálkozás, növekvő timeout-tal - lásd a modul
# docstringjének "EUR/HUF árfolyam-lekérés timeout" szakaszát.
_EUR_HUF_RETRY_TIMEOUTS = (10, 20, 30)


def _eur_huf_rate_for_date(date_str: str):
    """Az EUR/HUF árfolyam a megadott napon (YYYY-MM-DD), az Európai Központi
    Bank (ECB) napi referencia-árfolyamai alapján, a Frankfurter API-n keresztül
    (https://api.frankfurter.dev - ingyenes, kulcs nélküli szolgáltatás).
    Hétvégén/ünnepnapon nincs jegyzés - ilyenkor az API automatikusan az utolsó
    érvényes (megelőző) üzleti napi árfolyamot adja vissza, ami pontosan a
    kívánt "az adott napon érvényes árfolyam" viselkedés. Napi szinten
    gyorsítótárazva, hogy egy futás alatt ne kérdezzük le ugyanazt a napot
    többször. Hiba esetén None-t ad vissza (a hívó ilyenkor loggol és 0-t ír).

    2026.07.07-i javítás: éles Railway-logban minden hívás 403 Forbidden-nel
    halt el. Ennek oka (élőben megerősítve): a Python urllib.request alap
    User-Agent fejléce ("Python-urllib/3.x") sok API/CDN mögött ki van tiltva
    bot-védelemként - a Frankfurter API is ezt csinálja. Böngésző-szerű
    User-Agent fejléccel a kérés átmegy.

    2026.08.12-i javítás: éles Railway-logban sok hívás "The read operation
    timed out" hibával halt el (10 mp-es timeout mellett) - ez FÜGGETLEN
    külső teszteléssel ellenőrizve NEM az API hibája (ugyanazokra a napokra
    a Frankfurter azonnal válaszolt), hanem valószínűleg a konténer terhelése
    (ugyanazon az event loopon fut, mint a Playwright-scrapelés - lásd a modul
    docstringjét). Mivel ez jellemzően átmeneti, most 3 próbálkozás történik,
    növekvő timeout-tal (_EUR_HUF_RETRY_TIMEOUTS: 10 / 20 / 30 mp), rövid
    szünettel közöttük - csak a 3. sikertelen próbálkozás után adja fel
    véglegesen (rate=None).
    """
    if not date_str:
        return None
    if date_str in _EUR_HUF_RATE_CACHE:
        return _EUR_HUF_RATE_CACHE[date_str]

    url = f"https://api.frankfurter.dev/v1/{date_str}?from=EUR&to=HUF"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    rate = None
    last_error = None

    for attempt, timeout_s in enumerate(_EUR_HUF_RETRY_TIMEOUTS, start=1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            raw_rate = (data.get("rates") or {}).get("HUF")
            if raw_rate is not None:
                rate = float(raw_rate)
            break
        except Exception as e:  # noqa: BLE001
            last_error = e
            is_last_attempt = attempt == len(_EUR_HUF_RETRY_TIMEOUTS)
            if is_last_attempt:
                log.error(f"[SZAMLALO] EUR/HUF árfolyam lekérés hiba ({date_str}), {attempt}/{len(_EUR_HUF_RETRY_TIMEOUTS)} próbálkozás után is: {e}")
            else:
                log.warning(f"[SZAMLALO] EUR/HUF árfolyam lekérés {attempt}. próbálkozása sikertelen ({date_str}): {e} - újrapróbálom {_EUR_HUF_RETRY_TIMEOUTS[attempt]} mp-es timeouttal.")
                time.sleep(1.5)

    _EUR_HUF_RATE_CACHE[date_str] = rate
    return rate


def _parse_netto_native(text: str):
    """A sor teljes szövegéből kiolvassa a nettó összeget AZ EREDETI devizában,
    (összeg, deviza) párként - pl. (4528428.0, "HUF") vagy (1787.5, "EUR").
    Ha nincs felismerhető összeg: (0.0, "").

    2026.08.14-i bevezetés: a Billingo-párosításhoz (lásd _billingo_invoiced_bids)
    az EREDETI devizanemű összeg kell, mert a Billingo számla is az eredeti
    devizában van. Ha a HUF-ra váltott értéket hasonlítanánk össze, az ajánlat
    és a számla eltérő napi EUR/HUF árfolyama miatt csúszna az egyezés.
    A logika (HUF esetén a két szám közül a kisebb = nettó) megegyezik a
    _parse_netto_huf-ban használttal.
    """
    huf_amounts = _parse_huf_amounts(text)
    if huf_amounts:
        return float(min(huf_amounts)), "HUF"

    eur_amounts = _parse_eur_amounts(text)
    if eur_amounts:
        return float(min(eur_amounts)), "EUR"

    return 0.0, ""


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


async def _scrape_invoiced_refs(page, current_year: int, max_pages: int = 50):
    """Végiglapozza a Számlák listát (lásd _invoices_page_url), és összegyűjti,
    mely BID-számokra és/vagy megrendelésszámokra állítottak ki idei számlát.

    2026.08.14 ÓTA NEM HASZNÁLT A FŐ FOLYAMATBAN (INNONEST_INVOICES_ENABLED =
    False): a Billingo-váltás óta az Innonest /invoices listája üres, ezért ez
    a függvény mindig üres halmazokat adna vissza, cserébe egy több oldalas,
    lassú listalapozásba kerülne. A helyére a Billingo-alapú
    _billingo_invoiced_bids lépett - lásd a modul docstringjét. A függvényt
    szándékosan meghagytuk, hogy egyetlen kapcsolóval (INNONEST_INVOICES_ENABLED
    = True) visszakapcsolható legyen, ha az Innonest újra tölteni kezdené ezt a
    listát.

    2026.07.07-i bevezetés: a felhasználó jelezte, hogy a "Futó projektek" KPI
    korábban a megrendelőlap SAJÁT Státusz-szövegéből ("Megrendelt"/"Számlázva"
    stb., lásd _parse_status) következtetett arra, hogy egy projekt lezárult-e -
    ez nem volt elég megbízható. Ehelyett a tényleges számlázottságot kell
    nézni.

    A lista (a többihez hasonlóan) legújabb elöl rendezett - amint egy
    korábbi évre eső dátumú sort találunk, megállunk.
    """
    invoiced_bids, invoiced_orders = set(), set()
    offset = 0

    for _ in range(max_pages):
        url = _invoices_page_url(offset)
        raw_rows = await _scrape_rows(page, url)
        if not raw_rows:
            break

        reached_previous_year = False
        for r in raw_rows:
            text = r["text"]
            date_m = DATE_PATTERN.search(text)
            if date_m and int(date_m.group(1)[:4]) != current_year:
                reached_previous_year = True
                break
            bids, orders = _extract_invoice_refs(text)
            invoiced_bids |= bids
            invoiced_orders |= orders

        if reached_previous_year or len(raw_rows) < LISTA_OLDALMERET:
            break

        offset += LISTA_OLDALMERET

    return invoiced_bids, invoiced_orders


# ══════════════════════════════════════════════════════════════════════════════
# BILLINGO-ALAPÚ SZÁMLÁZOTTSÁG (2026.08.14, a Billingo-váltás miatt)
# ══════════════════════════════════════════════════════════════════════════════

def _sheets_for_matching(megrendelesek: list) -> list:
    """A már lekapart megrendelőlapokból felépíti azt a listát, amit a
    billingo_teljesitmeny._match_sheet_bid vár:
        {bid, ugyfel, netto, currency, datum}
    Csak azok a sorok kerülnek bele, amelyeknél van BID-hivatkozás ÉS
    felismerhető összeg - a többivel úgysem lehetne párosítani.

    Így NEM kell külön Playwright-scrapeléssel újra lekérni a megrendelőlap-
    listát (mint a billingo_teljesitmeny._get_ordersheets teszi az értékesítői
    teljesítménynél) - ez a modul amúgy is végigolvassa a /ordersheets listát.
    """
    sheets = []
    for m in megrendelesek:
        if not m.get("bid"):
            continue
        netto = m.get("netto_orig") or 0.0
        currency = m.get("penznem") or ""
        if not netto or not currency:
            continue
        sheets.append({
            "bid":      m["bid"],
            "ugyfel":   m.get("ugyfel", ""),
            "netto":    float(netto),
            "currency": currency,
            "datum":    m.get("datum", ""),
        })
    return sheets


def _billingo_invoiced_bids(megrendelesek: list, current_year: int) -> dict:
    """Végigolvassa a tárgyévi Billingo számlákat, és visszaad egy dict-et:
        {
          "bids":        {"BID-2026-257", ...},  # amelyekhez találtunk számlát
          "netto_ft":    156659115,              # TÉNYLEGES kiszámlázott nettó, Ft
          "szamla_db":   79,
          "parositatlan": 14,
        }

    *** 2026.08.14-i 2. kör - MIÉRT KELL A "netto_ft"? ***
    A dashboard "Leszámlázott tételek összege" kártyája eddig az Innonest
    MEGRENDELŐLAPOK értékét összegezte azoknál a projekteknél, amelyekhez
    találtunk számlát - ez szerkezetileg MÁS szám, mint a ténylegesen
    kiszámlázott összeg, és sosem fog egyezni a Billingóval. Élő példa
    (2026.08.14): dashboard 143 818 231 Ft, Billingo valójában 156 659 115 Ft.
    Az eltérés két, egymással ellentétes irányú okból áll:
      - RÉSZ-/GYŰJTŐSZÁMLÁZÁS: egy megrendelőlapot több számlán számláznak ki
        (élő példa: a 15 784 975 Ft-os Ganz megrendelőlap = SQ-2026-67
        13 940 125 Ft + SQ-2026-70 1 842 750 Ft), így EGYIK számla összege sem
        egyezik a megrendelőlapéval -> a _match_sheet_bid (±5 Ft / 0,1% tűrés)
        egyiket sem tudja párosítani;
      - PÁROSÍTATLAN SZÁMLÁK: 14 db (21,66 M Ft + 12 465 EUR) olyan számla van,
        amihez egyáltalán nincs illeszkedő idei megrendelőlap.
    Ezért a PÉNZÜGYI számot mostantól közvetlenül a Billingóból összegezzük -
    ez párosítás-független, tehát pontosan annyi lesz, amennyit a Billingo mutat.
    A BID-párosítás továbbra is kell, de már CSAK a projekt-státuszhoz
    ("Számlázva" -> kikerül a "Futó projektek" KPI-ból).

    EUR-s számlák: a nettót a SZÁMLA KELTÉNEK napján érvényes EUR/HUF
    árfolyammal váltjuk Ft-ra (_eur_huf_rate_for_date, ECB/Frankfurter) -
    ugyanaz a logika, mint az EUR-s megrendelőlapoknál. Ha az árfolyam nem
    kérhető le, az adott számla 0 Ft-tal szerepel és ERROR szintű logot kap
    (kézi ellenőrzés szükséges) - a többi számla ettől még összeadódik.

    Működés (a billingo_teljesitmeny.py-ban már élesben bevált logikára építve):
      1. Végigmegyünk a tárgyév hónapjain (januártól a mai hónapig), és minden
         hónapra lekérjük a Billingo számlákat (/documents).
      2. Kihagyjuk a sztornózott és a nem "invoice" típusú bizonylatokat.
      3. Minden számlához először megpróbáljuk a BID-et közvetlenül a számla
         szövegéből kiolvasni (_extract_bid - ha valaha rákerülne a
         megjegyzésre/tételnévre), különben a megrendelőlap-párosítást
         használjuk (_match_sheet_bid: nettó összeg + deviza, majd ügyfélnév,
         majd dátumközelség).

    Hibatűrés (szándékosan "néma" a hívó felé): ha nincs BILLINGO_API_KEY, vagy
    a Billingo API/import hibázik, ÜRES halmazt adunk vissza és figyelmeztetést
    naplózunk - a szinkron ilyenkor a megrendelések saját Innonest-státuszával
    fut tovább, semmi nem áll le. Ez azért fontos, mert ez a lépés csak
    "gazdagítja" az adatot; ha elbukik, attól még a Sheet frissülhet.

    FUTTATÁS HELYE: a szinkron burkolóból (innonest_full_data) hívjuk, MIUTÁN a
    Playwright-böngésző már bezárult - így ez a blokkoló HTTP-hívás nem a közös,
    egyszálú asyncio event loopot terheli (lásd a modul docstringjében az
    EUR/HUF timeout tanulságát).
    """
    ures = {"bids": set(), "netto_ft": 0, "szamla_db": 0, "parositatlan": 0}

    sheets = _sheets_for_matching(megrendelesek)
    if not sheets:
        log.warning("[SZAMLALO] Billingo-párosítás kihagyva: egyetlen megrendelőlap sem "
                    "adott BID + összeg + deviza hármast.")
        # FONTOS: a pénzügyi összeget ilyenkor is összeszedjük (az független a
        # párosítástól) - ezért NEM lépünk ki, csak üres sheets-szel megyünk tovább.

    try:
        from billingo_teljesitmeny import (
            BILLINGO_API_KEY,
            _billingo_get,
            _doc_net,
            _doc_currency,
            _extract_bid,
            _match_sheet_bid,
            _month_range,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"[SZAMLALO] Billingo modul nem elérhető ({e}) - a számlázottság "
                    f"jelölése kimarad, a megrendelések a saját Innonest-státuszukkal maradnak.")
        return ures

    if not BILLINGO_API_KEY:
        log.warning("[SZAMLALO] BILLINGO_API_KEY nincs beállítva a Railway-en - a "
                    "számlázottság jelölése kimarad (a dashboard 'Futó projektek' és "
                    "'Leszámlázott/Még nem számlázott' KPI-jai emiatt pontatlanok maradnak).")
        return ures

    invoiced_bids = set()
    netto_ft_osszesen = 0
    szamla_db = 0
    parositatlan = 0
    utolso_honap = datetime.now().month

    for honap in range(1, utolso_honap + 1):
        month = f"{current_year:04d}-{honap:02d}"
        try:
            start, end = _month_range(month)
            page_no = 1
            while True:
                data = _billingo_get("/documents", {
                    "start_date": start, "end_date": end, "per_page": 100, "page": page_no,
                })
                docs = data.get("data") or []
                for d in docs:
                    if (str(d.get("type") or "").lower() not in ("invoice", "")):
                        continue
                    if d.get("cancelled"):
                        continue
                    full = d
                    if not d.get("items") and not d.get("comment"):
                        try:
                            full = _billingo_get(f"/documents/{d.get('id')}")
                        except Exception:
                            full = d

                    szamla_db += 1
                    partner  = ((full.get("partner") or {}).get("name")) or ""
                    datum    = full.get("invoice_date") or ""
                    netto    = _doc_net(full)
                    currency = _doc_currency(full)

                    # ── A TÉNYLEGES kiszámlázott nettó összegzése, Ft-ban ──
                    # (párosítástól FÜGGETLEN - ez adja a dashboard
                    # "Leszámlázott tételek összege" kártyáját)
                    if currency == "HUF":
                        netto_ft_osszesen += round(netto)
                    else:
                        rate = _eur_huf_rate_for_date(datum) if currency == "EUR" else None
                        if rate:
                            netto_ft_osszesen += round(netto * rate)
                        else:
                            log.error(
                                f"[SZAMLALO] Számla összege NEM adódott hozzá a leszámlázott "
                                f"összeghez (nincs árfolyam): {full.get('invoice_number')!r} | "
                                f"{netto} {currency} | {datum} - kézi ellenőrzés szükséges."
                            )

                    bid = _extract_bid(full) or _match_sheet_bid(
                        netto, currency, partner, datum, sheets
                    )
                    if bid:
                        invoiced_bids.add(bid)
                    else:
                        parositatlan += 1
                        log.info(
                            f"[SZAMLALO] Billingo számla nem párosítható megrendelőlaphoz: "
                            f"{full.get('invoice_number')!r} | {partner!r} | "
                            f"{netto} {currency} | {datum} "
                            f"(az összege a leszámlázott összegben ATTÓL MÉG benne van)"
                        )

                if len(docs) < 100:
                    break
                page_no += 1
                if page_no > 30:  # biztonsági korlát
                    break
        except Exception as e:  # noqa: BLE001
            log.warning(f"[SZAMLALO] Billingo lekérés hiba ({month}): {e} - ez a hónap kimarad.")
            continue

    log.info(
        f"[SZAMLALO] Billingo számlázottság: {szamla_db} számla feldolgozva, "
        f"{len(invoiced_bids)} egyedi BID párosítva, {parositatlan} párosítatlan. "
        f"Ténylegesen kiszámlázott nettó: {netto_ft_osszesen:,} Ft".replace(",", " ")
    )
    return {
        "bids":         invoiced_bids,
        "netto_ft":     netto_ft_osszesen,
        "szamla_db":    szamla_db,
        "parositatlan": parositatlan,
    }


def _apply_billingo_invoiced(data: dict) -> None:
    """Kétféle dolgot tesz, helyben módosítva a data dict-et:

    1. A Billingóban kiszámlázott megrendelések Státuszát "Számlázva"-ra állítja
       (data["megrendelesek"]) - ez a dashboard "Futó projektek" KPI-ját teszi
       helyessé.
    2. Beleírja a TÉNYLEGES kiszámlázott nettót Ft-ban:
           data["szamlazott_netto_ft"]   (pl. 156659115)
           data["szamlazott_szamla_db"]  (pl. 79)
       Ezt a Sheets sync script a "Számlázás" munkalapra írja ki, és innen veszi
       a dashboard a "Leszámlázott tételek összege" kártyát - lásd a
       _billingo_invoiced_bids docstringjében, MIÉRT nem a megrendelőlapok
       értékét összegezzük.

    Megtartott, szándékos aszimmetria (a 2026.07.07-i logikából): ha egy
    megrendelőlap SAJÁT állapota már "Számlázva", azt NEM vesszük vissza akkor
    sem, ha a Billingóban nem találtunk hozzá számlát - így a Billingo-váltás
    ELŐTT, még az Innonestben/Számlázz.hu-n kiszámlázott régi projektek is
    lezártak maradnak (a dashboardon ezek adják a "Leszámlázott tételek
    összege" korábbi részét).
    """
    megrendelesek = data.get("megrendelesek") or []
    current_year = data.get("ev") or datetime.now().year

    billingo = _billingo_invoiced_bids(megrendelesek, current_year)
    invoiced_bids = billingo["bids"]

    # A pénzügyi számot akkor is átadjuk, ha egyetlen megrendelés sincs -
    # a "Leszámlázott tételek összege" kártya ettől függetlenül helyes lesz.
    data["szamlazott_netto_ft"]  = billingo["netto_ft"]
    data["szamlazott_szamla_db"] = billingo["szamla_db"]

    if not megrendelesek:
        return

    ujonnan_jelolt = 0
    for m in megrendelesek:
        mar_szamlazott = (m.get("allapot") == "Számlázva")
        billingo_szerint = bool(m.get("bid")) and m["bid"] in invoiced_bids
        m["szamlazva"] = mar_szamlazott or billingo_szerint
        if billingo_szerint and not mar_szamlazott:
            m["allapot"] = "Számlázva"
            ujonnan_jelolt += 1

    osszes_szamlazott = sum(1 for m in megrendelesek if m.get("szamlazva"))
    log.info(
        f"[SZAMLALO] Számlázottság alkalmazva: {len(megrendelesek)} megrendelésből "
        f"{osszes_szamlazott} számlázott (ebből {ujonnan_jelolt} a Billingo alapján újonnan jelölve)."
    )


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
        # Egységesen "BID-" előtaggal normalizálva (lásd BID_REF_PATTERN
        # komment) - így illeszkedik az Ajánlatok tábla "BID-YYYY-NN" kulcsaira
        # akkor is, ha a megrendelőlapon a hivatkozás előtag nélkül szerepelt.
        bid = ("BID-" + bid_ref.group(1)) if bid_ref else ""
        date_m = DATE_PATTERN.search(text)
        datum = date_m.group(1) if date_m else ""
        netto = _parse_netto_huf(text, datum)
        # 2026.08.14: az eredeti devizanemű összeget is eltesszük, mert a
        # Billingo-párosítás ezzel dolgozik (lásd _parse_netto_native).
        netto_orig, penznem = _parse_netto_native(text)
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
            "netto_orig": netto_orig,
            "penznem": penznem,
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

        # 2026.08.14-i módosítás: az Innonest Számlák listája (/invoices) a
        # Billingo-váltás óta ÜRES, ezért alapból NEM lapozzuk végig - lásd
        # INNONEST_INVOICES_ENABLED és a modul docstringjét. A számlázottságot
        # a Billingóból állapítjuk meg, a szinkron burkolóban
        # (innonest_full_data -> _apply_billingo_invoiced), miután ez a
        # böngésző már bezárult.
        if INNONEST_INVOICES_ENABLED:
            invoiced_bids, invoiced_orders = await _scrape_invoiced_refs(page, current_year)
            for m in megrendelesek:
                is_invoiced = (bool(m.get("bid")) and m["bid"] in invoiced_bids) or (m.get("rendelesszam") in invoiced_orders)
                if is_invoiced:
                    m["szamlazva"] = True
                    m["allapot"] = "Számlázva"

        await browser.close()

        return {
            "ev": current_year,
            "ajanlatok": ajanlatok,
            "megrendelesek": megrendelesek,
        }


def innonest_full_data() -> dict:
    try:
        data = run_in_loop(_innonest_full_data_async())
    except Exception as e:  # noqa: BLE001
        log.error(f"[SZAMLALO] Teljes lista hiba: {e}")
        return {
            "ev": datetime.now().year,
            "ajanlatok": [],
            "megrendelesek": [],
            "error": str(e),
        }

    # 2026.08.14: a számlázottság jelölése Billingo alapján - SZÁNDÉKOSAN itt,
    # a run_in_loop UTÁN, hogy a blokkoló HTTP-hívások ne a Playwright-tel közös
    # event loopon fussanak. Saját try/except: ha ez a lépés elbukik, a már
    # sikeresen lekapart ajánlat-/megrendelés-lista akkor is visszamegy a
    # Sheets sync scriptnek.
    try:
        _apply_billingo_invoiced(data)
    except Exception as e:  # noqa: BLE001
        log.error(f"[SZAMLALO] Billingo számlázottság-jelölés hiba: {e} - a megrendelések "
                  f"a saját Innonest-státuszukkal mennek tovább.")

    return data


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
