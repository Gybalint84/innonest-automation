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
  INNONEST_EMAIL/PASSWORD – az innonest_core.py-n keresztüli beléptetéshez

FONTOS — a BID forrása:
  A BID szám NEM szerepel a Billingo számla semelyik mezőjében (megjegyzés,
  tételnév stb.) — ezt élesben leellenőriztük. A BID↔számla összerendelést
  ténylegesen az Innonest "Számlázás" (/invoices) listája tartja nyilván: minden
  sorban ott van a Billingo számlaszám (pl. SQ-2026-80) ÉS a hozzá tartozó
  árajánlat/BID hivatkozás ("[Árajánlat KIV #BID-2026-164]"). Ezért a BID-et
  innen (Playwright-tal, az innonest_core session-jén keresztül) olvassuk ki,
  és a Billingo-szöveges regex-keresés csak tartalék (ha valaki mégis kézzel
  ráírná a BID-et egy számlára).
"""
import os
import re
import time
import calendar
import logging
import requests
from flask import request, jsonify
from playwright.async_api import async_playwright

from innonest_core import run_in_loop, login, load_session, make_browser_args

log = logging.getLogger(__name__)

BILLINGO_BASE     = "https://api.billingo.hu/v3"
BILLINGO_API_KEY  = os.environ.get("BILLINGO_API_KEY", "")
API_KEY           = os.environ.get("API_KEY", "389188")

# BID minta: "BID-2026-255", "BID 2026 255", "BID2026255" stb.
_BID_RE = re.compile(r"BID[-\s/]?\d{2,4}[-\s/]?\d{1,6}", re.IGNORECASE)

# Az Innonest "Számlázás" listasoraiban a hivatkozás mindig
# "[Árajánlat KIV #<ref>]" alakban jelenik meg — <ref> vagy "BID-2026-123",
# vagy régebbi számláknál puszta "2025-108" formátumú.
_INNONEST_REF_RE = re.compile(r"Árajánlat KIV #([^\]]+)\]")

# Egyszerű memória-cache hónaponként (ismételt megnyitás ne hívja újra az API-kat).
_cache = {}
_CACHE_TTL = 600  # 10 perc

# Innonest SQ-szám → BID cache (ritkábban változik, drágább lekérni — Playwright).
_bidmap_cache = {"at": 0, "map": {}}
_BIDMAP_TTL = 1800  # 30 perc


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
    """Nettó összeg védekező kinyerése a Billingo dokumentumból.
    A Billingo API v3 valódi mezőnevei (Document / DocumentSummary modell):
      Document.summary.net_amount   – a számla nettó végösszege
      Document.gross_total          – a számla bruttó végösszege
    (a korábbi total_net/net_total/total_gross kulcsok nem léteznek a sémában —
    ezért ez a két ág korábban sosem talált semmit, csak a tétel-összegzés futott.)
    """
    summ = doc.get("summary") or {}
    for k in ("net_amount", "net_amount_local", "total_net", "net_total"):
        v = summ.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    for k in ("total_net", "net_total"):
        v = doc.get(k)
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
    for k in ("gross_total", "total_gross"):
        g = doc.get(k)
        if isinstance(g, (int, float)):
            return round(float(g) / 1.27, 2)
        g = summ.get("gross_amount_local")
        if isinstance(g, (int, float)):
            return round(float(g) / 1.27, 2)
    return 0.0


def _extract_bid(doc: dict) -> str:
    """Tartalék: ha a BID mégis szerepelne a Billingo számla szövegében
    (megjegyzés/tétel/címke) — normál esetben az Innonest bid-map talál rá előbb."""
    parts = [str(doc.get("comment") or "")]
    tags = doc.get("tags")
    if isinstance(tags, list):
        parts.extend(str(t) for t in tags)
    parts.append(str(doc.get("invoice_number") or ""))
    for it in (doc.get("items") or []):
        parts.append(str(it.get("name") or ""))
        parts.append(str(it.get("comment") or ""))
    m = _BID_RE.search(" ".join(parts))
    return m.group(0).strip() if m else ""


# ══════════════════════════════════════════════════════════════════════════════
# INNONEST "SZÁMLÁZÁS" LISTA — SQ-szám → BID hozzárendelés
# ══════════════════════════════════════════════════════════════════════════════

async def _scrape_innonest_bid_map(max_pages: int = 30) -> dict:
    """Végigmegy az Innonest /invoices listáján (100 sor/oldal), és minden
    Billingo számlaszámhoz (SQ-2026-xx, D-SQ-xx stb.) hozzárendeli a sorban
    szereplő "[Árajánlat KIV #...]" hivatkozást (BID vagy régi puszta szám)."""
    mapping = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        await page.goto("https://app.innonest.hu/invoices", wait_until="networkidle")
        await page.wait_for_timeout(800)
        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu/invoices", wait_until="networkidle")
            await page.wait_for_timeout(800)
            if "login" in page.url:
                raise Exception("Innonest bejelentkezés sikertelen (bid-map lekéréshez)")

        for page_num in range(max_pages):
            offset = page_num * 100
            if offset > 0:
                url = f"https://app.innonest.hu/invoices/index/{offset}/"
                await page.goto(url, wait_until="networkidle")
                await page.wait_for_timeout(800)

            rows = await page.evaluate(
                """
                () => {
                    const out = [];
                    document.querySelectorAll('table tbody tr').forEach(tr => {
                        const linkCell = tr.querySelector('td a');
                        const sq = linkCell ? linkCell.textContent.trim() : '';
                        if (!sq) return;
                        out.push({ sq, text: tr.innerText || '' });
                    });
                    return out;
                }
                """
            )
            if not rows:
                break
            for r in rows:
                m = _INNONEST_REF_RE.search(r.get("text") or "")
                if m:
                    mapping[r["sq"]] = m.group(1).strip()

            has_next = await page.evaluate(
                "() => !!document.querySelector('a[href*=\"/invoices/index/\"]')"
            )
            if not has_next:
                break

        await browser.close()
    log.info(f"[TELJ] Innonest bid-map frissítve: {len(mapping)} számla")
    return mapping


def _get_bid_map() -> dict:
    """Szinkron wrapper — cache-elt Innonest SQ→BID hozzárendelés."""
    now = time.time()
    if now - _bidmap_cache["at"] < _BIDMAP_TTL and _bidmap_cache["map"]:
        return _bidmap_cache["map"]
    try:
        mapping = run_in_loop(_scrape_innonest_bid_map())
        _bidmap_cache["at"] = now
        _bidmap_cache["map"] = mapping
        return mapping
    except Exception as e:
        log.warning(f"[TELJ] Innonest bid-map lekérés hiba: {e} — a korábbi (esetleg üres) cache-t használom")
        return _bidmap_cache["map"]


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
    bid_map = _get_bid_map()  # Innonest SQ→BID (cache-elt, elsődleges forrás)
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
            szamlaszam = full.get("invoice_number") or str(full.get("id") or "")
            bid = bid_map.get(szamlaszam) or _extract_bid(full)
            items.append({
                "szamlaszam": szamlaszam,
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
