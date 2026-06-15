"""
arajanlat_pdf.py – Árajánlat PDF generátor
============================================
Az Innonest BID alapján:
  1. Beolvassa az árajánlat adatait (ügyfél, tételek, leírások, összesítők)
  2. Kitölti a HTML sablont (sablonok/arajanlat_sablon.html)
  3. PDF-fé renderel (Playwright)
  4. Feltölti az Innonestbe (csatolmányként a BID-hez)
  5. Feltölti a Pipedrive-ba (a BID alapján megtalált deal-hez csatolva)

Háttérben fut (job_id alapú polling) – a teljes folyamat 20-40 mp is lehet,
ami szinkron válasz esetén elérné a Railway proxy ~30 mp-es timeout-ját.

Végpontok:
  GET  /pdf-tool                  – a kezelőfelület (BID input + gomb)
  POST /generate-arajanlat-pdf    – elindítja a folyamatot, visszaad egy job_id-t
  GET  /pdf-status/<job_id>       – lekérdezhető állapot (polling)
  GET  /pdf-download/<job_id>     – a kész PDF letöltése/megnyitása
"""

import os
import re
import asyncio
import logging
import threading
import traceback
import uuid

import requests
from flask import request, jsonify, Response
from playwright.async_api import async_playwright

from innonest_core import login, load_session, make_browser_args, _loop

log = logging.getLogger(__name__)

PIPEDRIVE_API_TOKEN     = os.environ.get("PIPEDRIVE_API_TOKEN", "")
PIPEDRIVE_BID_FIELD_KEY = "b58dfacf166feaa1517c2c7d8b17052e4df78a28"

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
SABLON_PATH = os.path.join(BASE_DIR, "sablonok", "arajanlat_sablon.html")
TOOL_PATH   = os.path.join(BASE_DIR, "sablonok", "pdf_tool.html")


# ══════════════════════════════════════════════════════════════════════════════
# JOB ÁLLAPOT TÁROLÁS (memóriában – egyetlen Railway instance-hoz elegendő)
# ══════════════════════════════════════════════════════════════════════════════

_jobs = {}
_jobs_lock = threading.Lock()


def _set_job(job_id, **kwargs):
    with _jobs_lock:
        _jobs.setdefault(job_id, {})
        _jobs[job_id].update(kwargs)


def _get_job(job_id):
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


# ══════════════════════════════════════════════════════════════════════════════
# SZÁM FORMÁZÁS – magyar formátum
# ══════════════════════════════════════════════════════════════════════════════

def _to_float(value) -> float:
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        return 0.0


def fmt_huf(value) -> str:
    n = _to_float(value)
    return f"{n:,.0f}".replace(",", " ") + " Ft"


def fmt_huf_decimal(value) -> str:
    n = _to_float(value)
    s = f"{n:,.2f}".replace(",", " ").replace(".", ",")
    return s + " Ft"


