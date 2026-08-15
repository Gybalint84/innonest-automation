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

ÉLESBEN ELLENŐRIZVE (2026-08-15):
  - Az Innonest /acquisition lista + a sorhoz tartozó "my-modal" AJAX-részlet
    szerkezete élesben feltérképezve és ez alapján implementálva (lásd
    _get_beszerzesi_sorok / _nyisd_meg_reszletek). A "Megrendelt projektek"
    Sheet (webapp_sheet forrás) FONTOS KORLÁTJA: az ÜGYFÉL-oldali
    árajánlat-elfogadásokat rögzíti (megrendeles_figyelő.py tölti), NEM a
    beszállítói/alvállalkozói beszerzési megrendelőket — tehát ez a forrás
    csak akkor releváns, ha a bejövő számla kiállítója maga is egy Innonestes
    BID-hez kötött vevői visszaigazolást jelent, ami a legtöbb beszállítói
    számlánál NEM áll fenn. Érdemes újragondolni, hogy ez a forrás egyáltalán
    releváns-e a beszállítói számla-ellenőrzési láncban.
  - A Pipedrive BID-alapú keresés a /v1/deals/search endpointot használja
    (term = BID szám), és a deal ÖSSZ-értékét veti össze a számlával — ez
    viszont az ÜGYFÉLNEK adott ajánlat értéke, NEM az alvállalkozói költség.
    A pontosabb ellenőrzéshez a deal "Alvállalkozók" / "Alvállalkozó
    feladatok részletezése" egyedi mezőit kellene nézni (lásd
    pipedrive_addon.py alv_bontas_parse()) — ez még nincs bekötve ide.
