"""
Innonest Automatizáció – Szerver (egyesített)
=============================================
Két funkció:
  1. POST /create-arajanlat  – árajánlat feltöltés (eredeti, változatlan)
  2. Háttérszál              – 5 percenként figyeli az Innonest megrendelőlapokat,
                               és ha új "Megrendelt" státuszú tétel jelenik meg,
                               elküldi a BID számot a Google Apps Script Web App-nak,
                               ami átnevezi a sheetet: hozzáfűzi a " - MEGRENDELVE" utótagot.

Környezeti változók (Railway → Variables):
  INNONEST_EMAIL    – Innonest bejelentkezési email
  INNONEST_PASSWORD – Innonest jelszó
  API_KEY           – Titkos kulcs az Apps Script gombhoz
  WEBAPP_SECRET     – Titkos kulcs a Google Apps Script Web App-hoz
  WEBAPP_URL        – A Google Apps Script Web App URL-je (opcionális, default beégetve)
"""

import os, json, asyncio, threading, base64, time, re, logging
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.async_api import async_playwright

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
INNONEST_EMAIL    = os.environ.get("INNONEST_EMAIL", "")
INNONEST_PASSWORD = os.environ.get("INNONEST_PASSWORD", "")
API_KEY           = os.environ.get("API_KEY", "titkos-kulcs")
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
# BEJELENTKEZÉS (eredeti, működő verzió)
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# JS ÉRTÉKBEÁLLÍTÁS – React-kompatibilis (eredeti, változatlan)
# ══════════════════════════════════════════════════════════════════════════════

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
    ok = await page.evaluate(JS_SET_VALUE, {"selector": selector, "value": str(value)})
    if ok:
        print(f"   ✅ {label or selector}: '{value}'")
    else:
        print(f"   ⚠️  {label or selector}: nem találtam ({selector})")
    return ok


async def js_fill_nth(page, selector, value, nth=-1, label=""):
    ok = await page.evaluate(JS_SET_VALUE_NTH, {"selector": selector, "value": str(value), "nth": nth})
    if ok:
        print(f"   ✅ {label}: '{value}'")
    else:
        print(f"   ⚠️  {label}: nem találtam (nth={nth}, {selector})")
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# NÉV MEZŐ + TÉTEL KITÖLTÉS (eredeti, változatlan)
# ══════════════════════════════════════════════════════════════════════════════

async def fill_nev(page, value):
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
        await nev.click()
        await page.wait_for_timeout(200)
        await page.keyboard.type(str(value), delay=60)
        await page.wait_for_timeout(400)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(300)
        nev_val = await nev.input_value()

    print(f"   Név: '{nev_val}'")


async def fill_tetel(page, i, megnevezes, mennyiseg, egyseg, egysegar, megjegyzes=""):
    ok = await js_fill_nth(
        page,
        'input[placeholder="Tétel megnevezése"]',
        megnevezes,
        nth=i,
        label=f"[{i+1}] Megnevezés"
    )
    if not ok:
        await js_fill_nth(page, 'input[placeholder="Tétel megnevezése"]', megnevezes, nth=-1, label=f"[{i+1}] Megnevezés (last)")

    await page.wait_for_timeout(200)

    if megjegyzes:
        await page.evaluate(JS_SET_TEXTAREA_NTH, {"value": megjegyzes, "nth": i})
        print(f"   ✅ [{i+1}] Megjegyzés: '{megjegyzes[:30]}'")

    await page.wait_for_timeout(100)

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


# ══════════════════════════════════════════════════════════════════════════════
# CSATOLMÁNY FELTÖLTÉS (eredeti, változatlan)
# ══════════════════════════════════════════════════════════════════════════════

