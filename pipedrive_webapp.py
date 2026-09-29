"""
pipedrive_webapp.py – Pipedrive → SQM Kalkulátor webapp import pipeline
========================================================================
Amikor egy Pipedrive deal az 'Ajánlatra vár' stádiumba kerül, a Pipedrive
automatizáció meghívja a /pipedrive-deal-webhook endpointot. Ez:
  1. Lekéri a deal + szervezet adatait a Pipedrive API-ból
  2. Eltárolja memóriában (token alapú queue)
  3. Visszaírja a webapp projekt URL-t a deal megadott mezőjébe

A webapp bejelentkezés után lekéri (/pipedrive-consume-imports) és létrehozza
a projektet a kalkuátorban (cégadatok, helyszín, adószám, székhely előtöltve).

Végpontok:
  POST /pipedrive-deal-webhook      – Pipedrive automation hívja
  POST /pipedrive-consume-imports   – webapp hívja bejelentkezés után
  POST /pipedrive-set-project-url   – webapp hívja projekt létrehozás után
  GET  /pipedrive-import/<token>    – egyszeri token alapú lekérdezés
  GET  /pipedrive-import-by-project/<project_id>
                                    – ÁLLAPOTMENTES helyreállítás (2026-09-29)

2026-09-29 — HIBAJAVÍTÁS: elveszett Pipedrive-projektek
-------------------------------------------------------
A függőben lévő importok CSAK memóriában vannak (_pd_imports). Ha a Railway
újraindul/újratelepül, mielőtt valaki megnyitná a webappot, a várólista
elveszik — a Pipedrive-ba viszont a ?p=<project_id> link már be van írva,
így a link egy SOHA LÉTRE NEM JÖTT projektre mutat (élesben: mtwogbdvqwoo,
2026-09-11 10:11, aznap a szerver többször újraindult). Ugyanez történik, ha
a webapp kiürítette a várólistát, de a Firestore-mentés elbukott.

Javítás, két részben:
  1. Az új projekt-azonosító MAGÁBAN HORDOZZA a deal ID-t:
       pd<deal_id>-<ts36><4 véletlen>   pl. pd1234-mtwogbdvqwoo
     Így a linkből bármikor kiderül, melyik dealhez tartozik.
  2. Új végpont: GET /pipedrive-import-by-project/<project_id> — ha a webapp
     egy ?p= linknél nem találja a projektet (és a várólistán sincs), ezt
     hívja. A szerver a deal adatait FRISSEN lekéri a Pipedrive-ból (nincs
     szükség tárolt állapotra), és ugyanabban a formában adja vissza, mint a
     várólista — a webapp ebből, PONTOSAN ezzel az azonosítóval hozza létre a
     projektet. Régi formátumú azonosítóknál (deal ID nélkül, pl.
     mtwogbdvqwoo) a Pipedrive-keresővel keresi meg azt a dealt, amelynek
     Kalkulátor URL mezője ezt az azonosítót tartalmazza.
A memóriabeli várólista maradt (a gyors, szokásos út), csak már nem ez az
egyetlen út.
"""

import os
import time
import uuid
import random
import logging
import threading
import re

import requests
from flask import request, jsonify

log = logging.getLogger(__name__)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
PIPEDRIVE_API_TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN", "")
WEBAPP_BASE_URL     = os.environ.get("WEBAPP_BASE_URL", "https://sqm-hungary.hu/kalkulator/index.html")

# Pipedrive custom field API kulcsok (hardcode)
_PD_FIELD_HELYSZIN   = "7008531d11f5bade385cc7fb72bb2648d4b19137"  # Deal: Kivitelezés helyszíne
_PD_FIELD_WEBAPP_URL = os.environ.get("PD_WEBAPP_URL_FIELD", "")   # Deal: Kalkulátor URL visszaírás
_PD_FIELD_OSSZES_NAP    = "cdf639e8db9018be9f880366a03a17aee38284a3"  # Deal: Kivitelezési napok száma
_PD_FIELD_CONTRACTORS   = "94efcfc331d3531b03786aeb5d4dcc77606398f2"  # Deal: Alvállalkozók
_PD_FIELD_TASK_DETAIL   = "493b30f872256ef6b659c411dc32dbf541ef1d57"  # Deal: Alvállalkozó feladatok részletezése
_PD_FIELD_MATERIALS     = "d0be0839f0e42e84fcc1ed14d3ee5e3b1f0e324b"  # Deal: Megrendelendő anyagok
_PD_ORG_FIELD_ADOSZAM = "f8032f2bfb73caa261e7459ab2224b1a3704a111"  # Org: Adószám