"""

import os
import re
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

async def _get_beszerzesi_sorok(page) -> list:
    """
    Az /acquisition lista oldalról kinyeri a meglévő beszerzési megrendelők
    ALAP adatait (élesben feltérképezett szerkezet alapján — 2026-08-15-én
    manuálisan ellenőrizve a valódi Innonest felületen):

    Minden sor (table.table-softservice tr) szövege kb. így néz ki:
        KIV
        2026-113
        217 m2 ESD Csúszásmentes ... [Árajánlat KIV #BID-2026-137]   <- tárgy
        Sto Építőanyag Kft.                                          <- beszállító (lista-nézetben)
        0
        0
        2026-08-13 2026-08-10 Összeállítás alatt

    A sorban van egy <a class="my-modal" href=".../worksheets_pdf/open/<id>.html">
    link, amire kattintva egy .modal.in ablak nyílik AJAX-szal — ebben van a
    pontos beszállító név (h3) és a nettó összeg (.details-box.db-m .details-box-text).
    Az összeg a LISTÁBAN NEM látszik, csak a modálban — ezért csak a BID/cégnév
    alapján valószínű jelöltekhez nyitjuk meg a modált (lásd _innonest_ellenorzes_async).
    """
    await page.goto(ACQUISITION_URL, wait_until="networkidle")
    await page.wait_for_timeout(1500)

    sorok_raw = await page.evaluate(
        """
        () => {
            const eredmeny = [];
            document.querySelectorAll('table.table-softservice tr').forEach(tr => {
                const szoveg = (tr.innerText || '').trim();
                if (!szoveg) return;
                const link = tr.querySelector('a.my-modal[href*="worksheets_pdf"]');
                eredmeny.push({
                    szoveg: szoveg,
                    href: link ? link.getAttribute('href') : ''
                });
            });
            return eredmeny;
        }
        """
    )

    tetelek = []
    for sor in sorok_raw:
        szoveg = sor.get("szoveg", "")
        href = sor.get("href", "")
        if not href:
            continue  # nincs megnyitható részlet — nem tudjuk ellenőrizni

        sorok_lista = [s.strip() for s in szoveg.splitlines() if s.strip()]
        # Séma: [0]="KIV" jelölés, [1]=sorszám, [2]=tárgy, [3]=beszállító (lista-nézet), ...
        targya = sorok_lista[2] if len(sorok_lista) > 2 else ""
        beszallito_lista = sorok_lista[3] if len(sorok_lista) > 3 else ""

        bid_match = re.search(r"BID-\s?[0-9]{4}-\s?[0-9]+", targya) or re.search(r"BID-\s?[0-9]{4}-\s?[0-9]+", szoveg)
        bid = re.sub(r"\s", "", bid_match.group(0)) if bid_match else ""

        tetelek.append({
            "bid": bid,
            "targya": targya,
            "beszallito_lista": beszallito_lista,
            "href": href,
        })

    log.info(f"[SZAMLA-ELLENORZO] Innonest /acquisition: {len(tetelek)} sor beolvasva (href-fel)")
    return tetelek


async def _nyisd_meg_reszletek(page, href: str) -> dict:
    """
    A lista-sor 'my-modal' linkjére kattint, kiolvassa a felugró modál pontos
    beszállító nevét (h3) és a nettó összeget (.details-box.db-m .details-box-text),
    majd bezárja a modált (hogy a következő sor is nyitható legyen ugyanazon az oldalon).
    """
    try:
        link = page.locator(f'a.my-modal[href="{href}"]').first
        if await link.count() == 0:
            return {}
        await link.scroll_into_view_if_needed()
        await link.click()
        await page.wait_for_selector(".modal.in", timeout=8000)
        await page.wait_for_timeout(500)

        adat = await page.evaluate(
            """
            () => {
                const modal = document.querySelector('.modal.in');
                if (!modal) return null;
                const h3 = modal.querySelector('h3');
                const nettoBox = modal.querySelector('.details-box.db-m .details-box-text');
                return {
                    beszallito: h3 ? h3.innerText.trim() : '',
                    netto_szoveg: nettoBox ? nettoBox.innerText.trim() : '',
                };
            }
            """
        )

        await page.evaluate(
            """
            () => {
                const btn = document.querySelector('.modal.in .close, .modal.in button[data-dismiss="modal"]');
                if (btn) btn.click();
            }
            """
        )
        await page.wait_for_timeout(400)
        return adat or {}
    except Exception as e:
        log.warning(f"[SZAMLA-ELLENORZO] Beszerzési megrendelő részlet megnyitása hiba ({href}): {e}")
        return {}


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
            await page.goto(ACQUISITION_URL, wait_until="networkidle")
            await page.wait_for_timeout(1000)

        sorok = await _get_beszerzesi_sorok(page)

        # Jelöltek: elsősorban BID-egyezés; ha nincs BID vagy nincs rá találat,
        # a lista-nézeti beszállító-név alapján is próbálkozunk (max 5 jelölt,
        # hogy ne nyissunk meg fölöslegesen sok modált).
        jeloltek = [s for s in sorok if bid and s.get("bid") == bid]
        if not jeloltek and cegnev:
            cn = cegnev.strip().lower()
            jeloltek = [s for s in sorok if cn in s.get("beszallito_lista", "").lower()][:5]

        talalat = None
        for jelolt in jeloltek:
            reszlet = await _nyisd_meg_reszletek(page, jelolt["href"])
            if not reszlet:
                continue
            egyesitett = {**jelolt, **reszlet}
            if cegnev and cegnev.strip().lower() not in egyesitett.get("beszallito", "").lower():
                # Ugyanahhoz a BID-hez több beszállító is tartozhat (pl. anyag +
                # alvállalkozó) — ha ez a sor másik beszállítóé, tovább nézzük a többit.
                if talalat is None:
                    talalat = egyesitett  # tartalék, ha végül nem lesz jobb találat
                continue
            talalat = egyesitett
            break

        await browser.close()

    if not talalat:
        return {"talalat": False, "reszletek": None, "hiba": None}

    netto_szoveg = talalat.get("netto_szoveg", "")
    netto_ertek = None
    m = re.search(r"([\d\s]+)\s*HUF", netto_szoveg)
    if m:
        try:
            netto_ertek = int(m.group(1).replace(" ", ""))
        except Exception:
            pass

    osszeg_egyezik = None
    if osszeg is not None and netto_ertek is not None:
        osszeg_egyezik = (int(osszeg) == netto_ertek)

    cegnev_egyezik = None
    if cegnev:
        cegnev_egyezik = cegnev.strip().lower() in (talalat.get("beszallito", "") or "").lower()

    return {
        "talalat": True,
        "reszletek": {
            "bid": talalat.get("bid"),
            "beszallito": talalat.get("beszallito"),
            "osszeg": netto_ertek,
            "penznem": "HUF",
            "osszeg_egyezik": osszeg_egyezik,
            "cegnev_egyezik": cegnev_egyezik,
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
    """
    A BID alapján megkeresi a Pipedrive dealt, majd az "Alvállalkozó feladatok
    részletezése" egyedi mezőt (PIPEDRIVE_TASKDETAIL_FIELD) elemzi a
    pipedrive_addon.py-ban már meglévő alv_bontas_parse()-szal — ez adja meg,
    melyik alvállalkozóra mennyi költség lett rögzítve az adott projekten.
    FONTOS: NEM a deal teljes értékét (az ügyfélnek adott árat) hasonlítjuk
    össze a számlával, hanem a cégnévhez tartozó alvállalkozói részösszeget,
    ha az szerepel a bontásban.
    """
    if not bid:
        return {"talalat": False, "reszletek": None, "hiba": "Nincs BID szám, nem kereshető"}
    try:
        from pipedrive_addon import alv_bontas_parse, PIPEDRIVE_TASKDETAIL_FIELD

        r = requests.get(
            "https://api.pipedrive.com/v1/deals/search",
            params={"api_token": PIPEDRIVE_API_TOKEN, "term": bid, "fields": "custom_fields", "exact_match": "true"},
            timeout=15,
        )
        data = r.json()
        talalatok = ((data.get("data") or {}).get("items")) or []
        if not talalatok:
            return {"talalat": False, "reszletek": None, "hiba": None}

        deal_id = talalatok[0].get("item", {}).get("id")
        deal_r = requests.get(
            f"https://api.pipedrive.com/v1/deals/{deal_id}",
            params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=15,
        )
        deal_data = deal_r.json()
        deal = deal_data.get("data") or {}

        alv_raw = str(deal.get(PIPEDRIVE_TASKDETAIL_FIELD) or "").strip()
        alv_groups = alv_bontas_parse(alv_raw) if alv_raw else {}

        talalt_alv = None
        if cegnev:
            cn = cegnev.strip().lower()
            for nev, adat in alv_groups.items():
                if cn in nev.lower() or nev.lower() in cn:
                    talalt_alv = (nev, adat)
                    break

        if talalt_alv:
            nev, adat = talalt_alv
            osszesen_szam = None
            m = re.search(r"([\d\s]+)", adat.get("osszesen", ""))
            if m:
                try:
                    osszesen_szam = int(m.group(1).replace(" ", ""))
                except Exception:
                    pass
            osszeg_egyezik = None
            if osszeg is not None and osszesen_szam is not None:
                osszeg_egyezik = (int(osszeg) == osszesen_szam)
            return {
                "talalat": True,
                "reszletek": {
                    "deal_id": deal_id,
                    "deal_nev": deal.get("title"),
                    "alvallalkozo": nev,
                    "alvallalkozo_osszesen": osszesen_szam,
                    "osszeg_egyezik": osszeg_egyezik,
                },
                "hiba": None,
            }

        # A deal létezik, de a cégnév nem szerepel az alvállalkozói bontásban —
        # ez önmagában GYANÚS jelnek számít (a Cowork-oldal döntse el, hogyan
        # kezeli), de technikai hibának nem tekintjük.
        return {
            "talalat": True,
            "reszletek": {
                "deal_id": deal_id,
                "deal_nev": deal.get("title"),
                "alvallalkozo": None,
                "megjegyzes": "A cégnév nem szerepel az alvállalkozói feladatbontásban ezen a dealen.",
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
