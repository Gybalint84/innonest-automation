"""
pipedrive_addon.py – Pipedrive webhook + visszajelzési rendszer (v2, Sheet-mentes)
==================================================================================
VÁLTOZÁS a v1-hez képest: NINCS többé Google Sheet függőség.
  - Token:      HMAC-aláírt, öntartalmú token (deal_id + időbélyeg a tokenben,
                aláírással hitelesítve). Nincs tárolás — újraindítás/redeploy
                után is érvényes marad.
  - Duplikáció: Pipedrive jegyzet (note) a dealen. A kiküldött email és a
                beérkezett visszajelzés is note-ként rögzül, így az értékesítő
                a Pipedrive-ban látja a teljes történetet.
  - Kalkuláció: A kalkulátor webapp által a dealre visszaírt egyedi mezőkből
                (Alvállalkozók + Feladatrészletezés) — nem Sheetből.
  - E-mail:     Önálló Apps Script Web App Gmail-proxy (sendEmail,
                PDFquotationSENDdealOWNER) — KÜLÖN projekt, mint a
                megrendelés-figyelő scriptje, mert a Service Account tiltott.
                Lásd: EMAIL_WEBAPP_URL / EMAIL_WEBAPP_SECRET.

A server.py-ban változatlanul:
    from pipedrive_addon import register_pipedrive_routes
    register_pipedrive_routes(app)

Környezeti változók:
    PIPEDRIVE_API_TOKEN      – Pipedrive API token
    PIPEDRIVE_BID_FIELD_KEY  – BID szám deal-mező kulcsa
    EMAIL_WEBAPP_URL         – a KÜLÖN email-küldő Apps Script Web App URL-je
                                (NEM ugyanaz, mint a megrendelés-figyelő
                                WEBAPP_URL-je — az egy másik projekt!)
    EMAIL_WEBAPP_SECRET      – az email-küldő Apps Script közös titka
    TOKEN_SECRET             – token-aláíró titok (ha nincs, EMAIL_WEBAPP_SECRET)
    TOKEN_MAX_AGE_DAYS       – visszajelzés link érvényessége napokban (alap: 120)
"""

import os
import hmac
import json
import time
import base64
import hashlib
import datetime
import re
import logging
import requests
from urllib.parse import quote
from flask import Blueprint, request, jsonify

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# KONFIGURÁCIÓ
# ─────────────────────────────────────────────
PIPEDRIVE_API_TOKEN  = os.environ.get("PIPEDRIVE_API_TOKEN", "")
PIPEDRIVE_BID_FIELD  = os.environ.get("PIPEDRIVE_BID_FIELD_KEY", "")

# Az email-küldő KÜLÖN Apps Script projekt (webapp_email_v1.js) — nem
# ugyanaz, mint a megrendelés-figyelő WEBAPP_URL-je!
EMAIL_WEBAPP_URL     = os.environ.get("EMAIL_WEBAPP_URL", "")
EMAIL_WEBAPP_SECRET  = os.environ.get("EMAIL_WEBAPP_SECRET", "")

# Az ügyfélnek kiküldött linkek (email + visszajelző oldal) ezt a domaint
# használják. PUBLIC_BASE_URL-lel felülírható a saját domainre (Railway
# Custom Domain beállítás után) — ha nincs megadva, a Railway alap-domainre
# esik vissza, hogy fejlesztés közben ne kelljen semmit beállítani.
PUBLIC_BASE_URL      = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
BASE_URL             = PUBLIC_BASE_URL or ("https://" + os.environ.get("RAILWAY_PUBLIC_DOMAIN", ""))

TOKEN_SECRET         = os.environ.get("TOKEN_SECRET", "") or EMAIL_WEBAPP_SECRET
TOKEN_MAX_AGE_DAYS   = int(os.environ.get("TOKEN_MAX_AGE_DAYS", "120"))

# Pipedrive deal egyedi mezők (a kalkulátor webapp írja őket — lásd pipedrive_webapp.py)
PIPEDRIVE_NAPOK_FIELD       = os.environ.get("PIPEDRIVE_NAPOK_FIELD_KEY", "cdf639e8db9018be9f880366a03a17aee38284a3")   # Összesen hány nap a kivitelezés
PIPEDRIVE_CONTRACTORS_FIELD = os.environ.get("PIPEDRIVE_CONTRACTORS_FIELD_KEY", "94efcfc331d3531b03786aeb5d4dcc77606398f2")  # Alvállalkozók (soronként egy név)
PIPEDRIVE_TASKDETAIL_FIELD  = os.environ.get("PIPEDRIVE_TASKDETAIL_FIELD_KEY", "493b30f872256ef6b659c411dc32dbf541ef1d57")   # Alvállalkozó feladatok részletezése
PIPEDRIVE_SITE_ADDRESS_FIELD = os.environ.get("PIPEDRIVE_SITE_ADDRESS_FIELD_KEY", "7008531d11f5bade385cc7fb72bb2648d4b19137")  # Kivitelezés helyszíne

SABLONOK_MAPPA = os.path.join(os.path.dirname(__file__), "sablonok")

# A Pipedrive Projektmodul közvetlen webes linkje (cégdomain: sqmhu).
PIPEDRIVE_PROJECT_BASE = os.environ.get("PIPEDRIVE_PROJECT_BASE", "https://sqmhu.pipedrive.com/projects/")

# Pipedrive note-okban használt jelölők (duplikáció-védelem + napló)
NOTE_EMAIL_SENT_MARKER = "[SQM-AUTO] Kivitelezési tájékoztató elküldve"
NOTE_FEEDBACK_MARKER   = "[SQM-AUTO] Ügyfél visszajelzés beérkezett"


# ─────────────────────────────────────────────
# TOKEN – HMAC-aláírt, öntartalmú (nincs tárolás)
# Formátum: v2.<deal_id>.<kiallitas_unix>.<alairas>
# ─────────────────────────────────────────────
def _token_alairas(deal_id: int, ts: int) -> str:
    uzenet = f"v2.{deal_id}.{ts}".encode()
    mac = hmac.new(TOKEN_SECRET.encode(), uzenet, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:15]).decode().rstrip("=")


def token_general(deal_id: int) -> str:
    """Aláírt token a visszajelzési linkhez. Nem igényel tárolást."""
    ts = int(time.time())
    return f"v2.{deal_id}.{ts}.{_token_alairas(deal_id, ts)}"


def token_ellenoriz(token: str) -> int | None:
    """Visszaadja a deal_id-t, ha a token érvényes és nem járt le; különben None."""
    try:
        resz = (token or "").split(".")
        if len(resz) != 4 or resz[0] != "v2":
            return None
        deal_id, ts = int(resz[1]), int(resz[2])
        vart = _token_alairas(deal_id, ts)
        if not hmac.compare_digest(vart, resz[3]):
            return None
        if time.time() - ts > TOKEN_MAX_AGE_DAYS * 86400:
            log.info(f"[TOKEN] Lejárt token (deal {deal_id})")
            return None
        return deal_id
    except Exception:
        return None


# ─────────────────────────────────────────────
# INNONEST – tételek lekérése (változatlan)
# ─────────────────────────────────────────────
def _innonest_adatok_leker(bid: str) -> dict:
    try:
        from innonest_core import innonest_adatok_leker
        return innonest_adatok_leker(bid)
    except Exception as e:
        log.error(f"[INNONEST] Import/hívás hiba: {e}")
        return {
            "tetelek": [],
            "netto_osszeg": "",
            "penznem": "HUF",
            "fizetesi_feltetelek": "–",
            "ervenyes_ig": "–",
        }


# Rövid életű, memóriabeli cache — a visszajelző oldal minden megnyitása/
# frissítése ne indítson új Innonest-scrape-et ugyanarra a BID-re.
_INNONEST_CACHE_TTL = 600  # másodperc
_innonest_cache: dict = {}


def _innonest_adatok_leker_cached(bid: str) -> dict:
    most = time.time()
    talalat = _innonest_cache.get(bid)
    if talalat and most - talalat[0] < _INNONEST_CACHE_TTL:
        return talalat[1]
    adat = _innonest_adatok_leker(bid)
    _innonest_cache[bid] = (most, adat)
    return adat


# ─────────────────────────────────────────────
# PIPEDRIVE API
# ─────────────────────────────────────────────
def _pd_get(endpoint: str, params: dict = None) -> dict | None:
    url = f"https://api.pipedrive.com/v1/{endpoint}"
    p = {"api_token": PIPEDRIVE_API_TOKEN}
    if params:
        p.update(params)
    try:
        r = requests.get(url, params=p, timeout=15)
        data = r.json()
        if data.get("success"):
            return data["data"]
    except Exception as e:
        log.error(f"[PD] GET {endpoint}: {e}")
    return None


def _pd_project_by_deal(deal_id) -> dict | None:
    """A dealhez kötött Pipedrive projekt (Projects v2 API). A nyitottat preferálja.
    A projekt objektum tartalmazza: id, title, start_date, end_date, status, deal_ids."""
    url = "https://api.pipedrive.com/api/v2/projects"
    try:
        r = requests.get(url, params={"api_token": PIPEDRIVE_API_TOKEN, "deal_id": deal_id}, timeout=15)
        data = r.json()
        items = data.get("data") or []
        if not items:
            return None
        nyitott = [p for p in items if (p.get("status") or "") == "open"]
        return (nyitott or items)[0]
    except Exception as e:
        log.error(f"[PD] projekt lekérés (deal {deal_id}): {e}")
        return None


def _pd_post(endpoint: str, payload: dict) -> dict | None:
    url = f"https://api.pipedrive.com/v1/{endpoint}"
    try:
        r = requests.post(url, params={"api_token": PIPEDRIVE_API_TOKEN},
                          json=payload, timeout=15)
        data = r.json()
        if data.get("success"):
            return data.get("data")
        log.error(f"[PD] POST {endpoint} sikertelen: {data}")
    except Exception as e:
        log.error(f"[PD] POST {endpoint}: {e}")
    return None


