"""
innonest_szamlalo.py
---------------------
Az innonest_core.py meglévő async Playwright-mintájára épül (login/session,
run_in_loop, make_browser_args) - ugyanúgy hívható a szerverből, mint az
innonest_adatok_leker(bid) a core modulban.

Kiolvassa az Innonest két listaoldalának legfelső sorát (alapból legújabb
elöl rendezve, ahogy megerősítve lett), hogy megkapja az idei ajánlatok és
megrendelések darabszámát:
  - https://app.innonest.hu/bids          - legfelső sor "BID-2026-246" -> 246
  - https://app.innonest.hu/ordersheets   - legfelső sor "2026-69"      -> 69

Használat a Flask szerverben (server.py-ban regisztrálva, a többi modul
mintájára):
    from innonest_szamlalo import register_innonest_szamlalo_routes
    register_innonest_szamlalo_routes(app)

A Google Sheets sync script (SQM_PowerBI_Sheet_Sync.gs, frissitsTolcser_)
ezt a GET /innonest-counters endpointot hívja, ez adja a Tölcsér "Ajánlat"
és "Megrendelés" szakaszainak darabszámát.

Szelektor DevTools-szal ellenőrizve (2026.07.05, ordersheets oldal): a sorazonosító
mindig a "td.left.bold a" linkben van, pl. <td class="left bold"><a ...>2026-69</a></td>,
ugyanaz a minta, mint a bids oldalon (lásd innonest_core.py get_arajanlat_tetelek).
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


async def _first_row_number(page, url: str, pattern: "re.Pattern"):
    """Megnyitja a listaoldalt (alapból legújabb elöl), és a legfelső sor
    azonosító linkjéből ("td.left.bold a") kiolvassa az (év, sorszám) párt.

    A táblázatot a "table-softservice" osztályra szűkítve keresi (DevTools-szal
    megerősítve az ordersheets oldalon: <table class="table table-hover
    table-light table-softservice">) - egy sima "table tbody tr" ugyanis
    könnyen egy MÁSIK táblázat első sorát találja el (pl. a fejléc/menü
    valamelyik widgetjében), ami nem tartalmaz "td.left.bold a"-t, és emiatt
    végtelen várakozásba/időtúllépésbe fut.
    """
    await page.goto(url, wait_until="networkidle")
    await page.wait_for_timeout(1000)

    table = page.locator("table.table-softservice")
    if await table.count() == 0:
        table = page.locator("table")  # fallback, ha az osztálynév mégsem egyezik mindkét oldalon

    first_link = table.first.locator("tbody tr").first.locator("td.left.bold a").first
    try:
        text = (await first_link.inner_text(timeout=10000)).strip()
    except Exception:
        current_url = page.url
        try:
            body_snippet = (await page.locator("body").inner_text())[:300]
        except Exception:
            body_snippet = "(nem sikerült kiolvasni)"
        raise ValueError(
            f"Nem található azonosító link ({url}). Jelenlegi URL: {current_url}. "
            f"Oldal eleje: {body_snippet!r}"
        )

    match = pattern.search(text)
    if not match:
        raise ValueError(f"Nem található azonosító mintázat az első sorban ({url}): {text!r}")
    return int(match.group(1)), int(match.group(2))


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

        bid_year, bid_count = await _first_row_number(page, ARAJANLATOK_URL, BID_PATTERN)
        order_year, order_count = await _first_row_number(page, MEGRENDELOLAPOK_URL, ORDER_PATTERN)

        await browser.close()

        return {
            "ajanlatok_db": bid_count if bid_year == current_year else 0,
            "megrendelesek_db": order_count if order_year == current_year else 0,
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


def register_innonest_szamlalo_routes(app):
    """A meglévő server.py modulregisztrációs mintája szerint hívandó
    (lásd register_arajanlat_routes, register_pipedrive_routes stb.)."""

    @app.route("/innonest-counters", methods=["GET"])
    def innonest_counters_route():
        data = innonest_counters()
        status = 500 if data.get("error") else 200
        return data, status
