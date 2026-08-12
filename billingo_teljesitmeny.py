# -*- coding: utf-8 -*-
"""
Értékesítői teljesítmény — a Billingo-ban kiállított számlák (adott hónap) össze-
gyűjtése, és a számlán szereplő BID alapján a Pipedrive üzlet TULAJDONOSÁNAK
(owner) e-mail címéhez rendelése. A webapp ez alapján rendeli értékesítőhöz
(e-mail egyezéssel a felhasználó-regiszterrel).

Végpont:  GET /ertekesito-teljesitmeny?month=YYYY-MM
Fejléc:   X-API-Key: <Railway API_KEY>
Válasz:   { ok, month, items: [ {szamlaszam, datum, partner, netto, bid, ownerEmail} ] }

Környezeti változók (Railway):
  BILLINGO_API_KEY    – a Billingo v3 API kulcs (X-API-KEY fejléchez)
  API_KEY             – a webapp↔Railway közös kulcs (mint a többi végpontnál)
  PIPEDRIVE_API_TOKEN – a Pipedrive lekérésekhez (a pipedrive_webapp is ezt használja)

Megjegyzés: a Billingo mezőnevek védekezően vannak kezelve (nettó: summary/tétel/
bruttó-visszaszámolás; BID: a számla megjegyzés + tételnevek szövegében keresve).
Éles adaton finomítható, ha a tényleges mezők eltérnek.
"""
import os
import re
import time
import calendar
import logging
import requests
from flask import request, jsonify

log = logging.getLogger(__name__)

BILLINGO_BASE     = "https://api.billingo.hu/v3"
BILLINGO_API_KEY  = os.environ.get("BILLINGO_API_KEY", "")
API_KEY           = os.environ.get("API_KEY", "389188")

# BID minta: "BID-2026-255", "BID 2026 255", "BID2026255" stb.
_BID_RE = re.compile(r"BID[-\s/]?\d{2,4}[-\s/]?\d{1,6}", re.IGNORECASE)

# Egyszerű memória-cache hónaponként (ismételt megnyitás ne hívja újra az API-kat).
_cache = {}
_CACHE_TTL = 600  # 10 perc


def _month_range(month: str):
    y, m = (int(x) for x in month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def _billingo_get(path: str, params: dict = None) -> dict:
    r = requests.get(
        BILLINGO_BASE + path,
        headers={"X-API-KEY": BILLINGO_API_KEY, "Accept": "application/json"},
        params=params or {},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _doc_net(doc: dict) -> float:
    """Nettó összeg védekező kinyerése a Billingo dokumentumból."""
    for k in ("total_net", "net_total"):
        v = doc.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    summ = doc.get("summary") or {}
    for k in ("total_net", "net"):
        v = summ.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    # Tételekből
    net, found = 0.0, False
    for it in (doc.get("items") or []):
        v = it.get("net_amount")
        if isinstance(v, (int, float)):
            net += float(v); found = True; continue
        vu = it.get("net_unit_amount")
        if isinstance(vu, (int, float)):
            net += float(vu) * float(it.get("quantity") or 1); found = True
    if found:
        return round(net, 2)
    # Végső tartalék: bruttó / 1.27
    g = doc.get("total_gross")
    if isinstance(g, (int, float)):
        return round(float(g) / 1.27, 2)
    return 0.0


def _extract_bid(doc: dict) -> str:
    parts = [str(doc.get("comment") or "")]
    for it in (doc.get("items") or []):
        parts.append(str(it.get("name") or ""))
        parts.append(str(it.get("comment") or ""))
    m = _BID_RE.search(" ".join(parts))
    return m.group(0).strip() if m else ""


def _owner_email_for_bid(bid: str, cache: dict) -> str:
    """A BID-hez tartozó Pipedrive üzlet tulajdonosának e-mailje (email-egyezéshez)."""
    if not bid:
        return ""
    if bid in cache:
        return cache[bid]
    email = ""
    try:
        from pipedrive_webapp import _pd_find_deal_by_bid, _pd_fetch_deal
        did = _pd_find_deal_by_bid(bid)
        if did:
            deal = _pd_fetch_deal(did)
            u = deal.get("user_id")
            if isinstance(u, dict):
                email = (u.get("email") or "").strip().lower()
    except Exception as e:
        log.warning(f"[TELJ] owner lekérés hiba (BID {bid}): {e}")
    cache[bid] = email
    return email


def _collect_month(month: str) -> list:
    start, end = _month_range(month)
    items, owner_cache = [], {}
    page = 1
    while True:
        data = _billingo_get("/documents", {
            "start_date": start, "end_date": end, "per_page": 100, "page": page,
        })
        docs = data.get("data") or []
        for d in docs:
            # Csak (nem sztornózott) számlák.
            if (str(d.get("type") or "").lower() not in ("invoice", "")):
                continue
            if d.get("cancelled"):
                continue
            # Ha a lista-nézetben nincs tétel/megjegyzés, a részletből pótoljuk.
            full = d
            if not d.get("items") and not d.get("comment"):
                try:
                    full = _billingo_get(f"/documents/{d.get('id')}")
                except Exception:
                    full = d
            bid = _extract_bid(full)
            items.append({
                "szamlaszam": full.get("invoice_number") or str(full.get("id") or ""),
                "datum":      full.get("invoice_date") or "",
                "partner":    ((full.get("partner") or {}).get("name")) or "",
                "netto":      _doc_net(full),
                "bid":        bid,
                "ownerEmail": _owner_email_for_bid(bid, owner_cache),
            })
        if len(docs) < 100:
            break
        page += 1
        if page > 30:  # biztonsági korlát
            break
    return items


def register_teljesitmeny_routes(app):
    """Hívd meg a server.py-ból: register_teljesitmeny_routes(app)"""

    @app.route("/ertekesito-teljesitmeny", methods=["GET"], strict_slashes=False)
    def ertekesito_teljesitmeny():
        if request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if not BILLINGO_API_KEY:
            return jsonify({"ok": False, "error": "BILLINGO_API_KEY nincs beállítva a szerveren"}), 500

        month = (request.args.get("month") or "").strip()
        if not re.match(r"^\d{4}-\d{2}$", month):
            return jsonify({"ok": False, "error": "month=YYYY-MM formátum szükséges"}), 400

        # Cache
        now = time.time()
        c = _cache.get(month)
        if c and now - c["at"] < _CACHE_TTL:
            return jsonify({"ok": True, "month": month, "items": c["items"], "cached": True})

        try:
            items = _collect_month(month)
        except requests.HTTPError as e:
            return jsonify({"ok": False, "error": f"Billingo API hiba: {e}"}), 502
        except Exception as e:
            log.error(f"[TELJ] hiba ({month}): {e}")
            return jsonify({"ok": False, "error": f"Hiba: {e}"}), 500

        _cache[month] = {"at": now, "items": items}
        return jsonify({"ok": True, "month": month, "items": items})

    log.info("[TELJESITMENY] Végpont regisztrálva: /ertekesito-teljesitmeny")
