"""
megrendeles_figyelő.py – Innonest megrendelőlap figyelő
========================================================
5 percenként ellenőrzi az Innonest megrendelőlapjait.
Ha új "Megrendelt" státuszú tétel jelenik meg:
  - elküldi az adatokat a Google Apps Script Web App-nak
  - az Apps Script átnevezi a sheetet ("- MEGRENDELVE")
  - beírja az adatokat a QUiCK API sheetbe
"""

import os
import json
import re
import time
import logging
import threading

import requests
from playwright.async_api import async_playwright

from innonest_core import (
    run_in_loop, login, load_session, make_browser_args
)

log = logging.getLogger(__name__)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
WEBAPP_SECRET  = os.environ.get("WEBAPP_SECRET", "")
WEBAPP_URL     = os.environ.get(
    "WEBAPP_URL",
    "https://script.google.com/macros/s/AKfycbyy1PQmHyBSlnWpXQR9bygVfFV_g2gJI9_7UjDI5zHm2xXElIX1DvsszM_UJu8l7too/exec"
)
PROCESSED_FILE = "/tmp/feldolgozott_megrendelesek.json"
CHECK_INTERVAL = 300  # 5 perc


# ── Feldolgozott BID-ek tárolása ──────────────────────────────────────────────

def load_processed() -> set:
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_processed(processed: set):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(processed), f)


# ── Megrendelőlapok lekérése ──────────────────────────────────────────────────

async def get_megrendelt_tetelek(page) -> list:
    """
    Megnyitja a Megrendelőlapok listát és visszaadja
    a Megrendelt státuszú tételek adatait.
    """
    log.info("Megrendelőlapok ellenőrzése...")
    await page.goto("https://app.innonest.hu/ordersheets", wait_until="networkidle")
    await page.wait_for_timeout(2000)

    sorok_raw = await page.evaluate(
        "() => { "
        "  var eredmeny = []; "
        "  var sorok = document.querySelectorAll('tr, .list-item'); "
        "  sorok.forEach(function(sor) { "
        "    var szoveg = sor.innerText || ''; "
        "    var rowId = sor.getAttribute('data-id') || ''; "
        "    var linkek = []; "
        "    var as = sor.querySelectorAll('a[href]'); "
        "    for (var i=0; i<as.length; i++) { "
        "      var href = as[i].getAttribute('href') || ''; "
        "      if (href.indexOf('pdf') === -1 && href.indexOf('print') === -1) { "
        "        linkek.push(href); "
        "      } "
        "    } "
        "    eredmeny.push({szoveg: szoveg, row_id: rowId, link: linkek[0] || ''}); "
        "  }); "
        "  return eredmeny; "
        "}"
    )

    tetelek = []
    seen_bid = set()

    for sor in sorok_raw:
        szoveg = sor.get("szoveg", "")
        row_id = sor.get("row_id", "")
        link   = sor.get("link", "")

        bid_match = re.search(r"BID-[0-9]{4}-[0-9]+", szoveg)
        has_bid = bid_match is not None

        if has_bid:
            bid = bid_match.group(0)
        else:
            sorszam_match = re.search(r"[0-9]{4}-[0-9]+", szoveg)
            bid = "SORSZAM-" + sorszam_match.group(0) if sorszam_match else ""
            if not bid:
                continue

        if "megrendelt" not in szoveg.lower():
            continue

        if bid in seen_bid:
            continue
        seen_bid.add(bid)

        sorok_lista = [s.strip() for s in szoveg.splitlines() if s.strip()]

        ertelmes_sorok = []
        for s in sorok_lista:
            if re.match(r"^\d{4}-\d{2}-\d{2}", s): continue
            if re.match(r"^\d{4}-\d+$", s): continue
            if re.search(r"HUF|EUR|USD|GBP|CHF", s): continue
            if re.search(r"megrendelt|piszkozat|elküldve", s, re.IGNORECASE): continue
            if re.match(r"^[\d\s\.,]+$", s): continue
            if len(s) <= 5 and s.isupper(): continue
            ertelmes_sorok.append(s)

        targya = ertelmes_sorok[0] if len(ertelmes_sorok) >= 1 else ""
        cegnev = ertelmes_sorok[1] if len(ertelmes_sorok) >= 2 else ""

        penznem = "HUF"
        netto = ""
        arfolyam = "1"
        osszes_osszeg = []
        for sor_r in szoveg.splitlines():
            sor_r = sor_r.strip()
            penz_m = re.search(r"([0-9][0-9 ]{3,}[0-9])\s*(HUF|EUR|USD|GBP|CHF)", sor_r)
            if penz_m:
                szam = penz_m.group(1).replace(" ", "")
                if len(szam) >= 4:
                    osszes_osszeg.append((int(szam), penz_m.group(2)))

        if osszes_osszeg:
            netto = str(osszes_osszeg[0][0])
            penznem = osszes_osszeg[0][1]
            if penznem == "HUF":
                arfolyam = "1"
            else:
                huf_osszeg = next((s for s, p in osszes_osszeg if p == "HUF"), None)
                if huf_osszeg and osszes_osszeg[0][0] > 0:
                    arfolyam = str(round(huf_osszeg / osszes_osszeg[0][0]))
                else:
                    arfolyam = ""

        if not row_id:
            row_id = bid

        log.info(f"  → {bid}: cég='{cegnev}', tárgy='{targya[:40]}', "
                 f"pénznem={penznem}, nettó={netto}, árfolyam={arfolyam}")

        tetelek.append({
            "row_id": row_id, "bid": bid, "has_bid": has_bid,
            "cegnev": cegnev, "targya": targya,
            "penznem": penznem, "netto": netto, "arfolyam": arfolyam,
            "link": link,
        })

    log.info(f"Talált Megrendelt tételek: {len(tetelek)}")
    return tetelek


