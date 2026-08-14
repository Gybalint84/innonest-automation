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

FONTOS — a BID forrása (2026-08-14, Billingo-váltás után):
  A BID szám NEM szerepel a Billingo számla mezőiben, és az Innonest "Számlázás"
  (/invoices) lista a Billingo-váltás óta ÜRES ("Ez a modul a Számlázz.hu-val
  működik!") — tehát a korábbi közvetlen számlaszám→BID lista megszűnt.
  Ami viszont megvan: az Innonest MEGRENDELŐLAPOK (/ordersheets) listája,
  ahol minden sor tartalmazza az "[Árajánlat KIV #BID-2026-xxx]" hivatkozást,
  az ügyfél nevét ÉS a pontos nettó összeget devizával. A számlát mindig a
  megrendelőlap alapján állítjuk ki, ugyanarra az összegre és ugyanannak az
  ügyfélnek — ezért a párosítás: Billingo számla → megrendelőlap egyeztetés
  (nettó összeg + deviza + ügyfélnév + dátumközelség) → BID.
  Élesben ellenőrizve: SQ-2026-88 (7 HÁZ BT., 4 528 428 HUF) ↔ KIV 2026-84
  (7 HÁZ BT., 4 528 428 HUF, BID-2026-257) — pontos találat.
  A Billingo-szöveges regex-keresés tartalék marad: ha a jövőben a számlára
  (megjegyzésbe/tételnévbe) rákerül a BID, azt automatikusan felismeri.
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

# Az Innonest megrendelőlap-sorokban a hivatkozás mindig
# "[Árajánlat KIV #<ref>]" alakban jelenik meg — <ref> vagy "BID-2026-123",
# vagy régebbi tételeknél puszta "2025-108" formátumú.
_INNONEST_REF_RE = re.compile(r"Árajánlat KIV #([^\]]+)\]")
_AMOUNT_RE       = re.compile(r"([\d][\d \u00a0]*(?:,\d{1,2})?)[ \u00a0]*(HUF|EUR)")
_DATE_RE         = re.compile(r"\d{4}-\d{2}-\d{2}")


def _amount_to_float(s: str) -> float:
    """'5 454 961' / '6 049 192,10' (nem törő szóközökkel is) → float."""
    s = re.sub(r"[\s ]", "", s or "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0

# Egyszerű memória-cache hónaponként (ismételt megnyitás ne hívja újra az API-kat).
_cache = {}
_CACHE_TTL = 600  # 10 perc

# Innonest megrendelőlap-lista cache (ritkábban változik, drágább lekérni — Playwright).
_sheets_cache = {"at": 0, "sheets": []}
_SHEETS_TTL = 1800  # 30 perc


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
# INNONEST MEGRENDELŐLAPOK — a Billingo számlák BID-párosításának forrása
# ══════════════════════════════════════════════════════════════════════════════

def _parse_sheet_row(text: str) -> dict | None:
    """Egy megrendelőlap-sor szövegéből: {bid, ugyfel, netto, currency, datum}."""
    ref = _INNONEST_REF_RE.search(text)
    if not ref:
        return None
    bid = ref.group(1).strip()
    amounts = _AMOUNT_RE.findall(text)
    if not amounts:
        return None
    netto = _amount_to_float(amounts[0][0])
    currency = amounts[0][1]
    dm = _DATE_RE.search(text)
    # Ügyfél: a "[Árajánlat KIV #...]" utáni első nem üres, nem szám sor.
    ugyfel = ""
    after = text.split("]", 1)[1] if "]" in text else ""
    for line in after.splitlines():
        s = line.strip()
        if s and not re.match(r"^[\d\s]+$", s) and not _AMOUNT_RE.search(s) and not _DATE_RE.search(s):
            ugyfel = s
            break
    return {
        "bid": bid, "ugyfel": ugyfel, "netto": netto,
        "currency": currency, "datum": dm.group(0) if dm else "",
    }


async def _scrape_innonest_ordersheets() -> list:
    """Az Innonest /ordersheets listája "Összes" állapot-szűrővel (a lista egy
    oldalon adja vissza az összes megrendelőlapot). Minden sorból kinyerjük a
    BID hivatkozást, az ügyfelet, a nettó összeget és a dátumot."""
    sheets = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        await page.goto("https://app.innonest.hu/ordersheets", wait_until="networkidle")
        await page.wait_for_timeout(800)
        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu/ordersheets", wait_until="networkidle")
            await page.wait_for_timeout(800)
            if "login" in page.url:
                raise Exception("Innonest bejelentkezés sikertelen (megrendelőlapok lekéréséhez)")

        # Állapot-szűrő: "Összes" (alapból csak a "Megrendelt" látszik, a
        # már számlázott lapok nem) — a kereső űrlapot beküldjük searchN=all-lal.
        await page.evaluate(
            """
            () => {
                const form = Array.from(document.querySelectorAll('form'))
                    .find(f => (f.action || '').includes('do_search'));
                if (!form) return false;
                const n = form.querySelector('[name="searchN"]');
                if (n) n.value = 'all';
                form.submit();
                return true;
            }
            """
        )
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1200)

        rows = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('table tbody tr'))
                       .map(tr => tr.innerText || '')
            """
        )
        for text in rows:
            parsed = _parse_sheet_row(text)
            if parsed:
                sheets.append(parsed)

        await browser.close()
    log.info(f"[TELJ] Innonest megrendelőlapok beolvasva: {len(sheets)} BID-hivatkozással")
    return sheets


def _get_ordersheets() -> list:
    """Szinkron wrapper — cache-elt megrendelőlap-lista."""
    now = time.time()
    if now - _sheets_cache["at"] < _SHEETS_TTL and _sheets_cache["sheets"]:
        return _sheets_cache["sheets"]
    try:
        sheets = run_in_loop(_scrape_innonest_ordersheets())
        if sheets:
            _sheets_cache["at"] = now
            _sheets_cache["sheets"] = sheets
        return sheets or _sheets_cache["sheets"]
    except Exception as e:
        log.warning(f"[TELJ] Megrendelőlap-lista lekérés hiba: {e} — a korábbi (esetleg üres) cache-t használom")
        return _sheets_cache["sheets"]


def _norm_name(s: str) -> str:
    """Cégnév normalizálás összehasonlításhoz (kisbetű, írásjelek/cégforma nélkül)."""
    s = re.sub(r"[\"'.,()]+", " ", (s or "").lower())
    s = re.sub(r"\b(kft|bt|zrt|nyrt|ev|e\.v|korlátolt|felelősségű|társaság)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _match_sheet_bid(netto: float, currency: str, partner: str, datum: str, sheets: list) -> str:
    """Billingo számla → megrendelőlap párosítás: nettó összeg (kis tűréssel) +
    deviza; több jelölt esetén ügyfélnév-hasonlóság, majd dátumközelség dönt."""
    if not sheets or not netto:
        return ""
    cands = [s for s in sheets if s["currency"] == currency and abs(s["netto"] - netto) <= 5]
    if not cands:
        # kerekítési eltérésekre: 0,1% relatív tűrés
        cands = [s for s in sheets if s["currency"] == currency
                 and abs(s["netto"] - netto) <= max(5.0, netto * 0.001)]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0]["bid"]
    # Több jelölt: ügyfélnév szerint szűkítünk.
    p = _norm_name(partner)
    if p:
        by_name = [s for s in cands if _norm_name(s["ugyfel"]) and
                   (p in _norm_name(s["ugyfel"]) or _norm_name(s["ugyfel"]) in p)]
        if by_name:
            cands = by_name
    if len(cands) == 1:
        return cands[0]["bid"]
    # Még mindig több: a számla dátumához legközelebbi megrendelőlap nyer.
    def _day(d):
        try:
            return time.mktime(time.strptime(d[:10], "%Y-%m-%d"))
        except Exception:
            return 0.0
    inv_t = _day(datum)
    cands.sort(key=lambda s: abs(_day(s["datum"]) - inv_t) if inv_t and s["datum"] else 1e18)
    return cands[0]["bid"]


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


def _doc_currency(doc: dict) -> str:
    c = doc.get("currency")
    if isinstance(c, str) and c.strip():
        return c.strip().upper()
    return "HUF"


def _collect_month(month: str) -> list:
    start, end = _month_range(month)
    sheets = _get_ordersheets()  # Innonest megrendelőlapok (cache-elt) — BID-forrás
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
            partner    = ((full.get("partner") or {}).get("name")) or ""
            datum      = full.get("invoice_date") or ""
            netto      = _doc_net(full)
            # 1) szöveges BID a számlán (ha rákerült), 2) megrendelőlap-párosítás
            bid = _extract_bid(full) or _match_sheet_bid(
                netto, _doc_currency(full), partner, datum, sheets
            )
            items.append({
                "szamlaszam": szamlaszam,
                "datum":      datum,
                "partner":    partner,
                "netto":      netto,
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
