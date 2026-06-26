"""
arajanlat_feltolto.py – Innonest árajánlat feltöltő
====================================================
Playwright-tal feltölti az árajánlatot az Innonestbe.
A /create-arajanlat Flask végpontot regisztrálja.

CALLBACK ARCHITEKTÚRA:
  1. Az Apps Script elküldi a kérést → azonnal 200 OK visszatér (~2 mp)
  2. A Playwright automatizáció a háttérben fut
  3. Ha kész a BID szám, Railway visszahívja a webapp_script.js Web App-ot
  4. A webapp_script.js átnevezi a fájlt [BID-XXXX-NNN]-re
  → Nincs többé timeout probléma
"""

import os
import re
import base64
import logging
import traceback
import asyncio

import requests as req_lib
from flask import request, jsonify
from playwright.async_api import async_playwright

from innonest_core import (
    run_in_loop, login, load_session, make_browser_args,
    js_fill, js_fill_nth, fill_nev, fill_tetel, upload_csatolmany
)

log = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "titkos-kulcs")


# ══════════════════════════════════════════════════════════════════════════════
# MEGJEGYZÉS BEÍRÁSA A BID SORBA
# ══════════════════════════════════════════════════════════════════════════════

async def _write_megjegyzes(page, bid_szam: str, szoveg: str):
    """
    Megnyitja a BID sorának Megjegyzések paneljét az Innonest /bids listán,
    beírja a szöveget (projekt URL), majd rákattint a Mehet! gombra.
    """
    await page.goto("https://app.innonest.hu/bids", wait_until="networkidle")
    await page.wait_for_timeout(1500)

    # BID sor megkeresése
    bid_cell = page.locator(f"text={bid_szam}").first
    if not await bid_cell.count():
        log.warning(f"[MEGJEGYZES] Nem találtam a BID sort: {bid_szam}")
        return

    row = bid_cell.locator("xpath=ancestor::tr").first

    # Megjegyzés ikon keresése a sorban (speech bubble / comment icon)
    comment_btn = None
    for sel in [
        'button[title*="egjegyz"]', 'a[title*="egjegyz"]',
        'button[title*="omment"]', 'a[title*="omment"]',
        '[class*="comment"]', '[class*="speech"]', '[class*="bubble"]',
    ]:
        loc = row.locator(sel).first
        if await loc.count() > 0:
            comment_btn = loc
            log.info(f"[MEGJEGYZES] Ikon megtalálva: {sel}")
            break

    if comment_btn is None:
        # Fallback: a 2. ikon a sorban (a sorrend: csatolmány, megjegyzés, zászló)
        btns = row.locator('button')
        cnt = await btns.count()
        log.info(f"[MEGJEGYZES] Fallback: {cnt} gomb a sorban")
        if cnt >= 2:
            comment_btn = btns.nth(1)

    if comment_btn is None:
        log.warning("[MEGJEGYZES] Nem találtam a megjegyzés ikont")
        return

    await comment_btn.scroll_into_view_if_needed()
    await comment_btn.click()
    await page.wait_for_timeout(1000)

    # Megjegyzés textarea kitöltése
    textarea = page.locator('textarea[placeholder="Megjegyzés"]').first
    if not await textarea.count():
        log.warning("[MEGJEGYZES] Nem találtam a megjegyzés textarea-t")
        return

    await textarea.fill(szoveg)
    await page.wait_for_timeout(300)

    # Mehet! gomb megnyomása
    mehet = page.locator('button:has-text("Mehet!")').first
    if await mehet.count() > 0:
        await mehet.click()
        await page.wait_for_timeout(800)
        log.info(f"[MEGJEGYZES] Projekt URL beírva ({bid_szam}): {szoveg}")
    else:
        log.warning("[MEGJEGYZES] Nem találtam a Mehet! gombot")


# ══════════════════════════════════════════════════════════════════════════════
# FŐ PLAYWRIGHT AUTOMATIZÁCIÓ
# ══════════════════════════════════════════════════════════════════════════════

