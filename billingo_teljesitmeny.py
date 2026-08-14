# -*- coding: utf-8 -*-
"""
Értékesítői teljesítmény — a Billingo-ban kiállított számlák (adott hónap) össze-
gyűjtése, és a számlán szereplő BID alapján a Pipedrive üzlet TULAJDONOSÁNAK
(owner) e-mail címéhez rendelése. A webapp ez alapján rendeli értékesítőhöz
(e-mail egyezéssel a felhasználó-regiszterrel).

Végpont:  GET /ertekesito-teljesitmeny?month=YYYY-MM
Fejléc:   X-API-Key: <Railway API_KEY>
Válasz:   { ok, month, items: [ {szamlaszam, datum, partner, netto, bid,
                                  ownerEmail, ownerMatch} ] }
  ownerMatch: "bid" (BID-alapú, megbízható párosítás) | "org" (nincs BID-
  egyezés, csak a cég Pipedrive-tulajdonosa alapján — a frontend ezt sárgával
  jelöli) | "" (semmilyen jelzés nem talált tulajdonost).

Környezeti változók (Railway):
  BILLINGO_API_KEY    – a Billingo v3 API kulcs (X-API-KEY fejléchez)
  API_KEY             – a webapp↔Railway közös kulcs (mint a többi végpontnál)
  PIPEDRIVE_API_TOKEN – a Pipedrive lekérésekhez (a pipedrive_webapp is ezt használja)
  INNONEST_EMAIL/PASSWORD – az innonest_core.py-n keresztüli beléptetéshez

FONTOS — a BID párosítás felépítése (2026-08-14, Billingo-váltás után):
  A BID nem szerepel a Billingo számlán, és az Innonest "Számlázás" lista a
  Billingo-váltás óta üres — ezért a párosítás KÉT FÜGGETLEN FORRÁSBÓL és egy
  PONTOZÁSOS motorból áll (_best_bid):
    A) Innonest MEGRENDELŐLAPOK (/ordersheets, Playwright): BID + ügyfél +
       pontos nettó + munkaleírás.
    B) PIPEDRIVE ÜZLETEK (sima REST API): a deal címében ott a BID és a munka
       leírása, a value a nettó ajánlati összeg, org = cég. Ez Playwright
       nélkül, megbízhatóan elérhető — ha az Innonest-scrape elhasal, ez viszi.
  Jelzések (pontokkal): pontos összeg-egyezés, cégnév-egyezés, megnevezés-token
  egyezés (szavak + rendelésszámok, pl. '10393'), részszámla-közelség, egyetlen
  munka a partnernél. A legmagasabb pontszám nyer küszöb felett — DE a
  győztes BID-hez ismert cégneveket (ha vannak) mindig visszaellenőrizzük a
  számla partnerével; ha egyik sem egyezik, a BID-et elutasítjuk és a
  következő legjobb jelöltre lépünk (ez zárja ki, hogy egy más céghez tartozó
  BID-et fogadjunk el csak azért, mert az összege/megnevezése véletlenül
  hasonlít).
  A számlára írt BID (megjegyzés/tételnév) mindent felülír — ha ráírjátok a
  Billingo számlára, az 100%-os találat.
  Ha egy számlához SEM BID, SEM biztos párosítás nem jön ki, a cégnév alapján
  megnézzük, kié (melyik értékesítőé) az adott cég Pipedrive-ban a legutóbb
  mozgatott üzlet szerint (_org_owner_email) — ez gyengébb jelzés (ownerMatch
  = "org"), a frontend sárga háttérrel jelzi, de az összeg ugyanúgy
  beleszámít az összesítőbe.
  A hónap-cache üres forrásokkal nem íródik (ne ragadjon be a hibás állapot),
  és ?refresh=1 paraméterrel kényszeríthető a teljes újratöltés.
  SEBESSÉG: a három forrás (Innonest, Pipedrive, Billingo számlalista) és a
  hiányzó-adatú számlák részlet-lekérése is párhuzamosan fut
  (ThreadPoolExecutor) — korábban ezek egymás után, szinkron módon futottak,
  ami (főleg 30+ számlás hónapoknál) a betöltést jelentősen lelassította.
"""
import os
import re
import time
import calendar
import logging
import requests
from concurrent.futures import ThreadPoolExecutor
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

