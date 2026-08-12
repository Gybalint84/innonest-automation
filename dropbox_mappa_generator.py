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
7. Kiküld EGYÜTT (ugyanabban a lefutásban), a deal kapcsolattartójának emailjére, KÉT levelet:
   a) fotózási útmutató email - a File Request feltöltő linkkel
   b) folyamat-tájékoztató email - "Így fogunk együtt dolgozni", a teljes 6 lépéses
      folyamat leírásával
   Mindkettő az illetékes értékesítő (deal owner) VALÓDI Cc-jével megy ki
   (2026.08.12-i javítás - lásd lentebb az EMAIL KÜLDÉS szekciónál), az
   Apps Script Web App / céges Gmail proxyn keresztül.

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
- EMAIL_WEBAPP_URL               (a "SQM Email Küldő" Apps Script Web App URL-je - a fotózási útmutató és a
                                   folyamat-tájékoztató email küldéséhez, ugyanaz mint a visszajelző emailnél)
- EMAIL_WEBAPP_SECRET            (a "SQM Email Küldő" script titkos kulcsa - a doPost ezt megköveteli a body-ban,
                                   ugyanaz mint a visszajelző emailnél)
- WEBHOOK_SHARED_SECRET          (opcionális, ajánlott: Pipedrive webhook URL-jébe
                                   ?secret=... paraméterként, hogy ne tudja bárki meghívni)

VÉGPONTOK (ez a modul kettőt regisztrál)
  POST /pipedrive-webhook/dropbox-mappa  - Pipedrive webhook: mappa létrehozása,
                                           megtekintő link a deal mezőbe, File Request
                                           feltöltő link + fotózási útmutató email +
                                           folyamat-tájékoztató email
  GET  /pipedrive-dropbox-photos         - a kalkulátor "Fotók" nézete hívja: deal_id
                                           vagy bid alapján visszaadja a mappa képeit
                                           ideiglenes, megnyitható linkekkel

────────────────────────────────────────────────────────────────────────────
2026.08.12-i javítás (Bálint jelzése alapján):
  1) A "Felmérő és képlink generálás" oszlopba (2. pipeline-szakasz) húzáskor
     a fotózási segédlet email kiment, de a folyamat-tájékoztató NEM - mert
     korábban ez a modul csak a fotózási útmutató emailt küldte, a folyamat-
     tájékoztató email (sablonok/folyamat_tajekoztato_email.html) küldése
     itt egyáltalán nem volt beépítve (a "Kivitelezési tájékoztató" nevű,
     hasonló témájú email a pipedrive_addon.py-ban van, de az KIZÁRÓLAG a
     deal "Megnyert" állapotra váltásakor küldődik ki - ehhez a
     stage-váltáshoz semmi nem volt kötve). Mostantól MINDKÉT email egy
     lefutásban, együtt megy ki.
  2) A fotózási segédlet emailen nem volt Cc-ben az üzlet tulajdonosa, mert
     az itteni email_kuld() nem is támogatott Cc mezőt (szemben a
     pipedrive_addon.py azonos nevű függvényével, ami már régóta tud).
     Mostantól mindkét email (fotózási segédlet ÉS folyamat-tájékoztató)
     valódi Cc-ben megy az illetékes értékesítőnek (deal owner) is - egy
     API-hívással lekérve, egyszer, mindkét emailhez felhasználva.
────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import logging
import unicodedata
from datetime import datetime

import requests
from flask import request, jsonify