def _elso(lista, kulcs="value"):
    if isinstance(lista, list) and lista:
        return lista[0].get(kulcs, "")
    return ""


def _szam_fmt(ertek) -> str:
    try:
        return f"{int(float(str(ertek).replace(' ','').replace(',','.'))):,}".replace(",", " ")
    except Exception:
        return str(ertek)


def _datum_fmt(s: str) -> str:
    honapok = ["január","február","március","április","május","június",
               "július","augusztus","szeptember","október","november","december"]
    try:
        d = datetime.datetime.fromisoformat(s.split("T")[0])
        return f"{d.year}. {honapok[d.month-1]} {d.day}."
    except Exception:
        return s


def _kivitelezes_napok_fmt(ertek) -> str:
    """'5' → '5 munkanap'; '6-8 munkanap' → változatlan; üres → ''."""
    s = str(ertek if ertek is not None else "").strip()
    if not s:
        return ""
    if "nap" in s.lower():
        return s
    return f"{s} munkanap"


# ─────────────────────────────────────────────
# PIPEDRIVE NOTE – duplikáció-védelem + napló
# ─────────────────────────────────────────────
def deal_mar_ertesitve(deal_id: int) -> bool:
    """Igaz, ha a dealen már van 'tájékoztató elküldve' note (tartós védelem)."""
    notes = _pd_get("notes", {"deal_id": deal_id, "limit": 100}) or []
    for n in notes:
        tartalom = re.sub(r"<[^>]+>", "", n.get("content") or "")
        if NOTE_EMAIL_SENT_MARKER in tartalom:
            return True
    return False


def deal_visszajelzes_adatok(deal_id: int) -> dict | None:
    """
    None, ha az ügyfél MÉG NEM küldte be az adatlapot.
    dict (a beküldött adatokkal), ha MÁR beküldte — ez a tartós "zárolás":
    a note maga a bizonyíték, nincs hozzá külön tárolás.
    """
    notes = _pd_get("notes", {"deal_id": deal_id, "limit": 100}) or []
    for n in notes:
        tartalom = n.get("content") or ""
        if NOTE_FEEDBACK_MARKER in re.sub(r"<[^>]+>", "", tartalom):
            m = re.search(r"<!--SQM_JSON:([A-Za-z0-9+/=]+)-->", tartalom)
            if m:
                try:
                    nyers = base64.b64decode(m.group(1)).decode("utf-8")
                    return json.loads(nyers)
                except Exception as e:
                    log.warning(f"[NOTE] Beágyazott JSON nem olvasható (deal {deal_id}): {e}")
            return {}  # note megvan, de a régi formátumú (JSON nélküli) — üres, de zárolt
    return None