async def upload_csatolmany(page, csatolmany: dict, bid_szam: str):
    import tempfile
    fajl_nev  = csatolmany.get("nev", "arajanlat.xlsx")
    fajl_adat = base64.b64decode(csatolmany["adat"])
    tmp_dir   = tempfile.mkdtemp()
    tmp_path  = os.path.join(tmp_dir, fajl_nev)
    with open(tmp_path, "wb") as f:
        f.write(fajl_adat)

    print(f"   Temp fájl: {tmp_path} ({len(fajl_adat)} byte)")

    try:
        await page.goto("https://app.innonest.hu/bids", wait_until="networkidle")
        await page.wait_for_timeout(1500)

        if bid_szam:
            bid_lok = page.locator(f"text={bid_szam}").first
            if await bid_lok.count() > 0:
                sor = bid_lok.locator("xpath=ancestor::tr | ancestor::li | ancestor::div[@class]").first
                paperclip = sor.locator("button, a").filter(
                    has=page.locator('[class*="attach"], [class*="paper"], [title*="satolm"]')
                ).first
                if await paperclip.count() == 0:
                    paperclip = sor.locator("button:first-child, a:first-child").first
        else:
            paperclip = page.locator("table tbody tr:first-child button, table tbody tr:first-child a").first

        if await paperclip.count() > 0:
            await paperclip.scroll_into_view_if_needed()
            await paperclip.click()
            await page.wait_for_timeout(1000)
            print("   ✅ Gémkapocs ikon: kattintva")
        else:
            badges = page.locator('[title*="satolm"], [aria-label*="satolm"], button.badge, .attachment-btn')
            if await badges.count() > 0:
                await badges.first.click()
                await page.wait_for_timeout(1000)
            else:
                print("   ⚠️  Gémkapocs ikon nem található, fájl feltöltés kihagyva")
                return

        await page.wait_for_timeout(500)
        file_input = page.locator('input[type="file"]').first
        if await file_input.count() > 0:
            await file_input.set_input_files(tmp_path)
            await page.wait_for_timeout(3000)
            print(f"   ✅ Fájl feltöltve: {fajl_nev}")
        else:
            upload_zone = page.locator(
                'text=Húzd ebbe, [class*="upload"], [class*="dropzone"], [class*="drop-zone"]'
            ).first
            if await upload_zone.count() > 0:
                await upload_zone.click()
                await page.wait_for_timeout(500)
                file_input2 = page.locator('input[type="file"]').first
                if await file_input2.count() > 0:
                    await file_input2.set_input_files(tmp_path)
                    await page.wait_for_timeout(3000)
                    print(f"   ✅ Fájl feltöltve (2. kísérlet): {fajl_nev}")
                else:
                    print("   ⚠️  Fájl input nem található a dialógban")
            else:
                print("   ⚠️  Feltöltési zóna sem található")

    except Exception as e:
        print(f"   ❌ Csatolmány feltöltés hiba: {e}")
    finally:
        try:
            os.unlink(tmp_path)
            os.rmdir(tmp_dir)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# FŐ AUTOMATIZÁCIÓ – árajánlat létrehozás (eredeti, változatlan)
