"""
szamla_ellenorzo.py – Bejövő számla-ellenőrző Agent (Railway-oldali szolgáltatás)
==================================================================================
Ez a modul NEM dönt és NEM ír email-szöveget — a felismerést, az adatkinyerést,
a hangnem-választást és a döntéshozatalt a Cowork-oldali (Claude) ütemezett
feladat végzi. Ez a Railway-oldal a "kéz és láb": rendszer-lekérdezéseket végez
(Innonest, Pipedrive, "Megrendelt projektek" Sheet), állapotot tárol
(checkpoint, függőben lévő esetek), és a jóváhagyott emaileket ténylegesen
kiküldi a meglévő Gmail-proxyn keresztül.

Végpontok:
  GET  /invoice-checkpoint          – utolsó Gmail-ellenőrzés időpontja
  POST /invoice-checkpoint          – checkpoint frissítése
  POST /check-invoice               – 3 forrás lekérdezése (Innonest, Pipedrive, Sheet)
  POST /invoice-case                – új "függőben lévő eset" létrehozása (gyanús számla)
  GET  /invoice-case/<case_id>      – egy eset adatainak lekérdezése
  POST /invoice-resolve             – eset lezárása: email kiküldése vagy nem

A server.py-ban:
    from szamla_ellenorzo import register_szamla_routes
    register_szamla_routes(app)

Környezeti változók (a meglévőkön felül nem igényel újat):
    API_KEY              – ugyanaz, mint a többi végpontnál (X-API-Key header)
    PIPEDRIVE_API_TOKEN, PIPEDRIVE_BID_FIELD_KEY – ugyanaz, mint pipedrive_addon.py
    WEBAPP_URL, WEBAPP_SECRET       – a "Megrendelt projektek" webapp (webapp_v8.js)
    EMAIL_WEBAPP_URL, EMAIL_WEBAPP_SECRET – a különálló email-küldő webapp

ÉLES ELLENŐRZÉS SZÜKSÉGES:
  - Az Innonest /acquisition (beszerzési megrendelők) lista oldal pontos
    táblázat-szerkezete nincs feltérképezve (csak az /acquisition/add
    kitöltő oldal). A get_beszerzesi_megrendelok() ezért ugyanazt az
    "univerzális sor-scraping" technikát használja, mint a
    megrendeles_figyelő.py get_megrendelt_tetelek()-je — élesben
    valószínűleg finomítani kell a szelektorokat/regexeket.
  - A Pipedrive BID-alapú keresés a /v1/deals/search endpointot használja
    (term = BID szám) — ez sosem lett még élesben tesztelve ebben a
    projektben, érdemes 1-2 valós BID-del ellenőrizni.
"""

import os
import json
import time
import uuid
import logging
import datetime

import requests
from playwright.async_api import async_playwright

from innonest_core import run_in_loop, login, load_session, make_browser_args
from flask import request, jsonify

log = logging.getLogger(__name__)

# ── Konfiguráció ────────────────────────────────────────────────────────────
API_KEY              = os.environ.get("API_KEY", "titkos-kulcs")
PIPEDRIVE_API_TOKEN  = os.environ.get("PIPEDRIVE_API_TOKEN", "")
PIPEDRIVE_BID_FIELD  = os.environ.get("PIPEDRIVE_BID_FIELD_KEY", "")

WEBAPP_URL           = os.environ.get(
    "WEBAPP_URL",
    "https://script.google.com/macros/s/AKfycbyy1PQmHyBSlnWpXQR9bygVfFV_g2gJI9_7UjDI5zHm2xXElIX1DvsszM_UJu8l7too/exec"
)
WEBAPP_SECRET        = os.environ.get("WEBAPP_SECRET", "")

EMAIL_WEBAPP_URL     = os.environ.get("EMAIL_WEBAPP_URL", "")
EMAIL_WEBAPP_SECRET  = os.environ.get("EMAIL_WEBAPP_SECRET", "")

CHECKPOINT_FILE = "/tmp/szamla_checkpoint.json"
ESETEK_FILE     = "/tmp/szamla_esetek.json"

ACQUISITION_URL = "https://app.innonest.hu/acquisition"


# ══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT – az utolsó Gmail-ellenőrzés időpontja
# ══════════════════════════════════════════════════════════════════════════════

def _load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_check": None}


def _save_checkpoint(last_check: str):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({"last_check": last_check}, f)