def deal_ertesites_rogzit(deal_id: int, token: str, cimzett: str):
    """Note a dealre: a tájékoztató email kiment (ez a tartós duplikáció-védelem)."""
    url = f"{BASE_URL}/{token}"
    tartalom = (
        f"<b>{NOTE_EMAIL_SENT_MARKER}</b><br>"
        f"Címzett: {cimzett}<br>"
        f"Visszajelzési link: <a href='{url}'>{url}</a><br>"
        f"Időpont: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    _pd_post("notes", {"deal_id": deal_id, "content": tartalom})


def visszajelzes_note_rogzit(deal_id: int, idopontok: list, kovetelmenyek: list,
                             megjegyzes: str, helyszini_kapcsolattarto: dict):
    """
    Note a dealre a beküldött visszajelzés tartalmával — a Pipedrive-ban is
    látszik. A note egyben a TARTÓS ZÁROLÁS forrása is: a HTML tartalom végén
    egy láthatatlan <!--SQM_JSON:...--> komment viszi a nyers adatokat
    (base64-ben), amit a deal_visszajelzes_adatok() olvas vissza — így sem az
    ügyfél, sem a linket a note-ból megnyitó kolléga nem tud újra szerkeszteni
    egy már beküldött adatlapot, redeploy után sem.
    """
    beerkezve = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    sorok = [f"<b>{NOTE_FEEDBACK_MARKER}</b>"]
    if idopontok:
        sorok.append("<b>Preferált időpontok:</b> " + " | ".join(idopontok))
    hk = helyszini_kapcsolattarto or {}
    if hk.get("nev") or hk.get("telefon") or hk.get("email"):
        sorok.append(f"<b>Helyszíni kapcsolattartó:</b> {hk.get('nev','')} · {hk.get('telefon','')} · {hk.get('email','')}")
    for k in (kovetelmenyek or []):
        r = k.get("reszlet", "")
        sorok.append(f"✓ {k.get('label','')}" + (f" — {r}" if r else ""))
    if megjegyzes:
        sorok.append(f"<b>Megjegyzés:</b> {megjegyzes}")
    sorok.append(f"Beküldve: {beerkezve}")

    payload = {
        "idopontok":               idopontok or [],
        "kovetelmenyek":           kovetelmenyek or [],
        "megjegyzes":              megjegyzes or "",
        "helyszini_kapcsolattarto": hk,
        "beerkezve":               beerkezve,
    }
    json_blob = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    sorok.append(f"<!--SQM_JSON:{json_blob}-->")

    _pd_post("notes", {"deal_id": deal_id, "content": "<br>".join(sorok)})


# ─────────────────────────────────────────────
# DEAL ADATOK ÖSSZEÁLLÍTÁSA (Pipedrive + opcionálisan Innonest)
# ─────────────────────────────────────────────
def deal_adatok_osszerak(deal_id: int, innonest_kell: bool = True) -> dict | None:
    """
    Minden adat a Pipedrive API-ból jön (deal/person/org/owner + egyedi mezők).
    Az Innonest-scrape (tételek + árajánlat végösszege) csak akkor marad ki,
    ha a hívó explicit innonest_kell=False-t ad (pl. a submit-nál, ahol az
    összeget nem jelenítjük meg) — a BID-enkénti rövid cache miatt az oldal
    ismételt megnyitása nem indít új scrape-et.
    """
    deal = _pd_get(f"deals/{deal_id}")
    if not deal:
        return None

    bid = deal.get(PIPEDRIVE_BID_FIELD, "")
    if not bid:
        log.warning(f"[PD] Nincs BID szám a deal-ben: {deal_id}")
        return None

    person_id = (deal.get("person_id") or {}).get("value")
    org_id    = (deal.get("org_id")    or {}).get("value")
    owner_id  = (deal.get("user_id")   or {}).get("id")

    person = _pd_get(f"persons/{person_id}")  if person_id else {}
    org    = _pd_get(f"organizations/{org_id}") if org_id  else {}
    owner  = _pd_get(f"users/{owner_id}")     if owner_id else {}

    cim_reszek = [org.get("address_route",""), org.get("address_locality",""),
                  org.get("address_country","")] if org else []
    cim = ", ".join(r for r in cim_reszek if r) or (org or {}).get("address","–")

    won_time = deal.get("won_time") or deal.get("update_time","")
    datum    = _datum_fmt(won_time) if won_time else datetime.datetime.now().strftime("%Y. %m. %d.")

    kivitelezes_napok = _kivitelezes_napok_fmt(deal.get(PIPEDRIVE_NAPOK_FIELD, ""))

    if innonest_kell:
        log.info(f"[INNONEST] Tételek lekérése BID alapján: {bid}")
        innonest = _innonest_adatok_leker_cached(bid)
    else:
        innonest = {"tetelek": [], "netto_osszeg": "", "penznem": "",
                    "fizetesi_feltetelek": "–", "ervenyes_ig": "–"}

    return {
        "deal_id":                deal_id,
        "bid_szam":               bid,
        "datum":                  datum,
        "deal_nev":               deal.get("title","–"),
        "deal_ertek":             _szam_fmt(deal.get("value", 0)),
        "penznem":                innonest.get("penznem") or deal.get("currency","HUF"),
        "cegnev":                 (org or {}).get("name", deal.get("org_name","–")),
        "kapcsolattarto_nev":     (person or {}).get("name","–"),
        "kapcsolattarto_email":   _elso((person or {}).get("email",[])),
        "kapcsolattarto_telefon": _elso((person or {}).get("phone",[])) or "–",
        "cim":                    cim if cim and cim != "None" else "–",
        "owner_nev":              (owner or {}).get("name","–"),
        "owner_email":            (owner or {}).get("email",""),
        "owner_telefon":          _elso((owner or {}).get("phone",[])),
        "tetelek":                innonest.get("tetelek", []),
        "netto_osszeg":           innonest.get("netto_osszeg") or _szam_fmt(deal.get("value", 0)),
        "fizetesi_feltetelek":    innonest.get("fizetesi_feltetelek", "–"),
        "ervenyes_ig":            innonest.get("ervenyes_ig", "–"),
        "kivitelezes_napok":      kivitelezes_napok,
        # Alvállalkozói adatok a kalkulátor által visszaírt mezőkből:
        "alvallalkozok_raw":      str(deal.get(PIPEDRIVE_CONTRACTORS_FIELD) or "").strip(),
        "feladat_reszletezes_raw": str(deal.get(PIPEDRIVE_TASKDETAIL_FIELD) or "").strip(),
        "kivitelezes_helyszine":  str(deal.get(PIPEDRIVE_SITE_ADDRESS_FIELD) or "").strip(),
    }


# ─────────────────────────────────────────────
# ALVÁLLALKOZÓI FELADATBONTÁS PARSE
# A kalkulátor _buildAlvTaskDetail() formátuma:
#   ▪ Alvállalkozó Név
#     Feladat neve | 120 m2 × 1 500 Ft = 180 000 Ft
#     Összesen: 180 000 Ft
# ─────────────────────────────────────────────
def alv_bontas_parse(detail_text: str) -> dict:
    """
    Visszaad: { 'Alvállalkozó Név': {'tetelek': [{'feladat','mennyiseg','egysegar','reszosszeg'}],
                                     'osszesen': '180 000 Ft'} }
    Ha a szöveg nem parse-olható, üres dict — a hívó a nyers szöveget használja.
    """
    groups = {}
    aktualis = None
    for sor in (detail_text or "").splitlines():
        s = sor.strip()
        if not s:
            continue
        if s.startswith("▪"):
            aktualis = s.lstrip("▪").strip()
            groups[aktualis] = {"tetelek": [], "osszesen": ""}
        elif aktualis and s.lower().startswith("összesen:"):
            groups[aktualis]["osszesen"] = s.split(":", 1)[1].strip()
        elif aktualis and "|" in s:
            bal, jobb = s.split("|", 1)
            feladat = bal.strip()
            m = re.match(r"\s*(.+?)\s*×\s*(.+?)\s*=\s*(.+)\s*$", jobb)
            if m:
                groups[aktualis]["tetelek"].append({
                    "feladat":    feladat,
                    "mennyiseg":  m.group(1).strip(),
                    "egysegar":   m.group(2).strip(),
                    "reszosszeg": m.group(3).strip(),
                })
            else:
                groups[aktualis]["tetelek"].append({
                    "feladat": feladat, "mennyiseg": jobb.strip(),
                    "egysegar": "", "reszosszeg": "",
                })
    return groups


# ─────────────────────────────────────────────
# SABLON KITÖLTÉS (változatlan logika)
# ─────────────────────────────────────────────
def sablon_betolt(nev: str) -> str:
    with open(os.path.join(SABLONOK_MAPPA, nev), encoding="utf-8") as f:
        return f.read()


def tetelek_email_html(tetelek: list) -> str:
    html = ""
    for i, t in enumerate(tetelek):
        bg = "#ffffff" if i % 2 == 0 else "#fafafa"
        html += (
            f'<tr style="background:{bg};border-bottom:1px solid #f0f0f0;">'
            f'<td style="padding:9px 10px;font-family:Arial,sans-serif;font-size:11px;color:#999;">{t.get("sorszam","")}</td>'
            f'<td style="padding:9px 10px;font-family:Arial,sans-serif;font-size:13px;color:#444;">{t.get("megnevezes","")}</td>'
            f'<td align="right" style="padding:9px 10px;font-family:Arial,sans-serif;font-size:13px;color:#444;white-space:nowrap;">{t.get("mennyiseg","")}</td>'
            f'<td align="right" style="padding:9px 10px;font-family:Arial,sans-serif;font-size:13px;color:#444;white-space:nowrap;">{t.get("egysegar","")}</td>'
            f'<td align="right" style="padding:9px 10px;font-family:Arial,sans-serif;font-size:13px;font-weight:600;color:#1a1a1a;white-space:nowrap;">{t.get("osszesen","")}</td>'
            f'</tr>'
        )
    return html


def tetelek_web_html(tetelek: list) -> str:
    html = ""
    for t in tetelek:
        html += (
            f'<tr>'
            f'<td>{t.get("sorszam","")}</td>'
            f'<td>{t.get("megnevezes","")}</td>'
            f'<td>{t.get("mennyiseg","")}</td>'
            f'<td>{t.get("egysegar","")}</td>'
            f'<td>{t.get("osszesen","")}</td>'
            f'</tr>'
        )
    return html


def sablon_kitolt(html: str, adatok: dict, token: str = "") -> str:
    owner_tel_sor = (
        f'{adatok["owner_telefon"]}<br>'
        if adatok.get("owner_telefon") else ""
    )
    visszajelzes_url = f"{BASE_URL}/{token}" if token else ""

    # ── Kivitelezési időtartam blokkok (üres érték esetén nem jelennek meg) ──
    napok = adatok.get("kivitelezes_napok", "")

    kivitelezes_blokk_email = (
        f"""<p style="margin:12px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#444444;line-height:1.75;">A munka elvégzéséhez várhatóan <strong style="color:#1a1a1a;">{napok}</strong> szükséges — kérjük, ennyi napot foglaljon a naptárban a kivitelezésre.</p>"""
        if napok else ""
    )

    kivitelezes_blokk_oldal = (
        f"""<div style="margin-top:14px;padding:12px 16px;background:#fbf6ea;border-left:3px solid #f0a500;font-size:13px;color:#555;line-height:1.6;">A munkához szükséges munkanapok száma <span style="color:#888;">(kérjük, ennyi napot foglaljon a naptárban)</span>: <strong style="color:#1a1a1a;font-size:15px;">{napok}</strong></div>"""
        if napok else ""
    )

    # ── Kivitelezés helyszíne blokk (üres érték esetén nem jelenik meg) ──
    helyszin = adatok.get("kivitelezes_helyszine", "")

    helyszin_blokk_email = (
        f"""<p style="margin:12px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#444444;line-height:1.75;">Kérjük, ellenőrizze a kivitelezés helyszínét: <strong style="color:#1a1a1a;">{helyszin}</strong></p>"""
        if helyszin else ""
    )

    helyszin_blokk_oldal = (
        f"""<div style="margin-top:14px;padding:12px 16px;background:#fbf6ea;border-left:3px solid #f0a500;font-size:13px;color:#555;line-height:1.6;">Kérjük, ellenőrizze a kivitelezés helyszínét: <strong style="color:#1a1a1a;font-size:15px;">{helyszin}</strong></div>"""
        if helyszin else ""
    )

    csere = {
        "{{BID_SZAM}}":               adatok.get("bid_szam",""),
        "{{DATUM}}":                  adatok.get("datum",""),
        "{{DEAL_NEV}}":               adatok.get("deal_nev",""),
        "{{DEAL_ERTEK}}":             adatok.get("deal_ertek",""),
        "{{PENZNEM}}":                adatok.get("penznem","HUF"),
        "{{CEGNEV}}":                 adatok.get("cegnev",""),
        "{{KAPCSOLATTARTO_NEV}}":     adatok.get("kapcsolattarto_nev",""),
        "{{KAPCSOLATTARTO_EMAIL}}":   adatok.get("kapcsolattarto_email",""),
        "{{KAPCSOLATTARTO_TELEFON}}": adatok.get("kapcsolattarto_telefon","–"),
        "{{CIM}}":                    adatok.get("cim",""),
        "{{OWNER_NEV}}":              adatok.get("owner_nev",""),
        "{{OWNER_EMAIL}}":            adatok.get("owner_email",""),
        "{{OWNER_TELEFON_SOR}}":      owner_tel_sor,
        "{{OWNER_TELEFON_SOR_HTML}}":   f'<strong>Tel:</strong> {adatok["owner_telefon"]}<br>\n        ' if adatok.get("owner_telefon") else "",
        "{{NETTO_OSSZEG}}":           adatok.get("netto_osszeg",""),
        "{{FIZETESI_FELTETELEK}}":    adatok.get("fizetesi_feltetelek","–"),
        "{{ERVENYES_IG}}":            adatok.get("ervenyes_ig","–"),
        "{{KIVITELEZES_NAPOK}}":      napok,
        "{{KIVITELEZES_NAPOK_BLOKK_EMAIL}}": kivitelezes_blokk_email,
        "{{KIVITELEZES_NAPOK_BLOKK_OLDAL}}": kivitelezes_blokk_oldal,
        "{{KIVITELEZES_HELYSZINE}}":  helyszin,
        "{{KIVITELEZES_HELYSZINE_BLOKK_EMAIL}}": helyszin_blokk_email,
        "{{KIVITELEZES_HELYSZINE_BLOKK_OLDAL}}": helyszin_blokk_oldal,
        "{{TOKEN}}":                  token,
        "{{VISSZAJELZES_URL}}":       visszajelzes_url,
        "{{TETELEK_EMAIL_HTML}}":     tetelek_email_html(adatok.get("tetelek",[])),
        "{{TETELEK_HTML}}":           tetelek_web_html(adatok.get("tetelek",[])),
        "{{CALENDLY_LINK}}":          "#",
    }
    for k, v in csere.items():
        html = html.replace(k, str(v))
    return html


# ─────────────────────────────────────────────
# ALVÁLLALKOZÓI ÁRBEKÉRÉS – sablonfájlból (sablonok/alvallalkozoi_arbekero.html)
# épített email. A design pontosan az SQM Google Sheets "Alvállalkozó
# díjkalkulátor" Apps Script (sendSubcontractorRequestsCore_) kinézete és a
# "AJÁNLATOT ADOK" gomb mailto-logikája — csak az adatokat a kalkulátor
# webapp saját állapotából kapja (Alv oldal), nem Sheet-cellákból.
# ─────────────────────────────────────────────
def arbekero_feladatok_html(tasks: list) -> str:
    html = ""
    for i, t in enumerate(tasks):
        bg = "#ffffff" if i % 2 == 0 else "#fcfaf0"
        html += (
            f'<tr style="background:{bg};">'
            f'<td style="padding:14px 12px;border-bottom:1px solid #eceff3;text-align:center;width:46px;">'
            f'<span style="display:inline-block;width:26px;height:26px;line-height:26px;border-radius:50%;'
            f'background:#FFD21F;color:#12233b;font-weight:700;font-size:13px;">{i + 1}</span></td>'
            f'<td style="padding:14px 12px;border-bottom:1px solid #eceff3;font-weight:700;color:#12233b;font-size:15px;">{t.get("name","")}</td>'
            f'<td style="padding:14px 12px;border-bottom:1px solid #eceff3;text-align:right;color:#1a1a1a;font-size:15px;font-weight:700;white-space:nowrap;">{t.get("quantity","")}</td>'
            f'<td style="padding:14px 12px;border-bottom:1px solid #eceff3;color:#555;font-size:14px;">{t.get("unit","")}</td>'
            f'</tr>'
        )
    return html


def arbekero_anyagok_blokk(materials: list) -> str:
    """A feladatokhoz tartozó anyagok szekciója (anyagnév + mennyiség).
    Üres lista esetén nem jelenik meg."""
    if not materials:
        return ""
    sorok = ""
    for i, m in enumerate(materials):
        bg = "#ffffff" if i % 2 == 0 else "#fafafa"
        menny = " ".join(x for x in [str(m.get("quantity", "")).strip(), str(m.get("unit", "")).strip()] if x)
        sorok += (
            f'<tr style="background:{bg};">'
            f'<td style="padding:10px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#1a1a1a;">{m.get("name","")}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#333;text-align:right;white-space:nowrap;">{menny}</td>'
            f'</tr>'
        )
    return (
        '<h2 style="margin:6px 0 3px;font-size:16px;color:#0f766e;">A megbízó az alábbi anyagokat biztosítja a munkához:</h2>'
        '<p style="margin:0 0 10px;font-size:12px;color:#8a94a3;">Tájékoztató jellegű — ezekre az anyagokra nem kell árajánlatot adni.</p>'
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin-bottom:24px;">'
        '<thead><tr style="background:#0f766e;">'
        '<th style="padding:10px;text-align:left;font-size:11px;color:#fff;letter-spacing:1px;text-transform:uppercase;">Anyag megnevezése</th>'
        '<th style="padding:10px;text-align:right;font-size:11px;color:#fff;letter-spacing:1px;text-transform:uppercase;">Mennyiség</th>'
        '</tr></thead>'
        f'<tbody>{sorok}</tbody></table>'
    )


def arbekero_reply_mailto(request_id: str, project_location: str, contractor_nev: str, tasks: list) -> str:
    """Az "AJÁNLATOT ADOK" gomb — előre kitöltött, jól tagolt válasz e-mailt nyit
    a saját levelezőben, az info@sqm-hungary.hu címre. A sortöréseket CRLF-fel
    (RFC 6068: %0D%0A) kódoljuk, így a Gmail/Outlook is megőrzi a tagolást.
    Azonosítóként a BID szám megy — cégnevet szándékosan NEM teszünk bele —, a
    projekt helyszínét viszont igen."""
    NL = "\r\n"
    lines = [
        "Kedves SQM Hungary!",
        "",
        f"Azonosító (BID): {request_id or ''}",
        f"Projekt helyszíne: {project_location or ''}",
        "",
        "--------------------------------",
        "",
        "ÁRAJÁNLAT",
        "",
    ]
    for i, t in enumerate(tasks):
        unit = t.get("unit", "")
        lines += [
            f'{i + 1}. {t.get("name","")}',
            f'   Mennyiség: {t.get("quantity","")} {unit}',
            f'   Nettó egységár: ______ Ft/{unit}',
            f'   Nettó összesen: ______ Ft + ÁFA',
            "",
        ]
    lines += [
        "--------------------------------",
        "",
        "Kiszállás és gépek az árban: igen / nem",
        "Egyéb megjegyzés: ",
        "",
        "Üdvözlettel:",
        f"{contractor_nev or ''}",
    ]
    subject = f"Ajánlat – {request_id or 'SQM projekt'}"
    body = NL.join(lines)
    return f"mailto:info@sqm-hungary.hu?subject={quote(subject)}&body={quote(body)}"


def arbekero_kitolt(adatok: dict) -> str:
    request_id = adatok.get("requestId", "") or "SQM projekt"
    project_location = adatok.get("projectLocation", "") or "—"
    # Megszólítás: a kapcsolattartó neve, ha van; különben az alvállalkozó cégneve.
    kedves_nev = adatok.get("contractorGreeting") or adatok.get("contractorNev") or "Partnerünk"
    deal_id = str(adatok.get("dealId") or "").strip()
    tasks = adatok.get("tasks") or []
    materials = adatok.get("materials") or []

    # "Üzlet azonosítója" sor (Pipedrive üzlet ID) — csak ha van érték.
    uzlet_azonosito_sor = (
        f"""<tr>
        <td colspan="2" style="padding:14px 20px;border-top:1px solid #e6e9ee;">
          <div style="font-size:10px;color:#8a94a3;letter-spacing:1px;text-transform:uppercase;">Üzlet azonosítója</div>
          <div style="font-size:14px;font-weight:700;color:#1a1a1a;margin-top:4px;">{deal_id}</div>
        </td>
      </tr>"""
        if deal_id else ""
    )

    reply_mailto = arbekero_reply_mailto(request_id, project_location, adatok.get("contractorNev", ""), tasks)

    html = sablon_betolt("alvallalkozoi_arbekero.html")
    csere = {
        "{{AZONOSITO}}":            request_id,
        "{{PROJEKT_HELYSZINE}}":    project_location,
        "{{UZLET_AZONOSITO_SOR}}":  uzlet_azonosito_sor,
        "{{KEDVES_NEV}}":           kedves_nev,
        "{{FELADATOK_HTML}}":       arbekero_feladatok_html(tasks),
        "{{ANYAGOK_BLOKK}}":        arbekero_anyagok_blokk(materials),
        "{{REPLY_MAILTO}}":         reply_mailto,
    }
    for k, v in csere.items():
        html = html.replace(k, str(v))
    return html


# ─────────────────────────────────────────────
# EMAIL KÜLDÉS – az ÖNÁLLÓ email-küldő Apps Script-en keresztül
# (webapp_email_v1.js, EMAIL_WEBAPP_URL/EMAIL_WEBAPP_SECRET — külön projekt,
# NEM a megrendelés-figyelő scriptje). handleSendEmail "cc" mezőt is fogad
# (GmailApp.sendEmail options.cc) — valódi Cc fejléc, nem külön email.
# ─────────────────────────────────────────────
def email_kuld(cimzett: str, targy: str, html_body: str, cc: str = "") -> bool:
    try:
        payload = {
            "secret":   EMAIL_WEBAPP_SECRET,
            "action":   "sendEmail",
            "to":       cimzett,
            "subject":  targy,
            "htmlBody": html_body,
        }
        if cc:
            payload["cc"] = cc
        r = requests.post(EMAIL_WEBAPP_URL, json=payload, timeout=30)
        resp = r.json()
        if resp.get("success"):
            log.info(f"[EMAIL] Elküldve → {cimzett}" + (f" (Cc: {cc})" if cc else ""))
            return True
        else:
            log.error(f"[EMAIL] Apps Script hiba: {resp}")
            return False
    except Exception as e:
        log.error(f"[EMAIL] Küldési hiba: {e}")
        return False


# ─────────────────────────────────────────────
# PDF ÁRAJÁNLAT EMAIL – deal üzletfelelősének, csatolmánnyal
# Ugyanaz az ÖNÁLLÓ email-küldő Apps Script (EMAIL_WEBAPP_URL).
# Az arajanlat_pdf.py importálja — ne nevezd át!
# ─────────────────────────────────────────────
def PDFquotationSENDdealOWNER(cimzett: str, targy: str, html_body: str,
                               attachment_b64: str, attachment_name: str,
                               attachment_mime: str = "application/pdf") -> bool:
    try:
        r = requests.post(EMAIL_WEBAPP_URL, json={
            "secret":             EMAIL_WEBAPP_SECRET,
            "action":             "PDFquotationSENDdealOWNER",
            "to":                 cimzett,
            "subject":            targy,
            "htmlBody":           html_body,
            "attachmentBase64":   attachment_b64,
            "attachmentName":     attachment_name,
            "attachmentMimeType": attachment_mime,
        }, timeout=60)
        resp = r.json()
        if resp.get("success"):
            log.info(f"[PDF-EMAIL] Elküldve → {cimzett}")
            return True
        else:
            log.error(f"[PDF-EMAIL] Apps Script hiba: {resp}")
            return False
    except Exception as e:
        log.error(f"[PDF-EMAIL] Küldési hiba: {e}")
        return False


# ─────────────────────────────────────────────
# ALVÁLLALKOZÓI BONTÁS – HTML blokk az owner emailhez
# ─────────────────────────────────────────────
def alv_bontas_html_blokk(groups: dict, raw_text: str) -> str:
    """Az owner-összesítőbe: alvállalkozónkénti feladattáblák.
    Ha a parse nem sikerült, a nyers szöveg megy előformázva."""
    if not groups and not raw_text:
        return ""

    if not groups:
        return f"""
  <tr><td style="padding:20px 28px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 14px;font-size:10px;font-weight:700;color:#f0a500;letter-spacing:2px;text-transform:uppercase;">Alvállalkozói feladatok</p>
    <pre style="margin:0;font-family:Consolas,monospace;font-size:12px;color:#444;white-space:pre-wrap;">{raw_text}</pre>
  </td></tr>"""

    blokkokk = ""
    for nev, adat in groups.items():
        sorok = ""
        for i, t in enumerate(adat["tetelek"]):
            bg = "#ffffff" if i % 2 == 0 else "#fafafa"
            sorok += f"""
        <tr style="background:{bg};">
          <td style="padding:8px 10px;font-size:12px;color:#333;border-bottom:1px solid #f0f0f0;">{t["feladat"]}</td>
          <td align="right" style="padding:8px 10px;font-size:12px;color:#555;border-bottom:1px solid #f0f0f0;white-space:nowrap;">{t["mennyiseg"]}</td>
          <td align="right" style="padding:8px 10px;font-size:12px;color:#555;border-bottom:1px solid #f0f0f0;white-space:nowrap;">{t["egysegar"]}</td>
          <td align="right" style="padding:8px 10px;font-size:12px;font-weight:600;color:#1a1a1a;border-bottom:1px solid #f0f0f0;white-space:nowrap;">{t["reszosszeg"]}</td>
        </tr>"""
        osszesen_sor = (
            f'<tr style="background:#f7f7f7;border-top:2px solid #1a1a1a;">'
            f'<td colspan="3" style="padding:9px 10px;font-size:11px;font-weight:700;color:#1a1a1a;letter-spacing:1px;text-transform:uppercase;">Összesen</td>'
            f'<td align="right" style="padding:9px 10px;font-size:13px;font-weight:700;color:#f0a500;white-space:nowrap;">{adat["osszesen"]}</td></tr>'
            if adat.get("osszesen") else ""
        )
        blokkokk += f"""
    <p style="margin:16px 0 8px;font-size:12px;font-weight:700;color:#1a1a1a;">▪ {nev}</p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
      <tr style="background:#1a1a1a;">
        <th style="padding:8px 10px;text-align:left;font-size:8px;color:#aaa;letter-spacing:2px;text-transform:uppercase;font-weight:700;">Feladat</th>
        <th style="padding:8px 10px;text-align:right;font-size:8px;color:#aaa;letter-spacing:2px;text-transform:uppercase;font-weight:700;">Mennyiség</th>
        <th style="padding:8px 10px;text-align:right;font-size:8px;color:#aaa;letter-spacing:2px;text-transform:uppercase;font-weight:700;">Egységár</th>
        <th style="padding:8px 10px;text-align:right;font-size:8px;color:#aaa;letter-spacing:2px;text-transform:uppercase;font-weight:700;">Részösszeg</th>
      </tr>{sorok}{osszesen_sor}
    </table>"""

    return f"""
  <tr><td style="padding:20px 28px;border-bottom:1px solid #eee;">
    <p style="margin:0;font-size:10px;font-weight:700;color:#f0a500;letter-spacing:2px;text-transform:uppercase;">Alvállalkozói feladatbontás</p>{blokkokk}
  </td></tr>"""


# ─────────────────────────────────────────────
# HELYSZÍNI KAPCSOLATTARTÓ – HTML blokk
# ─────────────────────────────────────────────
def site_contact_html_blokk(helyszini_kapcsolattarto: dict, cell_padding: str = "20px 28px") -> str:
    if not helyszini_kapcsolattarto:
        return ""
    nev     = (helyszini_kapcsolattarto.get("nev") or "").strip()
    telefon = (helyszini_kapcsolattarto.get("telefon") or "").strip()
    email   = (helyszini_kapcsolattarto.get("email") or "").strip()
    if not (nev or telefon or email):
        return ""
    return f"""<tr><td style="padding:{cell_padding};border-bottom:1px solid #eee;">
    <p style="margin:0 0 12px;font-size:9px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;">Helyszíni kapcsolattartó</p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr><td style="padding:7px 10px;border-bottom:1px solid #f0f0f0;color:#999;font-size:12px;width:80px;">Név</td><td style="padding:7px 10px;border-bottom:1px solid #f0f0f0;font-weight:600;">{nev or "–"}</td></tr>
      <tr><td style="padding:7px 10px;border-bottom:1px solid #f0f0f0;color:#999;font-size:12px;">Telefon</td><td style="padding:7px 10px;border-bottom:1px solid #f0f0f0;font-weight:600;">{telefon or "–"}</td></tr>
      <tr><td style="padding:7px 10px;color:#999;font-size:12px;">Email</td><td style="padding:7px 10px;font-weight:600;">{email or "–"}</td></tr>
    </table>
  </td></tr>"""


# ─────────────────────────────────────────────
# KIVITELEZÉS HELYSZÍNE – HTML blokk
# ─────────────────────────────────────────────
def site_address_html_blokk(helyszin: str, cell_padding: str = "20px 28px",
                             cimke: str = "Kivitelezés helyszíne") -> str:
    helyszin = (helyszin or "").strip()
    if not helyszin:
        return ""
    return f"""<tr><td style="padding:{cell_padding};border-bottom:1px solid #eee;">
    <p style="margin:0 0 8px;font-size:9px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;">{cimke}</p>
    <p style="margin:0;font-size:14px;font-weight:600;color:#1a1a1a;">{helyszin}</p>
  </td></tr>"""


# ─────────────────────────────────────────────
# OWNER ÖSSZESÍTŐ EMAIL
# ─────────────────────────────────────────────
def owner_email_html(deal_adatok: dict, idopontok: list,
                     kovetelmenyek: list, megjegyzes: str,
                     alv_groups: dict = None, alv_raw: str = "",
                     helyszini_kapcsolattarto: dict = None) -> str:
    ido_sorok = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#999;font-size:12px;">{i+1}. opció</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;font-weight:600;">{d}</td></tr>'
        for i, d in enumerate(idopontok)
    ) or '<tr><td colspan="2" style="padding:8px 12px;color:#aaa;">Nem adott meg időpontot.</td></tr>'

    kov_sorok = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;">&#10003; {k["label"]}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#666;">{k.get("reszlet","") or "–"}</td></tr>'
        for k in kovetelmenyek
    )

    megjegyzes_blokk = (
        f'<tr><td colspan="2" style="padding:20px 28px;border-bottom:1px solid #eee;">'
        f'<p style="margin:0 0 8px;font-size:10px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;">Egyéb megjegyzés</p>'
        f'<p style="margin:0;font-size:13px;color:#444;line-height:1.6;">{megjegyzes}</p></td></tr>'
        if megjegyzes else ""
    )

    site_address_blokk = site_address_html_blokk(deal_adatok.get("kivitelezes_helyszine", ""))
    site_contact_blokk = site_contact_html_blokk(helyszini_kapcsolattarto)
    alv_blokk = alv_bontas_html_blokk(alv_groups or {}, alv_raw)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#e8e8e8;font-family:Arial,sans-serif;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#e8e8e8;">
