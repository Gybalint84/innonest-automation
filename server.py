"""
Innonest Automatizáció – Felhő Szerver
=======================================
Fogad egy POST kérést a Google Apps Script-től,
majd Playwright segítségével headless módban kitölti az Innonest árajánlat formot.

Környezeti változók (Railway / Render dashboard-on kell beállítani):
  INNONEST_EMAIL    – Innonest bejelentkezési email
  INNONEST_PASSWORD – Innonest jelszó
  API_KEY           – Titkos kulcs, amit az Apps Script is küld (bármit adhatsz meg)
"""

import os
import json
import asyncio
import threading
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright

app = Flask(__name__)

# ─── Konfiguráció (Railway env vars-ból) ───
INNONEST_EMAIL    = os.environ.get("INNONEST_EMAIL", "")
INNONEST_PASSWORD = os.environ.get("INNONEST_PASSWORD", "")
API_KEY           = os.environ.get("API_KEY", "titkos-kulcs")
SESSION_FILE      = "/tmp/innonest_session.json"

# ─── Playwright futtatás async loop-ban ───
_loop = asyncio.new_event_loop()

def run_in_loop(coro):
    """Szinkron Flask handlerből hív async Playwright kódot."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=300)

def start_loop():
    _loop.run_forever()

threading.Thread(target=start_loop, daemon=True).start()


# ═══════════════════════════════════════════
# INNONEST BEJELENTKEZÉS
# ═══════════════════════════════════════════

async def login(page):
    """Bejelentkezés és munkamenet-mentés."""
    print("🔑 Bejelentkezés az Innonestbe...")
    await page.goto("https://app.innonest.hu/login.html", wait_until="networkidle")
    await page.wait_for_timeout(2000)

    print(f"   Login oldal URL: {page.url}")

    # Screenshot debug célra
    await page.screenshot(path="/tmp/login_debug.png", full_page=True)

    # Email mező – több lehetséges selector
    email_selectors = [
        'input[type="email"]',
        'input[name="email"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="Email" i]',
        'input[id*="email" i]',
        'input[autocomplete="email"]',
        'input[autocomplete="username"]',
    ]
    email_filled = False
    for sel in email_selectors:
        try:
            loc = page.locator(sel).first
            count = await loc.count()
            if count > 0:
                await loc.fill(INNONEST_EMAIL, timeout=3000)
                print(f"   ✅ Email mező: {sel}")
                email_filled = True
                break
        except Exception:
            continue

    if not email_filled:
        # Utolsó próba: az első input mező
        try:
            await page.locator("input").first.fill(INNONEST_EMAIL, timeout=3000)
            print("   ✅ Email: első input mező")
            email_filled = True
        except Exception as e:
            raise Exception(f"Nem találtam email mezőt. URL: {page.url}. Hiba: {e}")

    # Jelszó mező
    pw_selectors = [
        'input[type="password"]',
        'input[name="password"]',
        'input[placeholder*="jelszó" i]',
        'input[placeholder*="password" i]',
        'input[id*="password" i]',
        'input[autocomplete="current-password"]',
    ]
    pw_filled = False
    for sel in pw_selectors:
        try:
            loc = page.locator(sel).first
            count = await loc.count()
            if count > 0:
                await loc.fill(INNONEST_PASSWORD, timeout=3000)
                print(f"   ✅ Jelszó mező: {sel}")
                pw_filled = True
                break
        except Exception:
            continue

    if not pw_filled:
        raise Exception("Nem találtam jelszó mezőt.")

    # Belépés gomb
    submit_selectors = [
        'button[type="submit"]',
        'button:has-text("Belépés")',
        'button:has-text("Login")',
        'button:has-text("Bejelentkezés")',
        'input[type="submit"]',
        'button:has-text("Sign in")',
    ]
    submitted = False
    for sel in submit_selectors:
        try:
            loc = page.locator(sel).first
            count = await loc.count()
            if count > 0:
                await loc.click(timeout=3000)
                print(f"   ✅ Submit gomb: {sel}")
                submitted = True
                break
        except Exception:
            continue

    if not submitted:
        await page.keyboard.press("Enter")
        print("   ✅ Submit: Enter billentyű")

    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(2000)

    print(f"   Bejelentkezés után URL: {page.url}")

    # Munkamenet mentése
    cookies = await page.context.cookies()
    storage = await page.evaluate("JSON.stringify(window.localStorage)")
    with open(SESSION_FILE, "w") as f:
        json.dump({"cookies": cookies, "localStorage": storage}, f)
    print("✅ Bejelentkezve, munkamenet mentve.")


async def load_session(context):
    """Betölti a mentett munkamenetet."""
    if not os.path.exists(SESSION_FILE):
        return False
    try:
        with open(SESSION_FILE) as f:
            data = json.load(f)
        await context.add_cookies(data["cookies"])
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════
# SEGÉDFÜGGVÉNYEK – FORM KITÖLTÉS
# ═══════════════════════════════════════════

async def kattint_es_gepel(page, selector, ertek, nth=0):
    loc = page.locator(selector).nth(nth)
    await loc.scroll_into_view_if_needed()
    await loc.click()
    await page.wait_for_timeout(100)
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    await page.keyboard.type(str(ertek), delay=30)
    await page.wait_for_timeout(100)


async def js_find_fill(page, js_getter, value, label):
    """Megkeresi az elemet JS-sel, majd Playwright triple_click+type tölti ki."""
    try:
        h = await page.evaluate_handle(js_getter)
        el = h.as_element()
        if el:
            await el.scroll_into_view_if_needed()
            await el.triple_click()
            await page.wait_for_timeout(100)
            await page.keyboard.type(str(value), delay=30)
            await page.wait_for_timeout(100)
        await h.dispose()
    except Exception as e:
        print(f"  ⚠️  {label} hiba: {e}")


JS_MENNY = """() => {
    const ar=[...document.querySelectorAll('input[placeholder="Egységár"]')];
    const mg=[...document.querySelectorAll('input[placeholder="Tétel megnevezése"]')];
    if(!ar.length||!mg.length)return null;
    const lastAr=ar[ar.length-1],lastMg=mg[mg.length-1];
    function path(e){const p=[];while(e){p.unshift(e);e=e.parentElement;}return p;}
    const p1=path(lastMg),p2=path(lastAr);
    let lca=document.body;
    for(let i=0;i<Math.min(p1.length,p2.length);i++){if(p1[i]===p2[i])lca=p1[i];else break;}
    const inp=[...lca.querySelectorAll('input:not([type="checkbox"])')];
    const ai=inp.indexOf(lastAr);
    return ai>=2?inp[ai-2]:null;
}"""

JS_EGYS = """() => {
    const ar=[...document.querySelectorAll('input[placeholder="Egységár"]')];
    const mg=[...document.querySelectorAll('input[placeholder="Tétel megnevezése"]')];
    if(!ar.length||!mg.length)return null;
    const lastAr=ar[ar.length-1],lastMg=mg[mg.length-1];
    function path(e){const p=[];while(e){p.unshift(e);e=e.parentElement;}return p;}
    const p1=path(lastMg),p2=path(lastAr);
    let lca=document.body;
    for(let i=0;i<Math.min(p1.length,p2.length);i++){if(p1[i]===p2[i])lca=p1[i];else break;}
    const inp=[...lca.querySelectorAll('input:not([type="checkbox"])')];
    const ai=inp.indexOf(lastAr);
    return ai>=1?inp[ai-1]:null;
}"""

JS_EGAR = """() => {
    const ar=[...document.querySelectorAll('input[placeholder="Egységár"]')];
    return ar.length?ar[ar.length-1]:null;
}"""


# ═══════════════════════════════════════════
# FŐ AUTOMATIZÁCIÓ
# ═══════════════════════════════════════════

async def run_automation(payload: dict):
    """Kitölti az Innonest árajánlat formot a kapott adatokkal."""

    ugyfel    = payload.get("ugyfel", {})
    targya    = payload.get("arajanlat_targya", "AI-ÁTNÉZÉSRE")
    items     = payload.get("items", [])

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page    = await context.new_page()

        # ── Bejelentkezés (munkamenet betöltése vagy friss login) ──
        session_loaded = await load_session(context)
        await page.goto("https://app.innonest.hu", wait_until="networkidle")
        await page.wait_for_timeout(1500)

        # Ha login oldalra kerültünk (session lejárt vagy nincs session)
        if "login" in page.url:
            await login(page)
            # Bejelentkezés után vissza a főoldalra
            await page.goto("https://app.innonest.hu", wait_until="networkidle")
            await page.wait_for_timeout(1000)
            # Ha még mindig login oldalon vagyunk, hiba
            if "login" in page.url:
                raise Exception("Bejelentkezés sikertelen! Ellenőrizd az INNONEST_EMAIL és INNONEST_PASSWORD env változókat.")

        # ── Navigálás az Új árajánlat formra ──
        await page.click("text=Munkavégzés")
        await page.wait_for_timeout(500)
        await page.click("text=Árajánlatok")
        await page.wait_for_load_state("networkidle")
        await page.click("text=Új árajánlat")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)

        # ── Ügyféladatok ──
        nev_input = page.locator('input[placeholder="Ügyfél neve"]').first
        await nev_input.click()
        await page.wait_for_timeout(200)
        await page.keyboard.type(ugyfel.get("nev", ""), delay=50)
        await page.wait_for_timeout(400)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(300)

        await kattint_es_gepel(page, 'input[placeholder="Adószám"]',        ugyfel.get("adoszam", ""))
        await kattint_es_gepel(page, 'input[placeholder="Irányítószám"]',   ugyfel.get("iranyitoszam", ""))
        await kattint_es_gepel(page, 'input[placeholder="Település"]',      ugyfel.get("telepules", ""))
        await kattint_es_gepel(page, 'input[placeholder="Utca"]',           ugyfel.get("utca", ""))
        await kattint_es_gepel(page, 'input[placeholder="Kapcsolattartó neve"]', ugyfel.get("kapcsolattarto", ""))

        # ── Árajánlat tárgya ──
        await kattint_es_gepel(page, 'input[placeholder="Árajánlat tárgya"]', targya)

        # ── Tételek ──
        for i, item in enumerate(items):
            if i > 0:
                await page.locator('button:has-text("Új tétel hozzáadása")').first.click()
                await page.wait_for_timeout(700)

            # Megnevezés
            megnev = page.locator('input[placeholder="Tétel megnevezése"]').last
            await megnev.scroll_into_view_if_needed()
            await megnev.click()
            await page.wait_for_timeout(100)
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            await page.keyboard.type(item["megnevezes"], delay=25)
            await page.wait_for_timeout(150)

            # Megjegyzés textarea
            if item.get("megjegyzes"):
                await page.evaluate("""
                    (args) => {
                        const megs=[...document.querySelectorAll('input[placeholder="Tétel megnevezése"]')];
                        if(!megs.length)return;
                        const lastMeg=megs[megs.length-1];
                        let el=lastMeg.parentElement,ta=null;
                        for(let d=0;d<8;d++){ta=el&&el.querySelector('textarea');if(ta)break;el=el&&el.parentElement;}
                        if(!ta)return;
                        const s=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;
                        s.call(ta,args.text);
                        ta.dispatchEvent(new Event('input',{bubbles:true}));
                        ta.dispatchEvent(new Event('change',{bubbles:true}));
                    }
                """, {"text": item["megjegyzes"]})
                await page.wait_for_timeout(100)

            # Mennyiség / Egység / Egységár
            await js_find_fill(page, JS_MENNY, item["mennyiseg"],  "Mennyiség")
            await js_find_fill(page, JS_EGYS,  item["egyseg"],     "Egység")
            await js_find_fill(page, JS_EGAR,  item["egysegar"],   "Egységár")

        import base64

        # ── Screenshot 1: form megnyílt ──
        sc1 = await page.screenshot(full_page=True)
        print(f"   📸 Screenshot 1: form megnyílt")

        # ── Tételek kitöltése után: Screenshot 2 ──
        # (a tételek kitöltése már megtörtént fentebb)
        await page.wait_for_timeout(500)
        sc2 = await page.screenshot(full_page=True)
        print(f"   📸 Screenshot 2: kitöltés után")

        # ── Mentés gomb keresése ──
        mentes_selectors = [
            'button:has-text("Mentés")',
            'button[type="submit"]',
            '.btn-primary',
            'button.btn-success',
        ]
        mentes_ok = False
        for sel in mentes_selectors:
            try:
                loc = page.locator(sel).first
                cnt = await loc.count()
                if cnt > 0:
                    print(f"   💾 Mentés gomb: {sel}")
                    await loc.scroll_into_view_if_needed()
                    await loc.click()
                    mentes_ok = True
                    break
            except Exception as e:
                print(f"   ⚠️  {sel}: {e}")
                continue

        if not mentes_ok:
            sc_err = await page.screenshot(full_page=True)
            raise Exception(f"Nem találtam Mentés gombot! screenshot_error: {base64.b64encode(sc_err).decode()}")

        print("   ⏳ Várakozás mentés után...")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(2000)

        result_url = page.url
        print(f"   ✅ Mentés után URL: {result_url}")

        # ── Screenshot 3: mentés után ──
        sc3 = await page.screenshot(full_page=True)
        print(f"   📸 Screenshot 3: mentés után")

        await browser.close()

    return {
        "ok": True,
        "url": result_url,
        "screenshots": {
            "1_form_megnyilt":   base64.b64encode(sc1).decode(),
            "2_kitoltes_utan":   base64.b64encode(sc2).decode(),
            "3_mentes_utan":     base64.b64encode(sc3).decode(),
        }
    }


# ═══════════════════════════════════════════
# API ENDPOINT
# ═══════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/create-arajanlat", methods=["POST"])
def create_arajanlat():
    # API kulcs ellenőrzés
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "Hiányzó JSON adat"}), 400

    try:
        result = run_in_loop(run_automation(payload))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
