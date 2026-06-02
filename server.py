"""
Innonest Automatizáció – Felhő Szerver
"""

import os, json, asyncio, threading, base64
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright

app = Flask(__name__)

INNONEST_EMAIL    = os.environ.get("INNONEST_EMAIL", "")
INNONEST_PASSWORD = os.environ.get("INNONEST_PASSWORD", "")
API_KEY           = os.environ.get("API_KEY", "titkos-kulcs")
SESSION_FILE      = "/tmp/innonest_session.json"

_loop = asyncio.new_event_loop()

def run_in_loop(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=300)

def start_loop():
    _loop.run_forever()

threading.Thread(target=start_loop, daemon=True).start()


# ═══════════════════════════════════════════
# BEJELENTKEZÉS
# ═══════════════════════════════════════════

async def login(page):
    print("🔑 Bejelentkezés...")
    await page.goto("https://app.innonest.hu/login.html", wait_until="networkidle")
    await page.wait_for_timeout(2000)
    print(f"   URL: {page.url}")

    for sel in ['input[type="email"]', 'input[name="email"]', 'input[placeholder*="email" i]', 'input[placeholder*="Email"]']:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.fill(INNONEST_EMAIL, timeout=3000)
                print(f"   Email: {sel}")
                break
        except Exception:
            continue

    for sel in ['input[type="password"]', 'input[name="password"]', 'input[placeholder*="jelszó" i]']:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.fill(INNONEST_PASSWORD, timeout=3000)
                print(f"   Jelszó: {sel}")
                break
        except Exception:
            continue

    for sel in ['button[type="submit"]', 'button:has-text("Belépés")', 'button:has-text("Login")']:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=3000)
                break
        except Exception:
            continue

    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(2000)
    print(f"   Bejelentkezés után: {page.url}")

    cookies = await page.context.cookies()
    with open(SESSION_FILE, "w") as f:
        json.dump({"cookies": cookies}, f)
    print("✅ Bejelentkezve")


async def load_session(context):
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
# JS ÉRTÉKBEÁLLÍTÁS – React-kompatibilis
# ═══════════════════════════════════════════

JS_SET_VALUE = """
    (args) => {
        const el = document.querySelector(args.selector);
        if (!el) return false;
        try {
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, args.value);
        } catch(e) {
            el.value = args.value;
        }
        el.dispatchEvent(new Event('input',  {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
        return true;
    }
"""

JS_SET_VALUE_NTH = """
    (args) => {
        const els = document.querySelectorAll(args.selector);
        if (!els.length) return false;
        const idx = args.nth < 0 ? els.length + args.nth : args.nth;
        const el = els[idx];
        if (!el) return false;
        try {
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, args.value);
        } catch(e) {
            el.value = args.value;
        }
        el.dispatchEvent(new Event('input',  {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
        return true;
    }
"""

JS_SET_TEXTAREA_NTH = """
    (args) => {
        const els = document.querySelectorAll('textarea');
        if (!els.length) return false;
        const idx = args.nth < 0 ? els.length + args.nth : args.nth;
        const el = els[idx];
        if (!el) return false;
        try {
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value').set;
            setter.call(el, args.value);
        } catch(e) {
            el.value = args.value;
        }
        el.dispatchEvent(new Event('input',  {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    }
"""


async def js_fill(page, selector, value, label=""):
    """Egyetlen selectorú input feltöltése JS-sel."""
    ok = await page.evaluate(JS_SET_VALUE, {"selector": selector, "value": str(value)})
    if ok:
        print(f"   ✅ {label or selector}: '{value}'")
    else:
        print(f"   ⚠️  {label or selector}: nem találtam ({selector})")
    return ok


async def js_fill_nth(page, selector, value, nth=-1, label=""):
    """N-edik matching input feltöltése JS-sel (nth=-1 = utolsó)."""
    ok = await page.evaluate(JS_SET_VALUE_NTH, {"selector": selector, "value": str(value), "nth": nth})
    if ok:
        print(f"   ✅ {label}: '{value}'")
    else:
        print(f"   ⚠️  {label}: nem találtam (nth={nth}, {selector})")
    return ok