<tr><td align="center" style="padding:24px 16px;">
<table cellpadding="0" cellspacing="0" border="0" width="680" style="background:#fff;max-width:680px;">
  <tr><td style="background:#1a1a1a;border-bottom:3px solid #f0a500;padding:18px 28px;">
    <p style="margin:0;font-size:18px;font-weight:700;color:#fff;letter-spacing:2px;text-transform:uppercase;">SQM HUNGARY</p>
    <p style="margin:4px 0 0;font-size:10px;color:#888;letter-spacing:2px;text-transform:uppercase;">Ügyfél visszajelzés érkezett</p>
  </td></tr>
  <tr><td style="padding:24px 28px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 4px;font-size:11px;color:#f0a500;font-weight:700;letter-spacing:2px;text-transform:uppercase;">Deal</p>
    <p style="margin:0;font-size:16px;font-weight:700;color:#1a1a1a;">{deal_adatok.get("deal_nev","")}</p>
    <p style="margin:4px 0 0;font-size:12px;color:#888;">{deal_adatok.get("bid_szam","")} · {deal_adatok.get("cegnev","")} · {deal_adatok.get("kapcsolattarto_nev","")}</p>
  </td></tr>
  <tr><td style="padding:20px 28px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 12px;font-size:10px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;">Preferált időpontok</p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%">{ido_sorok}</table>
  </td></tr>
  {site_address_blokk}
  {site_contact_blokk}
  {"<tr><td style='padding:20px 28px;border-bottom:1px solid #eee;'><p style='margin:0 0 12px;font-size:10px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;'>Belépési / helyszíni követelmények</p><table cellpadding='0' cellspacing='0' border='0' width='100%'>" + kov_sorok + "</table></td></tr>" if kov_sorok else ""}
  {megjegyzes_blokk}
  {alv_blokk}
  <tr><td style="background:#f9f9f9;padding:16px 28px;">
    <p style="margin:0;font-size:11px;color:#aaa;">Beküldve: {now} · {deal_adatok.get("bid_szam","")}</p>
  </td></tr>
