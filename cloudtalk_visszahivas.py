"""
cloudtalk_visszahivas.py – Nem fogadott CloudTalk hívás → Pipedrive "Visszahívás" tevékenység
==============================================================================================
A CloudTalk Workflow Automation (Trigger: Call > Ended, feltétel: direction = incoming,
talking_time = 0) API Request akciója POST-ol ide. A modul:
  1. ellenőrzi a titkos kulcsot (X-SQM-Secret header VAGY "secret" mező a bodyban),
  2. call_uuid alapján kiszűri a duplikált hívásokat,
  3. telefonszám alapján megkeresi a személyt Pipedrive-ban (+ cég, + legutóbbi nyitott deal),
  4. létrehoz egy "call" típusú, mai határidős tevékenységet, vagy ha ugyanarról a számról
     ma már van nyitott visszahívás-tevékenység, azt frissíti ("2x hívott").

Végpont: POST /cloudtalk-visszahivas

Környezeti változók (Railway → Variables):
  PIPEDRIVE_API_TOKEN            – (meglévő) Pipedrive API token
  CLOUDTALK_WEBHOOK_SECRET       – új, tetszőleges hosszú véletlen string
  CLOUDTALK_VISSZAHIVAS_USER_ID  – opcionális: Pipedrive user ID, akihez a tevékenység kerül
                                   (üresen a token tulajdonosához kerül)
  CLOUDTALK_SZAM_FELELOS         – opcionális JSON, a kódba írt SZAM_FELELOS_ALAP-ot
                                   egészíti ki / írja felül: belső szám → Pipedrive user
                                   neve VAGY ID-ja, pl. {"+3612345678": "Kiss Anna"}

Felelős kiválasztása: a HÍVOTT SQM szám (internal_number) alapján. A név alapján a
Pipedrive felhasználók közül keresi ki az ID-t (ékezet- és sorrendfüggetlenül, tehát
"Dudás Mária" = "Maria Dudas"). Ha nincs egyezés → CLOUDTALK_VISSZAHIVAS_USER_ID.

CloudTalk API Request "Values" (Key → Value, a Value-t a jobb oldali listából kattintva):
  secret          → <CLOUDTALK_WEBHOOK_SECRET értéke>
  external_number → {{ event.properties.external_number }}
  internal_number → {{ event.properties.internal_number }}
  call_uuid       → {{ event.properties.call_uuid }}
  started_at      → {{ event.properties.started_at }}
  waiting_time    → {{ event.properties.waiting_time }}
"""
import os
import re
import html
import json
import time
import logging
import threading
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
from flask import request, jsonify

log = logging.getLogger(__name__)

PIPEDRIVE_API_TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN", "")
CLOUDTALK_WEBHOOK_SECRET = os.environ.get("CLOUDTALK_WEBHOOK_SECRET", "")
ALAP_USER_ID = os.environ.get("CLOUDTALK_VISSZAHIVAS_USER_ID", "").strip()

# Hívott SQM szám → felelős (Pipedrive user neve vagy ID-ja)
SZAM_FELELOS_ALAP = {
    "+3619991661": "Dudás Mária",
    "+36202106309": "Dudás Mária",
}

try:
    _env_map = json.loads(os.environ.get("CLOUDTALK_SZAM_FELELOS", "") or "{}")
except Exception:
    log.error("[CT] CLOUDTALK_SZAM_FELELOS nem érvényes JSON – figyelmen kívül hagyva")
    _env_map = {}
SZAM_FELELOS = {**SZAM_FELELOS_ALAP, **(_env_map if isinstance(_env_map, dict) else {})}

PD_BASE = "https://api.pipedrive.com/api/v2"   # v1 aug. 1. óta out-of-support
TARGY_PREFIX = "📞 Visszahívás"

# ── In-memory állapot (újraindításkor törlődik – ez elfogadható) ──────────────
_lock = threading.Lock()
_feldolgozott_hivasok: dict[str, float] = {}            # call_uuid → timestamp
_mai_tevekenysegek: dict[str, tuple[int, str, int]] = {}  # szám → (activity_id, nap, hívásszám)
_DUP_TTL = 24 * 3600


