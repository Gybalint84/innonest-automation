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
        # ── Template sor sorrend kompenzálás ────────────────────────────────────
        # Az Innonest "Új árajánlat" formja N db üres template sort tölt be alapból.
        # Ezek a sorok (data-id="0") mentéskor MINDIG a lista VÉGÉRE kerülnek.
        #
        # Megoldás: ne töröljük a template sorokat (a React visszaállítaná őket),
        # hanem töltjük ki őket az UTOLSÓ N tétellel. Így:
        #   - template sorok → utolsó N tétel → végére kerülnek → HELYES pozíció
        #   - új sorok (gombbal hozzáadott) → első (len-N) tétel → helyes sorrend
        #
        # Pl. 6 tétel, 2 template sor:
        #   template[0] ← items[4], template[1] ← items[5]  (végére kerülnek)
        #   új sorok ← items[0], items[1], items[2], items[3]  (helyes sorrend)
        #   → végső Innonest sorrend: 0,1,2,3,4,5  ✅

        n_template = await page.evaluate(
            "() => { var inp = document.querySelectorAll('input'); var n = 0; inp.forEach(function(el){ if(el.placeholder === 'Tétel megnevezése') n++; }); return n; }"
        )
        log.info(f"Kezdeti template sorok: {n_template} db")

        n = len(items)
        n_tmpl = min(n_template, n)

        if n_tmpl > 0:
            # Utolsó n_tmpl tétel → template sorokba (ezek kerülnek a végére)
            # Első (n - n_tmpl) tétel → új sorokba (helyes sorrendben maradnak)
            items_form = list(items[n - n_tmpl:]) + list(items[:n - n_tmpl])
            log.info(f"Sorrend kompenzálva: utolsó {n_tmpl} tétel a template sorokba kerül")
        else:
            items_form = list(items)

        log.info(f"{n} tétel feltöltése...")
        for i, item in enumerate(items_form):
            if i >= n_tmpl:
                # Template sorok után: "Új tétel hozzáadása" gombra kattintunk
                uj = page.locator('button:has-text("Új tétel hozzáadása")').first
                await uj.scroll_into_view_if_needed()
                await uj.click()
                await page.wait_for_timeout(800)

            await fill_tetel(
                page, i,   # index i: template sorok 0..n_tmpl-1, új sorok n_tmpl..
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