</table></td></tr></table></body></html>"""


def kivitelezonkenti_email_html(deal_adatok: dict, idopontok: list,
                                kovetelmenyek: list, megjegyzes: str,
                                kivitelezo_nev: str, kivitelezo_adat: dict,
                                helyszini_kapcsolattarto: dict = None) -> str:
    """Owner emailje egy adott kivitelező tételeivel — továbbítható az alvállalkozónak."""

    ido_sorok = "".join(
        f'<tr><td style="padding:7px 10px;border-bottom:1px solid #f0f0f0;color:#999;font-size:12px;width:80px;">{i+1}. opció</td>'
        f'<td style="padding:7px 10px;border-bottom:1px solid #f0f0f0;font-weight:600;font-size:13px;">{d}</td></tr>'
        for i, d in enumerate(idopontok)
    ) or '<tr><td colspan="2" style="padding:8px 10px;color:#aaa;font-size:12px;">Nem adott meg időpontot.</td></tr>'

    kov_sorok = "".join(
        f'<tr><td style="padding:7px 10px;border-bottom:1px solid #f0f0f0;font-size:13px;">&#10003; {k["label"]}</td>'
        f'<td style="padding:7px 10px;border-bottom:1px solid #f0f0f0;font-size:12px;color:#666;">{k.get("reszlet","") or "–"}</td></tr>'
        for k in kovetelmenyek
    )

    tetel_sorok = ""
    for i, t in enumerate(kivitelezo_adat.get("tetelek", [])):
        bg = "#ffffff" if i % 2 == 0 else "#fafafa"
        tetel_sorok += f"""<tr style="background:{bg};">
          <td style="padding:9px 10px;font-size:12px;color:#333;border-bottom:1px solid #f0f0f0;">{t["feladat"]}</td>
          <td style="padding:9px 10px;font-size:12px;color:#555;border-bottom:1px solid #f0f0f0;text-align:right;white-space:nowrap;">{t["mennyiseg"]}</td>
          <td style="padding:9px 10px;font-size:12px;color:#555;border-bottom:1px solid #f0f0f0;text-align:right;white-space:nowrap;">{t["egysegar"]}</td>
          <td style="padding:9px 10px;font-size:12px;font-weight:700;color:#1a1a1a;border-bottom:1px solid #f0f0f0;text-align:right;white-space:nowrap;">{t["reszosszeg"]}</td>
        </tr>"""

    osszesen_sor = (
        f'<tr style="background:#f7f7f7;border-top:2px solid #1a1a1a;">'
        f'<td colspan="3" style="padding:10px;font-size:11px;font-weight:700;color:#1a1a1a;letter-spacing:1px;text-transform:uppercase;">Összesen</td>'
        f'<td style="padding:10px;font-size:14px;font-weight:700;color:#f0a500;text-align:right;white-space:nowrap;">{kivitelezo_adat.get("osszesen","")}</td></tr>'
        if kivitelezo_adat.get("osszesen") else ""
    )

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    bid = deal_adatok.get("bid_szam", "")

    site_address_blokk = site_address_html_blokk(deal_adatok.get("kivitelezes_helyszine", ""), cell_padding="18px 28px")
    site_contact_blokk = site_contact_html_blokk(helyszini_kapcsolattarto, cell_padding="18px 28px")

    megjegyzes_blokk = f"""<tr><td colspan="2" style="padding:18px 28px;border-bottom:1px solid #eee;">
        <p style="margin:0 0 6px;font-size:9px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;">Egyéb megjegyzés</p>
        <p style="margin:0;font-size:13px;color:#444;line-height:1.6;">{megjegyzes}</p>
      </td></tr>""" if megjegyzes else ""

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#e8e8e8;font-family:Arial,sans-serif;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#e8e8e8;">
<tr><td align="center" style="padding:24px 16px;">
<table cellpadding="0" cellspacing="0" border="0" width="680" style="background:#fff;max-width:680px;">

  <tr><td style="background:#1a1a1a;border-bottom:3px solid #f0a500;padding:18px 28px;">
    <p style="margin:0;font-size:18px;font-weight:700;color:#fff;letter-spacing:2px;text-transform:uppercase;">SQM HUNGARY</p>
    <p style="margin:4px 0 0;font-size:9px;color:#888;letter-spacing:2px;text-transform:uppercase;">Ügyfél visszajelzés — {kivitelezo_nev}</p>
  </td></tr>

  <tr><td style="padding:18px 28px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 4px;font-size:9px;font-weight:700;color:#f0a500;letter-spacing:2px;text-transform:uppercase;">Deal</p>
    <p style="margin:0;font-size:15px;font-weight:700;color:#1a1a1a;">{deal_adatok.get("deal_nev","")}</p>
    <p style="margin:4px 0 0;font-size:12px;color:#888;">{bid} · {deal_adatok.get("cegnev","")} · {deal_adatok.get("kapcsolattarto_nev","")}</p>
  </td></tr>

  <tr><td style="padding:18px 28px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 10px;font-size:9px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;">Preferált időpontok</p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%">{ido_sorok}</table>
  </td></tr>

  {site_address_blokk}

  {site_contact_blokk}

  {"<tr><td style='padding:18px 28px;border-bottom:1px solid #eee;'><p style='margin:0 0 10px;font-size:9px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;'>Helyszíni előírások</p><table cellpadding='0' cellspacing='0' border='0' width='100%'>" + kov_sorok + "</table></td></tr>" if kov_sorok else ""}

  {megjegyzes_blokk}

  <tr><td style="padding:18px 28px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 12px;font-size:9px;font-weight:700;color:#f0a500;letter-spacing:2px;text-transform:uppercase;">
      Elvégzendő munkák — {kivitelezo_nev}
    </p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
      <thead>
        <tr style="background:#1a1a1a;">
          <th style="padding:8px 10px;text-align:left;font-size:8px;color:#aaa;letter-spacing:1px;text-transform:uppercase;width:40%;">Feladat</th>
          <th style="padding:8px 10px;text-align:right;font-size:8px;color:#aaa;letter-spacing:1px;text-transform:uppercase;width:20%;">Mennyiség</th>
          <th style="padding:8px 10px;text-align:right;font-size:8px;color:#aaa;letter-spacing:1px;text-transform:uppercase;width:20%;">Egységár</th>
          <th style="padding:8px 10px;text-align:right;font-size:8px;color:#aaa;letter-spacing:1px;text-transform:uppercase;width:20%;">Részösszeg</th>
        </tr>
      </thead>
      <tbody>{tetel_sorok}{osszesen_sor}</tbody>
    </table>
  </td></tr>

  <tr><td style="background:#0d0d0d;padding:10px 28px;">
    <p style="margin:0;font-size:9px;color:#444;">Beküldve: {now} · {bid} · © 2026 SQM Hungary Kft.</p>
  </td></tr>

</table></td></tr></table></body></html>"""


