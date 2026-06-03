"""
Innonest Automatizáció – Szerver
=================================
Két funkció:
  1. POST /create-arajanlat  – árajánlat feltöltés (korábbi funkció)
  2. Háttérszál              – 5 percenként figyeli az Innonest megrendelőlapokat,
                               és ha új "Megrendelt" státuszú tétel jelenik meg,
                               elküldi a BID számot a Google Apps Script Web App-nak,
                               ami átnevezi a sheetet: hozzáfűzi a " - MEGRENDELVE" utótagot.

Környezeti változók (Railway → Variables):
  INNONEST_EMAIL    – Innonest bejelentkezési email
  INNONEST_PASSWORD – Innonest jelszó
  API_KEY           – Titkos kulcs az Apps Script gombhoz
  WEBAPP_SECRET     – Ugyanaz a titkos kulcs, amit az Apps Script Web App-ban beállítottál
  WEBAPP_URL        – A Google Apps Script Web App URL-je
"""

import os
import json
import asyncio
import threading
import time
import re
import logging
import requests
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
INNONEST_EMAIL    = os.environ.get("INNONEST_EMAIL", "")
INNONEST_PASSWORD = os.environ.get("INNONEST_PASSWORD", "")
API_KEY           = os.environ.get("API_KEY", "")
WEBAPP_SECRET     = os.environ.get("WEBAPP_SECRET", "")
WEBAPP_URL        = os.environ.get("WEBAPP_URL", "https://script.google.com/macros/s/AKfycbyy1PQmHyBSlnWpXQR9bygVfFV_g2gJI9_7UjDI5zHm2xXElIX1DvsszM_UJu8l7too/exec")
SESSION_FILE      = "/tmp/innonest_session.json"
PROCESSED_FILE    = "/tmp/feldolgozott_megrendelesek.json"
CHECK_INTERVAL    = 300   # 5 perc

# ── Async event loop ──────────────────────────────────────────────────────────
_loop = asyncio.new_event_loop()

def run_in_loop(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=300)

def start_loop():
    _loop.run_forever()

threading.Thread(target=start_loop, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE APPS SCRIPT WEB APP HÍVÁSA
# ══════════════════════════════════════════════════════════════════════════════

def rename_sheet_via_webapp(bid: str) -> dict:
    """
    Elküldi a BID számot a Google Apps Script Web App-nak,
    ami átnevezi a megfelelő sheetet.
    """
    try:
        response = requests.post(
            WEBAPP_URL,
            json={"secret": WEBAPP_SECRET, "bid": bid},
            timeout=30
        )
        result = response.json()
        log.info(f"Web App válasz ({bid}): {result}")
        return result
    except Exception as e:
        log.error(f"Web App hívás hiba ({bid}): {e}")
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# FELDOLGOZOTT MEGRENDELÉSEK
# ══════════════════════════════════════════════════════════════════════════════

def load_processed() -> set:
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_processed(processed: set):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(processed), f)


# ══════════════════════════════════════════════════════════════════════════════
# INNONEST – bejelentkezés + megrendelőlap figyelés
# ══════════════════════════════════════════════════════════════════════════════

async def innonest_login(page):
    log.info("Innonest bejelentkezés...")
    await page.goto("https://app.innonest.hu/login", wait_until="networkidle")
    await page.fill('input[type="email"], input[name="email"]', INNONEST_EMAIL)
    await page.fill('input[type="password"], input[name="password"]', INNONEST_PASSWORD)
    await page.click('button[type="submit"]')
    await page.wait_for_load_state("networkidle")
    log.info("Bejelentkezés kész.")


async def get_megrendelt_tetelek(page) -> list[dict]:
    """
    Megnyitja a Megrendelőlapok listát (Megrendelt szűrővel),
    és visszaadja az összes 'Megrendelt' státuszú tétel BID számát.
    """
    log.info("Megrendelőlapok ellenőrzése...")
    # Megrendelt státuszú szűrő
    await page.goto("https://app.innonest.hu/ordersheets?status=ordered", wait_until="networkidle")
    await page.wait_for_timeout(2000)

    tetelek = []
    sor_elemek = await page.query_selector_all("tr, .list-row, [data-id]")

    for sor in sor_elemek:
        try:
            sor_szoveg = await sor.inner_text()

            # BID szám kinyerése
            bid_match = re.search(r"BID-\d{4}-\d+", sor_szoveg)
            if not bid_match:
                continue
            bid = bid_match.group(0)

            # Állapot ellenőrzése
            if "megrendelt" not in sor_szoveg.lower() and "Megrendelt" not in sor_szoveg:
                continue

            row_id = await sor.get_attribute("data-id") or bid
            tetelek.append({"row_id": row_id, "bid": bid})

        except Exception:
            continue

    # Duplikátumok eltávolítása BID alapján
    seen = set()
    egyedi = []
    for t in tetelek:
        if t["bid"] not in seen:
            seen.add(t["bid"])
            egyedi.append(t)

    log.info(f"Talált Megrendelt tételek: {len(egyedi)}")
    return egyedi


async def check_megrendelesek():
    processed = load_processed()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()

        # Session visszatöltése
        if os.path.exists(SESSION_FILE):
            try:
                await context.add_cookies(json.loads(open(SESSION_FILE).read()))
            except Exception:
                pass

        page = await context.new_page()

        # Bejelentkezés ellenőrzése
        await page.goto("https://app.innonest.hu/ordersheets", wait_until="networkidle")
        if "login" in page.url.lower():
            await innonest_login(page)
            cookies = await context.cookies()
            with open(SESSION_FILE, "w") as f:
                json.dump(cookies, f)

        tetelek = await get_megrendelt_tetelek(page)

        for tetel in tetelek:
            row_id = tetel["row_id"]
            bid    = tetel["bid"]

            if row_id in processed:
                log.info(f"{bid} már feldolgozva – kihagyom.")
                continue

            log.info(f"Új megrendelés: {bid} – sheet átnevezése...")
            result = rename_sheet_via_webapp(bid)

            if result.get("success"):
                log.info(f"✅ {result.get('message')}")
                processed.add(row_id)
                save_processed(processed)
            else:
                log.warning(f"⚠️ {bid}: {result.get('message') or result.get('error')}")

        await browser.close()


# ══════════════════════════════════════════════════════════════════════════════
# HÁTTÉRSZÁL
# ══════════════════════════════════════════════════════════════════════════════

def megrendeles_figyelő():
    log.info("Megrendelés figyelő elindult.")
    while True:
        try:
            run_in_loop(check_megrendelesek())
        except Exception as e:
            log.error(f"Figyelő hiba: {e}")
        log.info(f"Következő ellenőrzés {CHECK_INTERVAL // 60} perc múlva...")
        time.sleep(CHECK_INTERVAL)

threading.Thread(target=megrendeles_figyelő, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# FLASK VÉGPONTOK
# ══════════════════════════════════════════════════════════════════════════════

def check_api_key():
    return request.headers.get("X-API-Key") == API_KEY

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/check-now", methods=["POST"])
def check_now():
    """Azonnali ellenőrzés kiváltása."""
    if not check_api_key():
        return jsonify({"error": "Érvénytelen API kulcs"}), 401
    try:
        run_in_loop(check_megrendelesek())
        return jsonify({"status": "ok", "message": "Ellenőrzés lefutott."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/create-arajanlat", methods=["POST"])
def create_arajanlat_endpoint():
    """Korábbi árajánlat feltöltő végpont."""
    if not check_api_key():
        return jsonify({"error": "Érvénytelen API kulcs"}), 401
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
