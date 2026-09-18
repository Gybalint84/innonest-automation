"""
sheets_kliens.py — vékony Google Sheets REST kliens (OAuth refresh tokennel).

Miért nem a google-api-python-client: az egy nagy függőség, és nekünk 5 hívás kell belőle.
Csak a `requests` kell, ami már fent van.

Env változók:
    SZAMLAZZ_GOOGLE_CLIENT_ID
    SZAMLAZZ_GOOGLE_CLIENT_SECRET
    SZAMLAZZ_GOOGLE_REFRESH_TOKEN
    SZAMLAZZ_SHEET_ID
"""

import logging
import os
import threading
import time

import requests

log = logging.getLogger("sheets")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://sheets.googleapis.com/v4/spreadsheets"

_token = {"ertek": None, "lejar": 0.0}
_token_lock = threading.Lock()


def access_token(kenyszeritett_frissites=False):
    """Access token a refresh tokenből, memóriában cache-elve (1 óra, 60 mp ráhagyással)."""
    with _token_lock:
        if not kenyszeritett_frissites and _token["ertek"] and time.time() < _token["lejar"]:
            return _token["ertek"]
        cid = os.environ.get("SZAMLAZZ_GOOGLE_CLIENT_ID")
        titok = os.environ.get("SZAMLAZZ_GOOGLE_CLIENT_SECRET")
        refresh = os.environ.get("SZAMLAZZ_GOOGLE_REFRESH_TOKEN")
        if not (cid and titok and refresh):
            raise RuntimeError("SZAMLAZZ_GOOGLE_CLIENT_ID / _SECRET / _REFRESH_TOKEN nincs beállítva")
        r = requests.post(TOKEN_URL, data={
            "client_id": cid, "client_secret": titok,
            "refresh_token": refresh, "grant_type": "refresh_token",
        }, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"OAuth token frissítés sikertelen: {r.status_code} {r.text[:200]}")
        adat = r.json()
        _token["ertek"] = adat["access_token"]
        _token["lejar"] = time.time() + int(adat.get("expires_in", 3600)) - 60
        return _token["ertek"]


def _hivas(metodus, ut, **kw):
    """Sheets API hívás tokennel, 429/5xx esetén exponenciális visszalépéssel."""
    varakozas = 1.0
    for probalkozas in range(5):
        fejlec = {"Authorization": "Bearer " + access_token(kenyszeritett_frissites=(probalkozas and _utolso_401))}
        r = requests.request(metodus, ut, headers=fejlec, timeout=20, **kw)
        if r.status_code == 401 and probalkozas < 4:
            globals()["_utolso_401"] = True
            continue
        globals()["_utolso_401"] = False
        if r.status_code in (429, 500, 502, 503) and probalkozas < 4:
            log.warning("Sheets API %s → %s, újrapróbálás %.1f mp múlva", r.status_code, r.reason, varakozas)
            time.sleep(varakozas)
            varakozas *= 2
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Sheets API {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}
    raise RuntimeError("Sheets API: az újrapróbálások elfogytak")


_utolso_401 = False


def _id(sheet_id=None):
    sid = sheet_id or os.environ.get("SZAMLAZZ_SHEET_ID")
    if not sid:
        raise RuntimeError("SZAMLAZZ_SHEET_ID nincs beállítva")
    return sid


# ---------------------------------------------------------------- műveletek

def lapok(sheet_id=None):
    """{lapnév: sheetId} — a munkafüzet lapjai."""
    adat = _hivas("GET", f"{API}/{_id(sheet_id)}", params={"fields": "sheets.properties(sheetId,title)"})
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in adat.get("sheets", [])}


def lap_letrehozas(nev, fejlec, sheet_id=None):
    _hivas("POST", f"{API}/{_id(sheet_id)}:batchUpdate",
           json={"requests": [{"addSheet": {"properties": {"title": nev, "gridProperties": {"frozenRowCount": 1}}}}]})
    ir(f"'{nev}'!A1", [fejlec], sheet_id=sheet_id)
    log.info("Sheet lap létrehozva: %s", nev)


def olvas(tartomany, sheet_id=None):
    adat = _hivas("GET", f"{API}/{_id(sheet_id)}/values/{requests.utils.quote(tartomany, safe='')}",
                  params={"majorDimension": "ROWS"})
    return adat.get("values", [])


def ir(tartomany, sorok, sheet_id=None):
    return _hivas("PUT", f"{API}/{_id(sheet_id)}/values/{requests.utils.quote(tartomany, safe='')}",
                  params={"valueInputOption": "RAW"}, json={"values": sorok})


def hozzafuz(tartomany, sorok, sheet_id=None):
    """Hozzáfűzés; visszaadja, hova került (updatedRange), hogy tudjuk a sorszámot."""
    return _hivas("POST", f"{API}/{_id(sheet_id)}/values/{requests.utils.quote(tartomany, safe='')}:append",
                  params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS",
                          "includeValuesInResponse": "false"},
                  json={"values": sorok})


def sorok_torlese(lap_sheet_id, blokkok, sheet_id=None):
    """blokkok: [(kezdo_sor_1alapu, darab)] — egy batchUpdate hívásban, alulról felfelé."""
    if not blokkok:
        return
    kerelmek = [{"deleteDimension": {"range": {
        "sheetId": lap_sheet_id, "dimension": "ROWS",
        "startIndex": kezd - 1, "endIndex": kezd - 1 + db}}}
        for kezd, db in sorted(blokkok, reverse=True)]
    _hivas("POST", f"{API}/{_id(sheet_id)}:batchUpdate", json={"requests": kerelmek})


def sorszam_updated_range(valasz):
    """'Számlák'!A12:AG12 → 12"""
    tart = (valasz.get("updates") or valasz).get("updatedRange", "")
    resz = tart.split("!")[-1]
    szam = "".join(c for c in resz.split(":")[0] if c.isdigit())
    return int(szam) if szam else 0


def torol(tartomany, sheet_id=None):
    """Cellatartomány kiürítése (a kimutatás-lapok újraírásához)."""
    return _hivas("POST", f"{API}/{_id(sheet_id)}/values/{requests.utils.quote(tartomany, safe='')}:clear", json={})