def ugyfel_visszaigazolo_html(deal_adatok: dict, idopontok: list,
                              kovetelmenyek: list, megjegyzes: str,
                              helyszini_kapcsolattarto: dict = None) -> str:
    """Visszaigazoló email az ügyfélnek a beküldött adatokról."""

    ido_sorok = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#999;font-size:12px;">{i+1}. opció</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;font-weight:600;">{d}</td></tr>'
        for i, d in enumerate(idopontok)
    ) or '<tr><td colspan="2" style="padding:8px 12px;color:#aaa;">Nem adott meg időpontot.</td></tr>'

    kov_sorok = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;">&#10003; {k["label"]}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#666;">{k.get("reszlet","") or "–"}</td></tr>'
        for k in kovetelmenyek
    )

    megjegyzes_blokk = (
        f'<tr><td colspan="2" style="padding:20px 28px;border-bottom:1px solid #eee;">'
        f'<p style="margin:0 0 8px;font-size:10px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;">Egyéb megjegyzés</p>'
        f'<p style="margin:0;font-size:13px;color:#444;line-height:1.6;">{megjegyzes}</p></td></tr>'
        if megjegyzes else ""
    )

    site_address_blokk = site_address_html_blokk(
        deal_adatok.get("kivitelezes_helyszine", ""),
        cimke="Kérjük, ellenőrizze a kivitelezés helyszínét"
    )
    site_contact_blokk = site_contact_html_blokk(helyszini_kapcsolattarto)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    bid = deal_adatok.get("bid_szam", "")
    owner_nev = deal_adatok.get("owner_nev", "kollégánk")

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#d8d8d8;font-family:Arial,sans-serif;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#d8d8d8;">
<tr><td align="center" style="padding:32px 16px;">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#fff;max-width:600px;">

  <tr><td style="background:#1a1a1a;border-bottom:3px solid #f0a500;padding:22px 32px;">
    <p style="margin:0;font-size:20px;font-weight:700;color:#fff;letter-spacing:2px;text-transform:uppercase;">SQM HUNGARY</p>
    <p style="margin:4px 0 0;font-size:9px;color:#888;letter-spacing:3px;text-transform:uppercase;">Visszajelzés visszaigazolása</p>
  </td></tr>

  <tr><td style="padding:28px 32px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 12px;font-size:15px;color:#1a1a1a;">Tisztelt <strong>{deal_adatok.get("kapcsolattarto_nev","")}</strong>!</p>
    <p style="margin:0;font-size:14px;color:#555;line-height:1.7;">
      Köszönjük visszajelzését a <strong>{bid}</strong> számú árajánlatunkra vonatkozóan.<br>
      Az alábbiakban összefoglaltuk az Ön által megadott adatokat. Kollégánk, <strong>{owner_nev}</strong> hamarosan felveszi Önnel a kapcsolatot a végleges időpont egyeztetése érdekében.
    </p>
  </td></tr>

  <tr><td style="padding:20px 32px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 12px;font-size:9px;font-weight:700;color:#f0a500;letter-spacing:2px;text-transform:uppercase;">Megadott preferált időpontok</p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%">{ido_sorok}</table>
  </td></tr>

  {site_address_blokk}

  {site_contact_blokk}

  {"<tr><td style='padding:20px 32px;border-bottom:1px solid #eee;'><p style='margin:0 0 12px;font-size:9px;font-weight:700;color:#f0a500;letter-spacing:2px;text-transform:uppercase;'>Jelzett helyszíni előírások</p><table cellpadding='0' cellspacing='0' border='0' width='100%'>" + kov_sorok + "</table></td></tr>" if kov_sorok else ""}

  {megjegyzes_blokk}

  <tr><td style="background:#1a1a1a;padding:20px 32px;">
    <table cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr>
        <td style="vertical-align:top;">
          <p style="margin:0 0 3px;font-size:13px;font-weight:700;color:#fff;letter-spacing:1px;text-transform:uppercase;">SQM Hungary Kft.</p>
          <p style="margin:0;font-size:9px;color:#555;letter-spacing:2px;text-transform:uppercase;">Ipari padlóburkolás &amp; bevonatok</p>
        </td>
        <td align="right" style="vertical-align:top;">
          <p style="margin:0 0 2px;font-size:11px;color:#777;">{owner_nev}</p>
          <p style="margin:0 0 2px;font-size:11px;color:#777;"><a href="mailto:{deal_adatok.get('owner_email','')}" style="color:#f0a500;text-decoration:none;">{deal_adatok.get("owner_email","")}</a></p>
          <p style="margin:0;font-size:11px;"><a href="https://sqm-hungary.hu" style="color:#f0a500;text-decoration:none;">sqm-hungary.hu</a></p>
        </td>
      </tr>
    </table>
  </td></tr>

  <tr><td style="background:#0d0d0d;padding:9px 32px;">
    <p style="margin:0;font-size:9px;color:#444;">Beküldve: {now} &nbsp;·&nbsp; {bid} &nbsp;·&nbsp; © 2026 SQM Hungary Kft.</p>
  </td></tr>