# ══════════════════════════════════════════════════════════════════════════════

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

        print("👤 Ügyféladatok...")
        await fill_nev(page, ugyfel.get("nev", ""))

        adoszam = ugyfel.get("adoszam", "")
        if adoszam:
            await js_fill(page, 'input[placeholder="Adószám"]', adoszam, "Adószám")

        irsz = ugyfel.get("iranyitoszam", "")
        if irsz:
            await js_fill(page, 'input[placeholder="Irányítószám"]', irsz, "Irányítószám")

        telep = ugyfel.get("telepules", "")
        if telep:
            await js_fill(page, 'input[placeholder="Település"]', telep, "Település")

        utca = ugyfel.get("utca", "")
        if utca:
            await js_fill(page, 'input[placeholder="Utca"]', utca, "Utca")

        kap = ugyfel.get("kapcsolattarto", "")
        if kap:
            await js_fill(page, 'input[placeholder="Kapcsolattartó neve"]', kap, "Kapcsolattartó")

        print(f"📄 Árajánlat tárgya: '{targya}'")
        await js_fill(page, 'input[placeholder="Árajánlat tárgya"]', targya, "Árajánlat tárgya")

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

        bid_szam = await page.evaluate("""
            () => {
                const text = document.body ? document.body.innerText : "";
                const m = text.match(/BID-[0-9]{4}-[0-9]+/);
                return m ? m[0] : null;
            }
        """)
        if bid_szam:
            print(f"✅ BID szám: {bid_szam}")
        else:
            print("⚠️  BID számot nem sikerült kinyerni az oldalból")

        csatolmany = payload.get("csatolmany")
        if csatolmany and csatolmany.get("adat"):
            print(f"📎 Csatolmány feltöltése: {csatolmany.get('nev', 'fajl.xlsx')}")
            await upload_csatolmany(page, csatolmany, bid_szam)
        else:
            print("ℹ️  Nincs csatolmány adat a payloadban")

        await browser.close()

    return {
        "ok":          True,
        "url":         result_url,
        "bid_szam":    bid_szam,
        "screenshots": screenshots,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MEGRENDELÉS FIGYELŐ – ÚJ FUNKCIÓ
# ══════════════════════════════════════════════════════════════════════════════

def rename_sheet_via_webapp(bid: str) -> dict:
    """Elküldi a BID számot a Google Apps Script Web App-nak, ami átnevezi a sheetet."""
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


def load_processed() -> set:
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_processed(processed: set):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(processed), f)


async def get_megrendelt_tetelek(page) -> list[dict]:
    """
    Megnyitja a Megrendelőlapok listát és visszaadja a Megrendelt státuszú tételek adatait.
    A JS csak nyers szöveget ad vissza, a Python elemzi.
    """
    log.info("Megrendelőlapok ellenőrzése...")
    await page.goto("https://app.innonest.hu/ordersheets", wait_until="networkidle")
    await page.wait_for_timeout(2000)

    # Egyszerű JS: csak a sorok szövegét és data-id-jét adjuk vissza
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

        # BID szám keresése
        bid_match = re.search(r"BID-\d{4}-\d+", szoveg)
        if not bid_match:
            continue
        bid = bid_match.group(0)

        # Csak Megrendelt státuszú sorok
        if "megrendelt" not in szoveg.lower():
            continue

        if bid in seen_bid:
            continue
        seen_bid.add(bid)

        # Sorokra bontás, üres sorok kiszűrése
        sorok_lista = [s.strip() for s in szoveg.splitlines() if s.strip()]

        # Cég neve és árajánlat tárgya kinyerése
        # Sor szerkezete: [KIV badge] [sorszám] [tárgy+BID] [cég] [összeg pénznem dátum státusz]
        ertelmes_sorok = []
        for s in sorok_lista:
            if re.match(r"^\d{4}-\d{2}-\d{2}", s): continue      # dátum
            if re.match(r"^\d{4}-\d+$", s): continue               # megrendelőlap szám
            if re.search(r"HUF|EUR|USD|GBP|CHF", s): continue       # összeg+pénznem sor
            if re.search(r"megrendelt|piszkozat|elküldve", s, re.IGNORECASE): continue
            if re.match(r"^[\d\s\.,]+$", s): continue              # csak számok
            if len(s) <= 5 and s.isupper(): continue                 # KIV, BEJ badge
            ertelmes_sorok.append(s)

        # Első sor = tárgy (tartalmazza a BID-et is), második sor = cég neve
        targya = ertelmes_sorok[0] if len(ertelmes_sorok) >= 1 else ""
        cegnev = ertelmes_sorok[1] if len(ertelmes_sorok) >= 2 else ""

        # Pénznem + nettó kinyerése EGYÜTT
        # Először egysoros szöveggé alakítjuk (sortörések eltávolítása)
        szoveg_1sor = re.sub(r"[\t\n\r]+", " ", szoveg)
        penznem = "HUF"
        netto = ""
        # Keresés: szám + pénznem egymás mellett (pl. "16 068 384 HUF" vagy "3 239 EUR")
        penz_match = re.search(r"([0-9][0-9 ]{2,}[0-9])\s*(HUF|EUR|USD|GBP|CHF)", szoveg_1sor)
        if penz_match:
            netto = penz_match.group(1).replace(" ", "")
            penznem = penz_match.group(2)

        if not row_id:
            row_id = bid

        log.info(f"  → {bid}: cég='{cegnev}', tárgy='{targya[:40]}', pénznem={penznem}, nettó={netto}")
        tetelek.append({
            "row_id":  row_id,
            "bid":     bid,
            "cegnev":  cegnev,
            "targya":  targya,
            "penznem": penznem,
            "netto":   netto,
            "link":    link,
        })

    log.info(f"Talált Megrendelt tételek: {len(tetelek)}")
    return tetelek


