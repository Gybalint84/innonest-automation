# -*- coding: utf-8 -*-
"""
Dropbox mappa auto-generátor Pipedrive webhookból.

FOLYAMAT:
1. Pipedrive Automation küld egy POST webhookot, amikor egy deal eléri a megadott szakaszt.
2. A szerver lekéri a deal adatait a Pipedrive API-ból.
3. Ellenőrzi, hogy a megadott custom mezőben van-e már Dropbox URL -> ha igen, LEÁLL (nincs duplikáció).
4. Ha nincs, létrehoz egy Dropbox mappát "{Cégnév} - {YYYY.MM.DD}" névvel.
5. Megosztható linket generál hozzá.
6. Visszaírja a linket a Pipedrive deal custom mezőjébe.

ELŐFELTÉTEL - Dropbox App Console beállítás (egyszeri, kézi lépés):
1. https://www.dropbox.com/developers/apps -> Create app
   - Scoped access
   - FULL DROPBOX (nem App folder!) - mert a mappák egy már létező,
     kézzel létrehozott felső szintű mappa ("Ügyfélképek") alá kerülnek,
     nem az App saját dedikált mappájába. Az App folder típus erre nem
     alkalmas, és utólag nem is konvertálható Full Dropbox-szá - ha
     tévedésből App folder-rel hoztad létre, törököld és hozz létre újat.
2. Permissions fülön engedélyezd: files.content.write, files.content.read,
   sharing.write, sharing.read
3. Settings fülön jegyezd fel az App key-t és App secret-et.
4. Refresh token megszerzése (egyszeri OAuth flow, lásd get_refresh_token.py
   a fájl végén kommentben) -> ezt Railway Environment Variable-ként mentsd el.

MEGJEGYZÉS a Full Dropbox hozzáférésről: mivel az App elméletileg a teljes
fiókodat látja, a kód lentebb szigorúan csak a DROPBOX_PARENT_FOLDER alatt
dolgozik (mappát csak ott hoz létre, máshova nem nyúl).

HASZNÁLAT A MEGLÉVŐ server.py-BAN (ugyanaz a minta, mint a többi modulnál -
pl. register_pipedrive_routes, register_pdf_routes):

    from dropbox_mappa_generator import register_dropbox_routes
    register_dropbox_routes(app)

Ezután az endpoint ugyanazon a domainen lesz elérhető, ahol a többi
automatizációtok fut, pl.:
    https://<railway-domain>/pipedrive-webhook/dropbox-mappa

KÖRNYEZETI VÁLTOZÓK (Railway) - ugyanabba a szervizbe, ahol a meglévők vannak:
- DROPBOX_APP_KEY
- DROPBOX_APP_SECRET
- DROPBOX_REFRESH_TOKEN
- DROPBOX_PARENT_FOLDER          (="/Ügyfélképek" - ide kerülnek az almappák)
- PIPEDRIVE_API_TOKEN             (valószínűleg már be van állítva)
- PIPEDRIVE_DROPBOX_FIELD_KEY     (=8551443f0e9f59d2af653f3df5c12a05b0c432a7)
- WEBHOOK_SHARED_SECRET          (opcionális, ajánlott: Pipedrive webhook URL-jébe
                                   ?secret=... paraméterként, hogy ne tudja bárki meghívni)
"""

import os
import re
import logging
from datetime import datetime

import requests
from flask import request, jsonify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dropbox_mappa_generator")

DROPBOX_APP_KEY = os.environ["DROPBOX_APP_KEY"]
DROPBOX_APP_SECRET = os.environ["DROPBOX_APP_SECRET"]
DROPBOX_REFRESH_TOKEN = os.environ["DROPBOX_REFRESH_TOKEN"]
# a szülőmappa, ami alá az almappák kerülnek - pl. "/Ügyfélképek"
# (a Dropbox path-eknél nincs URL-encode, a sima ékezetes szöveget kell megadni)
DROPBOX_PARENT_FOLDER = os.environ.get("DROPBOX_PARENT_FOLDER", "/Ügyfélképek").rstrip("/")
PIPEDRIVE_API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
PIPEDRIVE_DROPBOX_FIELD_KEY = os.environ.get(
    "PIPEDRIVE_DROPBOX_FIELD_KEY", "8551443f0e9f59d2af653f3df5c12a05b0c432a7"
)
WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET")  # opcionális