</table></td></tr></table></body></html>"""


# ─────────────────────────────────────────────
# ZÁROLT (READ-ONLY) OLDAL – ha az ügyfél már beküldte az adatlapot
# ─────────────────────────────────────────────
def visszajelzes_zarolt_oldal_html(adatok: dict, beerkezett: dict) -> str:
    """
    Statikus, nem szerkeszthető összefoglaló — ezt látja mind az ügyfél (ha
    újra megnyitja a linket), mind a kolléga (ha a Pipedrive note linkjéről
    nyitja meg). Nincs input, nincs submit — az adatlap le van zárva.
    """
    idopontok     = beerkezett.get("idopontok") or []
    kovetelmenyek = beerkezett.get("kovetelmenyek") or []
    megjegyzes    = beerkezett.get("megjegyzes") or ""
    hk            = beerkezett.get("helyszini_kapcsolattarto") or {}
    beerkezve     = beerkezett.get("beerkezve") or ""

    ido_html = "".join(
        f'<div class="row"><span class="row-label">{i+1}. opció</span><span class="row-value">{d}</span></div>'
        for i, d in enumerate(idopontok)
    ) or '<div class="row"><span class="row-value" style="color:#999;">Nem adott meg időpontot.</span></div>'

    hk_html = ""
    if hk.get("nev") or hk.get("telefon") or hk.get("email"):
        hk_html = f"""
    <div class="section">
      <div class="section-title">Helyszíni kapcsolattartó</div>
      <div class="row"><span class="row-label">Név</span><span class="row-value">{hk.get('nev','–')}</span></div>
      <div class="row"><span class="row-label">Telefon</span><span class="row-value">{hk.get('telefon','–')}</span></div>
      <div class="row"><span class="row-label">Email</span><span class="row-value">{hk.get('email','–')}</span></div>
    </div>"""

    kov_html = ""
    if kovetelmenyek:
        sorok = "".join(
            f'<div class="row"><span class="row-value">&#10003; {k.get("label","")}'
            + (f' — {k.get("reszlet")}' if k.get("reszlet") else "")
            + '</span></div>'
            for k in kovetelmenyek
        )
        kov_html = f"""
    <div class="section">
      <div class="section-title">Belépési &amp; helyszíni követelmények</div>
      {sorok}
    </div>"""

    megjegyzes_html = (
        f"""
    <div class="section">
      <div class="section-title">Egyéb megjegyzés</div>
      <p style="margin:0;font-size:14px;color:#444;line-height:1.6;">{megjegyzes}</p>
    </div>"""
        if megjegyzes else ""
    )

    owner_nev   = adatok.get("owner_nev", "kollégánk")
    owner_email = adatok.get("owner_email", "")
    bid         = adatok.get("bid_szam", "")

    return f"""<!DOCTYPE html>
