"""
cloudtalk_visszahivas.py – Nem fogadott CloudTalk hívás → Pipedrive "Visszahívás" tevékenység
==============================================================================================
EGY általános CloudTalk Workflow Automation (Trigger: Call > Ended, feltétel: direction =
incoming, talking_time = 0 – számra szűrés NEM kell) API Request akciója POST-ol ide.

A modul a háttérben:
  1. ellenőrzi a titkos kulcsot (X-SQM-Secret header VAGY "secret" mező a bodyban),
  2. call_uuid alapján kiszűri a duplikált hívásokat,
  3. a CloudTalk Analytics API-ból (call_id alapján) lekéri a hívás lépéseit (call_steps),
     és kiolvassa, melyik AGENTNÉL csengett ki a hívás → ő lesz a felelős,
     (CloudTalk agent → Pipedrive user: e-mail, majd ékezet/sorrend-független név alapján)
  4. ha ez nem sikerül: SZAM_FELELOS (hívott szám → név) → CLOUDTALK_VISSZAHIVAS_USER_ID,
  5. telefonszám alapján megkeresi a hívót Pipedrive-ban (+ cég, + legutóbbi nyitott deal),
  6. "call" típusú, mai határidős tevékenységet hoz létre, vagy ha ugyanarról a számról
     ma már van nyitott visszahívás-tevékenység, azt frissíti ("2x hívott").

Végpont: POST /cloudtalk-visszahivas   (azonnal 202-vel válaszol, a munka háttérszálon fut)

Környezeti változók (Railway → Variables):
  PIPEDRIVE_API_TOKEN            – (meglévő) Pipedrive API token
  CLOUDTALK_WEBHOOK_SECRET       – a CloudTalk X-SQM-Secret headerében küldött kulcs
  CLOUDTALK_API_KEY_ID           – CloudTalk → Account → Settings → API Keys → ID
  CLOUDTALK_API_KEY_SECRET       – ugyanott: Key (secret)
  CLOUDTALK_VISSZAHIVAS_USER_ID  – opcionális: végső tartalék Pipedrive user ID
  CLOUDTALK_SZAM_FELELOS         – opcionális JSON tartalék-térkép: hívott szám → név/ID

CloudTalk API Request "Values" (Key → Value, a Value-t a jobb oldali listából kattintva):
  call_id, call_uuid, external_number, internal_number, started_at, waiting_time
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
CT_KEY_ID = os.environ.get("CLOUDTALK_API_KEY_ID", "")
CT_KEY_SECRET = os.environ.get("CLOUDTALK_API_KEY_SECRET", "")
CT_ANALYTICS = "https://analytics-api.cloudtalk.io/api"
CT_CORE = "https://my.cloudtalk.io/api"
# A hívás vége után az Analytics adat néhány mp késéssel áll elő → ennyit várunk próbánként
CT_RETRY_DELAYS = [3, 7, 15, 30]
PD_RETRY_DELAYS = [0, 5, 20]

# TARTALÉK: hívott SQM szám → felelős, ha a CloudTalk call_steps nem ad agentet
# (pl. API-hiba, átirányított hívás). Név vagy Pipedrive user ID.
SZAM_FELELOS_ALAP = {
    "+3619991661": "Dudás Mária",
    "+36202106309": "Dudás Mária",
    "+3614088582": "Kanozsai Vivien",
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


def tartalek_felelos(internal_e164: str) -> tuple[int | None, str]:
    """SZAM_FELELOS térkép, majd CLOUDTALK_VISSZAHIVAS_USER_ID. → (user_id, forrás)"""
    cel = _utolso9(internal_e164)
    if cel:
        for k, v in SZAM_FELELOS.items():
            if _utolso9(k) == cel:
                if isinstance(v, int) or str(v).strip().isdigit():
                    return int(v), "számtérkép"
                uid = user_id_nev_alapjan(str(v))
                if uid:
                    return uid, "számtérkép"
                break
    if ALAP_USER_ID.isdigit():
        return int(ALAP_USER_ID), "alapértelmezett"
    return None, "nincs (token tulajdonosa)"


# ── CloudTalk ─────────────────────────────────────────────────────────────────
_ct_agent_cache: dict = {"ido": 0.0, "agentek": {}}


def _ct_get(url: str, params: dict | None = None):
    if not (CT_KEY_ID and CT_KEY_SECRET):
        log.warning("[CT] CLOUDTALK_API_KEY_ID / _SECRET nincs beállítva – call_steps kihagyva")
        return None
    try:
        r = requests.get(url, params=params, auth=(CT_KEY_ID, CT_KEY_SECRET), timeout=15)
        if r.status_code == 404:
            return None  # még nincs feldolgozva / nem létezik
        if r.status_code != 200:
            log.error(f"[CT] GET {url} → HTTP {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        log.error(f"[CT] GET {url}: {e}")
        return None


def ct_hivas_reszletek(call_id: str) -> dict | None:
    """Analytics API: hívás részletei call_steps-szel. Újrapróbál, amíg az agent-lépés meg
    nem jelenik (a Call ended esemény után az adat pár mp késéssel áll elő)."""
    if not str(call_id or "").strip().isdigit():
        return None
    if not (CT_KEY_ID and CT_KEY_SECRET):
        log.warning("[CT] CLOUDTALK_API_KEY_ID / _SECRET nincs beállítva – tartalék felelős-logika")
        return None
    utolso = None
    for varakozas in CT_RETRY_DELAYS:
        time.sleep(varakozas)
        data = _ct_get(f"{CT_ANALYTICS}/calls/{call_id}")
        if isinstance(data, dict) and isinstance(data.get("responseData"), dict):
            data = data["responseData"]
        if isinstance(data, dict) and data:
            utolso = data
            if any(l.get("type") == "agent" for l in data.get("call_steps") or []):
                return data
    if utolso is None:
        log.warning(f"[CT] Nem sikerült lekérni a hívás részleteit (call_id={call_id})")
    return utolso


def kicsengetett_agentek(reszletek: dict | None) -> list[dict]:
    """[{id, name, reason}] a call_steps agent-lépéseiből, sorrendben, ismétlés nélkül."""
    out, seen = [], set()
    for lepes in (reszletek or {}).get("call_steps") or []:
        if lepes.get("type") != "agent" or not lepes.get("id") or lepes["id"] in seen:
            continue
        seen.add(lepes["id"])
        out.append({"id": lepes["id"], "name": lepes.get("name") or "", "reason": lepes.get("reason") or ""})
    return out


def hangposta_hagyva(reszletek: dict | None) -> int:
    """Hagyott-e hangüzenetet: a voicemail-lépés hossza mp-ben (0 = nem)."""
    for lepes in (reszletek or {}).get("call_steps") or []:
        if lepes.get("type") == "voicemail" and str(lepes.get("status")).upper() == "SUCCESS":
            try:
                return max(int(lepes.get("total_time") or 0), 1)
            except (TypeError, ValueError):
                return 1
    return 0


def _ct_agentek() -> dict:
    """CloudTalk agentek id → {nev, email}, 1 órás cache-sel."""
    with _lock:
        if _ct_agent_cache["agentek"] and time.time() - _ct_agent_cache["ido"] < 3600:
            return _ct_agent_cache["agentek"]
    data = _ct_get(f"{CT_CORE}/agents/index.json", {"limit": 1000}) or {}
    agentek = {}
    for elem in ((data.get("responseData") or {}).get("data") or []):
        a = elem.get("Agent") or elem
        if a.get("id"):
            agentek[int(a["id"])] = {
                "nev": f'{a.get("firstname", "")} {a.get("lastname", "")}'.strip(),
                "email": (a.get("email") or "").strip().lower(),
            }
    if agentek:
        with _lock:
            _ct_agent_cache.update(ido=time.time(), agentek=agentek)
    return agentek


def pipedrive_user_agenthez(agent: dict) -> int | None:
    """CloudTalk agent → Pipedrive user ID: előbb e-mail, aztán név alapján."""
    info = _ct_agentek().get(int(agent["id"]), {})
    email = info.get("email", "")
    userek = _pipedrive_userek()
    if email:
        for u in userek:
            if (u.get("email") or "").strip().lower() == email:
                return u.get("id")
    for nev in (agent.get("name"), info.get("nev")):
        kulcs = _nev_kulcs(nev)
        if kulcs:
            for u in userek:
                if _nev_kulcs(u.get("name")) == kulcs:
                    return u.get("id")
    log.warning(f"[CT] Nincs Pipedrive user a CloudTalk agenthez: {agent.get('name')!r} ({email})")
    return None


OK_SZOVEG = {"not_picked_up": "nem vette fel", "busy": "foglalt volt", "rejected": "elutasította",
             "unavailable": "nem volt elérhető", "offline": "offline volt"}


# ── Fő logika ─────────────────────────────────────────────────────────────────
def _takarit(most: float):
    for k in [k for k, t in _feldolgozott_hivasok.items() if most - t > _DUP_TTL]:
        del _feldolgozott_hivasok[k]


def lefoglal(adat: dict) -> bool:
    """True, ha ezt a hívást még nem dolgoztuk fel (és lefoglalja)."""
    call_uuid = str(adat.get("call_uuid") or adat.get("call_id") or "").strip()
    if not call_uuid:
        return True
    most = time.time()
    with _lock:
        _takarit(most)
        if call_uuid in _feldolgozott_hivasok:
            return False
        _feldolgozott_hivasok[call_uuid] = most
    return True


def _pd_ujraprobal(method: str, endpoint: str, payload: dict):
    for varakozas in PD_RETRY_DELAYS:
        time.sleep(varakozas)
        eredmeny = _pd(method, endpoint, payload=payload)
        if eredmeny:
            return eredmeny
    return None


def feldolgoz(adat: dict) -> dict:
    call_uuid = str(adat.get("call_uuid") or "").strip()
    call_id = str(adat.get("call_id") or "").strip()
    kulso = normalizal_szam(adat.get("external_number"))
    belso = normalizal_szam(adat.get("internal_number"))
    hivas_hu = budapest_ido(_parse_utc(adat.get("started_at")) or datetime.now(timezone.utc))
    ma = budapest_ido(datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    ido_str = hivas_hu.strftime("%Y.%m.%d. %H:%M")
    try:
        varakozas = int(float(adat.get("waiting_time") or 0))
    except (TypeError, ValueError):
        varakozas = 0

    # ── 1. Kinél csengett? (CloudTalk call_steps) ────────────────────────────
    reszletek = ct_hivas_reszletek(call_id)
    if reszletek and str(reszletek.get("status") or "").lower() == "answered":
        log.info(f"[CT] call_id={call_id} közben fogadottnak látszik – kihagyva")
        return {"status": "kihagyva", "ok": "fogadott hívás"}
    if reszletek and not belso:
        belso = normalizal_szam((reszletek.get("internal_number") or {}).get("number"))
    agentek = kicsengetett_agentek(reszletek)
    hangposta = hangposta_hagyva(reszletek)

    user_id, forras = None, ""
    for ag in agentek:  # az első olyan agent, akihez van Pipedrive user
        user_id = pipedrive_user_agenthez(ag)
        if user_id:
            forras = f"CloudTalk: {ag['name']}"
            break
    if not user_id:
        user_id, forras = tartalek_felelos(belso)
    log.info(f"[CT] Felelős: user_id={user_id} ({forras})")

    # ── 2. Ugyanarról a számról ma már van nyitott tevékenység? → frissítés ──
    if kulso:
        with _lock:
            korabbi = _mai_tevekenysegek.get(kulso)
        if korabbi and korabbi[1] == ma:
            act_id, _, db = korabbi
            akt = _pd("GET", f"activities/{act_id}")
            if akt and not akt.get("done"):
                uj_db = db + 1
                targy = re.sub(r" \(\d+x hívott\)$", "", akt.get("subject") or "") + f" ({uj_db}x hívott)"
                jegyzet = (akt.get("note") or "") + f"<br>• Újabb nem fogadott hívás: {ido_str}" + \
                    (f" (hangüzenetet hagyott, {hangposta} mp)" if hangposta else "")
                if _pd("PATCH", f"activities/{act_id}", payload={"subject": targy, "note": jegyzet}):
                    with _lock:
                        _mai_tevekenysegek[kulso] = (act_id, ma, uj_db)
                    log.info(f"[CT] Tevékenység frissítve #{act_id} ({kulso}, {uj_db}x)")
                    return {"status": "frissitve", "activity_id": act_id}

    # ── 3. Hívó keresése Pipedrive-ban ───────────────────────────────────────
    szemely = szemely_keresese(kulso) if kulso else None
    deal = nyitott_deal(szemely["person_id"]) if szemely else None

    if not kulso:
        targy = f"{TARGY_PREFIX}: rejtett számról hívtak"
    else:
        ki = " – ".join(x for x in [(szemely or {}).get("ceg"), (szemely or {}).get("nev")] if x)
        targy = f"{TARGY_PREFIX}: {megjelenitett_szam(kulso)}" + (f" ({ki})" if ki else "")
    if hangposta:
        targy += " 🎙️"

    sorok = [
        f"<b>Nem fogadott bejövő hívás</b> – {ido_str}",
        f"Hívó: {megjelenitett_szam(kulso) if kulso else 'rejtett szám'}",
    ]
    if belso:
        sorok.append(f"Hívott SQM szám: {megjelenitett_szam(belso)}")
    if agentek:
        sorok.append("Kicsengetett: " + ", ".join(
            html.escape(a["name"]) + (f" ({OK_SZOVEG.get(a['reason'], a['reason'])})" if a["reason"] else "")
            for a in agentek))
    if varakozas:
        sorok.append(f"Kicsengetési idő: {varakozas} mp")
    if hangposta:
        sorok.append(f"<b>Hangüzenetet hagyott</b> ({hangposta} mp) – meghallgatható a CloudTalkban")
    if not szemely and kulso:
        sorok.append("<i>Ismeretlen szám – nincs hozzá Pipedrive kontakt.</i>")
    if deal:
        sorok.append(f"Kapcsolt nyitott deal: {html.escape(deal['cim'])}")
    if call_id or call_uuid:
        sorok.append(f"<small>CloudTalk call_id: {call_id or '-'} / {call_uuid or '-'}</small>")

    payload = {"subject": targy, "type": "call", "due_date": ma, "done": False,
               "note": "<br>".join(sorok)}
    if user_id:
        payload["owner_id"] = user_id
    if szemely:
        payload["participants"] = [{"person_id": szemely["person_id"], "primary": True}]
        if szemely.get("org_id"):
            payload["org_id"] = szemely["org_id"]
    if deal:
        payload["deal_id"] = deal["id"]

    uj = _pd_ujraprobal("POST", "activities", payload)
    if not uj:
        log.error(f"[CT] Tevékenység létrehozása VÉGLEG sikertelen – {kulso} {ido_str} (call_id={call_id})")
        return {"status": "hiba"}
    if kulso:
        with _lock:
            _mai_tevekenysegek[kulso] = (uj.get("id"), ma, 1)
    log.info(f"[CT] Visszahívás-tevékenység #{uj.get('id')} – {kulso or 'rejtett'} → user {user_id}")
    return {"status": "letrehozva", "activity_id": uj.get("id"), "owner_id": user_id,
            "person_id": (szemely or {}).get("person_id"), "deal_id": (deal or {}).get("id")}


def _hatterben(adat: dict):
    try:
        feldolgoz(adat)
    except Exception as e:
        log.exception(f"[CT] Feldolgozási hiba: {e}")


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
        if not lefoglal(adat):
            return jsonify({"status": "duplikalt"}), 200
        threading.Thread(target=_hatterben, args=(adat,), daemon=True).start()
        return jsonify({"status": "elfogadva"}), 202