PIPEDRIVE_BASE = "https://api.pipedrive.com/v1"

# egyszerű, in-memory lock a párhuzamos webhook-hívások ellen (pl. ha Pipedrive
# kétszer küldi ki ugyanazt az eseményt, ami előfordul)
_folyamatban_levo_dealek = set()


# ---------------------------------------------------------------------------
# DROPBOX TOKEN KEZELÉS
# ---------------------------------------------------------------------------

def dropbox_access_token_lekerese():
    """Refresh tokenből friss access tokent kér. Access token 4 órán át él,
    minden hívásnál újat kérünk, hogy sose fusson le lejárt tokennel."""
    resp = requests.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DROPBOX_REFRESH_TOKEN,
        },
        auth=(DROPBOX_APP_KEY, DROPBOX_APP_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# PIPEDRIVE SEGÉDFÜGGVÉNYEK
# ---------------------------------------------------------------------------

def pipedrive_deal_lekerese(deal_id):
    resp = requests.get(
        f"{PIPEDRIVE_BASE}/deals/{deal_id}",
        params={"api_token": PIPEDRIVE_API_TOKEN},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Pipedrive deal lekérés sikertelen: {data}")
    return data["data"]


def pipedrive_mezo_frissitese(deal_id, mezo_kulcs, ertek):
    resp = requests.put(
        f"{PIPEDRIVE_BASE}/deals/{deal_id}",
        params={"api_token": PIPEDRIVE_API_TOKEN},
        json={mezo_kulcs: ertek},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Pipedrive mező frissítés sikertelen: {data}")
    return data["data"]


# ---------------------------------------------------------------------------
# MAPPANÉV KÉPZÉS
# ---------------------------------------------------------------------------

def mappanev_tisztitasa(nev):
    """Dropbox-ban tiltott/problémás karakterek eltávolítása a mappanévből.
    A Dropbox path-eknál a '/' tiltott, ezért csak azt és a felesleges
    szóközöket kezeljük - az ékezetes magyar karaktereket meghagyjuk,
    a Dropbox ezekkel simán elboldogul."""
    nev = nev.replace("/", "-").replace("\\", "-")
    nev = re.sub(r'[<>:"|?*]', "", nev)
    nev = re.sub(r"\s+", " ", nev).strip()
    return nev


def mappanev_generalasa(cegnev):
    datum = datetime.now().strftime("%Y.%m.%d")
    nyers_nev = f"{cegnev} - {datum}"
    return mappanev_tisztitasa(nyers_nev)


# ---------------------------------------------------------------------------
# DROPBOX MŰVELETEK
# ---------------------------------------------------------------------------

def dropbox_mappa_letrehozasa(access_token, mappa_path):
    """Létrehozza a mappát. Ha már létezik (409 conflict), nem hibázik el,
    hanem jelzi, hogy már létezett - ilyenkor a meglévő mappához kérünk linket."""
    resp = requests.post(
        "https://api.dropboxapi.com/2/files/create_folder_v2",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"path": mappa_path, "autorename": False},
        timeout=15,
    )
    if resp.status_code == 409:
        hiba = resp.json()
        # ha azért 409, mert a mappa már létezik -> nem hiba, csak folytatjuk
        if "path" in hiba.get("error", {}) and "conflict" in str(hiba["error"]):
            logger.info("A mappa már létezett, folytatás megosztott link kéréssel: %s", mappa_path)
            return
        raise RuntimeError(f"Dropbox mappa létrehozás hiba: {hiba}")
    resp.raise_for_status()


def dropbox_megosztott_link_letrehozasa(access_token, mappa_path):
    """Megosztható linket hoz létre. Ha már létezik link erre a path-ra,
    a Dropbox 409-et ad -> ilyenkor lekérjük a meglévő linkek listáját."""
    resp = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"path": mappa_path},
        timeout=15,
    )
    if resp.status_code == 200:
        return resp.json()["url"]

    if resp.status_code == 409:
        # már van link erre a mappára -> listázzuk ki
        list_resp = requests.post(
            "https://api.dropboxapi.com/2/sharing/list_shared_links",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"path": mappa_path, "direct_only": True},
            timeout=15,
        )
        list_resp.raise_for_status()
        linkek = list_resp.json().get("links", [])
        if linkek:
            return linkek[0]["url"]
        raise RuntimeError(f"Nem sikerült linket szerezni a mappához: {resp.json()}")

    resp.raise_for_status()


