"""
innonest_core.py – Innonest Playwright alap
===========================================
Megosztott funkciók amelyeket mind a megrendelés figyelő,
mind az árajánlat feltöltő, mind a pipedrive_addon használ.

Tartalom:
  - Bejelentkezés + session kezelés
  - JS értékbeállítás (React-kompatibilis)
  - Tétel kitöltés
  - Csatolmány feltöltés
  - Árajánlat tételek kinyerése (Playwright scraping)
"""

import os
import json
import base64
import re
import logging
import asyncio
import threading

from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
INNONEST_EMAIL    = os.environ.get("INNONEST_EMAIL", "")
INNONEST_PASSWORD = os.environ.get("INNONEST_PASSWORD", "")
SESSION_FILE      = "/tmp/innonest_session.json"

# ── Async event loop ──────────────────────────────────────────────────────────
_loop = asyncio.new_event_loop()

def run_in_loop(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=300)

def start_loop():
    _loop.run_forever()

threading.Thread(target=start_loop, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# BEJELENTKEZÉS + SESSION
# ══════════════════════════════════════════════════════════════════════════════

async def login(page):
    log.info("Bejelentkezés...")
    await page.goto("https://app.innonest.hu/login.html", wait_until="networkidle")
    await page.wait_for_timeout(2000)

    for sel in ['input[type="email"]', 'input[name="email"]',
                'input[placeholder*="email" i]', 'input[placeholder*="Email"]']:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.fill(INNONEST_EMAIL, timeout=3000)
                break
        except Exception:
            continue

    for sel in ['input[type="password"]', 'input[name="password"]',
                'input[placeholder*="jelszó" i]']:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.fill(INNONEST_PASSWORD, timeout=3000)
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

    cookies = await page.context.cookies()
    with open(SESSION_FILE, "w") as f:
        json.dump({"cookies": cookies}, f)
    log.info("Bejelentkezés sikeres")


async def load_session(context) -> bool:
    if not os.path.exists(SESSION_FILE):
        return False
    try:
        with open(SESSION_FILE) as f:
            data = json.load(f)
        await context.add_cookies(data["cookies"])
        return True
    except Exception:
        return False


def make_browser_args() -> list:
    return [
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--no-zygote", "--single-process", "--disable-setuid-sandbox",
        "--disable-background-networking", "--disable-default-apps",
        "--disable-extensions", "--disable-sync", "--no-first-run",
        "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
    ]


# ══════════════════════════════════════════════════════════════════════════════
# JS ÉRTÉKBEÁLLÍTÁS – React-kompatibilis
# ══════════════════════════════════════════════════════════════════════════════

JS_SET_VALUE = """
    (args) => {
        const el = document.querySelector(args.selector);
        if (!el) return false;
        try {
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, args.value);
        } catch(e) { el.value = args.value; }
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
        } catch(e) { el.value = args.value; }
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
        } catch(e) { el.value = args.value; }
        el.dispatchEvent(new Event('input',  {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    }
"""


async def js_fill(page, selector, value, label=""):
    ok = await page.evaluate(JS_SET_VALUE, {"selector": selector, "value": str(value)})
    if ok:
        log.info(f"  ✅ {label or selector}: '{value}'")
    else:
        log.warning(f"  ⚠️  {label or selector}: nem találtam ({selector})")
    return ok


async def js_fill_nth(page, selector, value, nth=-1, label=""):
    ok = await page.evaluate(JS_SET_VALUE_NTH,
                              {"selector": selector, "value": str(value), "nth": nth})
    if ok:
        log.info(f"  ✅ {label}: '{value}'")
    else:
        log.warning(f"  ⚠️  {label}: nem találtam (nth={nth}, {selector})")
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# NÉV MEZŐ + TÉTEL KITÖLTÉS
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

    log.info(f"  Név: '{nev_val}'")


async def fill_tetel(page, i, megnevezes, mennyiseg, egyseg, egysegar, megjegyzes=""):
    ok = await js_fill_nth(
        page, 'input[placeholder="Tétel megnevezése"]',
        megnevezes, nth=i, label=f"[{i+1}] Megnevezés"
    )
    if not ok:
        await js_fill_nth(page, 'input[placeholder="Tétel megnevezése"]',
                          megnevezes, nth=-1, label=f"[{i+1}] Megnevezés (last)")

    await page.wait_for_timeout(200)

    if megjegyzes:
        await page.evaluate(JS_SET_TEXTAREA_NTH, {"value": megjegyzes, "nth": i})

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
                return {error: `index ${args.i} out of range`};
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
            return {
                mennyiseg: setVal(mennyisegEl, args.mennyiseg),
                egyseg:    setVal(egysegEl,    args.egyseg),
                egysegar:  setVal(thisAr,      args.egysegar),
                aIdx: aIdx, total: allInputs.length,
            };
        }
    """, {"i": i, "mennyiseg": str(mennyiseg), "egyseg": str(egyseg), "egysegar": str(egysegar)})

    if isinstance(fill_result, dict) and fill_result.get("error"):
        log.error(f"  [{i+1}] JS hiba: {fill_result['error']}")
    else:
        log.info(f"  [{i+1}] menny={mennyiseg}, egys={egyseg}, ar={egysegar}")

    await page.wait_for_timeout(200)


# ══════════════════════════════════════════════════════════════════════════════
# CSATOLMÁNY FELTÖLTÉS
# ══════════════════════════════════════════════════════════════════════════════

async def upload_csatolmany(page, csatolmany: dict, bid_szam: str):
    import tempfile
    fajl_nev  = csatolmany.get("nev", "arajanlat.xlsx")
    fajl_adat = base64.b64decode(csatolmany["adat"])
    tmp_dir   = tempfile.mkdtemp()
    tmp_path  = os.path.join(tmp_dir, fajl_nev)
    with open(tmp_path, "wb") as f:
        f.write(fajl_adat)

    try:
        await page.goto("https://app.innonest.hu/bids", wait_until="networkidle")
        await page.wait_for_timeout(1500)

        if bid_szam:
            bid_lok = page.locator(f"text={bid_szam}").first
            if await bid_lok.count() > 0:
                sor = bid_lok.locator(
                    "xpath=ancestor::tr | ancestor::li | ancestor::div[@class]"
                ).first
                paperclip = sor.locator("button, a").filter(
                    has=page.locator('[class*="attach"], [class*="paper"], [title*="satolm"]')
                ).first
                if await paperclip.count() == 0:
                    paperclip = sor.locator("button:first-child, a:first-child").first
        else:
            paperclip = page.locator(
                "table tbody tr:first-child button, table tbody tr:first-child a"
            ).first

        if await paperclip.count() > 0:
            await paperclip.scroll_into_view_if_needed()
            await paperclip.click()
            await page.wait_for_timeout(1000)

        file_input = page.locator('input[type="file"]').first
        if await file_input.count() > 0:
            await file_input.set_input_files(tmp_path)
            await page.wait_for_timeout(3000)
            log.info(f"Fájl feltöltve: {fajl_nev}")
        else:
            log.warning("Fájl input nem található")
    except Exception as e:
        log.error(f"Csatolmány feltöltés hiba: {e}")
    finally:
        try:
            os.unlink(tmp_path)
            os.rmdir(tmp_dir)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# ÁRAJÁNLAT TÉTELEK KINYERÉSE (Playwright scraping – pipedrive_addon hívja)
# ══════════════════════════════════════════════════════════════════════════════

async def get_arajanlat_tetelek(page, bid: str) -> dict:
    """
    BID szám alapján megkeresi az árajánlatot az Innonestben,
    megnyitja a szerkesztőt (/bids/change/ID), és kinyeri:
    tételek, nettó összeg, fizetési feltétel, érvényesség, pénznem.
    """
    eredmeny = {
        "tetelek": [], "netto_osszeg": "",
        "penznem": "HUF", "fizetesi_feltetelek": "–", "ervenyes_ig": "–",
    }
    try:
        await page.goto("https://app.innonest.hu/bids", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # PDF link lekérése a BID sorából → belső ID kinyerése
        bid_link = await page.evaluate(f"""
            () => {{
                const links = document.querySelectorAll('td.left.bold a, td a[href*="worksheets_pdf"]');
                for (const a of links) {{
                    if (a.innerText.trim().includes('{bid}') || a.href.includes('{bid}')) {{
                        return a.href;
                    }}
                }}
                const allTds = document.querySelectorAll('td');
                for (const td of allTds) {{
                    if (td.innerText.trim() === '{bid}') {{
                        const row = td.closest('tr');
                        if (row) {{
                            const pdfLink = row.querySelector('a[href*="worksheets_pdf"]');
                            if (pdfLink) return pdfLink.href;
                        }}
                    }}
                }}
                return null;
            }}
        """)

        if not bid_link:
            log.warning(f"[TETELEK] Nem találtam a BID sort: {bid}")
            return eredmeny

        id_match = re.search(r"/worksheets_pdf/open/(\d+)", bid_link)
        if not id_match:
            id_match = re.search(r"/bids/change/(\d+)", bid_link)
        if not id_match:
            log.warning(f"[TETELEK] Nem tudtam kinyerni az ID-t: {bid_link}")
            return eredmeny

        bid_id = id_match.group(1)
        szerkeszto_url = f"https://app.innonest.hu/bids/change/{bid_id}"
        log.info(f"[TETELEK] Szerkesztő URL: {szerkeszto_url}")

        await page.goto(szerkeszto_url, wait_until="networkidle")
        await page.wait_for_timeout(3000)

        # Tételek kinyerése: input[name^="productsName/Qty/Price"] + input[name^="nettPrice"]
        tetelek = await page.evaluate("""
            () => {
                const tetelek = [];
                let sorszam = 1;
                const sorok = document.querySelectorAll(
                    'tbody.items-box tr.items:not([data-id="0"])'
                );
                sorok.forEach(tr => {
                    const nevInput = tr.querySelector('input[name^="productsName"]');
                    const nev = nevInput ? nevInput.value.trim() : '';
                    if (!nev || nev.length < 2) return;

                    const mennyInput = tr.querySelector('input[name^="productsQty"]');
                    const menny = mennyInput ? mennyInput.value.trim() : '';

                    let egys = '';
                    const egysDiv = tr.querySelector('div.smaller.red');
                    if (egysDiv) {
                        const t = egysDiv.innerText.trim();
                        const m = t.match(/[A-Za-z]{1,5}\\d*$/i);
                        egys = m ? m[0].toLowerCase() : '';
                    }

                    const arInput = tr.querySelector('input[name^="productsPrice"]');
                    const egysegar = arInput ? arInput.value.trim() : '';

                    const osszInput = tr.querySelector('input[name^="nettPrice"]');
                    const osszesen = osszInput ? osszInput.value.trim() : '';

                    tetelek.push({
                        sorszam: sorszam++,
                        megnevezes: nev,
                        mennyiseg: menny + (egys ? ' ' + egys : ''),
                        egysegar: egysegar,
                        osszesen: osszesen
                    });
                });
                return tetelek;
            }
        """)

        if tetelek:
            eredmeny["tetelek"] = tetelek
            log.info(f"[TETELEK] {len(tetelek)} tétel kinyerve ({bid})")

        # Nettó végösszeg: input.fullTotalNett
        netto_js = await page.evaluate("""
            () => {
                const nettoInput = document.querySelector(
                    'input.fullTotalNett, input[name*="fullNett"], input[name*="totalNett"]'
                );
                if (nettoInput && nettoInput.value) return nettoInput.value.trim();
                const tfoot = document.querySelector('tfoot');
                if (tfoot) {
                    const inputs = tfoot.querySelectorAll('input');
                    for (const inp of inputs) {
                        const v = inp.value.replace(/[\\s,]/g,'');
                        if (/^[0-9]{4,}/.test(v)) return inp.value.trim();
                    }
                }
                return '';
            }
        """)

        if netto_js:
            try:
                eredmeny["netto_osszeg"] = f"{int(float(netto_js.replace(',', '.'))):,}".replace(",", " ")
            except Exception:
                eredmeny["netto_osszeg"] = netto_js

        # Oldalszöveg → pénznem, fizetési feltétel, érvényesség
        page_text = await page.evaluate(
            "function(){ return document.body ? document.body.innerText : ''; }"
        )

        penznem_m = re.search(r"\b(HUF|EUR|USD|GBP|CHF)\b", page_text)
        if penznem_m:
            eredmeny["penznem"] = penznem_m.group(1)

        fizetes_m = re.search(r"[Ff]izet[eé]si feltétel[ek]*[:\s]*([^\n]{5,80})", page_text)
        if fizetes_m:
            eredmeny["fizetesi_feltetelek"] = fizetes_m.group(1).strip()

        ervenyes_m = re.search(
            r"[Éé]rv[eé]nyes[^\n]{0,10}?(\d{4}[-. ]\d{2}[-. ]\d{2}|\d{4}\. \w+ \d{1,2}\.)",
            page_text
        )
        if ervenyes_m:
            eredmeny["ervenyes_ig"] = ervenyes_m.group(1).strip()

        log.info(
            f"[TETELEK] Kész ({bid}): tételek={len(eredmeny['tetelek'])}, "
            f"nettó={eredmeny['netto_osszeg']}, pénznem={eredmeny['penznem']}"
        )

    except Exception as e:
        log.error(f"[TETELEK] Hiba ({bid}): {e}")

    return eredmeny


async def _innonest_adatok_leker_async(bid: str) -> dict:
    """Önálló Playwright session a tételek lekéréséhez."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=make_browser_args()
        )
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        await page.goto("https://app.innonest.hu/bids", wait_until="networkidle")
        await page.wait_for_timeout(1000)

        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu/bids", wait_until="networkidle")
            await page.wait_for_timeout(1000)

        eredmeny = await get_arajanlat_tetelek(page, bid)
        await browser.close()
        return eredmeny


def innonest_adatok_leker(bid: str) -> dict:
    """Szinkron wrapper — ezt hívja a pipedrive_addon.py."""
    try:
        return run_in_loop(_innonest_adatok_leker_async(bid))
    except Exception as e:
        log.error(f"[INNONEST] Adatlekérés hiba ({bid}): {e}")
        return {
            "tetelek": [], "netto_osszeg": "",
            "penznem": "HUF", "fizetesi_feltetelek": "–", "ervenyes_ig": "–",
        }