# Pipedrive üzletek cache (BID a címben + org + érték) — sima API, megbízható.
_deals_cache = {"at": 0, "deals": [], "org_owners": []}
_DEALS_TTL = 1800  # 30 perc


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
    # Megnevezés (munkaleírás): a "KIV" + sorszám után, a "[Árajánlat KIV #..." előtti szöveg.
    desc = ""
    before_ref = text.split("[Árajánlat", 1)[0]
    for line in before_ref.splitlines():
        s = line.strip()
        if not s or s.upper() == "KIV" or re.match(r"^\d{2,4}-\d+$", s):
            continue
        desc = (desc + " " + s).strip() if desc else s
    return {
        "bid": bid, "ugyfel": ugyfel, "netto": netto,
        "currency": currency, "datum": dm.group(0) if dm else "", "desc": desc,
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
    """Szinkron wrapper — cache-elt megrendelőlap-lista, hibánál egy ismétléssel."""
    now = time.time()
    if now - _sheets_cache["at"] < _SHEETS_TTL and _sheets_cache["sheets"]:
        return _sheets_cache["sheets"]
    for attempt in (1, 2):
        try:
            sheets = run_in_loop(_scrape_innonest_ordersheets())
            if sheets:
                _sheets_cache["at"] = now
                _sheets_cache["sheets"] = sheets
                return sheets
            log.warning(f"[TELJ] Megrendelőlap-lista üres ({attempt}. próba)")
        except Exception as e:
            log.warning(f"[TELJ] Megrendelőlap-lista hiba ({attempt}. próba): {e}")
    return _sheets_cache["sheets"]


def _norm_name(s: str) -> str:
    """Cégnév normalizálás összehasonlításhoz (kisbetű, írásjelek/cégforma nélkül)."""
    s = re.sub(r"[\"'.,()]+", " ", (s or "").lower())
    s = re.sub(r"\b(kft|bt|zrt|nyrt|ev|e\.v|korlátolt|felelősségű|társaság)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _same_partner(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    # Szó-átfedés: pl. "Ganz Transzformátor- és Villamos Forgógépgyártó Zrt." vs
    # eltérő rövidítésű változatok — ha a jellemző szavak fele közös, egyezőnek vesszük.
    wa = {w for w in na.split() if len(w) >= 4}
    wb = {w for w in nb.split() if len(w) >= 4}
    if wa and wb:
        inter = len(wa & wb)
        return inter >= 1 and inter >= min(len(wa), len(wb)) / 2.0
    return False


def _norm_bid(s: str) -> str:
    """BID normalizálás: 'bid 2026 147' / 'BID-2026-147' → 'BID-2026-147'."""
    s = re.sub(r"[\s/]+", "-", (s or "").strip().upper())
    return s


# ══════════════════════════════════════════════════════════════════════════════
# PIPEDRIVE ÜZLETEK — BID a deal címében + org + nettó érték (sima API, nincs Playwright)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_pd_deals():
    """Az összes Pipedrive üzlet (lapozva). A cím formátuma jellemzően:
    '<monogram> - <Cég> - <munka leírása> - BID-2026-xxx - Határidő: ...',
    a value pedig a nettó ajánlati összeg (az Innonest ajánlattal egyezik).
    Emellett — mellékesen, ugyanabból a lapozásból, extra API-hívás nélkül —
    felépítjük a cég → tulajdonos (deal owner) térképet is: azoknál a
    számláknál, ahol NINCS BID-egyezés, ez a "második vonalbeli" jelzés (ki a
    cég gazdája Pipedrive-ban), sárga háttérrel jelezve a felületen."""
    token = os.environ.get("PIPEDRIVE_API_TOKEN", "")
    if not token:
        return [], []
    out, org_owners, start = [], [], 0
    while True:
        r = requests.get(
            "https://api.pipedrive.com/v1/deals",
            params={"api_token": token, "start": start, "limit": 500,
                    "status": "all_not_deleted", "sort": "update_time DESC"},
            timeout=25,
        )
        r.raise_for_status()
        j = r.json()
        data = j.get("data") or []
        for d in data:
            title = d.get("title") or ""
            org   = (d.get("org_name") or "").strip()
            owner = d.get("user_id")
            owner_email = ""
            if isinstance(owner, dict):
                owner_email = (owner.get("email") or "").strip().lower()
            # A cég→tulajdonos térképhez az ELSŐ (legfrissebb, mert update_time
            # DESC sorrendben lapozunk) találat marad — a legutóbb mozgatott
            # üzlet gazdáját tekintjük a cég "gazdájának".
            if org and owner_email:
                org_owners.append({"org": org, "email": owner_email})
            m = _BID_RE.search(title)
            if not m:
                continue
            try:
                value = float(d.get("value") or 0)
            except Exception:
                value = 0.0
            out.append({
                "bid":      _norm_bid(m.group(0)),
                "title":    title,
                "org":      org,
                "value":    value,
                "currency": (d.get("currency") or "HUF").upper(),
                "status":   d.get("status") or "",
                "datum":    (d.get("update_time") or "")[:10],
            })
        pag = ((j.get("additional_data") or {}).get("pagination") or {})
        if not pag.get("more_items_in_collection"):
            break
        start = pag.get("next_start") or (start + 500)
        if start > 10000:
            break
    return out, org_owners


def _get_pd_deals() -> list:
    """Cache-elt Pipedrive üzletlista (a cég→tulajdonos térképet is frissíti)."""
    now = time.time()
    if now - _deals_cache["at"] < _DEALS_TTL and _deals_cache["deals"]:
        return _deals_cache["deals"]
    try:
        deals, org_owners = _fetch_pd_deals()
        if deals:
            _deals_cache["at"] = now
            _deals_cache["deals"] = deals
            _deals_cache["org_owners"] = org_owners
        log.info(f"[TELJ] Pipedrive üzletek beolvasva: {len(deals)} BID-del, "
                 f"{len(org_owners)} cég-tulajdonos párral")
        return deals or _deals_cache["deals"]
    except Exception as e:
        log.warning(f"[TELJ] Pipedrive üzletlista hiba: {e} — korábbi cache használata")
        return _deals_cache["deals"]


def _org_owner_email(partner: str) -> str:
    """Ha egy számlához nem találtunk BID-et, a cégnév alapján megkeressük, kié
    (melyik értékesítőé) az adott cég Pipedrive-ban — a legutóbb mozgatott
    üzlet tulajdonosa alapján. Csak akkor ad vissza találatot, ha van cégnév-
    egyezés (_same_partner) — idegen céghez itt sem tippelünk."""
    if not partner:
        return ""
    for row in _deals_cache.get("org_owners") or []:
        if _same_partner(partner, row["org"]):
            return row["email"]
    return ""


# Gyakori, keveset megkülönböztető szavak — a megnevezés-egyezésnél kihagyjuk őket.
_DESC_STOP = {
    "felmérés", "követően", "korrigált", "mennyiség", "verzió", "szerint",
    "valamint", "illetve", "kivitelezés", "munka", "alapján", "módosított",
    "árajánlat", "megrendelőlap", "számla", "budapest", "magyarország",
}


def _sig_tokens(s: str) -> set:
    """Jellemző tokenek a megnevezés/számlaszöveg egyezéséhez:
    ≥5 betűs szavak + 3–6 jegyű számok (rendelésszám, m², db — pl. '10393', '217'),
    kivéve az évszám-szerű (20xx) számokat és a stop-szavakat."""
    s = (s or "").lower()
    words = re.findall(r"[a-záéíóöőúüű]{5,}", s)
    nums = [n for n in re.findall(r"\d{3,6}", s) if not re.match(r"^20\d{2}$", n)]
    return {t for t in (words + nums) if t not in _DESC_STOP}


def _invoice_text(doc: dict) -> str:
    """A Billingo számla szöveges tartalma (megjegyzés + tételnevek + tétel-megjegyzések)."""
    parts = [str(doc.get("comment") or "")]
    for it in (doc.get("items") or []):
        parts.append(str(it.get("name") or ""))
        parts.append(str(it.get("comment") or ""))
    return " ".join(parts)


def _best_bid(invoice_text: str, netto: float, currency: str,
              partner: str, datum: str, sheets: list, deals: list) -> str:
    """PONTOZÁSOS párosítás: minden BID-jelölt (Innonest megrendelőlap ÉS Pipedrive
    üzlet) pontokat gyűjt a különböző jelzésekből, a legmagasabb pontszámú nyer,
    ha átlépi a küszöböt. Jelzések:
      • pontos nettó összeg-egyezés (megrendelőlap nettó vagy deal érték)  — erős
      • vevő/cég egyezés (számla partner ↔ megrendelőlap ügyfél / deal org)
      • megnevezés-tokenek egyezése (számlaszöveg ↔ megrendelőlap leírás / deal cím)
      • részszámla-jelzés: a partner munkái közül az összeghez legközelebbi (≥) tétel
      • a partnernek csak egyetlen BID-es munkája van (bármely forrásban)
    Így a részszámlák, kerekítések és hiányzó számlaszövegek mellett is a lehető
    legtöbb számlához találunk BID-et — de idegen partnerhez sosem tippelünk."""
    scores = {}       # bid -> pont
    meta = {}         # bid -> {amtdiff, datum} tie-breakhez
    bid_partners = {}  # bid -> {ismert cégnevek a forrásokból} — visszaellenőrzéshez

    def add(bid, pts):
        if not bid:
            return
        scores[bid] = scores.get(bid, 0) + pts

    def note(bid, amtdiff=None, dt=""):
        m = meta.setdefault(bid, {"amtdiff": 1e18, "datum": ""})
        if amtdiff is not None and amtdiff < m["amtdiff"]:
            m["amtdiff"] = amtdiff
        if dt and not m["datum"]:
            m["datum"] = dt

    inv_tok = _sig_tokens(invoice_text)
    tol = max(5.0, (netto or 0) * 0.001)

    def remember_partner(bid, name):
        if bid and name:
            bid_partners.setdefault(bid, set()).add(name)

    # ── Innonest megrendelőlapok ──
    partner_sheets = []
    for s in sheets:
        bid = _norm_bid(s["bid"]) if s["bid"].upper().startswith("BID") else s["bid"]
        remember_partner(bid, s.get("ugyfel", ""))
        same = _same_partner(partner, s.get("ugyfel", ""))
        if same:
            partner_sheets.append(s)
            add(bid, 12)
        if netto and s["currency"] == currency:
            diff = abs(s["netto"] - netto)
            if diff <= tol:
                add(bid, 65 if same else 40)
            note(bid, amtdiff=diff, dt=s.get("datum", ""))
        if inv_tok:
            n = len(inv_tok & _sig_tokens(s.get("desc", "")))
            if n:
                add(bid, min(30, 12 + 6 * (n - 1)) + (4 if same else 0))

    # részszámla-jelzés: a partner tételei közül a legközelebbi NAGYOBB összegű
    # (pontos egyezésnél nem jár — azt a pontos-egyezés pontja már lefedi, és
    # azonos összegű lapok között nem szabad ezzel dönteni).
    if netto and partner_sheets:
        ge = [s for s in partner_sheets if s["currency"] == currency and s["netto"] > netto + tol]
        if ge:
            ge.sort(key=lambda s: s["netto"] - netto)
            b = _norm_bid(ge[0]["bid"]) if ge[0]["bid"].upper().startswith("BID") else ge[0]["bid"]
            add(b, 10)

    # ── Pipedrive üzletek ──
    partner_deals = []
    for d in deals:
        bid = d["bid"]
        remember_partner(bid, d.get("org", ""))
        same = _same_partner(partner, d.get("org", ""))
        if same:
            partner_deals.append(d)
            add(bid, 10)
        if netto and d["currency"] == currency and d["value"]:
            diff = abs(d["value"] - netto)
            if diff <= tol:
                add(bid, 45 if same else 25)
            note(bid, amtdiff=diff, dt=d.get("datum", ""))
        if inv_tok:
            n = len(inv_tok & _sig_tokens(d.get("title", "")))
            if n:
                add(bid, min(26, 10 + 5 * (n - 1)) + (4 if same else 0))

    if netto and partner_deals:
        ge = [d for d in partner_deals if d["currency"] == currency and d["value"] > netto + tol]
        if ge:
            ge.sort(key=lambda d: d["value"] - netto)
            add(ge[0]["bid"], 8)

    # a partnernek csak EGY BID-es munkája van (a két forrás uniójában)
    partner_bids = {(_norm_bid(s["bid"]) if s["bid"].upper().startswith("BID") else s["bid"])
                    for s in partner_sheets}
    partner_bids |= {d["bid"] for d in partner_deals}
    if len(partner_bids) == 1:
        add(next(iter(partner_bids)), 10)

    if not scores:
        return ""
    ranked = sorted(
        scores.items(),
        key=lambda kv: (-kv[1], meta.get(kv[0], {}).get("amtdiff", 1e18),
                        meta.get(kv[0], {}).get("datum", "")),
    )
    # Visszaellenőrzés: a nyertes BID-hez ismert cégneveink vannak (az Innonest
    # megrendelőlapból és/vagy a Pipedrive deal org-jából) — ha EGYIK sem
    # egyezik a számla partnerével, ez idegen céghez tartozó BID, sosem
    # fogadjuk el, akkor sem, ha az összeg/megnevezés véletlenül egyezett
    # (pl. SQ-2026-67 → BID-2026-159, ami egy másik cég ajánlata volt).
    for bid, pts in ranked:
        if pts < 22:
            break
        known = bid_partners.get(bid)
        if known and not any(_same_partner(partner, kp) for kp in known):
            continue
        return bid
    return ""


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


def _list_billingo_docs(start: str, end: str) -> list:
    """A hónap összes (nem sztornózott) számla-listaeleme, lapozva."""
    docs, page = [], 1
    while True:
        data = _billingo_get("/documents", {
            "start_date": start, "end_date": end, "per_page": 100, "page": page,
        })
        batch = data.get("data") or []
        for d in batch:
            if str(d.get("type") or "").lower() not in ("invoice", ""):
                continue
            if d.get("cancelled"):
                continue
            docs.append(d)
        if len(batch) < 100:
            break
        page += 1
        if page > 30:  # biztonsági korlát
            break
    return docs


def _fetch_full_doc(d: dict) -> dict:
    """Ha a lista-nézetben nincs tétel/megjegyzés, a részletből pótoljuk."""
    if d.get("items") or d.get("comment"):
        return d
    try:
        return _billingo_get(f"/documents/{d.get('id')}")
    except Exception:
        return d


def _collect_month(month: str):
    start, end = _month_range(month)
    # A három forrás (Innonest megrendelőlapok, Pipedrive üzletek, Billingo
    # számlalista) egymástól független hálózati hívás — párhuzamosan indítjuk,
    # hogy ne adódjanak össze a válaszidők (ez volt a fő oka a lassú betöltésnek).
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_sheets = pool.submit(_get_ordersheets)
        f_deals  = pool.submit(_get_pd_deals)
        f_docs   = pool.submit(_list_billingo_docs, start, end)

        try:
            sheets = f_sheets.result()
        except Exception as e:
            log.warning(f"[TELJ] ordersheets forrás hiba: {e}")
            sheets = []
        try:
            deals = f_deals.result()
        except Exception as e:
            log.warning(f"[TELJ] pipedrive forrás hiba: {e}")
            deals = []
        docs = f_docs.result()

    # A hiányzó tétel/megjegyzés-adatú számlák részlet-lekérése is egymástól
    # független hívás soronként — ezeket is párhuzamosítjuk (ez volt a másik
    # nagy lassító tényező: 30-50 számlánál 30-50 egymás utáni API-hívás).
    if docs:
        with ThreadPoolExecutor(max_workers=8) as pool:
            fulls = list(pool.map(_fetch_full_doc, docs))
    else:
        fulls = []

    items, owner_cache = [], {}
    for full in fulls:
        szamlaszam = full.get("invoice_number") or str(full.get("id") or "")
        partner    = ((full.get("partner") or {}).get("name")) or ""
        datum      = full.get("invoice_date") or ""
        netto      = _doc_net(full)
        # 1) szöveges BID a számlán (ha rákerült) — 100%-os jelzés,
        # 2) pontozásos párosítás a két forrás (Innonest + Pipedrive) alapján,
        #    ami visszaellenőrzi a cégnevet is (idegen céghez sosem tippel).
        bid = _extract_bid(full)
        if bid:
            bid = _norm_bid(bid)
        else:
            bid = _best_bid(_invoice_text(full), netto, _doc_currency(full),
                            partner, datum, sheets, deals)
        owner_match = ""
        if bid:
            owner_email = _owner_email_for_bid(bid, owner_cache)
            if owner_email:
                owner_match = "bid"
        else:
            # Nincs BID-egyezés: legalább a cég Pipedrive-tulajdonosát próbáljuk
            # megtalálni (sárga jelzés a felületen — az összeg beleszámít az
            # összesítőbe, de ez gyengébb jelzés, mint a BID-alapú párosítás).
            owner_email = _org_owner_email(partner)
            if owner_email:
                owner_match = "org"
        items.append({
            "szamlaszam":  szamlaszam,
            "datum":       datum,
            "partner":     partner,
            "netto":       netto,
            "bid":         bid,
            "ownerEmail":  owner_email,
            "ownerMatch":  owner_match,   # "bid" | "org" | ""
        })
    matched = sum(1 for i in items if i["bid"])
    log.info(f"[TELJ] {month}: {matched}/{len(items)} számlához találtunk BID-et "
             f"(források: {len(sheets)} megrendelőlap, {len(deals)} pipedrive deal)")
    diag = {"sheets": len(sheets), "deals": len(deals), "matched": matched}
    return items, diag


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

        # Cache (?refresh=1 kihagyja — teszteléshez/kényszerített frissítéshez)
        refresh = (request.args.get("refresh") or "") == "1"
        now = time.time()
        c = _cache.get(month)
        if c and not refresh and now - c["at"] < _CACHE_TTL:
            return jsonify({"ok": True, "month": month, "items": c["items"],
                            "diag": c.get("diag", {}), "cached": True})
        if refresh:
            # a forrás-cache-eket is frissítjük
            _sheets_cache["at"] = 0
            _deals_cache["at"] = 0

        try:
            items, diag = _collect_month(month)
        except requests.HTTPError as e:
            return jsonify({"ok": False, "error": f"Billingo API hiba: {e}"}), 502
        except Exception as e:
            log.error(f"[TELJ] hiba ({month}): {e}")
            return jsonify({"ok": False, "error": f"Hiba: {e}"}), 500

        # Ha MINDKÉT BID-forrás üres volt (pl. Innonest-belépés és Pipedrive is
        # elhasalt), ne mérgezzük a cache-t üres párosítással — a következő
        # kérés újra próbálkozik.
        if diag.get("sheets") or diag.get("deals"):
            _cache[month] = {"at": now, "items": items, "diag": diag}
        return jsonify({"ok": True, "month": month, "items": items, "diag": diag})

    log.info("[TELJESITMENY] Végpont regisztrálva: /ertekesito-teljesitmeny")