# ── Fő ellenőrző funkció ──────────────────────────────────────────────────────

async def check_megrendelesek():
    """Bejelentkezik, lekéri a megrendelőlapokat, feldolgozza az újakat."""
    processed = load_processed()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=make_browser_args()
        )
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        await page.goto("https://app.innonest.hu/ordersheets", wait_until="networkidle")
        await page.wait_for_timeout(1500)

        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu/ordersheets", wait_until="networkidle")
            await page.wait_for_timeout(1000)

        tetelek = await get_megrendelt_tetelek(page)

        for tetel in tetelek:
            row_id = tetel["row_id"]
            bid    = tetel["bid"]

            if row_id in processed:
                log.info(f"{bid} már feldolgozva – kihagyom.")
                continue

            log.info(f"Új megrendelés feldolgozása: {bid} (BID: {tetel.get('has_bid')})")

            try:
                response = requests.post(
                    WEBAPP_URL,
                    json={
                        "secret":   WEBAPP_SECRET,
                        "bid":      bid,
                        "has_bid":  tetel.get("has_bid", False),
                        "cegnev":   tetel.get("cegnev", ""),
                        "targya":   tetel.get("targya", ""),
                        "penznem":  tetel.get("penznem", "HUF"),
                        "netto":    tetel.get("netto", ""),
                        "arfolyam": tetel.get("arfolyam", "1"),
                    },
                    timeout=30
                )
                result = response.json()
                log.info(f"Web App válasz ({bid}): {result}")

                if result.get("success"):
                    log.info(f"✅ {bid} sikeresen feldolgozva")
                    processed.add(row_id)
                    save_processed(processed)
                else:
                    log.warning(f"⚠️ {bid}: {result.get('error')}")

            except Exception as e:
                log.error(f"Web App hívás hiba ({bid}): {e}")

        await browser.close()


# ── Háttérszál indítása ───────────────────────────────────────────────────────

def megrendeles_figyelő():
    """Háttérszál: 5 percenként ellenőrzi az Innonest megrendelőlapokat."""
    log.info("Megrendelés figyelő elindult.")
    while True:
        try:
            run_in_loop(check_megrendelesek())
        except Exception as e:
            log.error(f"Figyelő hiba: {e}")
        log.info(f"Következő ellenőrzés {CHECK_INTERVAL // 60} perc múlva...")
        time.sleep(CHECK_INTERVAL)


def start_figyelő():
    """Elindítja a figyelőt egy daemon szálban. Hívd meg a server.py-ból."""
    threading.Thread(target=megrendeles_figyelő, daemon=True).start()