def _esc(text) -> str:
    return (str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# ══════════════════════════════════════════════════════════════════════════════
# TÉTEL SOR HTML GENERÁLÁS
# ══════════════════════════════════════════════════════════════════════════════

def tetel_sor(item: dict) -> str:
    """
    Egy tétel HTML blokkját generálja a sablon 2. oldalához.
    item: { megnevezes, mennyiseg (pl. "130 m²"), egyseg, egysegar, osszesen, leiras, megjegyzes }
    """
    megnevezes = _esc(item.get("megnevezes"))
    mennyiseg  = _esc(item.get("mennyiseg"))
    egysegar_szam = fmt_huf(item.get("egysegar", 0))
    egyseg     = (item.get("egyseg") or "").strip()
    egysegar   = f"{egysegar_szam}/{egyseg}" if egyseg else egysegar_szam
    osszesen   = fmt_huf(item.get("osszesen", 0))
    leiras     = (item.get("leiras") or "").strip()
    megjegyzes = (item.get("megjegyzes") or "").strip()

    if megjegyzes:
        nev_html = (
            f'<div><span style="font-weight:700;color:#333;">{megnevezes}</span>'
            f'<br><span style="font-size:9.5px;color:#8a8a8a;">{_esc(megjegyzes)}</span></div>'
        )
    else:
        nev_html = f'<div style="font-weight:700;color:#333;">{megnevezes}</div>'

    html = (
        '<div style="display:grid;grid-template-columns:1fr 86px 100px 104px;'
        'background:#f4f4f4;padding:9px 8px;font-size:11px;align-items:start;">'
        f'{nev_html}'
        f'<div style="text-align:right;color:#444;">{mennyiseg}</div>'
        f'<div style="text-align:right;color:#444;">{_esc(egysegar)}</div>'
        f'<div style="text-align:right;color:#333;">{_esc(osszesen)}</div>'
        '</div>'
    )

    if leiras:
        html += (
            '<div style="font-size:9.5px;color:#7e7e7e;line-height:1.55;'
            f'padding:7px 8px 13px;">{_esc(leiras)}</div>'
        )
    else:
        html += '<div style="height:9px;"></div>'

    return html


# ══════════════════════════════════════════════════════════════════════════════
# SABLON KITÖLTÉS
# ══════════════════════════════════════════════════════════════════════════════

def build_arajanlat_html(adatok: dict) -> str:
    with open(SABLON_PATH, encoding="utf-8") as f:
        html = f.read()

    items = adatok.get("tetelek", [])
    tetelek_html = "".join(tetel_sor(item) for item in items)

    # "Nettó egységár összesen" – csak ha minden tétel m²-ben van
    netto_egysegar_sor = ""
    egysegek = set((it.get("egyseg") or "").strip().lower() for it in items)
    if egysegek and egysegek.issubset({"m²", "m2"}):
        try:
            osszeg = sum(_to_float(it.get("egysegar", 0)) for it in items)
            netto_egysegar_sor = (
                '<div style="text-align:right;font-size:10.5px;color:#8a8a8a;margin-top:18px;">'
                f'Nettó egységár összesen: {fmt_huf(osszeg)}/m2</div>'
            )
        except Exception:
            pass

    text_values = {
        "UGYFEL_CEGNEV":     adatok.get("ugyfel_nev", ""),
        "BID_SZAM":          adatok.get("bid_szam", ""),
        "TARGYA":            adatok.get("targya", ""),
        "KELTEZES":          adatok.get("keltezes", ""),
        "ERVENYES":          adatok.get("ervenyes", ""),
        "UGYFEL_NEV":        adatok.get("ugyfel_nev", ""),
        "UGYFEL_CIM":        adatok.get("ugyfel_cim", ""),
        "UGYFEL_ADOSZAM":    adatok.get("ugyfel_adoszam", ""),
        "FIZETESI_FELTETEL": adatok.get("fizetesi_feltetel", ""),
        "NETTO_OSSZESEN":    fmt_huf(adatok.get("netto_osszesen", 0)),
        "AFA_OSSZESEN":      fmt_huf_decimal(adatok.get("afa_osszesen", 0)),
        "BRUTTO_OSSZESEN":   fmt_huf_decimal(adatok.get("brutto_osszesen", 0)),
    }

    for key, val in text_values.items():
        html = html.replace("{{" + key + "}}", _esc(val))

    # HTML tartalmú placeholderek – nem escape-eljük
    html = html.replace("{{TETELEK_SOROK}}", tetelek_html)
    html = html.replace("{{NETTO_EGYSEGAR_SOR}}", netto_egysegar_sor)

    return html


# ══════════════════════════════════════════════════════════════════════════════
# INNONEST SCRAPING – kiterjesztett adatok egy konkrét BID-hez
# ══════════════════════════════════════════════════════════════════════════════

async def _find_bid_edit_url(page, bid: str) -> str:
    """A 'bids' listán megkeresi a BID sort, és visszaadja a szerkesztő URL-jét."""
    await page.goto("https://app.innonest.hu/bids", wait_until="networkidle")
    await page.wait_for_timeout(1500)

    bid_link = await page.evaluate(
        """
        (bid) => {
            const escaped = bid.replace(/-/g, '\\\\-');
            const bidRegex = new RegExp('(^|[^0-9-])' + escaped + '([^0-9]|$)');
            const allTds = document.querySelectorAll('td');
            for (const td of allTds) {
                if (bidRegex.test(td.innerText.trim())) {
                    const row = td.closest('tr');
                    if (row) {
                        const link = row.querySelector(
                            'a[href*="worksheets_pdf"], a[href*="/bids/change/"]'
                        );
                        if (link) return link.href;
                    }
                }
            }
            return null;
        }
        """,
        bid,
    )
    if not bid_link:
        raise Exception(f"Nem találtam a BID sort a listában: {bid}")

    id_match = re.search(r"/(?:worksheets_pdf/open|bids/change)/(\d+)", bid_link)
    if not id_match:
        raise Exception(f"Nem tudtam kinyerni a szerkesztő ID-t: {bid_link}")

    return f"https://app.innonest.hu/bids/change/{id_match.group(1)}"


async def _scrape_arajanlat(page, bid: str) -> dict:
    """
    Megnyitja a BID szerkesztő oldalát és kiolvas mindent
    amire a PDF sablonnak szüksége van.
    """
    eredmeny = {
        "bid_szam": bid,
        "targya": "", "keltezes": "", "ervenyes": "",
        "ugyfel_nev": "", "ugyfel_cim": "", "ugyfel_adoszam": "",
        "fizetesi_feltetel": "Átutalás",
        "tetelek": [],
        "netto_osszesen": 0, "afa_osszesen": 0, "brutto_osszesen": 0,
    }

    edit_url = await _find_bid_edit_url(page, bid)
    log.info(f"[PDF] Szerkesztő URL: {edit_url}")
    await page.goto(edit_url, wait_until="networkidle")
    await page.wait_for_timeout(2000)

    # ── Ügyféladatok ──
    for key, placeholder in [
        ("ugyfel_nev",     "Ügyfél neve"),
        ("ugyfel_adoszam", "Adószám"),
    ]:
        try:
            loc = page.locator(f'input[placeholder="{placeholder}"]').first
            if await loc.count() > 0:
                eredmeny[key] = (await loc.input_value()).strip()
        except Exception as e:
            log.warning(f"[PDF] '{key}' olvasási hiba: {e}")

    # ── Cím összerakása: Irányítószám + Település, Utca ──
    try:
        irsz = telepules = utca = ""
        loc = page.locator('input[placeholder="Irányítószám"]').first
        if await loc.count() > 0:
            irsz = (await loc.input_value()).strip()
        loc = page.locator('input[placeholder="Település"]').first
        if await loc.count() > 0:
            telepules = (await loc.input_value()).strip()
        loc = page.locator('input[placeholder="Utca"]').first
        if await loc.count() > 0:
            utca = (await loc.input_value()).strip()

        cim = " ".join(p for p in [irsz, telepules] if p)
        if utca:
            cim = f"{cim}, {utca}" if cim else utca
        eredmeny["ugyfel_cim"] = cim
    except Exception as e:
        log.warning(f"[PDF] Cím összeállítás hiba: {e}")

    # ── Árajánlat tárgya ──
    try:
        loc = page.locator('input[placeholder="Árajánlat tárgya"]').first
        if await loc.count() > 0:
            eredmeny["targya"] = (await loc.input_value()).strip()
    except Exception as e:
        log.warning(f"[PDF] Tárgya olvasási hiba: {e}")

    # ── Tételek + leírások ──
    # FONTOS: a textarea (leírás) sorrendje feltételezetten megegyezik
    # a tétel-sorok sorrendjével (ugyanaz a feltételezés mint a fill_tetel-ben).
    tetelek = await page.evaluate(
        """
        () => {
            const rows = document.querySelectorAll('tbody.items-box tr.items:not([data-id="0"])');
            const out = [];
            rows.forEach(tr => {
                const nevInput = tr.querySelector('input[name^="productsName"]');
                const nev = nevInput ? nevInput.value.trim() : '';
                if (!nev) return;

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

                const taInput = tr.querySelector('textarea');
                const leiras = taInput ? taInput.value.trim() : '';

                out.push({
                    megnevezes: nev,
                    mennyiseg: menny + (egys ? ' ' + egys : ''),
                    egyseg: egys,
                    egysegar: egysegar,
                    osszesen: osszesen,
                    leiras: leiras,
                });
            });
            return out;
        }
        """
    )
    eredmeny["tetelek"] = tetelek
    log.info(f"[PDF] {len(tetelek)} tétel kiolvasva")
    for i, t in enumerate(tetelek):
        log.info(
            f"[PDF]   [{i+1}] {t['megnevezes'][:40]!r} | {t['mennyiseg']} | "
            f"{t['egysegar']} | {t['osszesen']} | leírás={'van' if t['leiras'] else 'nincs'}"
        )

    # ── Nettó / Áfa / Bruttó összegek ──
    netto_js = await page.evaluate(
        """
        () => {
            const el = document.querySelector(
                'input.fullTotalNett, input[name*="fullNett"], input[name*="totalNett"]'
            );
            return el ? el.value.trim() : '';
        }
        """
    )
    brutto_js = await page.evaluate(
        """
        () => {
            const el = document.querySelector(
                'input.fullTotalGross, input[name*="fullGross"], input[name*="totalGross"], input[name*="fullBrutto"]'
            );
            return el ? el.value.trim() : '';
        }
        """
    )

    netto = _to_float(netto_js) if netto_js else sum(_to_float(t["osszesen"]) for t in tetelek)
    if brutto_js:
        brutto = _to_float(brutto_js)
        afa = brutto - netto
    else:
        afa = round(netto * 0.27, 2)
        brutto = netto + afa
        log.warning("[PDF] Bruttó összeg nem található a lapon, 27%-os ÁFA-val becsülve.")

    eredmeny["netto_osszesen"]  = netto
    eredmeny["afa_osszesen"]    = afa
    eredmeny["brutto_osszesen"] = brutto

    # ── Keltezés / Érvényesség / Fizetési feltétel – oldal szöveg alapján ──
    page_text = await page.evaluate("() => document.body ? document.body.innerText : ''")

    keltezes_m = re.search(r"[Kk]eltez[eé]s[^\n]{0,10}?(\d{4}-\d{2}-\d{2})", page_text)
    if keltezes_m:
        eredmeny["keltezes"] = keltezes_m.group(1)
    else:
        import datetime
        eredmeny["keltezes"] = datetime.date.today().isoformat()
        log.warning("[PDF] Keltezés nem található, mai dátum használva.")

    ervenyes_m = re.search(r"[Éé]rv[eé]nyes[^\n]{0,15}?(\d{4}-\d{2}-\d{2})", page_text)
    if ervenyes_m:
        eredmeny["ervenyes"] = ervenyes_m.group(1)

    fizetes_m = re.search(r"[Ff]izet[eé]si feltétel[ek]*[:\s]*([^\n]{3,60})", page_text)
    if fizetes_m:
        eredmeny["fizetesi_feltetel"] = fizetes_m.group(1).strip()

    log.info(
        f"[PDF] Összegek: nettó={eredmeny['netto_osszesen']}, "
        f"áfa={eredmeny['afa_osszesen']}, bruttó={eredmeny['brutto_osszesen']}"
    )
    log.info(
        f"[PDF] Fejléc adatok: ügyfél='{eredmeny['ugyfel_nev']}', "
        f"cím='{eredmeny['ugyfel_cim']}', adószám='{eredmeny['ugyfel_adoszam']}', "
        f"tárgy='{eredmeny['targya'][:50]}', keltezés={eredmeny['keltezes']}, "
        f"érvényes={eredmeny['ervenyes']}, fizetés='{eredmeny['fizetesi_feltetel']}'"
    )

    return eredmeny


# ══════════════════════════════════════════════════════════════════════════════
# PDF FELTÖLTÉS AZ INNONESTBE
# ══════════════════════════════════════════════════════════════════════════════

async def _upload_pdf_to_innonest(page, pdf_path: str, bid: str):
    await page.goto("https://app.innonest.hu/bids", wait_until="networkidle")
    await page.wait_for_timeout(1500)

    bid_lok = page.locator(f"text={bid}").first
    if await bid_lok.count() == 0:
        raise Exception(f"PDF feltöltés: nem találtam a BID sort: {bid}")

    sor = bid_lok.locator(
        "xpath=ancestor::tr | ancestor::li | ancestor::div[@class]"
    ).first
    paperclip = sor.locator("button, a").filter(
        has=page.locator('[class*="attach"], [class*="paper"], [title*="satolm"]')
    ).first
    if await paperclip.count() == 0:
        paperclip = sor.locator("button:first-child, a:first-child").first

    if await paperclip.count() == 0:
        raise Exception("PDF feltöltés: nem találtam a csatolmány gombot")

    await paperclip.scroll_into_view_if_needed()
    await paperclip.click()
    await page.wait_for_timeout(1000)

    file_input = page.locator('input[type="file"]').first
    if await file_input.count() == 0:
        raise Exception("PDF feltöltés: nem találtam a fájl input mezőt")

    await file_input.set_input_files(pdf_path)
    await page.wait_for_timeout(3000)
    log.info(f"[PDF] Feltöltve Innonestbe: {pdf_path}")


# ══════════════════════════════════════════════════════════════════════════════
# PIPEDRIVE FELTÖLTÉS
# ══════════════════════════════════════════════════════════════════════════════

def _find_pipedrive_deal_by_bid(bid: str):
    if not PIPEDRIVE_API_TOKEN:
        log.warning("[PIPEDRIVE] PIPEDRIVE_API_TOKEN nincs beállítva")
        return None

    url = "https://api.pipedrive.com/v1/itemSearch/field"
    params = {
        "term": bid,
        "field_type": "dealField",
        "field_key": PIPEDRIVE_BID_FIELD_KEY,
        "exact_match": "true",
        "return_item_ids": "true",
        "api_token": PIPEDRIVE_API_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=20)
    data = resp.json()
    items = data.get("data") or []
    if not items:
        log.warning(f"[PIPEDRIVE] Nem található deal ehhez a BID-hez: {bid}")
        return None

    item = items[0]
    deal_id = item.get("id") or (item.get("item") or {}).get("id")
    log.info(f"[PIPEDRIVE] Deal találat ({bid}): #{deal_id}")
    return deal_id


def upload_pdf_to_pipedrive(bid: str, pdf_path: str) -> dict:
    deal_id = _find_pipedrive_deal_by_bid(bid)
    if not deal_id:
        return {"success": False, "message": "Deal nem található a BID alapján"}

    url = "https://api.pipedrive.com/v1/files"
    with open(pdf_path, "rb") as f:
        files = {"file": (f"{bid}.pdf", f, "application/pdf")}
        data = {"deal_id": deal_id}
        resp = requests.post(
            url, files=files, data=data,
            params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=60,
        )

    try:
        result = resp.json()
    except Exception:
        result = {}

    success = bool(result.get("success"))
    log.info(f"[PIPEDRIVE] Fájl feltöltés deal #{deal_id} -> success={success}")
    return {"success": success, "deal_id": deal_id}


# ══════════════════════════════════════════════════════════════════════════════
# TELJES FOLYAMAT
# ══════════════════════════════════════════════════════════════════════════════

async def _full_pipeline(bid: str, job_id: str):
    pdf_path = f"/tmp/{bid}.pdf"

    _set_job(job_id, status="scraping", message="Adatok kiolvasása az Innonestből...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        await page.goto("https://app.innonest.hu", wait_until="networkidle")
        await page.wait_for_timeout(800)
        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu", wait_until="networkidle")
            await page.wait_for_timeout(800)
            if "login" in page.url:
                raise Exception("Bejelentkezés sikertelen!")

        adatok = await _scrape_arajanlat(page, bid)

        _set_job(job_id, status="rendering", message="PDF renderelése...")
        html = build_arajanlat_html(adatok)

        pdf_page = await context.new_page()
        await pdf_page.set_content(html, wait_until="networkidle")
        await pdf_page.pdf(path=pdf_path, format="A4", print_background=True)
        await pdf_page.close()
        log.info(f"[PDF] Renderelve: {pdf_path}")

        _set_job(job_id, status="uploading_innonest", message="Feltöltés az Innonestbe...")
        await _upload_pdf_to_innonest(page, pdf_path, bid)

        await browser.close()

    _set_job(job_id, status="uploading_pipedrive", message="Csatolás a Pipedrive dealhez...")
    pipedrive_result = upload_pdf_to_pipedrive(bid, pdf_path)

    _set_job(
        job_id,
        status="done",
        message="Kész!",
        bid_szam=adatok.get("bid_szam"),
        ugyfel_nev=adatok.get("ugyfel_nev"),
        netto_osszesen=adatok.get("netto_osszesen"),
        pipedrive=pipedrive_result,
        pdf_path=pdf_path,
    )


async def _run_pipeline_safe(bid: str, job_id: str):
    try:
        await _full_pipeline(bid, job_id)
    except Exception as e:
        log.error(f"[PDF] Hiba ({bid}): {e}")
        log.error(traceback.format_exc())
        _set_job(job_id, status="error", message=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# FLASK VÉGPONTOK
# ══════════════════════════════════════════════════════════════════════════════

def register_pdf_routes(app):
    """Hívd meg a server.py-ból: register_pdf_routes(app)"""

    @app.route("/pdf-tool", methods=["GET"])
    def pdf_tool():
        with open(TOOL_PATH, encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")

    @app.route("/generate-arajanlat-pdf", methods=["POST"])
    def generate_arajanlat_pdf():
        data = request.get_json() or {}
        bid = (data.get("bid") or "").strip().upper()

        if not re.match(r"^BID-\d{4}-\d+$", bid):
            return jsonify({"error": "Érvénytelen BID formátum (pl. BID-2026-185)"}), 400

        job_id = str(uuid.uuid4())
        _set_job(job_id, status="started", message="Indítás...", bid=bid)

        asyncio.run_coroutine_threadsafe(_run_pipeline_safe(bid, job_id), _loop)

        return jsonify({"job_id": job_id})

    @app.route("/pdf-status/<job_id>", methods=["GET"])
    def pdf_status(job_id):
        job = _get_job(job_id)
        if not job:
            return jsonify({"error": "Ismeretlen job"}), 404
        return jsonify(job)

    @app.route("/pdf-download/<job_id>", methods=["GET"])
    def pdf_download(job_id):
        job = _get_job(job_id)
        pdf_path = job.get("pdf_path")
        if not pdf_path or not os.path.exists(pdf_path):
            return jsonify({"error": "PDF nem található"}), 404
        with open(pdf_path, "rb") as f:
            data = f.read()
        return Response(
            data,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{job.get("bid","arajanlat")}.pdf"'},
        )

    log.info("[PDF] Végpontok regisztrálva: /pdf-tool, /generate-arajanlat-pdf, /pdf-status/<id>, /pdf-download/<id>")
