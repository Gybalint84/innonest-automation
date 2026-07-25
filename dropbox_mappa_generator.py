# -*- coding: utf-8 -*-
"""
Dropbox mappa auto-generátor Pipedrive webhookból.

FOLYAMAT:
1. Pipedrive Automation küld egy POST webhookot, amikor egy deal eléri a megadott szakaszt.
2. A szerver lekéri a deal adatait a Pipedrive API-ból.
3. Ellenőrzi, hogy a megadott custom mezőben van-e már Dropbox URL -> ha igen, LEÁLL (nincs duplikáció).
4. Ha nincs, létrehoz egy Dropbox mappát "{Cégnév} - {YYYY.MM.DD}" névvel.
5. Létrehoz KÉT linket ehhez a mappához:
   a) egy sima megtekintésre jogosult linket - ez látja a mappa teljes tartalmát,
      ez kerül a PIPEDRIVE_DROPBOX_FIELD_KEY mezőbe (belső, saját használatra)
   b) egy Dropbox File Request (Fájlkérés) linket - ezzel az ügyfél fel tud tölteni
      a mappába fiók nélkül is, de mást nem lát
6. A megtekintő linket visszaírja a Pipedrive mezőbe.
7. Kiküld egy fotózási útmutató emailt az ügyfélnek (a deal kapcsolattartójának
   email címére, ugyanúgy mint a visszajelző email), benne a File Request
   feltöltő linkkel - az Apps Script Web App / céges Gmail proxyn keresztül.

ELŐFELTÉTEL - Dropbox App Console beállítás (egyszeri, kézi lépés):
1. https://www.dropbox.com/developers/apps -> Create app
   - Scoped access
   - FULL DROPBOX (nem App folder!) - mert a mappák egy már létező,
     kézzel létrehozott felső szintű mappa ("Ügyfélképek") alá kerülnek,
     nem az App saját dedikált mappájába. Az App folder típus erre nem
     alkalmas, és utólag nem is konvertálható Full Dropbox-szá - ha
     tévedésből App folder-rel hoztad létre, törököld és hozz létre újat.
2. Permissions fülön engedélyezd: files.content.write, files.content.read,
   sharing.write, sharing.read, file_requests.write
   (a file_requests.write kell a File Request/Fájlkérés funkcióhoz, amivel
   az ügyfél fiók nélkül is fel tud tölteni a mappába, de mást nem lát)
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
- PIPEDRIVE_DROPBOX_FIELD_KEY     (=8551443f0e9f59d2af653f3df5c12a05b0c432a7 - megtekintésre jogosult link, belső használatra)
- WEBAPP_URL                      (Apps Script Web App URL - a fotózási útmutató email küldéséhez, ugyanaz mint a visszajelző emailnél)
- WEBHOOK_SHARED_SECRET          (opcionális, ajánlott: Pipedrive webhook URL-jébe
                                   ?secret=... paraméterként, hogy ne tudja bárki meghívni)
"""

import os
import re
import logging
import unicodedata
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
# NFC normalizálás: Railway/böngésző env változó mezőkben előfordulhat, hogy az
# ékezetes karakterek felbontott (NFD) formában kerülnek be (pl. "u" + önálló
# umlaut-jel, ami vizuálisan ugyanúgy néz ki, mint az "ü", de bájtszinten más) -
# ez a Dropbox API-nál "400 Bad Request"-et okozhat érvénytelen path miatt.
DROPBOX_PARENT_FOLDER = unicodedata.normalize(
    "NFC", os.environ.get("DROPBOX_PARENT_FOLDER", "/Ügyfélképek").strip()
).rstrip("/")
PIPEDRIVE_API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
PIPEDRIVE_DROPBOX_FIELD_KEY = os.environ.get(
    "PIPEDRIVE_DROPBOX_FIELD_KEY", "8551443f0e9f59d2af653f3df5c12a05b0c432a7"
)
# az Apps Script Web App URL-je, amin keresztül a céges Gmailből megy ki az email
# (ugyanaz, amit a visszajelző email / Won automatizáció is használ - a meglévő
#  WEBAPP_URL env változó a szerveren)
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
# a fotózási útmutató email HTML sablon útvonala (a sablonok/ mappában, ugyanott,
# ahol a többi email-sablon van)
FOTO_EMAIL_SABLON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "sablonok", "fotozasi_utmutato_email.html",
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