async def run_automation(payload: dict):
    ugyfel = payload.get("ugyfel", {})
    targya = payload.get("arajanlat_targya", "AI-ÁTNÉZÉSRE")
    items  = payload.get("items", [])
    log.info("Árajánlat: böngésző indítás...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=make_browser_args()
        )
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page    = await context.new_page()

        await load_session(context)
        await page.goto("https://app.innonest.hu", wait_until="networkidle")
        await page.wait_for_timeout(500)

        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu", wait_until="networkidle")
            await page.wait_for_timeout(500)
            if "login" in page.url:
                raise Exception("Bejelentkezés sikertelen!")

        await page.click("text=Munkavégzés")
        await page.wait_for_timeout(500)
        await page.click("text=Árajánlatok")
        await page.wait_for_load_state("networkidle")
        await page.click("text=Új árajánlat")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(800)

        # Screenshot eltávolítva – callback módban a Railway azonnal visszatér,
        # a screenshotok sosem kerülnek felhasználásra, csak lassítanak (~2mp/db)

        # Ügyféladatok
        await fill_nev(page, ugyfel.get("nev", ""))
        if ugyfel.get("adoszam"):
            await js_fill(page, 'input[placeholder="Adószám"]', ugyfel["adoszam"], "Adószám")
        if ugyfel.get("iranyitoszam"):
            await js_fill(page, 'input[placeholder="Irányítószám"]', ugyfel["iranyitoszam"], "Irányítószám")
        if ugyfel.get("telepules"):
            await js_fill(page, 'input[placeholder="Település"]', ugyfel["telepules"], "Település")
        if ugyfel.get("utca"):
            await js_fill(page, 'input[placeholder="Utca"]', ugyfel["utca"], "Utca")
        if ugyfel.get("kapcsolattarto"):
            await js_fill(page, 'input[placeholder="Kapcsolattartó neve"]', ugyfel["kapcsolattarto"], "Kapcsolattartó")

        await js_fill(page, 'input[placeholder="Árajánlat tárgya"]', targya, "Árajánlat tárgya")

        # Tételek
        # ── Innonest template sor viselkedése (tesztek alapján feltérképezve) ────
        #
        # Az "Új árajánlat" form 2 template sorral nyílik meg:
        #   - Sor 0 (data-id=0): SOHA nem mentődik el → hagyjuk üresen
        #   - Sor 1 (data-id>0): mentődik, és a PDF ELEJÉRE kerül
        #
        # Stratégia:
        #   - Sor 0: nem töltjük ki (üresen marad, nem lesz az árajánlatban)
        #   - Sor 1: items[0]-t töltjük be → PDF első helye ✓
        #   - Új sorok (gomb): items[1..n-1] → PDF 2., 3., ... helye ✓
        #   → Végső sorrend: helyes ✅

        log.info(f"{len(items)} tétel feltöltése...")

        # 1. tétel: az 1-es indexű template sorba (sor 0-t kihagyjuk)
        await fill_tetel(
            page, 1,
            megnevezes = items[0]["megnevezes"],
            mennyiseg  = items[0]["mennyiseg"],
            egyseg     = items[0]["egyseg"],
            egysegar   = items[0]["egysegar"],
            megjegyzes = items[0].get("megjegyzes", ""),
        )

        # 2..n. tételek: minden tételnél új sort adunk hozzá
        for i, item in enumerate(items[1:], start=1):
            uj = page.locator('button:has-text("Új tétel hozzáadása")').first
            await uj.scroll_into_view_if_needed()
            await uj.click()
            await page.wait_for_timeout(800)

            await fill_tetel(
                page, i + 1,   # sor 0 kihagyva: items[1]→idx=2, items[2]→idx=3, stb.
                megnevezes = item["megnevezes"],
                mennyiseg  = item["mennyiseg"],
                egyseg     = item["egyseg"],
                egysegar   = item["egysegar"],
                megjegyzes = item.get("megjegyzes", ""),
            )

        await page.wait_for_timeout(300)

        # Mentés
        mentes_ok = False
        for sel in ['button:has-text("Mentés")', 'button[type="submit"]', '.btn-primary']:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.scroll_into_view_if_needed()
                    await loc.click()
                    mentes_ok = True
                    break
            except Exception:
                continue

        if not mentes_ok:
            raise Exception("Nem találtam Mentés gombot!")

        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)
        result_url = page.url
        log.info(f"Mentés utáni URL: {result_url}")

        # Debug: oldal tartalom logolása hogy lássuk mi jelent meg mentés után
        try:
            page_snippet = await page.evaluate(
                "() => document.body ? document.body.innerText.substring(0, 400) : '(üres)'"
            )
            log.info(f"Oldal tartalom mentés után: {page_snippet[:400]}")
        except Exception as e:
            log.warning(f"Oldal tartalom lekérés hiba: {e}")

        bid_szam = await page.evaluate("""
            () => {
                const text = document.body ? document.body.innerText : "";
                const m = text.match(/BID-[0-9]{4}-[0-9]+/);
                return m ? m[0] : null;
            }
        """)

        if bid_szam:
            log.info(f"BID szám: {bid_szam}")

        # Csatolmány feltöltés
        csatolmany = payload.get("csatolmany")
        if csatolmany and csatolmany.get("adat"):
            eredeti_nev = csatolmany.get("nev", "arajanlat.xlsx")
            biztonságos_nev = re.sub(r'[/\\:*?"<>|]', '_', eredeti_nev)
            if biztonságos_nev != eredeti_nev:
                log.info(f"Csatolmány fájlnév javítva: '{eredeti_nev}' → '{biztonságos_nev}'")
            csatolmany = dict(csatolmany)
            csatolmany["nev"] = biztonságos_nev
            await upload_csatolmany(page, csatolmany, bid_szam)

        # Projekt URL beírása a BID Megjegyzések mezőjébe
        projekt_url = payload.get("projekt_url", "")
        if bid_szam and projekt_url:
            try:
                await _write_megjegyzes(page, bid_szam, projekt_url)
            except Exception as e:
                log.warning(f"Megjegyzés írás hiba: {e}")

        await browser.close()

    return {
        "ok":       True,
        "url":      result_url,
        "bid_szam": bid_szam,
    }