<html lang="hu"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SQM Hungary – Előkészítési adatlap | {bid}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{background:#dcdcdc;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#333;padding:40px 16px;}}
.wrapper{{max-width:680px;margin:0 auto;background:#fff;box-shadow:0 8px 48px rgba(0,0,0,0.18);}}
.header{{background:#1a1a1a;padding:26px 36px;border-bottom:3px solid #f0a500;}}
.header p{{margin:0;color:#fff;font-weight:700;font-size:22px;letter-spacing:2px;text-transform:uppercase;}}
.banner{{background:#fbf6ea;border-bottom:3px solid #f0a500;padding:22px 36px;}}
.banner-title{{font-weight:700;font-size:16px;color:#1a1a1a;margin-bottom:6px;}}
.banner-text{{font-size:13px;color:#666;line-height:1.6;}}
.section{{padding:20px 36px;border-bottom:1px solid #ebebeb;}}
.section-title{{font-weight:700;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#f0a500;margin-bottom:12px;}}
.row{{display:flex;gap:12px;padding:6px 0;border-bottom:1px solid #f5f5f5;font-size:13px;}}
.row-label{{color:#999;width:90px;flex-shrink:0;}}
.row-value{{color:#1a1a1a;font-weight:600;}}
.footer{{background:#f4f4f4;padding:20px 36px;border-top:3px solid #f0a500;font-size:12px;color:#666;}}
</style></head>
<body>
<div class="wrapper">
  <div class="header"><p>SQM Hungary</p></div>
  <div class="banner">
    <div class="banner-title">✓ Ezt az adatlapot már beküldték</div>
    <div class="banner-text">
      Beküldve: <strong>{beerkezve}</strong> · Referencia: <strong>{bid}</strong><br>
      Az adatok módosításához vegye fel a kapcsolatot kollégánkkal: <strong>{owner_nev}</strong>
      {f' (<a href="mailto:{owner_email}" style="color:#b5790a;">{owner_email}</a>)' if owner_email else ''}.
    </div>
  </div>
  <div class="section">
    <div class="section-title">Beküldött preferált időpontok</div>
    {ido_html}
  </div>
  {hk_html}
  {kov_html}
  {megjegyzes_html}
  <div class="footer">SQM Hungary Kft. · Ipari padlóburkolás &amp; bevonatok · sqm-hungary.hu</div>
</div>
</body></html>"""


# ─────────────────────────────────────────────
# IN-MEMORY LOCK – azonnali dupla-webhook védelem
# (a tartós védelem a Pipedrive note)
# ─────────────────────────────────────────────
_feldolgozott_dealek: set = set()
_bekuldott_tokenek: set = set()   # dupla submit védelem (best effort)


# ─────────────────────────────────────────────
# FLASK BLUEPRINT – 3 végpont
# ─────────────────────────────────────────────
pd_bp = Blueprint("pipedrive", __name__)


@pd_bp.route("/pipedrive-webhook", methods=["POST"])
@pd_bp.route("/pipedrive-webhook/", methods=["POST"])
def pipedrive_webhook():
    """Pipedrive Automation hívja, ha egy deal won-ra változik."""
    adat = request.get_json(silent=True)
    if not adat:
        return jsonify({"ok": False, "hiba": "Üres payload"}), 400

    log.info(f"[WEBHOOK] Payload: {str(adat)[:300]}")

    deal_id = None
    if "current" in adat:
        deal_id = adat["current"].get("id")
    elif "deal" in adat:
        deal_id = adat["deal"].get("id")
    elif "dealId" in adat:
        deal_id = adat["dealId"]
    elif "id" in adat:
        deal_id = adat["id"]

    if not deal_id:
        log.warning(f"[WEBHOOK] Nem találtam deal ID-t: {adat}")
        return jsonify({"ok": False, "hiba": "Nincs deal ID"}), 400

    deal_id = int(deal_id)

    # ── 1. In-memory lock – azonnali duplikáció védelem ──
    if deal_id in _feldolgozott_dealek:
        log.info(f"[WEBHOOK] Deal {deal_id} – már feldolgozás alatt, kihagyva.")
        return jsonify({"ok": True, "info": "Már feldolgozás alatt"}), 200
    _feldolgozott_dealek.add(deal_id)

    # ── 2. Pipedrive API státusz ellenőrzés ──
    deal_ellenorzes = _pd_get(f"deals/{deal_id}")
    if not deal_ellenorzes:
        _feldolgozott_dealek.discard(deal_id)
        return jsonify({"ok": False, "hiba": "Deal nem található"}), 404

    aktualis_status = deal_ellenorzes.get("status", "")
    log.info(f"[WEBHOOK] Deal {deal_id} státusz: '{aktualis_status}'")
    if aktualis_status != "won":
        _feldolgozott_dealek.discard(deal_id)
        return jsonify({"ok": True, "info": f"Státusz: {aktualis_status}"}), 200

    # ── 3. Tartós duplikáció-védelem: Pipedrive note ──
    if deal_mar_ertesitve(deal_id):
        log.info(f"[WEBHOOK] Deal {deal_id} – note szerint már értesítve.")
        return jsonify({"ok": True, "info": "Email már elküldve (note)"}), 200

    log.info(f"[WEBHOOK] Deal megnyerve, email küldés indul: {deal_id}")

    adatok = deal_adatok_osszerak(deal_id, innonest_kell=True)
    if not adatok:
        _feldolgozott_dealek.discard(deal_id)
        return jsonify({"ok": False, "hiba": "Adatok lekérése sikertelen"}), 500

    token = token_general(deal_id)

    try:
        email_html = sablon_kitolt(sablon_betolt("email_kikuldo.html"), adatok, token)
        targy = f"Kivitelezési tájékoztató – {adatok['bid_szam']}"

        # ── Valódi Cc a deal owner-nek ──
        # A webapp_v7.js handleSendEmail mostantól "cc" mezőt is fogad
        # (GmailApp.sendEmail options.cc) — egyetlen email megy ki, az owner
        # valódi Cc-ben kapja, nem külön (duplikált) levélként.
        owner_email = adatok.get("owner_email", "")
        if not owner_email:
            log.warning(f"[WEBHOOK] Nincs owner email a dealen ({deal_id}) — Cc kihagyva.")

        siker = email_kuld(adatok["kapcsolattarto_email"], targy, email_html, cc=owner_email)
        if siker:
            deal_ertesites_rogzit(deal_id, token, adatok["kapcsolattarto_email"])
            log.info(f"[WEBHOOK] Email elküldve: {adatok['kapcsolattarto_email']}" +
                    (f" (Cc: {owner_email})" if owner_email else ""))
        else:
            _feldolgozott_dealek.discard(deal_id)
            return jsonify({"ok": False, "hiba": "Email küldés sikertelen"}), 500
    except Exception as e:
        _feldolgozott_dealek.discard(deal_id)
        log.error(f"[WEBHOOK] Email hiba: {e}")
        return jsonify({"ok": False, "hiba": str(e)}), 500

    return jsonify({"ok": True, "token": token, "bid": adatok["bid_szam"]}), 200


@pd_bp.route("/<token>", methods=["GET"])
def visszajelzes_oldal(token):
    """Az ügyfél böngészőben nyitja meg. Az adatok élőben jönnek a Pipedrive-ból
    (+ Innonestből az árajánlat végösszege, rövid BID-cache-eléssel)."""
    deal_id = token_ellenoriz(token)
    if not deal_id:
        return "<h2 style='font-family:sans-serif;padding:40px;color:#c00;'>Ez a link már nem érvényes.</h2>", 404
    try:
        adatok = deal_adatok_osszerak(deal_id, innonest_kell=True)
        if not adatok:
            return "<h2 style='font-family:sans-serif;padding:40px;color:#c00;'>Ez a link már nem érvényes.</h2>", 404

        # ── Zárolás: ha már beküldte, senki nem szerkesztheti újra ──
        beerkezett = deal_visszajelzes_adatok(deal_id)
        if beerkezett is not None:
            return visszajelzes_zarolt_oldal_html(adatok, beerkezett), 200

        html = sablon_kitolt(sablon_betolt("visszajelzes_oldal.html"), adatok, token)
        return html, 200
    except Exception as e:
        log.error(f"[OLDAL] Render hiba: {e}")
        return "<h2 style='font-family:sans-serif;padding:40px;'>Hiba. Vegye fel a kapcsolatot kollégánkkal.</h2>", 500


@pd_bp.route("/visszajelzes-submit", methods=["POST"])
def visszajelzes_submit():
    """Az ügyfél beküldi a kitöltött adatlapot."""
    adat = request.get_json(silent=True)
    if not adat:
        return jsonify({"ok": False}), 400

    token = adat.get("token", "")
    deal_id = token_ellenoriz(token)
    if not deal_id:
        return jsonify({"ok": False, "hiba": "Érvénytelen token"}), 404

    # Dupla submit védelem — gyors út memóriából, majd tartós ellenőrzés note-ból
    # (ez utóbbi redeploy után is véd, nem csak a folyamat élettartama alatt).
    if token in _bekuldott_tokenek:
        return jsonify({"ok": True, "info": "Már beküldve"}), 200
    if deal_visszajelzes_adatok(deal_id) is not None:
        _bekuldott_tokenek.add(token)
        return jsonify({"ok": True, "info": "Már beküldve"}), 200

    deal = deal_adatok_osszerak(deal_id, innonest_kell=False)
    if not deal:
        return jsonify({"ok": False, "hiba": "Deal nem elérhető"}), 502

    idopontok                = adat.get("idopontok", [])
    kovetelmenyek            = adat.get("kovetelmenyek", [])
    megjegyzes               = adat.get("megjegyzes", "")
    helyszini_kapcsolattarto = adat.get("helyszini_kapcsolattarto") or {}

    # ── Alvállalkozói bontás a Pipedrive mezőkből (a kalkulátor írta oda) ──
    alv_raw    = deal.get("feladat_reszletezes_raw", "")
    alv_groups = alv_bontas_parse(alv_raw)
    log.info(f"[SUBMIT] Alvállalkozói bontás: {len(alv_groups)} kivitelező ({deal.get('bid_szam','')})")

    # ── Owner összesítő email ──
    o_html = owner_email_html(deal, idopontok, kovetelmenyek, megjegyzes,
                              alv_groups, alv_raw, helyszini_kapcsolattarto)
    o_targy = f"[Visszajelzés] {deal.get('cegnev','')} – {deal.get('bid_szam','')}"
    owner_siker = email_kuld(deal.get("owner_email", ""), o_targy, o_html)

    if not owner_siker:
        # Az ügyfél újra próbálkozhat — még semmi mást nem küldtünk ki.
        return jsonify({"ok": False}), 500

    _bekuldott_tokenek.add(token)

    # ── Kivitelezőnként külön (továbbítható) email az ownernek ──
    if len(alv_groups) > 1:
        for kiv_nev, kiv_adat in alv_groups.items():
            kiv_html = kivitelezonkenti_email_html(
                deal, idopontok, kovetelmenyek, megjegyzes,
                kiv_nev, kiv_adat, helyszini_kapcsolattarto
            )
            kiv_targy = f"[{kiv_nev}] {deal.get('cegnev','')} – {deal.get('bid_szam','')}"
            email_kuld(deal.get("owner_email", ""), kiv_targy, kiv_html)
            log.info(f"[SUBMIT] Kivitelező email elküldve: {kiv_nev}")

    # ── Ügyfél visszaigazoló email ──
    u_html = ugyfel_visszaigazolo_html(deal, idopontok, kovetelmenyek, megjegyzes,
                                       helyszini_kapcsolattarto)
    u_targy = f"Visszajelzés visszaigazolása – {deal.get('bid_szam','')} | SQM Hungary"
    email_kuld(deal.get("kapcsolattarto_email", ""), u_targy, u_html)

    # ── Visszajelzés naplózása a Pipedrive dealen (note) ──
    try:
        visszajelzes_note_rogzit(deal_id, idopontok, kovetelmenyek,
                                 megjegyzes, helyszini_kapcsolattarto)
    except Exception as e:
        log.warning(f"[SUBMIT] Note rögzítés sikertelen: {e}")

    return jsonify({"ok": True}), 200


# ─────────────────────────────────────────────
# ÁLTALÁNOS EMAIL VÉGPONT – a kalkulátor (böngésző) hívja
# Ugyanazt a már meglévő email_kuld() segédfüggvényt (Apps Script proxy)
# használja, mint a fenti visszajelzés-folyamat. Elsődlegesen az
# Alvállalkozói díjkalkulátor "Árajánlat-kérés" gombjához készült, de
# bármelyik nézet hívhatja, ahol a kalkulátorból kell emailt küldeni.
# A titkos kulcs (EMAIL_WEBAPP_SECRET) csak itt, szerver-oldalon él —
# a böngésző csak a to/subject/htmlBody adatokat küldi, titok nélkül.
# ─────────────────────────────────────────────
@pd_bp.route("/send-alv-email", methods=["POST"])
def send_alv_email():
    """A kalkulátor hívja árajánlat-kérés kiküldésekor."""
    adat = request.get_json(silent=True) or {}
    cimzett   = (adat.get("to") or "").strip()
    targy     = (adat.get("subject") or "").strip()
    html_body = adat.get("htmlBody") or ""
    cc        = (adat.get("cc") or "").strip()

    if not cimzett:
        return jsonify({"ok": False, "hiba": "Hiányzó 'to' cím"}), 400
    if not targy:
        return jsonify({"ok": False, "hiba": "Hiányzó tárgy"}), 400

    siker = email_kuld(cimzett, targy, html_body, cc=cc)
    if not siker:
        return jsonify({"ok": False, "hiba": "Email küldés sikertelen (lásd Railway log)"}), 502
    return jsonify({"ok": True}), 200


# ─────────────────────────────────────────────
# ALVÁLLALKOZÓI ÁRBEKÉRÉS VÉGPONT – a kalkulátor csak strukturált adatot küld
# (nem kész HTML-t); a sablonok/alvallalkozoi_arbekero.html fájlból épül fel
# a levél, ugyanazon az EMAIL_WEBAPP_URL / email_kuld() csatornán megy ki,
# mint a /send-alv-email.
# ─────────────────────────────────────────────
@pd_bp.route("/send-arbekero-email", methods=["POST"])
def send_arbekero_email():
    """A kalkulátor Alv oldala hívja az "Árajánlat-kérés" gomb megnyomásakor."""
    adat = request.get_json(silent=True) or {}
    cimzett = (adat.get("to") or "").strip()
    if not cimzett:
        return jsonify({"ok": False, "hiba": "Hiányzó 'to' cím"}), 400

    request_id = (adat.get("requestId") or "").strip()
    project_location = (adat.get("projectLocation") or "").strip()
    targy = f"Alvállalkozói árbekérés – {project_location or 'Projekt helyszín nélkül'} – {request_id or 'SQM projekt'}"

    try:
        html_body = arbekero_kitolt(adat)
    except FileNotFoundError:
        return jsonify({"ok": False, "hiba": "Hiányzó sablon: sablonok/alvallalkozoi_arbekero.html"}), 500

    siker = email_kuld(cimzett, targy, html_body)
    if not siker:
        return jsonify({"ok": False, "hiba": "Email küldés sikertelen (lásd Railway log)"}), 502
    return jsonify({"ok": True}), 200


# ─────────────────────────────────────────────
# PIPEDRIVE PROJEKT LEKÉRÉS – üzlet ID (vagy BID) → projekt id, dátumok, link
# A projekt mindig a dealből "átmásolt" projekt a Pipedrive Projektmodulban.
# A kalkulátor hívja (Projekt oldal), hogy megjelenítse a linket + dátumokat,
# és hogy az Innonest megrendelésbe is beírhassa a kezdés/befejezés dátumot.
# ─────────────────────────────────────────────
@pd_bp.route("/pipedrive-project", methods=["GET"])
def pipedrive_project():
    deal_id = (request.args.get("deal_id") or "").strip()
    bid     = (request.args.get("bid") or "").strip()

    if not deal_id and bid:
        try:
            from pipedrive_webapp import _pd_find_deal_by_bid
            did = _pd_find_deal_by_bid(bid)
            if did:
                deal_id = str(did)
        except Exception as e:
            log.warning(f"[PD] BID→deal keresés hiba: {e}")

    if not deal_id:
        return jsonify({"ok": False, "hiba": "Nincs üzlet ID vagy BID"}), 400

    proj = _pd_project_by_deal(deal_id)
    if not proj:
        return jsonify({"ok": True, "found": False, "dealId": deal_id})

    pid = proj.get("id")
    return jsonify({
        "ok":        True,
        "found":     True,
        "dealId":    deal_id,
        "projectId": pid,
        "title":     proj.get("title"),
        "startDate": proj.get("start_date"),
        "endDate":   proj.get("end_date"),
        "url":       (PIPEDRIVE_PROJECT_BASE + str(pid)) if pid else "",
    }), 200


def register_pipedrive_routes(app):
    """Hívd meg a server.py-ból: register_pipedrive_routes(app)"""
    app.register_blueprint(pd_bp)
    log.info("[PIPEDRIVE] Végpontok regisztrálva: /pipedrive-webhook, /visszajelzes/<token>, /visszajelzes-submit, /send-alv-email, /send-arbekero-email, /pipedrive-project (v2, Sheet-mentes)")