async def get_arajanlat_reszletek(page, bid: str) -> dict:
    """
    Megnyitja az árajánlat oldalát BID alapján és kinyeri a pontos adatokat:
    pénznem, nettó összérték.
    """
    reszletek = {"penznem": "HUF", "netto": ""}
    try:
        await page.goto("https://app.innonest.hu/bids", wait_until="networkidle")
        await page.wait_for_timeout(1500)

        # BID szám megkeresése a listában, kattintás a sorra
        bid_elem = page.locator(f"text={bid}").first
        if await bid_elem.count() > 0:
            await bid_elem.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(1500)
            log.info(f"Árajánlat oldal: {page.url}")

            page_text = await page.evaluate("function(){ return document.body ? document.body.innerText : ''; }")

            penznem_m = re.search(r"(HUF|EUR|USD|GBP|CHF)", page_text)
            if penznem_m:
                reszletek["penznem"] = penznem_m.group(1)

            netto_m = re.search(r"[Nn]ett[oo].{0,10}([0-9][0-9 .,]{3,})", page_text)
            if netto_m:
                reszletek["netto"] = re.sub(r" ", "", netto_m.group(1))

            log.info(f"Arajanlat adatok ({bid}): penznem={reszletek['penznem']}, netto={reszletek['netto']}")
        else:
            log.warning(f"Nem találtam az árajánlatot: {bid}")
    except Exception as e:
        log.error(f"Árajánlat részletek hiba ({bid}): {e}")
    return reszletek


async def check_megrendelesek():
    """Bejelentkezik az Innonestbe, lekéri a megrendelőlapokat, feldolgozza az újakat."""
    processed = load_processed()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
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
            link   = tetel.get("link")

            if row_id in processed:
                log.info(f"{bid} már feldolgozva – kihagyom.")
                continue

            log.info(f"Új megrendelés feldolgozása: {bid}")

            # Adatok kinyerése a listából (már megvan a tetelben)
            cegnev  = tetel.get("cegnev", "")
            targya  = tetel.get("targya", "")
            penznem = tetel.get("penznem", "HUF")
            netto   = tetel.get("netto", "")

            # Ha nincs nettó, megpróbáljuk az árajánlat oldaláról kinyerni
            if not netto:
                ar_reszletek = await get_arajanlat_reszletek(page, bid)
                penznem = ar_reszletek.get("penznem", penznem)
                netto   = ar_reszletek.get("netto", "")

            # Web App hívása: átnevezés + G11 kiolvasás + cél sheetbe írás
            try:
                response = requests.post(
                    WEBAPP_URL,
                    json={
                        "secret":  WEBAPP_SECRET,
                        "bid":     bid,
                        "cegnev":  cegnev,
                        "targya":  targya,
                        "penznem": penznem,
                        "netto":   netto,
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

threading.Thread(target=megrendeles_figyelő, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# FLASK VÉGPONTOK
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/check-now", methods=["POST"])
def check_now():
    """Azonnali ellenőrzés kiváltása manuálisan."""
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        run_in_loop(check_megrendelesek())
        return jsonify({"status": "ok", "message": "Ellenőrzés lefutott."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