# ═══════════════════════════════════════════
# NÉV MEZŐ – autocomplete, Tab-bal lép ki
# ═══════════════════════════════════════════

async def fill_nev(page, value):
    """Ügyfél neve autocomplete mező – Tab-bal lépünk ki."""
    nev = page.locator('input[placeholder="Ügyfél neve"]').first
    await nev.scroll_into_view_if_needed()
    await nev.click()
    await page.wait_for_timeout(300)
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(200)
    await page.keyboard.type(str(value), delay=60)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(400)

    nev_val = await nev.input_value()
    if not nev_val.strip():
        # Második próba
        await nev.click()
        await page.wait_for_timeout(200)
        await page.keyboard.type(str(value), delay=60)
        await page.wait_for_timeout(400)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(300)
        nev_val = await nev.input_value()

    print(f"   Név: '{nev_val}'")


# ═══════════════════════════════════════════
# TÉTEL KITÖLTÉS
# ═══════════════════════════════════════════

async def fill_tetel(page, i, megnevezes, mennyiseg, egyseg, egysegar, megjegyzes=""):
    """
    Egy tétel sort tölt ki.
    i = sorszám (0-tól), ezzel számítjuk a mezők indexeit.
    """

    # Megnevezés – az i-edik tétel megnevezés inputja
    ok = await js_fill_nth(
        page,
        'input[placeholder="Tétel megnevezése"]',
        megnevezes,
        nth=i,
        label=f"[{i+1}] Megnevezés"
    )
    if not ok:
        # Fallback: utolsó
        await js_fill_nth(page, 'input[placeholder="Tétel megnevezése"]', megnevezes, nth=-1, label=f"[{i+1}] Megnevezés (last)")

    await page.wait_for_timeout(200)

    # Megjegyzés textarea – az i-edik textarea
    if megjegyzes:
        await page.evaluate(JS_SET_TEXTAREA_NTH, {"value": megjegyzes, "nth": i})
        print(f"   ✅ [{i+1}] Megjegyzés: '{megjegyzes[:30]}'")

    await page.wait_for_timeout(100)

    # Mennyiség, Egység, Egységár megkeresése LCA módszerrel JS-ből
    # Az i-edik megnevezés és az i-edik egységár közös ősét keressük
    fill_result = await page.evaluate("""
        (args) => {
            function setVal(el, val) {
                if (!el) return false;
                try {
                    const s = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    s.call(el, String(val));
                } catch(e) { el.value = String(val); }
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true}));
                return true;
            }

            const megInputs = [...document.querySelectorAll('input[placeholder="Tétel megnevezése"]')];
            const arInputs  = [...document.querySelectorAll('input[placeholder="Egységár"]')];

            if (args.i >= megInputs.length || args.i >= arInputs.length)
                return {error: `index ${args.i} out of range: meg=${megInputs.length}, ar=${arInputs.length}`};

            const thisMeg = megInputs[args.i];
            const thisAr  = arInputs[args.i];

            // LCA keresés
            function getPath(el) {
                const p = []; while(el) { p.unshift(el); el = el.parentElement; } return p;
            }
            const p1 = getPath(thisMeg), p2 = getPath(thisAr);
            let lca = document.body;
            for (let j = 0; j < Math.min(p1.length, p2.length); j++) {
                if (p1[j] === p2[j]) lca = p1[j]; else break;
            }

            const allInputs = [...lca.querySelectorAll('input:not([type="checkbox"])')];
            const aIdx = allInputs.indexOf(thisAr);

            const mennyisegEl = aIdx >= 2 ? allInputs[aIdx - 2] : null;
            const egysegEl    = aIdx >= 1 ? allInputs[aIdx - 1] : null;

            const r = {
                mennyiseg: setVal(mennyisegEl, args.mennyiseg),
                egyseg:    setVal(egysegEl,    args.egyseg),
                egysegar:  setVal(thisAr,      args.egysegar),
                aIdx: aIdx,
                total: allInputs.length,
            };
            return r;
        }
    """, {
        "i":         i,
        "mennyiseg": str(mennyiseg),
        "egyseg":    str(egyseg),
        "egysegar":  str(egysegar),
    })

    if isinstance(fill_result, dict) and fill_result.get("error"):
        print(f"   ❌ [{i+1}] JS hiba: {fill_result['error']}")
    else:
        print(f"   ✅ [{i+1}] menny={mennyiseg}, egys={egyseg}, ar={egysegar} "
              f"(aIdx={fill_result.get('aIdx','?')}, total={fill_result.get('total','?')})")

    await page.wait_for_timeout(200)


