"""
Innonest_alvallalkozo_beszermegrend.py – Innonest beszerzési megrendelőlap feltöltő
====================================================================================
Playwright-tal létrehoz egy "Beszerzési megrendelőlap"-ot az Innonestben az
alvállalkozói megrendeléshez. A /create-alv-megrendeles Flask végpontot regisztrálja.
A kalkulátor (Alv oldal → "Megrendelés" szekció) hívja.

Folyamat (a megadott lépések szerint):
  1. https://app.innonest.hu/acquisition  → "Új beszerzési megrendelőlap" gomb
     (fallback: közvetlenül https://app.innonest.hu/acquisition/add)
  2. Ügyféladatok kitöltése az ADOTT ALVÁLLALKOZÓ adataival — mint az árajánlatnál.
     Ha egy mező üres, "1"-et írunk (üresen nem menthető az Innonestben).
  3. "Tárgy, megnevezés" = projekt cégnév + " -" + BID szám.
  4. Tételek feltöltése ugyanúgy, mint az árajánlatnál (fill_tetel).
     A feladathoz tartozó anyagok a tétel megnevezése alatti szövegboxba (megjegyzés).
  5. Mentés — mint az árajánlatnál.

A kitöltő/mentő segédfüggvények az innonest_core-ból jönnek (UGYANAZOK, mint az
árajánlatnál: fill_nev, fill_tetel, js_fill, login, load_session, ...), így a
viselkedés konzisztens az árajánlat-feltöltővel.

ÉLES ELLENŐRZÉS: az /acquisition/add oldal mezőválasztóit érdemes élesben
visszaigazolni. Ahol eltérhet az árajánlattól (Tárgy mező, "Új tétel" gomb,
Mentés gomb), ott több fallback-szelektort próbálunk, és a logba írjuk a talált
elemeket. Ha valamelyik lépés nem talál elemet, a log alapján gyorsan igazítható.
"""

import os
import logging
import traceback

from flask import request, jsonify
from playwright.async_api import async_playwright

from innonest_core import (
    run_in_loop, login, load_session, make_browser_args,
    js_fill, fill_nev, fill_tetel,
)

log = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "titkos-kulcs")

ACQUISITION_URL     = "https://app.innonest.hu/acquisition"
ACQUISITION_ADD_URL = "https://app.innonest.hu/acquisition/add"


# ══════════════════════════════════════════════════════════════════════════════
# SEGÉDFÜGGVÉNYEK
# ══════════════════════════════════════════════════════════════════════════════

def _v(x, default="1"):
    """Mezőérték: ha üres/None → a default ('1'), különben a trimmelt érték.
    (Az Innonest nem enged üres kötelező mezővel menteni.)"""
    s = ("" if x is None else str(x)).strip()
    return s if s else default


def _anyag_megjegyzes(anyagok: list) -> str:
    """A tétel anyaglistája a megjegyzés-szövegboxba: soronként 'Név – menny egység'."""
    sorok = []
    for a in (anyagok or []):
        nev = str(a.get("name", "")).strip()
        if not nev:
            continue
        menny = " ".join(
            x for x in [str(a.get("quantity", "")).strip(), str(a.get("unit", "")).strip()] if x
        )
        sor = f"{nev} – {menny}".strip()
        sorok.append(sor.rstrip(" –"))
    return "\n".join(sorok)


async def _fill_targy(page, ertek: str):
    """A 'Tárgy, megnevezés' mező kitöltése — több lehetséges szelektorral."""
    szelektorok = [
        'input[placeholder="Tárgy, megnevezés"]',
        'input[placeholder*="Tárgy"]',
        'input[placeholder*="megnevez"]',
        'textarea[placeholder*="Tárgy"]',
        'input[name*="targy"]',
        'input[name*="subject"]',
    ]
    for sel in szelektorok:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await js_fill(page, sel, ertek, "Tárgy, megnevezés")
                return True
        except Exception:
            continue
    log.warning("[BESZERMEGREND] Nem találtam a 'Tárgy, megnevezés' mezőt egyik szelektorral sem.")
    return False


async def _open_add_page(page):
    """A beszerzési megrendelőlap 'add' oldalának megnyitása:
    lista → 'Új beszerzési megrendelőlap' gomb; fallback: közvetlen URL."""
    await page.goto(ACQUISITION_URL, wait_until="networkidle")
    await page.wait_for_timeout(600)
    btn = page.locator(
        'a:has-text("Új beszerzési megrendelőlap"), button:has-text("Új beszerzési megrendelőlap")'
    ).first
    try:
        if await btn.count() > 0:
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(800)
            log.info("[BESZERMEGREND] 'Új beszerzési megrendelőlap' megnyitva (gomb).")
            return
    except Exception as e:
        log.warning(f"[BESZERMEGREND] Gombra kattintás hiba: {e} — közvetlen URL-lel próbálom.")
    await page.goto(ACQUISITION_ADD_URL, wait_until="networkidle")
    await page.wait_for_timeout(800)
    log.info("[BESZERMEGREND] 'add' oldal megnyitva (közvetlen URL).")