# a "Fotók" végpont BID alapján is meg tudja keresni a dealt (a kalkulátor
# nem mindig ismeri a deal_id-t) - a kereső a pipedrive_webapp modulban van
from pipedrive_webapp import _pd_find_deal_by_bid

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
# az Apps Script Web App URL-je, amin keresztül a céges Gmailből megy ki az email.
# FONTOS: az email-küldés funkció külön Apps Script projektbe ("SQM Email Küldő")
# van kiszervezve, aminek saját Web App URL-je van -> ez a Railway-en az
# EMAIL_WEBAPP_URL változóban van (NEM a WEBAPP_URL-ben, ami a régi megrendelés-
# feldolgozó projektre mutat). A visszajelző email is ezt az EMAIL_WEBAPP_URL-t használja.
WEBAPP_URL = os.environ.get("EMAIL_WEBAPP_URL", "")
# a "SQM Email Küldő" script kötelezően megköveteli ezt a titkot a POST body-ban
# (a doPost legelső ellenőrzése: data.secret !== WEBAPP_SECRET -> "Érvénytelen kulcs").
# Ugyanaz az érték, amit a visszajelző email is küld: EMAIL_WEBAPP_SECRET env változó.
EMAIL_WEBAPP_SECRET = os.environ.get("EMAIL_WEBAPP_SECRET", "")
# a fotózási útmutató email HTML sablon útvonala (a sablonok/ mappában, ugyanott,
# ahol a többi email-sablon van)
FOTO_EMAIL_SABLON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "sablonok", "fotozasi_utmutato_email.html",
)
# a folyamat-tájékoztató email HTML sablon útvonala (2026.08.12., új)
FOLYAMAT_EMAIL_SABLON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "sablonok", "folyamat_tajekoztato_email.html",
)
WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET")  # opcionális

PIPEDRIVE_BASE = "https://api.pipedrive.com/v1"

# a kalkulátor "Fotók" nézetéhez: mit tekintünk képnek, és legfeljebb hány
# fotót adunk vissza egyetlen kérésre
KEP_KITERJESZTESEK = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp")
MAX_FOTO_SZAM = 60

# egyszerű, in-memory lock a párhuzamos webhook-hívások ellen (pl. ha Pipedrive
# kétszer küldi ki ugyanazt az eseményt, ami előfordul)
_folyamatban_levo_dealek = set()

# magyar hónapnevek a dátum-formázáshoz (ua. minta, mint a pipedrive_addon.py-ban)
_HONAPOK = ["január", "február", "március", "április", "május", "június",
            "július", "augusztus", "szeptember", "október", "november", "december"]


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