# ═══════════════════════════════════════════
# FŐ AUTOMATIZÁCIÓ
# ═══════════════════════════════════════════

async def run_automation(payload: dict):
    ugyfel = payload.get("ugyfel", {})
    targya = payload.get("arajanlat_targya", "AI-ÁTNÉZÉSRE")
    items  = payload.get("items", [])

    screenshots = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page    = await context.new_page()

        # Bejelentkezés
        await load_session(context)
        await page.goto("https://app.innonest.hu", wait_until="networkidle")
        await page.wait_for_timeout(1500)

        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu", wait_until="networkidle")
            await page.wait_for_timeout(1000)
            if "login" in page.url:
                raise Exception("Bejelentkezés sikertelen!")

        print(f"✅ Bent vagyunk: {page.url}")

        # Navigálás
        await page.click("text=Munkavégzés")
        await page.wait_for_timeout(500)
        await page.click("text=Árajánlatok")
        await page.wait_for_load_state("networkidle")
        await page.click("text=Új árajánlat")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)
        print(f"✅ Form megnyílt: {page.url}")

        screenshots["1_form_megnyilt"] = base64.b64encode(
            await page.screenshot(full_page=True)).decode()

        # ── Ügyféladatok ──
        print("👤 Ügyféladatok...")

        # Név
        await fill_nev(page, ugyfel.get("nev", ""))

        # Adószám
        adoszam = ugyfel.get("adoszam", "")
        if adoszam:
            await js_fill(page, 'input[placeholder="Adószám"]', adoszam, "Adószám")

        # Irányítószám
        irsz = ugyfel.get("iranyitoszam", "")
        if irsz:
            await js_fill(page, 'input[placeholder="Irányítószám"]', irsz, "Irányítószám")

        # Település
        telep = ugyfel.get("telepules", "")
        if telep:
            await js_fill(page, 'input[placeholder="Település"]', telep, "Település")

        # Utca
        utca = ugyfel.get("utca", "")
        if utca:
            await js_fill(page, 'input[placeholder="Utca"]', utca, "Utca")

        # Kapcsolattartó
        kap = ugyfel.get("kapcsolattarto", "")
        if kap:
            await js_fill(page, 'input[placeholder="Kapcsolattartó neve"]', kap, "Kapcsolattartó")

        # ── Árajánlat tárgya ──
        print(f"📄 Árajánlat tárgya: '{targya}'")
        await js_fill(page, 'input[placeholder="Árajánlat tárgya"]', targya, "Árajánlat tárgya")

        # ── Tételek ──
        print(f"📋 {len(items)} tétel...")
        for i, item in enumerate(items):
            print(f"\n   [{i+1}/{len(items)}] {item['megnevezes'][:50]}")

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
        print("\n📸 Screenshot: kitöltés után")

        # ── Mentés ──
        mentes_ok = False
        for sel in ['button:has-text("Mentés")', 'button[type="submit"]', '.btn-primary']:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.scroll_into_view_if_needed()
                    await loc.click()
                    mentes_ok = True
                    print(f"💾 Mentés: {sel}")
                    break
            except Exception as e:
                print(f"   ⚠️  {sel}: {e}")

        if not mentes_ok:
            raise Exception("Nem találtam Mentés gombot!")

        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(2000)
        result_url = page.url
        print(f"✅ Mentés után URL: {result_url}")

        screenshots["3_mentes_utan"] = base64.b64encode(
            await page.screenshot(full_page=True)).decode()

        await browser.close()

    return {"ok": True, "url": result_url, "screenshots": screenshots}


# ═══════════════════════════════════════════
# API
# ═══════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