def pipedrive_mezok_frissitese(deal_id, mezok: dict):
    """Egy vagy több custom mező frissítése EGYETLEN API-hívással."""
    resp = requests.put(
        f"{PIPEDRIVE_BASE}/deals/{deal_id}",
        params={"api_token": PIPEDRIVE_API_TOKEN},
        json=mezok,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Pipedrive mező frissítés sikertelen: {data}")
    return data["data"]


def pipedrive_person_lekerese(person_id):
    """Egy Pipedrive kapcsolattartó (person) adatainak lekérése."""
    resp = requests.get(
        f"{PIPEDRIVE_BASE}/persons/{person_id}",
        params={"api_token": PIPEDRIVE_API_TOKEN},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Pipedrive person lekérés sikertelen: {data}")
    return data["data"]


def ugyfel_email_kinyerese(deal):
    """Kinyeri a deal kapcsolattartójának email címét - PONTOSAN ugyanaz a
    metódus, mint a visszajelző email / Won automatizációnál: a deal
    person_id-jából lekéri a personont, és annak ELSŐ email címét veszi
    (a Pipedrive listaként adja: [{"value": "...", "primary": true}, ...])."""
    person_ref = deal.get("person_id")
    if not person_ref:
        return ""
    # a person_id lehet dict ({"value": 123, ...}) vagy sima szám
    person_id = person_ref.get("value") if isinstance(person_ref, dict) else person_ref
    if not person_id:
        return ""
    person = pipedrive_person_lekerese(person_id)
    email_lista = person.get("email", [])
    if isinstance(email_lista, list) and email_lista:
        # elsődleges email keresése, ha van megjelölve, egyébként az első
        for e in email_lista:
            if e.get("primary") and e.get("value"):
                return e["value"]
        return email_lista[0].get("value", "")
    return ""


# ---------------------------------------------------------------------------
# EMAIL KÜLDÉS (Apps Script Web App proxy, ugyanaz mint a visszajelző email)
# ---------------------------------------------------------------------------

def email_kuld(cimzett, targy, html_tartalom):
    """Email küldése az Apps Script Web App 'sendEmail' action-jén keresztül,
    a céges Gmailből - ugyanaz a mechanizmus, mint a visszajelző emailnél."""
    if not WEBAPP_URL:
        raise RuntimeError("Nincs beállítva WEBAPP_URL env változó az email küldéshez.")
    if not cimzett:
        raise RuntimeError("Nincs címzett email cím - az emailt nem lehet kiküldeni.")
    payload = {
        "action": "sendEmail",
        "to": cimzett,
        "subject": targy,
        "htmlBody": html_tartalom,
    }
    r = requests.post(WEBAPP_URL, json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Email küldés sikertelen (HTTP {r.status_code}): {r.text}")
    return True


def foto_email_osszeallitasa(feltoltes_link):
    """Betölti a fotózási útmutató HTML sablont és behelyettesíti a feltöltő
    linket a {{FELTOLTES_LINK}} helyőrző helyére."""
    with open(FOTO_EMAIL_SABLON, "r", encoding="utf-8") as f:
        html = f.read()
    return html.replace("{{FELTOLTES_LINK}}", feltoltes_link)


# ---------------------------------------------------------------------------
# MAPPANÉV KÉPZÉS
# ---------------------------------------------------------------------------

def mappanev_tisztitasa(nev):
    """Dropbox-ban tiltott/problémás karakterek eltávolítása a mappanévből.
    A Dropbox path-eknál a '/' tiltott, ezért csak azt és a felesleges
    szóközöket kezeljük - az ékezetes magyar karaktereket meghagyjuk,
    a Dropbox ezekkel simán elboldogul. NFC normalizálás: lásd a
    DROPBOX_PARENT_FOLDER-nél írt megjegyzést a fájl elején - ugyanaz a
    kódolási védőháló kell ide is, mert a cégnév Pipedrive-ból jön, ahol
    szintén előfordulhat felbontott (NFD) ékezet-kódolás."""
    nev = unicodedata.normalize("NFC", nev)
    nev = nev.replace("/", "-").replace("\\", "-")
    nev = re.sub(r'[<>:"|?*]', "", nev)
    nev = re.sub(r"\s+", " ", nev).strip()
    # a Dropbox nem enged pontra vagy szóközre végződő path-elemet
    nev = nev.rstrip(". ")
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
        raise RuntimeError(f"Dropbox mappa létrehozás hiba (409): {hiba}")
    if not resp.ok:
        # a resp.text tartalmazza a Dropbox pontos hibaindoklását (pl. "path/malformed_path")
        # - ez sokkal többet mond, mint a puszta státuszkód
        logger.error(
            "Dropbox mappa létrehozás sikertelen (%s) path=%r válasz=%s",
            resp.status_code, mappa_path, resp.text,
        )
        raise RuntimeError(f"Dropbox mappa létrehozás hiba ({resp.status_code}): {resp.text}")


def dropbox_fajlkeres_letrehozasa(access_token, mappa_path, cim):
    """Dropbox 'File Request' (Fájlkérés) létrehozása a megadott mappához.

    Ez ad egy olyan linket, amivel BÁRKI - Dropbox-fiók nélkül is - fel tud
    tölteni fájlokat KIZÁRÓLAG ebbe az egy mappába, de semmi mást nem lát:
    sem a mappa többi tartalmát (még a saját feltöltött fájljait sem tudja
    utólag megnézni), sem a fiók bármely más részét. Ez a Dropbox hivatalosan
    ajánlott megoldása erre a use case-re (sima 'szerkesztésre jogosult'
    mappalinkkel ez NEM megy: fiók nélküli címzettek ott is csak megtekintésre
    jogosultak, függetlenül a link beállításától).

    FONTOS: ehhez az App Console Permissions fülén be kell kapcsolni a
    'file_requests.write' scope-ot, és - ugyanúgy, mint a files.content.write
    scope bekapcsolásakor - a meglévő refresh token NEM kapja meg automatikusan
    az új jogot: újra le kell futtatni az OAuth authorize + token_csere.py
    flow-t egy friss refresh tokenért.
    """
    resp = requests.post(
        "https://api.dropboxapi.com/2/file_requests/create",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"title": cim, "destination": mappa_path, "open": True},
        timeout=15,
    )
    if not resp.ok:
        logger.error(
            "Dropbox fájlkérés létrehozás sikertelen (%s) path=%r válasz=%s",
            resp.status_code, mappa_path, resp.text,
        )
        raise RuntimeError(f"Dropbox fájlkérés létrehozás hiba ({resp.status_code}): {resp.text}")
    return resp.json()["url"]


def dropbox_megosztott_link_letrehozasa(access_token, mappa_path):
    """Megosztható (megtekintésre jogosult) linket hoz létre. Ha már létezik
    link erre a path-ra, a Dropbox 409-et ad -> ilyenkor lekérjük a meglévő
    linkek listáját. MEGJEGYZÉS: ez a függvény jelenleg nincs használatban a
    fő folyamatban (a dropbox_fajlkeres_letrehozasa váltotta fel, mert az
    ügyfél feltöltési joga volt a cél) - itt hagytam, ha később mégis kellene
    egy sima, csak-megtekintő link is valamilyen célra."""
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

    logger.error(
        "Dropbox megosztott link létrehozás sikertelen (%s) path=%r válasz=%s",
        resp.status_code, mappa_path, resp.text,
    )
    raise RuntimeError(f"Dropbox link létrehozás hiba ({resp.status_code}): {resp.text}")


# ---------------------------------------------------------------------------
# WEBHOOK PAYLOAD ÉRTELMEZÉS
# ---------------------------------------------------------------------------

def _deal_id_kinyerese(payload):
    """Rugalmasan kinyeri a deal_id-t a webhook payloadból. A Pipedrive
    Automations 'raw' JSON body-t a felhasználó saját maga állítja össze
    a felületen, ezért többféle lehetséges szerkezetet is elfogadunk:
      {"data": {"id": 123}}
      {"current": {"id": 123}}
      {"id": 123}
      {"deal_id": 123}
    """
    for kulcs in ("data", "current"):
        beagyazott = payload.get(kulcs)
        if isinstance(beagyazott, dict) and beagyazott.get("id"):
            return beagyazott.get("id")
    return payload.get("id") or payload.get("deal_id")


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
        deal_id = _deal_id_kinyerese(payload)

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
        except Exception as e:
            # a hiba szövegét a válaszba is betesszük, hogy a Pipedrive Automation
            # "Execution history" nézetében is látszódjon - ne kelljen Railway
            # logot nézni minden alkalommal
            logger.exception("Hiba a deal %s feldolgozása közben", deal_id)
            return jsonify({"error": str(e)}), 500
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
    # 1) sima, megtekintésre jogosult link - ez látja a mappa teljes tartalmát,
    #    ez kerül a Pipedrive mezőbe, saját/belső használatra (te nézed)
    megtekintheto_url = dropbox_megosztott_link_letrehozasa(access_token, mappa_path)
    # 2) File Request (Fájlkérés) link - ezzel az ügyfél fiók nélkül fel tud
    #    tölteni, de mást nem lát; ez NEM mezőbe kerül, hanem a fotózási
    #    útmutató emailbe, amit kiküldünk neki
    feltoltesi_url = dropbox_fajlkeres_letrehozasa(access_token, mappa_path, cim=f"Fotók feltöltése - {cegnev}")

    # megtekintő link visszaírása a Pipedrive mezőbe
    pipedrive_mezok_frissitese(deal_id, {PIPEDRIVE_DROPBOX_FIELD_KEY: megtekintheto_url})

    # fotózási útmutató email kiküldése az ügyfélnek, benne a feltöltő linkkel
    email_statusz = "kihagyva (nincs ügyfél email)"
    ugyfel_email = ugyfel_email_kinyerese(deal)
    if ugyfel_email:
        html = foto_email_osszeallitasa(feltoltesi_url)
        targy = "Fotózási útmutató a padlófelület árajánlatához – SQM Hungary"
        email_kuld(ugyfel_email, targy, html)
        email_statusz = f"elküldve ide: {ugyfel_email}"
    else:
        logger.warning("Deal %s: nincs ügyfél email cím, a fotózási útmutató email nem ment ki.", deal_id)

    logger.info(
        "Deal %s: Dropbox mappa kész. Megtekintő link -> %s | Feltöltő link -> %s | Email: %s",
        deal_id, megtekintheto_url, feltoltesi_url, email_statusz,
    )
    return jsonify({
        "status": "created",
        "folder": mappanev,
        "megtekintheto_url": megtekintheto_url,
        "feltoltesi_url": feltoltesi_url,
        "email": email_statusz,
    }), 200