# ---------------------------------------------------------------------------
# WEBHOOK ENDPOINT REGISZTRÁCIÓ
# ---------------------------------------------------------------------------

def register_dropbox_routes(app):
    """Regisztrálja a Dropbox mappa-generáló webhook végpontot a Flask app-on.
    Hívás a server.py-ban: register_dropbox_routes(app)."""

    @app.route("/pipedrive-webhook/dropbox-mappa", methods=["POST"])
    def dropbox_mappa_webhook():
        if WEBHOOK_SHARED_SECRET:
            if request.args.get("secret") != WEBHOOK_SHARED_SECRET:
                logger.warning("Érvénytelen webhook secret, kérés elutasítva.")
                return jsonify({"error": "unauthorized"}), 401

        payload = request.get_json(force=True, silent=True) or {}

        # Pipedrive v1 Automation webhook payload szerkezete: current/previous objektum
        deal = payload.get("current") or payload.get("data") or {}
        deal_id = deal.get("id")

        if not deal_id:
            logger.warning("Webhook payload nem tartalmaz deal id-t: %s", payload)
            return jsonify({"error": "missing deal id"}), 400

        # dupla-hívás elleni egyszerű lock
        if deal_id in _folyamatban_levo_dealek:
            logger.info("Deal %s már feldolgozás alatt, kihagyva.", deal_id)
            return jsonify({"status": "already processing"}), 200

        _folyamatban_levo_dealek.add(deal_id)
        try:
            return _deal_feldolgozasa(deal_id)
        finally:
            _folyamatban_levo_dealek.discard(deal_id)

    return app


def _deal_feldolgozasa(deal_id):
    deal = pipedrive_deal_lekerese(deal_id)

    # KRITIKUS: ha már van érték a mezőben, NEM generálunk újat és NEM írjuk felül
    meglevo_url = deal.get(PIPEDRIVE_DROPBOX_FIELD_KEY)
    if meglevo_url:
        logger.info("Deal %s-hez már tartozik Dropbox URL, kihagyva: %s", deal_id, meglevo_url)
        return jsonify({"status": "already has dropbox url", "url": meglevo_url}), 200

    cegnev = deal.get("org_name") or deal.get("title") or f"Deal {deal_id}"
    mappanev = mappanev_generalasa(cegnev)
    mappa_path = f"{DROPBOX_PARENT_FOLDER}/{mappanev}"

    access_token = dropbox_access_token_lekerese()
    dropbox_mappa_letrehozasa(access_token, mappa_path)
    url = dropbox_megosztott_link_letrehozasa(access_token, mappa_path)

    pipedrive_mezo_frissitese(deal_id, PIPEDRIVE_DROPBOX_FIELD_KEY, url)

    logger.info("Deal %s: Dropbox mappa létrehozva és visszaírva -> %s", deal_id, url)
    return jsonify({"status": "created", "folder": mappanev, "url": url}), 200