# ── Idő ───────────────────────────────────────────────────────────────────────
def _utolso_vasarnap(ev: int, honap: int) -> datetime:
    # A hónap utolsó napjától visszalépünk vasárnapig
    kov = datetime(ev + (honap == 12), honap % 12 + 1, 1, tzinfo=timezone.utc)
    nap = kov - timedelta(days=1)
    return nap - timedelta(days=(nap.weekday() - 6) % 7)


def budapest_ido(utc_dt: datetime) -> datetime:
    """UTC → Budapest helyi idő (EU nyári időszámítás), tzdata nélkül."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    utc_dt = utc_dt.astimezone(timezone.utc)
    ev = utc_dt.year
    nyar_kezd = _utolso_vasarnap(ev, 3).replace(hour=1)   # 01:00 UTC
    nyar_vege = _utolso_vasarnap(ev, 10).replace(hour=1)  # 01:00 UTC
    eltolas = 2 if nyar_kezd <= utc_dt < nyar_vege else 1
    return (utc_dt + timedelta(hours=eltolas)).replace(tzinfo=None)


def _parse_utc(s: str) -> datetime | None:
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ── Telefonszám ───────────────────────────────────────────────────────────────
def normalizal_szam(nyers: str) -> str:
    """Bármilyen formátum → +36… (E.164). Üres string, ha nem értelmezhető."""
    if not nyers:
        return ""
    s = str(nyers).strip()
    plusz = s.startswith("+")
    d = re.sub(r"\D", "", s)
    if len(d) < 6:
        return ""
    if plusz:
        return "+" + d
    if d.startswith("00"):
        return "+" + d[2:]
    if d.startswith("06"):
        return "+36" + d[2:]
    if d.startswith("36") and len(d) >= 10:
        return "+" + d
    return "+36" + d  # pl. 301234567 → +36301234567


def _utolso9(szam: str) -> str:
    return re.sub(r"\D", "", szam or "")[-9:]


def kereso_variansok(e164: str) -> list[str]:
    """Pipedrive-ban a számok vegyes formátumban vannak – több alakkal keresünk."""
    d = re.sub(r"\D", "", e164)
    v = [e164, d]
    if d.startswith("36"):
        belfoldi = d[2:]
        v += ["06" + belfoldi, belfoldi]
    v.append(d[-9:])
    seen, out = set(), []
    for x in v:
        if x and len(x) >= 6 and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def megjelenitett_szam(e164: str) -> str:
    """+36301234567 → +36 30 123 4567 ; +3612345678 → +36 1 234 5678"""
    d = re.sub(r"\D", "", e164)
    if d.startswith("361") and len(d) == 10:  # budapesti vezetékes
        return f"+36 1 {d[3:6]} {d[6:]}"
    if d.startswith("36") and len(d) == 11:  # mobil: +36 30 123 4567
        b = d[2:]
        return f"+36 {b[:2]} {b[2:5]} {b[5:]}"
    if d.startswith("36") and len(d) == 10:  # vidéki vezetékes: +36 72 123 456
        b = d[2:]
        return f"+36 {b[:2]} {b[2:5]} {b[5:]}"
    return e164


# ── Pipedrive ─────────────────────────────────────────────────────────────────
def _pd(method: str, endpoint: str, params: dict | None = None, payload: dict | None = None):
    p = {"api_token": PIPEDRIVE_API_TOKEN}
    if params:
        p.update(params)
    try:
        r = requests.request(method, f"{PD_BASE}/{endpoint}", params=p, json=payload, timeout=15)
        data = r.json()
        if data.get("success"):
            return data.get("data")
        log.error(f"[CT→PD] {method} {endpoint} sikertelen: {str(data)[:300]}")
    except Exception as e:
        log.error(f"[CT→PD] {method} {endpoint}: {e}")
    return None


def szemely_keresese(e164: str) -> dict | None:
    """Visszaadja: {person_id, nev, org_id, ceg} vagy None. Csak valódi egyezést fogad el
    (a talált személy valamelyik telefonszámának utolsó 9 jegye egyezik)."""
    cel = _utolso9(e164)
    for term in kereso_variansok(e164):
        data = _pd("GET", "persons/search",
                   {"term": term, "fields": "phone", "exact_match": "false", "limit": 10})
        for elem in (data or {}).get("items", []) or []:
            item = elem.get("item") or {}
            if any(_utolso9(p) == cel for p in (item.get("phones") or [])):
                org = item.get("organization") or {}
                return {
                    "person_id": item.get("id"),
                    "nev": item.get("name") or "",
                    "org_id": org.get("id"),
                    "ceg": org.get("name") or "",
                }
    return None


def nyitott_deal(person_id: int) -> dict | None:
    deals = _pd("GET", "deals", {"person_id": person_id, "status": "open",
                                 "sort_by": "update_time", "sort_direction": "desc", "limit": 1})
    if deals:
        return {"id": deals[0].get("id"), "cim": deals[0].get("title") or ""}
    return None


_user_cache: dict = {"ido": 0.0, "userek": []}


def _nev_kulcs(nev: str) -> tuple:
    """Ékezet- és sorrendfüggetlen összehasonlító kulcs: 'Dudás Mária' = 'Maria Dudas'."""
    tiszta = unicodedata.normalize("NFKD", str(nev or "")).encode("ascii", "ignore").decode()
    return tuple(sorted(re.findall(r"[a-z]+", tiszta.lower())))


def _pipedrive_userek() -> list:
    """Aktív Pipedrive felhasználók, 1 órás cache-sel. (A /v1/users nincs a kivezetett
    végpontok között, ezért itt v1-et használunk.)"""
    with _lock:
        if _user_cache["userek"] and time.time() - _user_cache["ido"] < 3600:
            return _user_cache["userek"]
    try:
        r = requests.get("https://api.pipedrive.com/v1/users",
                         params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=15)
        data = r.json()
        userek = [u for u in (data.get("data") or []) if u.get("active_flag", True)] \
            if data.get("success") else []
    except Exception as e:
        log.error(f"[CT→PD] users lekérés: {e}")
        userek = []
    if userek:
        with _lock:
            _user_cache.update(ido=time.time(), userek=userek)
    return userek


def user_id_nev_alapjan(nev: str) -> int | None:
    kulcs = _nev_kulcs(nev)
    if not kulcs:
        return None
    for u in _pipedrive_userek():
        if _nev_kulcs(u.get("name")) == kulcs:
            return u.get("id")
    log.warning(f"[CT] Nincs ilyen nevű aktív Pipedrive user: {nev!r}")
    return None


def felelos_user(internal_e164: str) -> int | None:
    cel = _utolso9(internal_e164)
    if cel:
        for k, v in SZAM_FELELOS.items():
            if _utolso9(k) == cel:
                if isinstance(v, int) or str(v).strip().isdigit():
                    return int(v)
                uid = user_id_nev_alapjan(str(v))
                if uid:
                    return uid
                break  # név nem található → alapértelmezett felelős
    return int(ALAP_USER_ID) if ALAP_USER_ID.isdigit() else None


# ── Fő logika ─────────────────────────────────────────────────────────────────
def _takarit(most: float):
    for k in [k for k, t in _feldolgozott_hivasok.items() if most - t > _DUP_TTL]:
        del _feldolgozott_hivasok[k]


def feldolgoz(adat: dict) -> dict:
    most = time.time()
    call_uuid = str(adat.get("call_uuid") or "").strip()

    with _lock:
        _takarit(most)
        if call_uuid and call_uuid in _feldolgozott_hivasok:
            return {"status": "duplikalt", "call_uuid": call_uuid}
        if call_uuid:
            _feldolgozott_hivasok[call_uuid] = most

    kulso = normalizal_szam(adat.get("external_number"))
    belso = normalizal_szam(adat.get("internal_number"))
    hivas_utc = _parse_utc(adat.get("started_at")) or datetime.now(timezone.utc)
    hivas_hu = budapest_ido(hivas_utc)
    ma = budapest_ido(datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    ido_str = hivas_hu.strftime("%Y.%m.%d. %H:%M")
    try:
        varakozas = int(float(adat.get("waiting_time") or 0))
    except (TypeError, ValueError):
        varakozas = 0

    # Ugyanarról a számról ma már van nyitott tevékenység? → frissítjük
    if kulso:
        with _lock:
            korabbi = _mai_tevekenysegek.get(kulso)
        if korabbi and korabbi[1] == ma:
            act_id, _, db = korabbi
            akt = _pd("GET", f"activities/{act_id}")
            if akt and not akt.get("done"):
                uj_db = db + 1
                targy = re.sub(r" \(\d+x hívott\)$", "", akt.get("subject") or "") + f" ({uj_db}x hívott)"
                jegyzet = (akt.get("note") or "") + f"<br>• Újabb nem fogadott hívás: {ido_str}"
                if _pd("PATCH", f"activities/{act_id}", payload={"subject": targy, "note": jegyzet}):
                    with _lock:
                        _mai_tevekenysegek[kulso] = (act_id, ma, uj_db)
                    log.info(f"[CT] Tevékenység frissítve #{act_id} ({kulso}, {uj_db}x)")
                    return {"status": "frissitve", "activity_id": act_id}

    szemely = szemely_keresese(kulso) if kulso else None
    deal = nyitott_deal(szemely["person_id"]) if szemely else None

    if not kulso:
        targy = f"{TARGY_PREFIX}: rejtett számról hívtak"
    else:
        ki = " – ".join(x for x in [(szemely or {}).get("ceg"), (szemely or {}).get("nev")] if x)
        targy = f"{TARGY_PREFIX}: {megjelenitett_szam(kulso)}" + (f" ({ki})" if ki else "")

    sorok = [
        f"<b>Nem fogadott bejövő hívás</b> – {ido_str}",
        f"Hívó: {megjelenitett_szam(kulso) if kulso else 'rejtett szám'}",
    ]
    if belso:
        sorok.append(f"Hívott SQM szám: {megjelenitett_szam(belso)}")
    if varakozas:
        sorok.append(f"Kicsengetési idő: {varakozas} mp")
    if not szemely and kulso:
        sorok.append("<i>Ismeretlen szám – nincs hozzá Pipedrive kontakt.</i>")
    if deal:
        sorok.append(f"Kapcsolt nyitott deal: {html.escape(deal['cim'])}")
    if call_uuid:
        sorok.append(f"<small>CloudTalk call_uuid: {call_uuid}</small>")

    payload = {
        "subject": targy,
        "type": "call",
        "due_date": ma,
        "done": False,
        "note": "<br>".join(sorok),
    }
    user_id = felelos_user(belso)
    if user_id:
        payload["owner_id"] = user_id
    if szemely:
        payload["participants"] = [{"person_id": szemely["person_id"], "primary": True}]
        if szemely.get("org_id"):
            payload["org_id"] = szemely["org_id"]
    if deal:
        payload["deal_id"] = deal["id"]

    uj = _pd("POST", "activities", payload=payload)
    if not uj:
        # Hogy egy CloudTalk újrapróbálkozás ne vesszen el
        with _lock:
            _feldolgozott_hivasok.pop(call_uuid, None)
        return {"status": "hiba", "uzenet": "Pipedrive tevékenység létrehozása sikertelen"}

    if kulso:
        with _lock:
            _mai_tevekenysegek[kulso] = (uj.get("id"), ma, 1)
    log.info(f"[CT] Visszahívás-tevékenység #{uj.get('id')} – {kulso or 'rejtett'}")
    return {"status": "letrehozva", "activity_id": uj.get("id"),
            "person_id": (szemely or {}).get("person_id"), "deal_id": (deal or {}).get("id")}


# ── Route ─────────────────────────────────────────────────────────────────────
def register_cloudtalk_routes(app):

    @app.route("/cloudtalk-visszahivas", methods=["POST"])
    @app.route("/cloudtalk-visszahivas/", methods=["POST"])
    def cloudtalk_visszahivas():
        adat = request.get_json(silent=True) or request.form.to_dict() or {}
        kapott = request.headers.get("X-SQM-Secret") or str(adat.get("secret") or "")
        if not CLOUDTALK_WEBHOOK_SECRET or kapott != CLOUDTALK_WEBHOOK_SECRET:
            log.warning("[CT] Jogosulatlan hívás a /cloudtalk-visszahivas végpontra")
            return jsonify({"error": "Unauthorized"}), 401
        log.info(f"[CT] Beérkezett: { {k: v for k, v in adat.items() if k != 'secret'} }")
        eredmeny = feldolgoz(adat)
        kod = 500 if eredmeny.get("status") == "hiba" else 200
        return jsonify(eredmeny), kod