# ── Függőben lévő importok (memória, token → adatok) ─────────────────────────
_pd_imports      = {}
_pd_imports_lock = threading.Lock()


# ── Pipedrive API hívások ─────────────────────────────────────────────────────

def _pd_fetch_deal(deal_id: int) -> dict:
    """Lekéri a deal adatait a Pipedrive API-ból."""
    url = f"https://api.pipedrive.com/v1/deals/{deal_id}"
    r = requests.get(url, params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=10)
    r.raise_for_status()
    return r.json().get("data") or {}


def _pd_fetch_org(org_id: int) -> dict:
    """Lekéri a szervezet adatait a Pipedrive API-ból (cím, egyedi mezők)."""
    url = f"https://api.pipedrive.com/v1/organizations/{org_id}"
    r = requests.get(url, params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=10)
    r.raise_for_status()
    return r.json().get("data") or {}


def _pd_find_deal_by_bid(bid: str) -> int | None:
    """Megkeresi a Pipedrive deal ID-t BID szám alapján (keresés a deal title-ben)."""
    url = "https://api.pipedrive.com/v1/deals/search"
    r = requests.get(url, params={"api_token": PIPEDRIVE_API_TOKEN, "term": bid, "fields": "title", "limit": 5}, timeout=10)
    r.raise_for_status()
    items = (r.json().get("data") or {}).get("items") or []
    for item in items:
        deal = item.get("item") or {}
        if bid.lower() in (deal.get("title") or "").lower():
            return deal.get("id")
    return None


def _pd_update_deal_field(deal_id: int, field_key: str, value) -> None:
    """Általános Pipedrive deal mező frissítő."""
    url = f"https://api.pipedrive.com/v1/deals/{deal_id}"
    r = requests.put(url, params={"api_token": PIPEDRIVE_API_TOKEN},
                     json={field_key: value}, timeout=10)
    r.raise_for_status()


def _pd_write_webapp_url(deal_id: int, webapp_url: str):
    """Visszaírja a webapp projekt URL-t a megadott Pipedrive mezőbe."""
    if not _PD_FIELD_WEBAPP_URL:
        log.info("[PD] PD_WEBAPP_URL_FIELD nincs beállítva – URL visszaírás kihagyva")
        return
    url = f"https://api.pipedrive.com/v1/deals/{deal_id}"
    r = requests.put(
        url,
        params={"api_token": PIPEDRIVE_API_TOKEN},
        json={_PD_FIELD_WEBAPP_URL: webapp_url},
        timeout=10
    )
    r.raise_for_status()
    log.info(f"[PD] URL visszaírva deal #{deal_id}: {webapp_url}")


def _gen_project_id(deal_id=None) -> str:
    """base36 timestamp + 4 véletlen karakter (mint a webapp JS-ben).
    2026-09-29: ha van deal_id, előtagként bekerül (pd<deal_id>-...), hogy a
    projekt a linkből bármikor, tárolt állapot nélkül is helyreállítható
    legyen (lásd /pipedrive-import-by-project)."""
    _chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    ts36, n = "", int(time.time() * 1000)
    while n:
        ts36 = _chars[n % 36] + ts36
        n //= 36
    base = ts36 + "".join(random.choices(_chars, k=4))
    return f"pd{int(deal_id)}-{base}" if deal_id else base


_PD_PROJECT_ID_RE = re.compile(r"^pd(\d+)-[0-9a-z]+$")


def _deal_id_from_project_id(project_id: str):
    """pd<deal_id>-... formátumból a deal ID, különben None."""
    m = _PD_PROJECT_ID_RE.match(project_id or "")
    return int(m.group(1)) if m else None