# ══════════════════════════════════════════════════════════════════════════════
# HÁTTÉRFUTÁS CALLBACK-KEL
# ══════════════════════════════════════════════════════════════════════════════

async def run_automation_background(payload: dict):
    """
    Háttérben futtatja az automatizációt, majd a BID számot
    visszaküldi a webapp_script.js Web App-nak (callback).
    """
    callback_url    = payload.get("callback_url")
    callback_secret = payload.get("callback_secret")
    spreadsheet_id  = payload.get("spreadsheet_id")

    try:
        result   = await run_automation(payload)
        bid_szam = result.get("bid_szam")
        log.info(f"Háttér automatizáció kész. BID: {bid_szam}")

        if not bid_szam:
            log.error("❌ Nem sikerült BID számot kinyerni az Innonestből!")
            return

        if not callback_url:
            log.warning("⚠️  callback_url nincs megadva – fájl átnevezés kihagyva.")
            return

        # Callback hívás a webapp_script.js-nek
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(None, lambda: req_lib.post(
                callback_url,
                json={
                    "secret":         callback_secret,
                    "action":         "setBidSzam",
                    "spreadsheet_id": spreadsheet_id,
                    "bid_szam":       bid_szam,
                },
                timeout=30
            ))
            log.info(f"Callback válasz: {resp.status_code} – {resp.text[:300]}")
        except Exception as cb_err:
            log.error(f"❌ Callback hiba: {cb_err}")

    except Exception as e:
        log.error(f"❌ Háttér automatizáció hiba: {e}")
        log.error(traceback.format_exc())


# ══════════════════════════════════════════════════════════════════════════════
# FLASK VÉGPONT REGISZTRÁCIÓ
# ══════════════════════════════════════════════════════════════════════════════

def register_arajanlat_routes(app):
    """Hívd meg a server.py-ból: register_arajanlat_routes(app)"""

    @app.route("/create-arajanlat", methods=["POST"])
    def create_arajanlat():
        if request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401

        payload = request.get_json()
        if not payload:
            return jsonify({"error": "Hiányzó JSON"}), 400

        from innonest_core import _loop

        if payload.get("callback_url"):
            # ── ÚJ: callback mód ──────────────────────────────────────────
            # Azonnal visszatér, a Playwright a háttérben fut.
            # A BID számot a webapp_script.js callback kapja meg.
            asyncio.run_coroutine_threadsafe(
                run_automation_background(payload),
                _loop
            )
            log.info("Árajánlat háttérbe indítva (callback mód).")
            return jsonify({
                "ok":     True,
                "status": "processing",
                "message": "Árajánlat elkészítése folyamatban. BID callback-en érkezik."
            })
        else:
            # ── RÉGI: szinkron mód (visszafelé kompatibilis) ───────────────
            try:
                from innonest_core import run_in_loop
                result = run_in_loop(run_automation(payload))
                return jsonify(result)
            except Exception as e:
                log.error(f"❌ /create-arajanlat szinkron hiba: {e}")
                log.error(traceback.format_exc())
                return jsonify({"error": str(e)}), 500

    log.info("[ARAJANLAT] Végpont regisztrálva: /create-arajanlat")