def pipedrive_user_lekerese(user_id):
    """Egy Pipedrive felhasználó (értékesítő / deal owner) adatainak lekérése."""
    resp = requests.get(
        f"{PIPEDRIVE_BASE}/users/{user_id}",
        params={"api_token": PIPEDRIVE_API_TOKEN},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Pipedrive user lekérés sikertelen: {data}")
    return data["data"]


def ugyfel_adatok_kinyerese(deal):
    """Kinyeri a deal kapcsolattartójának nevét és email címét - PONTOSAN
    ugyanaz a metódus, mint a visszajelző email / Won automatizációnál: a
    deal person_id-jából lekéri a personont, és annak ELSŐ (vagy elsődleges)
    email címét veszi (a Pipedrive listaként adja: [{"value": "...", "primary": true}, ...]).
    Visszaad egy {"nev": ..., "email": ...} dict-et - üres string, ha nincs adat."""
    ures = {"nev": "", "email": ""}
    person_ref = deal.get("person_id")
    if not person_ref:
        return ures
    # a person_id lehet dict ({"value": 123, ...}) vagy sima szám
    person_id = person_ref.get("value") if isinstance(person_ref, dict) else person_ref
    if not person_id:
        return ures
    person = pipedrive_person_lekerese(person_id)
    email = ""
    email_lista = person.get("email", [])
    if isinstance(email_lista, list) and email_lista:
        for e in email_lista:
            if e.get("primary") and e.get("value"):
                email = e["value"]
                break
        else:
            email = email_lista[0].get("value", "")
    return {"nev": person.get("name", "") or "", "email": email}


def ugyfel_email_kinyerese(deal):
    """Visszafelé kompatibilis wrapper - csak az email címet adja vissza
    (a "Fotók" endpoint és korábbi hívók ezt várják)."""
    return ugyfel_adatok_kinyerese(deal).get("email", "")


def owner_adatok_kinyerese(deal):
    """Kinyeri az illetékes értékesítő (deal owner / tulajdonos) nevét és
    email címét - ugyanaz a minta, mint a pipedrive_addon.py Kivitelezési
    tájékoztató emailjénél (deal.user_id.id -> /users/{id})."""
    ures = {"nev": "", "email": ""}
    owner_ref = deal.get("user_id")
    owner_id = owner_ref.get("id") if isinstance(owner_ref, dict) else owner_ref
    if not owner_id:
        return ures
    try:
        owner = pipedrive_user_lekerese(owner_id)
    except Exception as e:
        logger.warning("Owner lekérés sikertelen (user #%s): %s", owner_id, e)
        return ures
    return {"nev": owner.get("name", "") or "", "email": owner.get("email", "") or ""}


# ---------------------------------------------------------------------------
# EMAIL KÜLDÉS (Apps Script Web App proxy, ugyanaz mint a visszajelző email)
# ---------------------------------------------------------------------------

def email_kuld(cimzett, targy, html_tartalom, cc=""):
    """Email küldése az Apps Script Web App 'sendEmail' action-jén keresztül,
    a céges Gmailből - ugyanaz a mechanizmus, mint a visszajelző emailnél.

    2026.08.12-i javítás: mostantól 'cc' paramétert is elfogad (ugyanúgy,
    mint a pipedrive_addon.py azonos nevű függvénye) - korábban ez a
    függvény sosem küldött Cc-t, ezért az illetékes értékesítő (deal owner)
    sosem látta másolatban az innen kiküldött leveleket."""
    if not WEBAPP_URL:
        raise RuntimeError("Nincs beállítva EMAIL_WEBAPP_URL env változó az email küldéshez.")
    if not cimzett:
        raise RuntimeError("Nincs címzett email cím - az emailt nem lehet kiküldeni.")
    payload = {
        "action": "sendEmail",
        "secret": EMAIL_WEBAPP_SECRET,
        "to": cimzett,
        "subject": targy,
        "htmlBody": html_tartalom,
    }
    if cc:
        payload["cc"] = cc
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


def _ma_datum_fmt():
    """Mai dátum magyar formában, ua. minta mint a pipedrive_addon.py
    _datum_fmt()-je (pl. '2026. augusztus 12.')."""
    d = datetime.now()
    return f"{d.year}. {_HONAPOK[d.month - 1]} {d.day}."


def folyamat_tajekoztato_email_osszeallitasa(kapcsolattarto_nev, owner_nev, owner_email):
    """Betölti a folyamat-tájékoztató HTML sablont (sablonok/folyamat_tajekoztato_email.html)
    és behelyettesíti a benne szereplő helyőrzőket:
      {{DATUM}}, {{OWNER_NEV}}, {{KAPCSOLATTARTO_NEV}}, {{OWNER_EMAIL}}"""
    with open(FOLYAMAT_EMAIL_SABLON, "r", encoding="utf-8") as f:
        html = f.read()
    csere = {
        "{{DATUM}}":               _ma_datum_fmt(),
        "{{OWNER_NEV}}":           owner_nev or "kollégánk",
        "{{KAPCSOLATTARTO_NEV}}":  kapcsolattarto_nev or "Partnerünk",
        "{{OWNER_EMAIL}}":         owner_email or "",
    }
    for k, v in csere.items():
        html = html.replace(k, str(v))
    return html


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
# DROPBOX MŰVELETEK (OLVASÁS: fotók listázása egy megosztott mappalinkből)
# ---------------------------------------------------------------------------

def dropbox_megosztott_mappa_elerese(access_token, url):
    """Egy megosztott Dropbox-link mögötti valós elérési utat (path) adja
    vissza, ha a linket ez a fiók hozta létre (a mappa-generátor mindig ezt a
    fiókot használja, ezért ez a normál eset)."""
    resp = requests.post(
        "https://api.dropboxapi.com/2/sharing/get_shared_link_metadata",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"url": url},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("path_lower")


def dropbox_mappa_tartalmanak_listazasa(access_token, mappa_path, megosztott_url):
    """A mappa fájljainak listája - elsőként a valós path-tal (ha ismert,
    gyorsabb és teljesebb jogosultságú), ha az nem elérhető, visszaesünk a
    megosztott linkes hívásra."""
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    body = {"path": mappa_path} if mappa_path else {"path": "", "shared_link": {"url": megosztott_url}}
    resp = requests.post("https://api.dropboxapi.com/2/files/list_folder", headers=headers, json=body, timeout=20)
    if resp.status_code != 200 and mappa_path:
        resp = requests.post(
            "https://api.dropboxapi.com/2/files/list_folder", headers=headers,
            json={"path": "", "shared_link": {"url": megosztott_url}}, timeout=20,
        )
    resp.raise_for_status()
    return resp.json().get("entries", [])


def dropbox_ideiglenes_link_lekerese(access_token, fajl_path):
    """Egy fájlhoz kb. 4 órán át érvényes, közvetlenül megnyitható linket kér."""
    resp = requests.post(
        "https://api.dropboxapi.com/2/files/get_temporary_link",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"path": fajl_path},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("link")


def dropbox_fotok_listazasa(megosztott_url):
    """Egy megosztott Dropbox-mappalink képfájljainak listája, mindegyikhez
    ideiglenes, közvetlenül megnyitható linkkel (a kalkulátor "Fotók" menüjének
    kell, mert egy sima megosztott mappalink önmagában nem jeleníthető meg
    képként a böngészőben)."""
    access_token = dropbox_access_token_lekerese()
    mappa_path = dropbox_megosztott_mappa_elerese(access_token, megosztott_url)
    entries = dropbox_mappa_tartalmanak_listazasa(access_token, mappa_path, megosztott_url)

    kepek = [
        e for e in entries
        if e.get(".tag") == "file" and e.get("name", "").lower().endswith(KEP_KITERJESZTESEK)
    ]
    kepek.sort(key=lambda e: e.get("name", ""))
    kepek = kepek[:MAX_FOTO_SZAM]

    fotok = []
    for f in kepek:
        fajl_path = f.get("path_lower") or f.get("id")
        if not fajl_path:
            continue
        try:
            link = dropbox_ideiglenes_link_lekerese(access_token, fajl_path)
            if link:
                fotok.append({"name": f.get("name"), "url": link})
        except Exception as e:
            logger.warning("Ideiglenes link hiba (%s): %s", f.get("name"), e)
            continue  # egy hibás fájl ne akassza meg a többit
    return fotok


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
    """Regisztrálja a Dropbox mappa-generáló webhook végpontot ÉS a kalkulátor
    "Fotók" nézetét kiszolgáló olvasó végpontot a Flask app-on.
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

    @app.route("/pipedrive-dropbox-photos", methods=["GET"])
    def dropbox_fotok_endpoint():
        """A kalkulátor "Fotók" menüje hívja: deal_id vagy bid alapján
        visszaadja a projekthez tartozó Dropbox-mappa fotóit (ideiglenes,
        közvetlenül megnyitható linkekkel)."""
        deal_id = request.args.get("deal_id")
        bid = (request.args.get("bid") or "").strip()

        resolved_id = None
        if deal_id:
            try:
                resolved_id = int(deal_id)
            except (ValueError, TypeError):
                pass
        if not resolved_id and bid:
            try:
                resolved_id = _pd_find_deal_by_bid(bid)
            except Exception as e:
                logger.warning("BID keresés sikertelen (%s): %s", bid, e)

        if not resolved_id:
            return jsonify({"ok": False, "error": "Nem találtam Pipedrive deal-t (deal_id és BID alapján sem)"}), 404

        try:
            deal = pipedrive_deal_lekerese(resolved_id)
        except Exception as e:
            logger.error("Deal lekérés sikertelen #%s: %s", resolved_id, e)
            return jsonify({"ok": False, "error": f"Pipedrive API hiba: {e}"}), 502

        dropbox_url = (deal.get(PIPEDRIVE_DROPBOX_FIELD_KEY) or "").strip()
        if not dropbox_url:
            return jsonify({"ok": True, "deal_id": resolved_id, "dropbox_url": None, "photos": []})

        try:
            fotok = dropbox_fotok_listazasa(dropbox_url)
        except Exception as e:
            logger.error("Dropbox listázás sikertelen (deal #%s): %s", resolved_id, e)
            return jsonify({"ok": False, "error": f"Dropbox API hiba: {e}", "dropbox_url": dropbox_url}), 502

        logger.info("Fotók: deal #%s, %d fotó visszaadva", resolved_id, len(fotok))
        return jsonify({"ok": True, "deal_id": resolved_id, "dropbox_url": dropbox_url, "photos": fotok})

    return app


def _deal_feldolgozasa(deal_id):
    deal = pipedrive_deal_lekerese(deal_id)

    # KRITIKUS: ha már van érték a mezőben, NEM generálunk újat és NEM írjuk felül
    # (ez véd a duplikáció ellen - ha ez a lépés már lefutott egyszer, sem a
    # Dropbox mappa, sem a két email nem megy ki újra).
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

    # ── Ügyfél és értékesítő (owner) adatai - EGYSZER lekérve, mindkét emailhez ──
    ugyfel = ugyfel_adatok_kinyerese(deal)
    ugyfel_email = ugyfel.get("email", "")
    owner = owner_adatok_kinyerese(deal)
    owner_email = owner.get("email", "")
    if not owner_email:
        logger.warning("Deal %s: nincs owner email a dealen - Cc kihagyva mindkét emailről.", deal_id)

    email_statusz = "kihagyva (nincs ügyfél email)"
    folyamat_email_statusz = "kihagyva (nincs ügyfél email)"

    if ugyfel_email:
        # 1) fotózási útmutató email - a feltöltő linkkel, Cc-ben az owner-rel
        html_foto = foto_email_osszeallitasa(feltoltesi_url)
        targy_foto = "Fotózási útmutató a padlófelület árajánlatához – SQM Hungary"
        email_kuld(ugyfel_email, targy_foto, html_foto, cc=owner_email)
        email_statusz = f"elküldve ide: {ugyfel_email}" + (f" (Cc: {owner_email})" if owner_email else "")

        # 2) folyamat-tájékoztató email - "Így fogunk együtt dolgozni", ugyanabban
        #    a lefutásban, ugyanannak a címzettnek, szintén Cc-ben az owner-rel
        try:
            html_folyamat = folyamat_tajekoztato_email_osszeallitasa(
                kapcsolattarto_nev=ugyfel.get("nev", ""),
                owner_nev=owner.get("nev", ""),
                owner_email=owner_email,
            )
            targy_folyamat = "Így fogunk együtt dolgozni – SQM Hungary"
            email_kuld(ugyfel_email, targy_folyamat, html_folyamat, cc=owner_email)
            folyamat_email_statusz = f"elküldve ide: {ugyfel_email}" + (f" (Cc: {owner_email})" if owner_email else "")
        except Exception as e:
            # a fotózási útmutató email ekkorra már kiment - ez a második levél
            # hibája ne dobja el az egész webhook-választ hibára, csak logolva legyen
            logger.error("Deal %s: folyamat-tájékoztató email küldése sikertelen: %s", deal_id, e)
            folyamat_email_statusz = f"hiba: {e}"
    else:
        logger.warning("Deal %s: nincs ügyfél email cím, sem a fotózási útmutató, sem a folyamat-tájékoztató email nem ment ki.", deal_id)

    logger.info(
        "Deal %s: Dropbox mappa kész. Megtekintő link -> %s | Feltöltő link -> %s | "
        "Fotó email: %s | Folyamat-tájékoztató email: %s",
        deal_id, megtekintheto_url, feltoltesi_url, email_statusz, folyamat_email_statusz,
    )
    return jsonify({
        "status": "created",
        "folder": mappanev,
        "megtekintheto_url": megtekintheto_url,
        "feltoltesi_url": feltoltesi_url,
        "email": email_statusz,
        "folyamat_tajekoztato_email": folyamat_email_statusz,
    }), 200