def _build_import(deal_id: int, project_id: str) -> dict:
    """A deal (+ szervezet) adataiból az import-rekord — ugyanaz a szerkezet,
    amit a várólista is tárol. Kivételt dob, ha a deal nem kérhető le."""
    deal      = _pd_fetch_deal(deal_id)
    deal_name = (deal.get("title") or f"Deal #{deal_id}").strip()
    cegnev    = (deal.get("org_name") or "").strip()
    helyszin  = (deal.get(_PD_FIELD_HELYSZIN) or "").strip()
    adoszam, szekhely = "", ""
    org_ref    = deal.get("org_id")
    org_id_val = (org_ref.get("value") if isinstance(org_ref, dict) else org_ref) if org_ref else None
    if org_id_val:
        try:
            org      = _pd_fetch_org(int(org_id_val))
            szekhely = (org.get("address") or "").strip()
            adoszam  = (org.get(_PD_ORG_FIELD_ADOSZAM) or "").strip()
        except Exception as e:
            log.warning(f"[PD] Szervezet lekérés sikertelen (org #{org_id_val}): {e}")
    return {
        "nev": deal_name, "helyszin": helyszin, "cegnev": cegnev,
        "adoszam": adoszam, "szekhely": szekhely,
        "deal_id": deal_id, "project_id": project_id,
    }


def _find_deal_by_project_id(project_id: str):
    """Régi formátumú azonosítóhoz (deal ID nélkül): megkeresi azt a dealt,
    amelynek Kalkulátor URL mezője ezt a project_id-t tartalmazza. Legjobb
    szándékú keresés — a találatot MINDIG ellenőrizzük a mező tényleges
    értékével, hogy véletlenül se rossz dealhez kössük a projektet."""
    if not _PD_FIELD_WEBAPP_URL:
        return None
    url = "https://api.pipedrive.com/v1/deals/search"
    for term in (project_id, f"p={project_id}"):
        try:
            r = requests.get(url, params={
                "api_token": PIPEDRIVE_API_TOKEN, "term": term,
                "fields": "custom_fields", "limit": 10,
            }, timeout=10)
            r.raise_for_status()
            items = (r.json().get("data") or {}).get("items") or []
        except Exception as e:
            log.warning(f"[PD] Deal-keresés sikertelen ({term}): {e}")
            continue
        for it in items:
            did = (it.get("item") or {}).get("id")
            if not did:
                continue
            try:
                deal = _pd_fetch_deal(int(did))
            except Exception:
                continue
            if project_id in str(deal.get(_PD_FIELD_WEBAPP_URL) or ""):
                return int(did)
    return None


# ── Flask route regisztráció ──────────────────────────────────────────────────