# ══════════════════════════════════════════════════════════════════════════════
# ESETEK – gyanús számlák, amikre válasz/döntés vár
# Formátum: { case_id: { ...adatok..., "resolved": bool, "resolution": {...} } }
# ══════════════════════════════════════════════════════════════════════════════

def _load_esetek() -> dict:
    if os.path.exists(ESETEK_FILE):
        try:
            with open(ESETEK_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_esetek(esetek: dict):
    with open(ESETEK_FILE, "w") as f:
        json.dump(esetek, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# 1. FORRÁS – INNONEST BESZERZÉSI MEGRENDELŐK
# ══════════════════════════════════════════════════════════════════════════════

async def _get_beszerzesi_megrendelok(page) -> list:
    """
    Az /acquisition lista oldalról kinyeri a meglévő beszerzési megrendelőket.
    Univerzális sor-scraping (ua. technika, mint megrendeles_figyelő.py
    get_megrendelt_tetelek()-je) — ÉLES ELLENŐRZÉS SZÜKSÉGES, lásd fájl fejléc.
    """
    import re

    await page.goto(ACQUISITION_URL, wait_until="networkidle")
    await page.wait_for_timeout(1500)

    sorok_raw = await page.evaluate(
        "() => { "
        "  var eredmeny = []; "
        "  var sorok = document.querySelectorAll('tr, .list-item'); "
        "  sorok.forEach(function(sor) { "
        "    var szoveg = sor.innerText || ''; "
        "    var linkek = []; "
        "    var as = sor.querySelectorAll('a[href]'); "
        "    for (var i=0; i<as.length; i++) { linkek.push(as[i].getAttribute('href') || ''); } "
        "    eredmeny.push({szoveg: szoveg, link: linkek[0] || ''}); "
        "  }); "
        "  return eredmeny; "
        "}"
    )

    tetelek = []
    for sor in sorok_raw:
        szoveg = sor.get("szoveg", "")
        if not szoveg or len(szoveg.strip()) < 3:
            continue

        bid_match = re.search(r"BID-[0-9]{4}-[0-9]+", szoveg)
        bid = bid_match.group(0) if bid_match else ""

        osszeg = None
        penznem = "HUF"
        penz_m = re.search(r"([0-9][0-9 ]{2,}[0-9])\s*(HUF|EUR|USD|GBP|CHF)", szoveg)
        if penz_m:
            try:
                osszeg = int(penz_m.group(1).replace(" ", ""))
                penznem = penz_m.group(2)
            except Exception:
                pass

        sorok_lista = [s.strip() for s in szoveg.splitlines() if s.strip()]
        cegnev_jeloltek = [
            s for s in sorok_lista
            if not re.match(r"^\d{4}-\d{2}-\d{2}", s)
            and not re.search(r"HUF|EUR|USD|GBP|CHF", s)
            and not re.match(r"^[\d\s\.,]+$", s)
            and len(s) > 3
        ]

        tetelek.append({
            "bid": bid,
            "cegnev_jeloltek": cegnev_jeloltek,
            "osszeg": osszeg,
            "penznem": penznem,
            "nyers_szoveg": szoveg[:300],
            "link": sor.get("link", ""),
        })

    log.info(f"[SZAMLA-ELLENORZO] Innonest /acquisition: {len(tetelek)} sor beolvasva")
    return tetelek


async def _innonest_ellenorzes_async(bid: str, cegnev: str, osszeg) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        await page.goto(ACQUISITION_URL, wait_until="networkidle")
        await page.wait_for_timeout(1000)
        if "login" in page.url:
            await login(page)

        tetelek = await _get_beszerzesi_megrendelok(page)
        await browser.close()

    talalat = None
    for t in tetelek:
        if bid and t.get("bid") == bid:
            talalat = t
            break
    if not talalat and cegnev:
        for t in tetelek:
            if any(cegnev.strip().lower() in c.lower() for c in t.get("cegnev_jeloltek", [])):
                talalat = t
                break

    if not talalat:
        return {"talalat": False, "reszletek": None, "hiba": None}

    osszeg_egyezik = None
    if osszeg is not None and talalat.get("osszeg") is not None:
        osszeg_egyezik = (int(osszeg) == int(talalat["osszeg"]))

    return {
        "talalat": True,
        "reszletek": {
            "bid": talalat.get("bid"),
            "osszeg": talalat.get("osszeg"),
            "penznem": talalat.get("penznem"),
            "osszeg_egyezik": osszeg_egyezik,
            "nyers_szoveg": talalat.get("nyers_szoveg"),
        },
        "hiba": None,
    }


def innonest_ellenorzes(bid: str, cegnev: str, osszeg) -> dict:
    try:
        return run_in_loop(_innonest_ellenorzes_async(bid, cegnev, osszeg))
    except Exception as e:
        log.error(f"[SZAMLA-ELLENORZO] Innonest ellenőrzés hiba: {e}")
        return {"talalat": None, "reszletek": None, "hiba": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# 2. FORRÁS – PIPEDRIVE DEAL (BID alapján)
# ══════════════════════════════════════════════════════════════════════════════

def pipedrive_ellenorzes(bid: str, cegnev: str, osszeg) -> dict:
    if not bid:
        return {"talalat": False, "reszletek": None, "hiba": "Nincs BID szám, nem kereshető"}
    try:
        r = requests.get(
            "https://api.pipedrive.com/v1/deals/search",
            params={"api_token": PIPEDRIVE_API_TOKEN, "term": bid, "fields": "custom_fields", "exact_match": "true"},
            timeout=15,
        )
        data = r.json()
        talalatok = ((data.get("data") or {}).get("items")) or []
        if not talalatok:
            return {"talalat": False, "reszletek": None, "hiba": None}

        deal = talalatok[0].get("item", {})
        deal_ertek = deal.get("value")
        osszeg_egyezik = None
        if osszeg is not None and deal_ertek is not None:
            try:
                osszeg_egyezik = (int(osszeg) == int(float(deal_ertek)))
            except Exception:
                osszeg_egyezik = None

        return {
            "talalat": True,
            "reszletek": {
                "deal_id": deal.get("id"),
                "deal_nev": deal.get("title"),
                "deal_ertek": deal_ertek,
                "osszeg_egyezik": osszeg_egyezik,
                "status": deal.get("status"),
            },
            "hiba": None,
        }
    except Exception as e:
        log.error(f"[SZAMLA-ELLENORZO] Pipedrive ellenőrzés hiba: {e}")
        return {"talalat": None, "reszletek": None, "hiba": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# 3. FORRÁS – "Megrendelt projektek" SHEET (webapp_v8.js checkOrder akció)
# ══════════════════════════════════════════════════════════════════════════════

def webapp_sheet_ellenorzes(bid: str, cegnev: str, osszeg) -> dict:
    if not bid:
        return {"talalat": False, "reszletek": None, "hiba": "Nincs BID szám, nem kereshető"}
    try:
        r = requests.post(
            WEBAPP_URL,
            json={"secret": WEBAPP_SECRET, "action": "checkOrder", "bid": bid},
            timeout=30,
        )
        data = r.json()
        if data.get("error"):
            return {"talalat": None, "reszletek": None, "hiba": data["error"]}
        if not data.get("found"):
            return {"talalat": False, "reszletek": None, "hiba": None}

        sheet_osszeg = data.get("netto")
        osszeg_egyezik = None
        if osszeg is not None and sheet_osszeg not in (None, ""):
            try:
                osszeg_egyezik = (int(osszeg) == int(float(sheet_osszeg)))
            except Exception:
                osszeg_egyezik = None

        return {
            "talalat": True,
            "reszletek": {
                "cegnev": data.get("cegnev"),
                "osszeg": sheet_osszeg,
                "penznem": data.get("penznem"),
                "osszeg_egyezik": osszeg_egyezik,
            },
            "hiba": None,
        }
    except Exception as e:
        log.error(f"[SZAMLA-ELLENORZO] Webapp/Sheet ellenőrzés hiba: {e}")
        return {"talalat": None, "reszletek": None, "hiba": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL KIKÜLDÉS – jóváhagyott döntés végrehajtása
# (a szöveget/hangnemet a Cowork-oldal állítja elő, ez a függvény csak küld)
# ══════════════════════════════════════════════════════════════════════════════

def _email_kuld(cimzett: str, targy: str, html_body: str) -> bool:
    try:
        r = requests.post(EMAIL_WEBAPP_URL, json={
            "secret": EMAIL_WEBAPP_SECRET,
            "action": "sendEmail",
            "to": cimzett,
            "subject": targy,
            "htmlBody": html_body,
        }, timeout=30)
        resp = r.json()
        if resp.get("success"):
            log.info(f"[SZAMLA-ELLENORZO] Email kiküldve → {cimzett}")
            return True
        log.error(f"[SZAMLA-ELLENORZO] Email küldés Apps Script hiba: {resp}")
        return False
    except Exception as e:
        log.error(f"[SZAMLA-ELLENORZO] Email küldés hiba: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# FLASK VÉGPONTOK
# ══════════════════════════════════════════════════════════════════════════════

def register_szamla_routes(app):
    """Hívd meg a server.py-ból: register_szamla_routes(app)"""

    def _auth_ok():
        return request.headers.get("X-API-Key") == API_KEY

    # ── Checkpoint ──────────────────────────────────────────────────────────
    @app.route("/invoice-checkpoint", methods=["GET"])
    def get_invoice_checkpoint():
        if not _auth_ok():
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify(_load_checkpoint())

    @app.route("/invoice-checkpoint", methods=["POST"])
    def set_invoice_checkpoint():
        if not _auth_ok():
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        last_check = data.get("last_check")
        if not last_check:
            return jsonify({"error": "Hiányzó last_check mező"}), 400
        _save_checkpoint(last_check)
        return jsonify({"ok": True})

    # ── Ellenőrzés (3 forrás) ───────────────────────────────────────────────
    @app.route("/check-invoice", methods=["POST"])
    def check_invoice():
        if not _auth_ok():
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        bid     = str(data.get("bid") or "").strip()
        cegnev  = str(data.get("cegnev") or "").strip()
        osszeg  = data.get("osszeg")

        if not bid and not cegnev:
            return jsonify({"error": "Legalább a BID vagy a cégnév megadása kötelező"}), 400

        log.info(f"[SZAMLA-ELLENORZO] /check-invoice: bid={bid!r} cegnev={cegnev!r} osszeg={osszeg!r}")

        eredmeny = {
            "innonest":     innonest_ellenorzes(bid, cegnev, osszeg),
            "pipedrive":    pipedrive_ellenorzes(bid, cegnev, osszeg),
            "webapp_sheet": webapp_sheet_ellenorzes(bid, cegnev, osszeg),
        }
        return jsonify(eredmeny)

    # ── Függőben lévő eset létrehozása ──────────────────────────────────────
    @app.route("/invoice-case", methods=["POST"])
    def create_invoice_case():
        if not _auth_ok():
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}

        case_id = uuid.uuid4().hex[:12]
        esetek = _load_esetek()
        esetek[case_id] = {
            **data,
            "case_id": case_id,
            "created_at": datetime.datetime.utcnow().isoformat() + "Z",
            "resolved": False,
            "resolution": None,
        }
        _save_esetek(esetek)
        log.info(f"[SZAMLA-ELLENORZO] Új eset létrehozva: {case_id}")
        return jsonify({"case_id": case_id})

    # ── Eset lekérdezése ─────────────────────────────────────────────────────
    @app.route("/invoice-case/<case_id>", methods=["GET"])
    def get_invoice_case(case_id):
        if not _auth_ok():
            return jsonify({"error": "Unauthorized"}), 401
        esetek = _load_esetek()
        eset = esetek.get(case_id)
        if not eset:
            return jsonify({"error": "Ismeretlen case_id"}), 404
        return jsonify(eset)

    # ── Eset lezárása (email kiküldése vagy nem) ────────────────────────────
    @app.route("/invoice-resolve", methods=["POST"])
    def resolve_invoice_case():
        if not _auth_ok():
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        case_id = data.get("case_id")
        action  = data.get("action")  # "send_email" | "no_email"

        esetek = _load_esetek()
        eset = esetek.get(case_id)
        if not eset:
            return jsonify({"error": "Ismeretlen case_id"}), 404
        if eset.get("resolved"):
            return jsonify({"ok": True, "message": "Ez az eset már le volt zárva.", "already_resolved": True})

        email_kuldve = False
        if action == "send_email":
            email_to  = data.get("email_to")
            subject   = data.get("subject")
            html_body = data.get("html_body")
            if not (email_to and subject and html_body):
                return jsonify({"error": "send_email esetén email_to, subject és html_body kötelező"}), 400
            email_kuldve = _email_kuld(email_to, subject, html_body)
            if not email_kuldve:
                return jsonify({"error": "Az email küldése nem sikerült"}), 502

        eset["resolved"] = True
        eset["resolution"] = {
            "action": action,
            "email_kuldve": email_kuldve,
            "resolved_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        esetek[case_id] = eset
        _save_esetek(esetek)

        log.info(f"[SZAMLA-ELLENORZO] Eset lezárva: {case_id} ({action}, email_kuldve={email_kuldve})")
        return jsonify({"ok": True, "email_kuldve": email_kuldve})

    log.info("[SZAMLA-ELLENORZO] Végpontok regisztrálva: /check-invoice, /invoice-checkpoint, "
             "/invoice-case, /invoice-resolve")
