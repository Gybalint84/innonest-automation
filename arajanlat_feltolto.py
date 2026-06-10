"""
arajanlat_feltolto.py – Innonest árajánlat feltöltő
====================================================
Playwright-tal feltölti az árajánlatot az Innonestbe.
A /create-arajanlat Flask végpontot regisztrálja.
"""

import os
import base64
import logging

from flask import request, jsonify
from playwright.async_api import async_playwright

from innonest_core import (
    run_in_loop, login, load_session, make_browser_args,
    js_fill, js_fill_nth, fill_nev, fill_tetel, upload_csatolmany
)

log = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "titkos-kulcs")


# ══════════════════════════════════════════════════════════════════════════════
# FŐ AUTOMATIZÁCIÓ
# ══════════════════════════════════════════════════════════════════════════════

async def run_automation(payload: dict):
    ugyfel = payload.get("ugyfel", {})
    targya = payload.get("arajanlat_targya", "AI-ÁTNÉZÉSRE")
    items  = payload.get("items", [])
    screenshots = {}

    log.info("Árajánlat: böngésző indítás...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=make_browser_args()
        )
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page    = await context.new_page()

        await load_session(context)
        await page.goto("https://app.innonest.hu", wait_until="networkidle")
        await page.wait_for_timeout(1500)

        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu", wait_until="networkidle")
            await page.wait_for_timeout(1000)
            if "login" in page.url:
                raise Exception("Bejelentkezés sikertelen!")

        await page.click("text=Munkavégzés")
        await page.wait_for_timeout(500)
        await page.click("text=Árajánlatok")
        await page.wait_for_load_state("networkidle")
        await page.click("text=Új árajánlat")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)

        screenshots["1_form_megnyilt"] = base64.b64encode(
            await page.screenshot(full_page=True)).decode()

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
        log.info(f"{len(items)} tétel feltöltése...")
        for i, item in enumerate(items):
            if i > 0:
                uj = page.locator('button:has-text("Új tétel hozzáadása")').first
                await uj.scroll_into_view_if_needed()
                await uj.click()
                await page.wait_for_timeout(800)

            await fill_tetel(
                page, i,
                megnevezes = item["megnevezes"],
                mennyiseg  = item["mennyiseg"],
                egyseg     = item["egyseg"],
                egysegar   = item["egysegar"],
                megjegyzes = item.get("megjegyzes", ""),
            )

        await page.wait_for_timeout(500)
        screenshots["2_kitoltes_utan"] = base64.b64encode(
            await page.screenshot(full_page=True)).decode()

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
        await page.wait_for_timeout(2000)
        result_url = page.url

        screenshots["3_mentes_utan"] = base64.b64encode(
            await page.screenshot(full_page=True)).decode()

        bid_szam = await page.evaluate("""
            () => {
                const text = document.body ? document.body.innerText : "";
                const m = text.match(/BID-[0-9]{4}-[0-9]+/);
                return m ? m[0] : null;
            }
        """)

        if bid_szam:
            log.info(f"BID szám: {bid_szam}")

        # Csatolmány
        csatolmany = payload.get("csatolmany")
        if csatolmany and csatolmany.get("adat"):
            await upload_csatolmany(page, csatolmany, bid_szam)

        await browser.close()

    return {
        "ok":          True,
        "url":         result_url,
        "bid_szam":    bid_szam,
        "screenshots": screenshots,
    }


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
        try:
            result = run_in_loop(run_automation(payload))
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    log.info("[ARAJANLAT] Végpont regisztrálva: /create-arajanlat")