def register_pipedrive_webapp_routes(app):
    """Hívd meg a server.py-ból: register_pipedrive_webapp_routes(app)"""

    @app.route("/pipedrive-deal-webhook", methods=["POST"], strict_slashes=False)
    def pipedrive_deal_webhook():
        """
        Pipedrive automatizáció hívja meg amikor egy deal az 'Ajánlatra vár'
        stádiumba kerül.

        Pipedrive automation beállítása:
          Trigger: Deal stage changed → Ajánlatra vár
          Action:  Send HTTP request
          URL:     https://sqm-visszajelzes.up.railway.app/pipedrive-deal-webhook
          Method:  POST
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

        # 1) Deal + szervezet adatok. A projekt-azonosító már a deal ID-t is
        #    tartalmazza — lásd a fájl eleji 2026-09-29-es megjegyzést.
        project_id = _gen_project_id(deal_id)
        try:
            imp = _build_import(deal_id, project_id)
        except Exception as e:
            log.error(f"[PD] Deal lekérés sikertelen #{deal_id}: {e}")
            return jsonify({"error": f"Pipedrive API hiba: {e}"}), 502

        log.info(f"[PD] Deal #{deal_id}: '{imp['nev']}' | cég: '{imp['cegnev']}' | helyszín: '{imp['helyszin']}' | székhely: '{imp['szekhely']}' | adószám: '{imp['adoszam']}'")

        # 2) Várólistára (gyors út). Ha ez egy újraindulásnál elveszik, a
        #    webapp a /pipedrive-import-by-project végponttal helyreállítja.
        token = str(uuid.uuid4()).replace("-", "")
        with _pd_imports_lock:
            _pd_imports[token] = {**imp, "created_at": time.time()}

        # 4) Webapp projekt URL visszaírása Pipedrive-ba
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
             "cegnev": v["cegnev"], "adoszam": v.get("adoszam", ""),
             "szekhely": v.get("szekhely", ""), "deal_id": v["deal_id"],
             "project_id": v.get("project_id")}
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


    @app.route("/pipedrive-set-contractors", methods=["POST"])
    def pipedrive_set_contractors():
        """Webapp hívja mentéskor: visszaírja az alvállalkozók nevét a deal mezőbe.
        Keresi a dealt: deal_id alapján (ha van), vagy bid alapján."""
        data         = request.get_json(silent=True) or {}
        deal_id      = data.get("deal_id")
        bid          = (data.get("bid") or "").strip()
        contractors  = (data.get("contractors") or "").strip()

        if not contractors:
            return jsonify({"ok": False, "error": "contractors hiányzik"}), 400

        # Deal megkeresése
        resolved_id = None
        if deal_id:
            try:
                resolved_id = int(deal_id)
            except (ValueError, TypeError):
                pass

        if not resolved_id and bid:
            try:
                resolved_id = _pd_find_deal_by_bid(bid)
            except Exception as e:
                log.warning(f"[PD] BID keresés sikertelen ({bid}): {e}")

        if not resolved_id:
            return jsonify({"ok": False, "error": "Nem találtam Pipedrive deal-t (deal_id és BID alapján sem)"}), 404

        try:
            _pd_update_deal_field(resolved_id, _PD_FIELD_CONTRACTORS, contractors)
            log.info(f"[PD] Alvállalkozók visszaírva deal #{resolved_id}: {contractors}")
            return jsonify({"ok": True, "deal_id": resolved_id, "contractors": contractors})
        except Exception as e:
            log.error(f"[PD] Alvállalkozók visszaírás sikertelen deal #{resolved_id}: {e}")
            return jsonify({"ok": False, "error": str(e)}), 502

    @app.route("/pipedrive-set-task-detail", methods=["POST"])
    def pipedrive_set_task_detail():
        """Webapp hívja: alvállalkozónként részletes feladatlista visszaírása."""
        data    = request.get_json(silent=True) or {}
        deal_id = data.get("deal_id")
        bid     = (data.get("bid") or "").strip()
        detail  = (data.get("detail") or "").strip()

        if not detail:
            return jsonify({"ok": False, "error": "detail hiányzik"}), 400

        resolved_id = None
        if deal_id:
            try:
                resolved_id = int(deal_id)
            except (ValueError, TypeError):
                pass
        if not resolved_id and bid:
            try:
                resolved_id = _pd_find_deal_by_bid(bid)
            except Exception as e:
                log.warning(f"[PD] BID keresés sikertelen ({bid}): {e}")

        if not resolved_id:
            return jsonify({"ok": False, "error": "Nem találtam Pipedrive deal-t"}), 404

        try:
            _pd_update_deal_field(resolved_id, _PD_FIELD_TASK_DETAIL, detail)
            log.info(f"[PD] Feladat részletek visszaírva deal #{resolved_id}")
            return jsonify({"ok": True})
        except Exception as e:
            log.error(f"[PD] Feladat részletek visszaírás sikertelen: {e}")
            return jsonify({"ok": False, "error": str(e)}), 502

    @app.route("/pipedrive-set-materials", methods=["POST"])
    def pipedrive_set_materials():
        """Webapp hívja mentéskor: megrendelendő anyagok visszaírása a deal mezőbe."""
        data    = request.get_json(silent=True) or {}
        deal_id = data.get("deal_id")
        bid     = (data.get("bid") or "").strip()
        materials = (data.get("materials") or "").strip()

        if not materials:
            return jsonify({"ok": False, "error": "materials hiányzik"}), 400

        resolved_id = None
        if deal_id:
            try:
                resolved_id = int(deal_id)
            except (ValueError, TypeError):
                pass
        if not resolved_id and bid:
            try:
                resolved_id = _pd_find_deal_by_bid(bid)
            except Exception as e:
                log.warning(f"[PD] BID keresés sikertelen ({bid}): {e}")

        if not resolved_id:
            return jsonify({"ok": False, "error": "Nem találtam Pipedrive deal-t"}), 404

        try:
            _pd_update_deal_field(resolved_id, _PD_FIELD_MATERIALS, materials)
            log.info(f"[PD] Anyagok visszaírva deal #{resolved_id}")
            return jsonify({"ok": True})
        except Exception as e:
            log.error(f"[PD] Anyagok visszaírás sikertelen: {e}")
            return jsonify({"ok": False, "error": str(e)}), 502

    @app.route("/pipedrive-set-days", methods=["POST"])
    def pipedrive_set_days():
        """Webapp hívja mentéskor: visszaírja az összes kivitelezési napot a deal mezőbe."""
        data    = request.get_json(silent=True) or {}
        deal_id = data.get("deal_id")
        days    = data.get("days")
        if not deal_id or days is None:
            return jsonify({"ok": False, "error": "deal_id és days szükséges"}), 400
        try:
            deal_id = int(deal_id)
            days    = int(days)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Érvénytelen deal_id vagy days"}), 400
        # A mezőbe a szám után "munkanap" szöveget is írunk (pl. "5 munkanap").
        # FONTOS: ehhez a Pipedrive mezőnek szöveges (text) típusúnak kell lennie.
        days_text = f"{days} munkanap"
        try:
            url = f"https://api.pipedrive.com/v1/deals/{deal_id}"
            r = requests.put(url, params={"api_token": PIPEDRIVE_API_TOKEN},
                             json={_PD_FIELD_OSSZES_NAP: days_text}, timeout=10)
            r.raise_for_status()
            log.info(f"[PD] Kivitelezési napok visszaírva deal #{deal_id}: {days_text}")
            return jsonify({"ok": True, "deal_id": deal_id, "days": days, "value": days_text})
        except Exception as e:
            log.error(f"[PD] Napok visszaírás sikertelen deal #{deal_id}: {e}")
            return jsonify({"ok": False, "error": str(e)}), 502

    @app.route("/pipedrive-import-by-project/<project_id>", methods=["GET"])
    def pipedrive_import_by_project(project_id):
        """ÁLLAPOTMENTES helyreállítás (2026-09-29): a webapp hívja, ha egy
        ?p=<project_id> linkhez nem talál projektet. A deal adatait frissen
        lekéri a Pipedrive-ból, és import-rekordként adja vissza — a webapp
        ebből ugyanezzel az azonosítóval hozza létre a projektet."""
        project_id = (project_id or "").strip().lower()
        if not re.fullmatch(r"[0-9a-z-]{6,40}", project_id):
            return jsonify({"ok": False, "error": "Érvénytelen projekt-azonosító"}), 400

        # Ha még a memóriabeli várólistán van, azt adjuk (és levesszük róla).
        with _pd_imports_lock:
            for t, v in list(_pd_imports.items()):
                if v.get("project_id") == project_id:
                    _pd_imports.pop(t, None)
                    rec = {k: v.get(k, "") for k in ("nev", "helyszin", "cegnev", "adoszam", "szekhely", "deal_id", "project_id")}
                    log.info(f"[PD] import-by-project: {project_id} a várólistáról")
                    return jsonify({"ok": True, "import": rec})

        deal_id = _deal_id_from_project_id(project_id) or _find_deal_by_project_id(project_id)
        if not deal_id:
            log.warning(f"[PD] import-by-project: nincs deal ehhez: {project_id}")
            return jsonify({"ok": False, "error": "Nem található Pipedrive deal ehhez a projekthez"}), 404
        try:
            rec = _build_import(deal_id, project_id)
        except Exception as e:
            log.error(f"[PD] import-by-project: deal #{deal_id} lekérés sikertelen: {e}")
            return jsonify({"ok": False, "error": f"Pipedrive API hiba: {e}"}), 502
        log.info(f"[PD] import-by-project: {project_id} helyreállítva deal #{deal_id} alapján ('{rec['nev']}')")
        return jsonify({"ok": True, "import": rec})

    @app.route("/pipedrive-import/<token>", methods=["GET"])
    def pipedrive_import_data(token):
        """Egyszeri token alapú lekérdezés (legacy endpoint)."""
        now = time.time()
        with _pd_imports_lock:
            expired = [k for k, v in _pd_imports.items() if now - v.get("created_at", 0) > 72 * 3600]
            for k in expired:
                del _pd_imports[k]
            entry = _pd_imports.pop(token, None)
        if not entry:
            return jsonify({"error": "Token nem található vagy már felhasználva"}), 404
        return jsonify({
            "ok": True, "nev": entry["nev"], "helyszin": entry["helyszin"],
            "cegnev": entry["cegnev"], "deal_id": entry["deal_id"]
        })

    log.info("[PD] Végpontok regisztrálva: /pipedrive-deal-webhook, /pipedrive-consume-imports, /pipedrive-set-project-url, /pipedrive-import/<token>, /pipedrive-import-by-project/<project_id>")