# ══════════════════════════════════════════════════════════════════════════════
# FŐ PLAYWRIGHT AUTOMATIZÁCIÓ
# ══════════════════════════════════════════════════════════════════════════════

async def run_automation(payload: dict):
    contractor  = payload.get("contractor", {}) or {}
    projekt_ceg = str(payload.get("projektCeg", "") or "").strip()
    bid         = str(payload.get("requestId", "") or "").strip()
    items       = payload.get("items", []) or []

    if not items:
        raise Exception("Nincs tétel a megrendelésben.")

    # Tárgy: projekt cégnév + " -" + BID (a megadott formátum szerint)
    if projekt_ceg or bid:
        targya = f"{projekt_ceg} -{bid}".strip()
    else:
        targya = "Alvállalkozói megrendelés"

    log.info(f"[BESZERMEGREND] Indul — alvállalkozó: {contractor.get('nev','')} | "
             f"tárgy: {targya} | {len(items)} tétel")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page    = await context.new_page()

        # Bejelentkezés (mint az árajánlatnál)
        await load_session(context)
        await page.goto("https://app.innonest.hu", wait_until="networkidle")
        await page.wait_for_timeout(500)
        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu", wait_until="networkidle")
            await page.wait_for_timeout(500)
            if "login" in page.url:
                raise Exception("Bejelentkezés sikertelen!")

        # 1. add oldal megnyitása
        await _open_add_page(page)

        # 2. Ügyféladatok = az ADOTT ALVÁLLALKOZÓ adatai (üres mezőbe "1")
        await fill_nev(page, _v(contractor.get("nev"), "1"))
        await js_fill(page, 'input[placeholder="Adószám"]',            _v(contractor.get("adoszam")),      "Adószám")
        await js_fill(page, 'input[placeholder="Irányítószám"]',       _v(contractor.get("iranyitoszam")), "Irányítószám")
        await js_fill(page, 'input[placeholder="Település"]',          _v(contractor.get("telepules")),    "Település")
        await js_fill(page, 'input[placeholder="Utca"]',               _v(contractor.get("utca")),         "Utca")
        await js_fill(page, 'input[placeholder="Kapcsolattartó neve"]', _v(contractor.get("kapcsolat")),   "Kapcsolattartó")

        # 3. Tárgy, megnevezés
        await _fill_targy(page, targya)

        # 4. Tételek — mint az árajánlatnál: a 0. template sort kihagyjuk,
        #    az 1. sorba az első tétel, a többihez "Új tétel hozzáadása".
        first = items[0]
        await fill_tetel(
            page, 1,
            megnevezes=first.get("megnevezes", ""),
            mennyiseg =first.get("mennyiseg", ""),
            egyseg    =first.get("egyseg", ""),
            egysegar  =first.get("egysegar", ""),
            megjegyzes=_anyag_megjegyzes(first.get("anyagok")),
        )
        for i, item in enumerate(items[1:], start=1):
            uj = page.locator('button:has-text("Új tétel hozzáadása")').first
            await uj.scroll_into_view_if_needed()
            await uj.click()
            await page.wait_for_timeout(800)
            await fill_tetel(
                page, i + 1,
                megnevezes=item.get("megnevezes", ""),
                mennyiseg =item.get("mennyiseg", ""),
                egyseg    =item.get("egyseg", ""),
                egysegar  =item.get("egysegar", ""),
                megjegyzes=_anyag_megjegyzes(item.get("anyagok")),
            )
        await page.wait_for_timeout(300)

        # 5. Mentés — mint az árajánlatnál
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
            raise Exception("Nem találtam Mentés gombot a beszerzési megrendelőlapon!")

        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)
        result_url = page.url
        log.info(f"[BESZERMEGREND] Mentés utáni URL: {result_url}")

        await browser.close()

    return {"ok": True, "url": result_url, "targy": targya}


# ══════════════════════════════════════════════════════════════════════════════
# FLASK VÉGPONT REGISZTRÁCIÓ
# ══════════════════════════════════════════════════════════════════════════════

def register_alv_megrendeles_routes(app):
    """Hívd meg a server.py-ból: register_alv_megrendeles_routes(app)"""

    @app.route("/create-alv-megrendeles", methods=["POST"])
    def create_alv_megrendeles():
        if request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        payload = request.get_json(silent=True)
        if not payload:
            return jsonify({"ok": False, "hiba": "Hiányzó JSON"}), 400

        try:
            result = run_in_loop(run_automation(payload))
            return jsonify(result)
        except Exception as e:
            log.error(f"❌ /create-alv-megrendeles hiba: {e}")
            log.error(traceback.format_exc())
            return jsonify({"ok": False, "hiba": str(e)}), 500

    log.info("[BESZERMEGREND] Végpont regisztrálva: /create-alv-megrendeles")
