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
  GET  /pdf-tool                     – a kezelőfelület (BID input + gomb)
  POST /generate-arajanlat-pdf       – elindítja a folyamatot, visszaad egy job_id-t
  GET  /pdf-status/<job_id>          – lekérdezhető állapot (polling)
  GET  /pdf-download/<job_id>        – a kész PDF letöltése/megnyitása
  GET  /pdf-kikuld-status/<proj_id>  – webapp polling: email sikeresen elküldve?
"""

import os
import re
import json
import time
import base64
import asyncio
import logging
import threading
import traceback
import uuid

import requests
from flask import request, jsonify, Response, redirect
from playwright.async_api import async_playwright

from innonest_core import login, load_session, make_browser_args, _loop, upload_csatolmany
from pipedrive_addon import PDFquotationSENDdealOWNER

log = logging.getLogger(__name__)

PIPEDRIVE_API_TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN", "")
PDF_TOOL_SECRET     = os.environ.get("PDF_TOOL_SECRET", "")

# ── Pipedrive → webapp import konfiguráció ────────────────────────────────
# WEBAPP_BASE_URL     : Railway env var – pl. "https://sqm-hungary.hu/kalkulator"
# PD_WEBAPP_URL_FIELD : Pipedrive custom field API key ahova a webapp URL-t
#                       visszaírja (majd megadod amikor létrehozod a mezőt)
WEBAPP_BASE_URL     = os.environ.get("WEBAPP_BASE_URL", "https://sqm-hungary.hu/kalkulator/index.html")
PD_WEBAPP_URL_FIELD = os.environ.get("PD_WEBAPP_URL_FIELD", "")

# Pipedrive deal custom field kulcsok (hardcode – nem kell env var)
_PD_FIELD_HELYSZIN = "7008531d11f5bade385cc7fb72bb2648d4b19137"  # Kivitelezés helyszíne

# ── Pipedrive függőben lévő importok (memória) ────────────────────────────
# token → {nev, helyszin, cegnev, deal_id, created_at}
# A webapp lekéri és egyszeri alkalommal visszaadja (törli), mint a PDF polling.
_pd_imports      = {}
_pd_imports_lock = threading.Lock()


def _pd_fetch_deal(deal_id: int) -> dict:
    """Lekéri a deal adatait a Pipedrive API-ból."""
    url = f"https://api.pipedrive.com/v1/deals/{deal_id}"
    r = requests.get(url, params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=10)
    r.raise_for_status()
    return r.json().get("data") or {}


def _pd_write_webapp_url(deal_id: int, webapp_url: str):
    """Visszaírja a webapp projekt URL-t a megadott Pipedrive mezőbe."""
    if not PD_WEBAPP_URL_FIELD:
        log.info("[PD] PD_WEBAPP_URL_FIELD nincs beállítva – URL visszaírás kihagyva")
        return
    url = f"https://api.pipedrive.com/v1/deals/{deal_id}"
    r = requests.put(
        url,
        params={"api_token": PIPEDRIVE_API_TOKEN},
        json={PD_WEBAPP_URL_FIELD: webapp_url},
        timeout=10
    )
    r.raise_for_status()
    log.info(f"[PD] URL visszaírva deal #{deal_id}: {webapp_url}")


def _pdf_tool_auth_ok(req) -> bool:
    """
    Ellenőrzi a PDF tool hitelesítést.
    - Ha PDF_TOOL_SECRET nincs beállítva: szabad hozzáférés (fejlesztési mód)
    - Ha be van állítva: cookie VAGY ?pw= query paraméter kell
    """
    if not PDF_TOOL_SECRET:
        return True
    if req.cookies.get("pdf_tool_auth") == PDF_TOOL_SECRET:
        return True
    if req.args.get("pw") == PDF_TOOL_SECRET:
        return True
    return False

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
SABLON_PATH = os.path.join(BASE_DIR, "sablonok", "arajanlat_sablon.html")
TOOL_PATH   = os.path.join(BASE_DIR, "sablonok", "pdf_tool.html")


# ══════════════════════════════════════════════════════════════════════════════
# JOB ÁLLAPOT TÁROLÁS (memóriában – egyetlen Railway instance-hoz elegendő)
# ══════════════════════════════════════════════════════════════════════════════

_jobs = {}
_jobs_lock = threading.Lock()

_pdf_kikuld_done = {}          # proj_id → dátum string (email sikeresen elküldve)
_pdf_kikuld_lock = threading.Lock()


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
        '<div style="break-inside:avoid;">'
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

    html += '</div>'

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
    # ÉS minden tétel ugyanannyi m²-re vonatkozik (különben az összegzés félrevezető lenne)
    netto_egysegar_sor = ""
    egysegek = set((it.get("egyseg") or "").strip().lower() for it in items)
    if egysegek and egysegek.issubset({"m²", "m2"}):
        try:
            mennyisegek = set()
            for it in items:
                mennyiseg_szam = _to_float(
                    re.sub(r"[^\d.,]", "", (it.get("mennyiseg") or ""))
                )
                mennyisegek.add(round(mennyiseg_szam, 2))

            if len(mennyisegek) == 1:
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
    tetelek = await page.evaluate(
        r"""
        () => {
            const rows = document.querySelectorAll('tbody.items-box tr.items:not([data-id="0"])');
            const out = [];
            rows.forEach(tr => {
                const nevInput = tr.querySelector('input[name^="productsName"]');
                const nev = nevInput ? nevInput.value.trim() : '';
                if (!nev) return;

                const mennyInput = tr.querySelector('input[name^="productsQty"]');
                const menny = mennyInput ? mennyInput.value.trim() : '';

                const arInput = tr.querySelector('input[name^="productsPrice"]');
                const egysegar = arInput ? arInput.value.trim() : '';

                let egys = '';
                if (arInput) {
                    const allInputs = [...tr.querySelectorAll('input:not([type="checkbox"])')];
                    const aIdx = allInputs.indexOf(arInput);
                    if (aIdx >= 1) {
                        const egysegEl = allInputs[aIdx - 1];
                        egys = egysegEl ? egysegEl.value.trim() : '';
                    }
                }

                const osszInput = tr.querySelector('input[name^="nettPrice"]');
                let osszesen = osszInput ? osszInput.value.trim() : '';

                const osszesenNum = parseFloat(osszesen.replace(/\s/g, '').replace(',', '.')) || 0;
                if (!osszesenNum) {
                    const mennyNum = parseFloat(menny.replace(/\s/g, '').replace(',', '.')) || 0;
                    const arNum   = parseFloat(egysegar.replace(/\s/g, '').replace(',', '.')) || 0;
                    if (mennyNum && arNum) {
                        osszesen = String(Math.round(mennyNum * arNum * 100) / 100);
                    }
                }

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

    netto_from_items = sum(_to_float(t.get("osszesen", 0)) for t in tetelek)

    if netto_from_items > 0:
        netto = netto_from_items
        log.info(f"[PDF] Nettó összeg tételekből számolva: {netto:,.0f} Ft")
    else:
        netto = _to_float(netto_js) if netto_js else 0
        log.warning(f"[PDF] Nettó összeg Innonest oldalról kiolvasva (fallback): {netto:,.0f} Ft")

    if brutto_js:
        brutto = _to_float(brutto_js)
        afa = brutto - netto
        if afa < 0:
            log.warning("[PDF] Kiolvasott bruttó hibásnak tűnik (ÁFA negatív), 27%-os ÁFA-val becsülve.")
            afa = round(netto * 0.27, 2)
            brutto = netto + afa
    else:
        afa = round(netto * 0.27, 2)
        brutto = netto + afa
        log.warning("[PDF] Bruttó összeg nem található a lapon, 27%-os ÁFA-val becsülve.")

    eredmeny["netto_osszesen"]  = netto
    eredmeny["afa_osszesen"]    = afa
    eredmeny["brutto_osszesen"] = brutto

    form_vals = await page.evaluate("""
        () => {
            const getVal = (name) => {
                const el = document.querySelector(`[name="${name}"]`);
                return el ? el.value.trim() : '';
            };
            const getSelectText = (name) => {
                const el = document.querySelector(`select[name="${name}"]`);
                if (!el || el.selectedIndex < 0) return '';
                return el.options[el.selectedIndex].text.trim();
            };
            return {
                keltezes:          getVal('crDate'),
                ervenyes:          getVal('expiration'),
                fizetesi_feltetel: getSelectText('paymentMethod'),
            };
        }
    """)

    log.info(f"[PDF] Form értékek: keltezés='{form_vals.get('keltezes')}', "
             f"ervenyes='{form_vals.get('ervenyes')}', "
             f"fizetés='{form_vals.get('fizetesi_feltetel')}'")

    if form_vals.get("keltezes"):
        eredmeny["keltezes"] = form_vals["keltezes"]
    else:
        import datetime
        eredmeny["keltezes"] = datetime.date.today().isoformat()
        log.warning("[PDF] Keltezés nem található, mai dátum használva.")
    if form_vals.get("ervenyes"):
        eredmeny["ervenyes"] = form_vals["ervenyes"]
    if form_vals.get("fizetesi_feltetel"):
        eredmeny["fizetesi_feltetel"] = form_vals["fizetesi_feltetel"]

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

async def _upload_pdf_to_innonest(page, pdf_path: str, bid: str, ugyfel_nev: str = ""):
    with open(pdf_path, "rb") as f:
        pdf_b64 = base64.b64encode(f.read()).decode("ascii")
    filename = _pdf_filename(bid, ugyfel_nev)
    await page.goto("https://app.innonest.hu/bids", wait_until="networkidle")
    await page.wait_for_timeout(1500)

    bid_index = await page.evaluate(
        """
        (bid) => {
            const escaped = bid.replace(/-/g, '\\\\-');
            const bidRegex = new RegExp('(^|[^0-9-])' + escaped + '([^0-9]|$)');
            const rows = [...document.querySelectorAll('tr')].filter(tr =>
                tr.querySelector('a[href*="worksheets_pdf"]')
            );
            return rows.findIndex(tr => bidRegex.test(tr.innerText));
        }
        """,
        bid,
    )

    if bid_index < 0:
        raise Exception(f"PDF feltöltés: nem találtam a BID sort: {bid}")

    log.info(f"[PDF] BID sor index: {bid_index}")

    bid_rows = page.locator("tr").filter(
        has=page.locator('a[href*="worksheets_pdf"]')
    )
    target_row = bid_rows.nth(bid_index)

    trigger = target_row.locator("button, a").filter(
        has=page.locator('[class*="attach"], [class*="paper"], [title*="satolm"]')
    ).first
    if await trigger.count() == 0:
        trigger = target_row.locator("button:first-child, a:first-child").first

    if await trigger.count() == 0:
        raise Exception(f"PDF feltöltés: nem találtam trigger elemet a BID sorban: {bid}")

    await trigger.scroll_into_view_if_needed()
    await trigger.click()
    log.info("[PDF] Specifikus sor trigger kattintva")

    await page.wait_for_timeout(1500)
    total = await page.locator('input[type="file"]').count()
    log.info(f"[PDF] File inputok száma: {total}")

    if total == 0:
        raise Exception("PDF feltöltés: Dropzone input nem jelent meg")

    file_input = page.locator('input[type="file"]').first
    await file_input.set_input_files(pdf_path)
    await page.wait_for_timeout(4000)
    log.info(f"[PDF] Feltöltve Innonestbe ({bid})")


# ══════════════════════════════════════════════════════════════════════════════
# PIPEDRIVE FELTÖLTÉS
# ══════════════════════════════════════════════════════════════════════════════

def _find_open_deal_owner_by_cegnev(cegnev: str) -> dict:
    if not PIPEDRIVE_API_TOKEN:
        return {"found": False, "message": "PIPEDRIVE_API_TOKEN nincs beállítva"}
    if not cegnev:
        return {"found": False, "message": "Üres cégnév, nem lehet keresni"}

    try:
        resp = requests.get(
            "https://api.pipedrive.com/v1/organizations/search",
            params={"term": cegnev, "exact_match": "false", "api_token": PIPEDRIVE_API_TOKEN},
            timeout=20,
        )
        items = ((resp.json().get("data") or {}).get("items")) or []
    except Exception as e:
        return {"found": False, "message": f"Pipedrive cégkeresés hiba: {e}"}

    if not items:
        log.warning(f"[PIPEDRIVE] Nincs cég találat: '{cegnev}'")
        return {"found": False, "message": f"Nincs Pipedrive cég ehhez: {cegnev}"}

    log.info(
        f"[PIPEDRIVE] {len(items)} cég találat '{cegnev}'-re: "
        + ", ".join(f"{it['item']['name']} (#{it['item']['id']})" for it in items)
    )

    candidates = []
    for it in items:
        org_id = it["item"]["id"]
        try:
            resp = requests.get(
                f"https://api.pipedrive.com/v1/organizations/{org_id}/deals",
                params={"status": "open", "api_token": PIPEDRIVE_API_TOKEN},
                timeout=20,
            )
            deals = resp.json().get("data") or []
            candidates.extend(deals)
        except Exception as e:
            log.warning(f"[PIPEDRIVE] Deal lekérés hiba (org #{org_id}): {e}")

    if not candidates:
        return {"found": False, "message": f"Nincs nyitott Pipedrive deal ehhez a céghez: {cegnev}"}

    candidates.sort(key=lambda d: d.get("update_time") or "", reverse=True)
    deal = candidates[0]

    owner = deal.get("user_id") or {}
    owner_email = owner.get("email", "")
    owner_name  = owner.get("name", "")

    log.info(
        f"[PIPEDRIVE] Legfrissebb nyitott deal: #{deal.get('id')} "
        f"'{deal.get('title','')}' -> owner={owner_name} <{owner_email}>"
    )

    if not owner_email:
        return {"found": False, "message": f"A dealhez (#{deal.get('id')}) nincs üzletfelelős email"}

    return {
        "found": True,
        "owner_email": owner_email,
        "owner_name": owner_name,
        "deal_id": deal.get("id"),
        "deal_title": deal.get("title", ""),
    }


def _pdf_filename(bid: str, ugyfel_nev: str = "") -> str:
    """PDF fájlnév: VELUX_BID-2026-xxx.pdf (cégnév első szava + BID)"""
    if ugyfel_nev:
        elso_szo = ugyfel_nev.strip().split()[0]
        elso_szo = re.sub(r"[^\w\-]", "", elso_szo, flags=re.UNICODE)
        if elso_szo:
            return f"{elso_szo}_{bid}.pdf"
    return f"{bid}.pdf"


def _generate_default_message(adatok: dict) -> str:
    """Az értékesítőnek küldendő email alapértelmezett szövege (szerkeszthető)."""
    return (
        f"Elkészült egy árajánlat PDF.\n\n"
        f"Csatolva találod a {adatok.get('bid_szam', '')} árajánlat PDF verzióját "
        f"— a fájl az Innonestben is csatolásra kerül a BID-hez.\n\n"
        f"Ügyfél:\t{adatok.get('ugyfel_nev', '')}\n"
        f"Tárgy:\t{adatok.get('targya', '')}\n"
        f"Nettó összeg:\t{fmt_huf(adatok.get('netto_osszesen', 0))}"
    )


def _text_to_email_html(text: str) -> str:
    """Sima szöveget (sortörésekkel) HTML email törzssé alakít."""
    html_text = (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;")
        .replace("\n", "<br>")
    )
    return f"""
    <div style="font-family:Arial,Helvetica,sans-serif;color:#2e2c2c;max-width:520px;">
      <p style="margin:0 0 4px;color:#999;font-size:11px;letter-spacing:.5px;text-transform:uppercase;">SQM Hungary</p>
      <div style="font-size:14px;line-height:1.8;color:#2e2c2c;">{html_text}</div>
    </div>
    """


def _send_pdf_email_custom(adatok: dict, pdf_path: str, custom_message: str) -> dict:
    """Egyedi szöveggel küld email a deal üzletfelelősének, PDF csatolmánnyal."""
    info = _find_open_deal_owner_by_cegnev(adatok.get("ugyfel_nev", ""))
    if not info.get("found"):
        log.warning(f"[PDF] Email küldés kihagyva: {info.get('message')}")
        return {"success": False, "message": info.get("message")}

    subject = f"Árajánlat PDF – {adatok.get('ugyfel_nev', '')} ({adatok.get('bid_szam', '')})"
    html_body = _text_to_email_html(custom_message)

    with open(pdf_path, "rb") as f:
        pdf_b64 = base64.b64encode(f.read()).decode("ascii")

    filename = _pdf_filename(adatok.get("bid_szam", "arajanlat"), adatok.get("ugyfel_nev", ""))

    ok = PDFquotationSENDdealOWNER(
        info["owner_email"], subject, html_body,
        attachment_b64=pdf_b64,
        attachment_name=filename,
        attachment_mime="application/pdf",
    )

    if ok:
        log.info(f"[PDF] Email elküldve: {info['owner_email']}")
        return {"success": True, "owner_email": info["owner_email"], "owner_name": info["owner_name"]}
    else:
        return {"success": False, "message": "Email küldés sikertelen (Apps Script hiba)"}


# ══════════════════════════════════════════════════════════════════════════════
# KÉTFÁZISÚ FOLYAMAT
# ══════════════════════════════════════════════════════════════════════════════

async def _full_pipeline(bid: str, job_id: str):
    """1. fázis: adatok kiolvasása + PDF render. Megáll a user megerősítéséig."""
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
        await browser.close()

    _set_job(
        job_id,
        status="pdf_ready",
        message="PDF kész — szerkeszd az üzenetet és küld el",
        bid_szam=adatok.get("bid_szam"),
        ugyfel_nev=adatok.get("ugyfel_nev"),
        targya=adatok.get("targya"),
        netto_osszesen=adatok.get("netto_osszesen"),
        default_message=_generate_default_message(adatok),
        pdf_path=pdf_path,
        _adatok=adatok,
    )


async def _set_innonest_status_elkuldve(page, bid: str):
    """A BID státuszát 'Elküldve'-re állítja a szerkesztő oldalon."""
    edit_url = await _find_bid_edit_url(page, bid)
    await page.goto(edit_url, wait_until="networkidle")
    await page.wait_for_timeout(1000)

    status_select = page.locator('select[name="status"]')
    if await status_select.count() == 0:
        log.warning(f"[PDF] Státusz: select[name=status] nem található az edit oldalon: {bid}")
        return

    current = await status_select.input_value()
    log.info(f"[PDF] Jelenlegi státusz value: {current}")

    await status_select.select_option(label="Elküldve")
    log.info(f"[PDF] Státusz 'Elküldve'-re állítva ({bid})")

    submit = page.locator('button[type="submit"], input[type="submit"]').first
    if await submit.count() > 0:
        await submit.click()
        await page.wait_for_timeout(2000)
        log.info(f"[PDF] Státusz mentés kész ({bid})")
    else:
        await page.evaluate("() => document.querySelector('form') && document.querySelector('form').submit()")
        await page.wait_for_timeout(2000)
        log.info(f"[PDF] Státusz form.submit() JS-sel ({bid})")


async def _run_phase2(job_id: str, custom_message: str):
    """2. fázis: Innonest feltöltés + email + státusz változtatás."""
    job = _get_job(job_id)
    pdf_path = job.get("pdf_path")
    bid = job.get("bid_szam")
    adatok = job.get("_adatok") or {}

    # Innonest feltöltés
    _set_job(job_id, status="uploading_innonest", message="Feltöltés az Innonestbe...")
    innonest_ok = False
    try:
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
            await _upload_pdf_to_innonest(page, pdf_path, bid, adatok.get("ugyfel_nev", ""))
            await browser.close()
        innonest_ok = True
    except Exception as e:
        log.error(f"[PDF] Innonest feltöltés hiba: {e}")

    # Email küldés
    _set_job(job_id, status="sending_email", message="Email küldése az értékesítőnek...")
    email_result = _send_pdf_email_custom(adatok, pdf_path, custom_message)

    # ── ÚJ: Webapp polling visszajelzés ──────────────────────────────────────
    # Ha az email sikeresen elment, tároljuk a proj_id → dátum párost.
    # A webapp 3 másodpercenként lekérdezi a /pdf-kikuld-status/<proj_id>
    # endpointot, és ha done:true-t kap, frissíti a projekt "Ügyfél" státuszát.
    if email_result.get("success"):
        import datetime
        _kikuld_date = datetime.date.today().isoformat()
        _proj_id_ref = job.get("proj_id")
        if _proj_id_ref:
            with _pdf_kikuld_lock:
                _pdf_kikuld_done[_proj_id_ref] = _kikuld_date
            log.info(f"[PDF] Polling visszajelzés tárolva: proj_id={_proj_id_ref}, dátum={_kikuld_date}")

    # Státusz változtatás: Piszkozat → Elküldve (csak ha az email sikeres volt)
    if email_result.get("success"):
        try:
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
                await _set_innonest_status_elkuldve(page, bid)
                await browser.close()
        except Exception as e:
            log.warning(f"[PDF] Státusz változtatás hiba (nem kritikus): {e}")
    else:
        log.info(f"[PDF] Státusz változtatás kihagyva (email sikertelen): {bid}")

    _set_job(
        job_id,
        status="done",
        message="Kész!",
        bid_szam=bid,
        ugyfel_nev=adatok.get("ugyfel_nev"),
        netto_osszesen=adatok.get("netto_osszesen"),
        innonest_ok=innonest_ok,
        email=email_result,
        pdf_path=pdf_path,
    )


async def _run_pipeline_safe(bid: str, job_id: str):
    try:
        await _full_pipeline(bid, job_id)
    except Exception as e:
        log.error(f"[PDF] Hiba ({bid}): {e}")
        log.error(traceback.format_exc())
        _set_job(job_id, status="error", message=str(e))


async def _run_phase2_safe(job_id: str, custom_message: str):
    try:
        await _run_phase2(job_id, custom_message)
    except Exception as e:
        log.error(f"[PDF] 2. fázis hiba: {e}")
        log.error(traceback.format_exc())
        _set_job(job_id, status="error", message=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# FLASK VÉGPONTOK
# ══════════════════════════════════════════════════════════════════════════════

def register_pdf_routes(app):
    """Hívd meg a server.py-ból: register_pdf_routes(app)"""
    _login_attempts = {}  # {ip: [timestamp, ...]} – rate limiting a login endpointhoz

    @app.route("/pdf-tool", methods=["GET"])
    def pdf_tool():
        if not _pdf_tool_auth_ok(request):
            login_html = """<!DOCTYPE html>
<html lang="hu"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SQM PDF generátor – Belépés</title>
<style>
  * { box-sizing:border-box; font-family:'Segoe UI',Arial,sans-serif; margin:0; }
  body { background:#f4f4f4; display:flex; align-items:center; justify-content:center; min-height:100vh; }
  .card { background:#fff; border-radius:12px; box-shadow:0 4px 24px rgba(0,0,0,.08); padding:32px; width:320px; }
  h1 { font-size:17px; color:#2e2c2c; margin-bottom:4px; }
  p { font-size:12px; color:#888; margin-bottom:22px; }
  label { font-size:13px; color:#555; display:block; margin-bottom:6px; }
  input[type=password] { width:100%; padding:9px 11px; border:1px solid #ddd; border-radius:8px; font-size:14px; margin-bottom:14px; }
  input[type=password]:focus { outline:none; border-color:#ffde1d; }
  button { width:100%; padding:11px; background:#2e2c2c; color:#ffde1d; border:none; border-radius:8px; font-size:14px; font-weight:700; cursor:pointer; }
  .err { color:#b3261e; font-size:12px; margin-bottom:10px; display:none; }
</style></head>
<body><div class="card">
  <h1>Árajánlat PDF generátor</h1>
  <p>SQM Hungary – belső eszköz</p>
  <form method="POST" action="/pdf-tool-login">
    <label>Jelszó</label>
    <input type="password" name="pw" autofocus placeholder="••••••••">
    <p class="err" id="err">Hibás jelszó</p>
    <button type="submit">Belépés</button>
  </form>
</div>
<script>
  const p = new URLSearchParams(location.search);
  if (p.get('err')) {
    document.getElementById('err').style.display = 'block';
  }
</script>
</body></html>"""
            return Response(login_html, mimetype="text/html")

        with open(TOOL_PATH, encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")

    @app.route("/pdf-tool-login", methods=["POST"])
    def pdf_tool_login():
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        now = __import__("time").time()
        attempts = _login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < 600]
        if len(attempts) >= 5:
            return Response("Túl sok próbálkozás. Várj 10 percet.", status=429)
        attempts.append(now)
        _login_attempts[ip] = attempts

        pw = (request.form.get("pw") or "").strip()
        if PDF_TOOL_SECRET and pw == PDF_TOOL_SECRET:
            _login_attempts.pop(ip, None)
            resp = redirect("/pdf-tool")
            resp.set_cookie(
                "pdf_tool_auth", PDF_TOOL_SECRET,
                max_age=365 * 24 * 3600,
                httponly=True, samesite="Lax"
            )
            return resp
        return redirect("/pdf-tool?err=1")

    @app.route("/generate-arajanlat-pdf", methods=["POST"])
    def generate_arajanlat_pdf():
        if not _pdf_tool_auth_ok(request):
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json() or {}
        bid = (data.get("bid") or "").strip().upper()
        if not re.match(r"^BID-\d{4}-\d+$", bid):
            return jsonify({"error": "Érvénytelen BID formátum (pl. BID-2026-185)"}), 400
        proj_id = (data.get("proj_id") or "").strip()
        job_id = str(uuid.uuid4())
        _set_job(job_id, status="started", message="Indítás...", bid=bid, proj_id=proj_id)
        asyncio.run_coroutine_threadsafe(_run_pipeline_safe(bid, job_id), _loop)
        return jsonify({"job_id": job_id})

    @app.route("/confirm-arajanlat-pdf/<job_id>", methods=["POST"])
    def confirm_arajanlat_pdf(job_id):
        if not _pdf_tool_auth_ok(request):
            return jsonify({"error": "Unauthorized"}), 401
        job = _get_job(job_id)
        if not job:
            return jsonify({"error": "Ismeretlen job"}), 404
        if job.get("status") != "pdf_ready":
            return jsonify({"error": f"Nem megfelelő állapot: {job.get('status')}"}), 400
        data = request.get_json() or {}
        custom_message = (data.get("custom_message") or "").strip()
        if not custom_message:
            custom_message = _generate_default_message(job.get("_adatok") or {})
        asyncio.run_coroutine_threadsafe(_run_phase2_safe(job_id, custom_message), _loop)
        return jsonify({"ok": True})

    @app.route("/pdf-status/<job_id>", methods=["GET"])
    def pdf_status(job_id):
        if not _pdf_tool_auth_ok(request):
            return jsonify({"error": "Unauthorized"}), 401
        job = _get_job(job_id)
        if not job:
            return jsonify({"error": "Ismeretlen job"}), 404
        safe = {k: v for k, v in job.items() if not k.startswith("_")}
        return jsonify(safe)

    @app.route("/pdf-download/<job_id>", methods=["GET"])
    def pdf_download(job_id):
        if not _pdf_tool_auth_ok(request):
            return jsonify({"error": "Unauthorized"}), 401
        job = _get_job(job_id)
        pdf_path = job.get("pdf_path")
        if not pdf_path or not os.path.exists(pdf_path):
            return jsonify({"error": "PDF nem található"}), 404
        with open(pdf_path, "rb") as f:
            data = f.read()
        dl_name = _pdf_filename(job.get("bid_szam", "arajanlat"), job.get("ugyfel_nev", ""))
        return Response(
            data,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{dl_name}"'},
        )

    @app.route("/pdf-kikuld-status/<proj_id>", methods=["GET"])
    def pdf_kikuld_status(proj_id):
        """Webapp polling: az email elküldése után visszaadja a dátumot (egyszeri)."""
        with _pdf_kikuld_lock:
            date = _pdf_kikuld_done.pop(proj_id, None)
        if date:
            return jsonify({"done": True, "date": date})
        return jsonify({"done": False})

    # ── Pipedrive → webapp import ──────────────────────────────────────────────
    @app.route("/pipedrive-deal-webhook", methods=["POST"], strict_slashes=False)
    def pipedrive_deal_webhook():
        """
        Pipedrive automatizáció hívja meg amikor egy deal az 'Ajánlatra vár'
        stádiumba kerül. Eltárolja az adatokat memóriában, majd visszaírja
        a webapp import URL-t a deal megadott mezőjébe.

        Pipedrive automation beállítása:
          Trigger: Deal stage changed → Ajánlatra vár
          Action:  Send HTTP request
            Method:  POST
            URL:     https://sqm-visszajelzes.up.railway.app/pipedrive-deal-webhook
            Headers: Content-Type: application/json
            Body:    {"deal_id": "{{deal.id}}"}
        """
        data    = request.get_json(silent=True) or {}
        deal_id = data.get("deal_id") or data.get("dealId") or data.get("dealid")

        if not deal_id:
            return jsonify({"error": "deal_id hiányzik a kérés body-jából"}), 400

        try:
            deal_id = int(deal_id)
        except (ValueError, TypeError):
            return jsonify({"error": f"Érvénytelen deal_id: {deal_id}"}), 400

        # 1) Deal adatok kiolvasása Pipedrive-ból
        try:
            deal = _pd_fetch_deal(deal_id)
        except Exception as e:
            log.error(f"[PD] Deal lekérés sikertelen #{deal_id}: {e}")
            return jsonify({"error": f"Pipedrive API hiba: {e}"}), 502

        deal_name = (deal.get("title") or f"Deal #{deal_id}").strip()
        cegnev    = (deal.get("org_name") or "").strip()
        helyszin  = (deal.get(_PD_FIELD_HELYSZIN) or "").strip()

        log.info(f"[PD] Deal #{deal_id}: '{deal_name}' | cég: '{cegnev}' | helyszín: '{helyszin}'")

        # 2) Projekt ID előgenerálás (ugyanaz a formátum mint a webapp JS-ben)
        import random as _random
        _chars = "0123456789abcdefghijklmnopqrstuvwxyz"
        _ts = int(time.time() * 1000)
        _ts36 = ""
        _n = _ts
        while _n:
            _ts36 = _chars[_n % 36] + _ts36
            _n //= 36
        project_id = _ts36 + "".join(_random.choices(_chars, k=4))

        # 3) Token generálás és adatok memóriában tárolása
        token = str(uuid.uuid4()).replace("-", "")
        with _pd_imports_lock:
            _pd_imports[token] = {
                "nev":        deal_name,
                "helyszin":   helyszin,
                "cegnev":     cegnev,
                "deal_id":    deal_id,
                "project_id": project_id,
                "created_at": time.time()
            }

        # 4) Azonnal a végleges projekt URL-t írjuk Pipedrive-ba
        base        = (WEBAPP_BASE_URL or "").rstrip("/")
        project_url = f"{base}?p={project_id}"

        log.info(f"[PD] Deal #{deal_id}: projekt ID={project_id}, URL={project_url}")

        try:
            _pd_write_webapp_url(deal_id, project_url)
        except Exception as e:
            log.warning(f"[PD] URL visszaírás sikertelen: {e}")

        return jsonify({"ok": True, "token": token, "project_url": project_url})


    @app.route("/pipedrive-consume-imports", methods=["POST"])
    def pipedrive_consume_imports():
        """Webapp hívja bejelentkezés után: visszaadja ÉS törli az összes
        függőben lévő Pipedrive importot egyszerre (atomikus)."""
        now = time.time()
        with _pd_imports_lock:
            expired = [k for k, v in _pd_imports.items() if now - v.get("created_at", 0) > 72 * 3600]
            for k in expired:
                del _pd_imports[k]
            result = list(_pd_imports.items())
            _pd_imports.clear()
        imports = [
            {"token": t, "nev": v["nev"], "helyszin": v["helyszin"],
             "cegnev": v["cegnev"], "deal_id": v["deal_id"]}
            for t, v in result
        ]
        log.info(f"[PD] consume-imports: {len(imports)} tétel visszaadva")
        return jsonify({"imports": imports})

    @app.route("/pipedrive-set-project-url", methods=["POST"])
    def pipedrive_set_project_url():
        """Webapp hívja miután létrehozta a projektet: visszaírja a valódi
        projekt URL-t (?p=...) a Pipedrive Kalkulátor URL mezőbe."""
        data       = request.get_json(silent=True) or {}
        deal_id    = data.get("deal_id")
        project_id = data.get("project_id")
        if not deal_id or not project_id:
            return jsonify({"ok": False, "error": "deal_id és project_id szükséges"}), 400
        base        = (WEBAPP_BASE_URL or "").rstrip("/")
        project_url = f"{base}?p={project_id}"
        _pd_write_webapp_url(deal_id, project_url)
        log.info(f"[PD] Projekt URL visszaírva deal #{deal_id}: {project_url}")
        return jsonify({"ok": True, "url": project_url})

    @app.route("/pipedrive-import/<token>", methods=["GET"])
    def pipedrive_import_data(token):
        """
        A webapp hívja meg amikor ?pd_import=<token> URL paraméterrel nyílik meg.
        Egyszeri lekérdezés: visszaadja az adatokat és törli a tokent.
        72 óránál régebbi tokeneket is törli (takarítás).
        """
        now = time.time()
        with _pd_imports_lock:
            # Régi tokenek takarítása (72 óra)
            expired = [k for k, v in _pd_imports.items() if now - v.get("created_at", 0) > 72 * 3600]
            for k in expired:
                del _pd_imports[k]
            entry = _pd_imports.pop(token, None)

        if not entry:
            return jsonify({"error": "Token nem található vagy már felhasználva"}), 404

        return jsonify({
            "ok":      True,
            "nev":     entry["nev"],
            "helyszin": entry["helyszin"],
            "cegnev":  entry["cegnev"],
            "deal_id": entry["deal_id"]
        })

    log.info("[PDF] Végpontok regisztrálva: /pdf-tool, /generate-arajanlat-pdf, /confirm-arajanlat-pdf/<id>, /pdf-status/<id>, /pdf-download/<id>, /pdf-kikuld-status/<proj_id>, /pipedrive-deal-webhook, /pipedrive-import/<token>")
