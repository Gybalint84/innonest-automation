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
"""

import os
import time
import uuid
import random
import logging
import threading

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


def _gen_project_id() -> str:
    """Ugyanaz a formátum mint a webapp JS-ben (base36 timestamp + 4 random char)."""
    _chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    ts36, n = "", int(time.time() * 1000)
    while n:
        ts36 = _chars[n % 36] + ts36
        n //= 36
    return ts36 + "".join(random.choices(_chars, k=4))


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

        # 1) Deal adatok kiolvasása Pipedrive-ból
        try:
            deal = _pd_fetch_deal(deal_id)
        except Exception as e:
            log.error(f"[PD] Deal lekérés sikertelen #{deal_id}: {e}")
            return jsonify({"error": f"Pipedrive API hiba: {e}"}), 502

        deal_name = (deal.get("title") or f"Deal #{deal_id}").strip()
        cegnev    = (deal.get("org_name") or "").strip()
        helyszin  = (deal.get(_PD_FIELD_HELYSZIN) or "").strip()

        # 2) Szervezet adatok (székhely, adószám)
        adoszam  = ""
        szekhely = ""
        org_ref     = deal.get("org_id")
        org_id_val  = (org_ref.get("value") if isinstance(org_ref, dict) else org_ref) if org_ref else None
        if org_id_val:
            try:
                org      = _pd_fetch_org(int(org_id_val))
                szekhely = (org.get("address") or "").strip()
                adoszam  = (org.get(_PD_ORG_FIELD_ADOSZAM) or "").strip()
            except Exception as e:
                log.warning(f"[PD] Szervezet lekérés sikertelen (org #{org_id_val}): {e}")

        log.info(f"[PD] Deal #{deal_id}: '{deal_name}' | cég: '{cegnev}' | helyszín: '{helyszin}' | székhely: '{szekhely}' | adószám: '{adoszam}'")

        # 3) Token generálás és adatok memóriában tárolása
        project_id = _gen_project_id()
        token      = str(uuid.uuid4()).replace("-", "")
        with _pd_imports_lock:
            _pd_imports[token] = {
                "nev":        deal_name,
                "helyszin":   helyszin,
                "cegnev":     cegnev,
                "adoszam":    adoszam,
                "szekhely":   szekhely,
                "deal_id":    deal_id,
                "project_id": project_id,
                "created_at": time.time()
            }

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
        try:
            url = f"https://api.pipedrive.com/v1/deals/{deal_id}"
            r = requests.put(url, params={"api_token": PIPEDRIVE_API_TOKEN},
                             json={_PD_FIELD_OSSZES_NAP: days}, timeout=10)
            r.raise_for_status()
            log.info(f"[PD] Kivitelezési napok visszaírva deal #{deal_id}: {days} nap")
            return jsonify({"ok": True, "deal_id": deal_id, "days": days})
        except Exception as e:
            log.error(f"[PD] Napok visszaírás sikertelen deal #{deal_id}: {e}")
            return jsonify({"ok": False, "error": str(e)}), 502

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

    log.info("[PD] Végpontok regisztrálva: /pipedrive-deal-webhook, /pipedrive-consume-imports, /pipedrive-set-project-url, /pipedrive-import/<token>")
