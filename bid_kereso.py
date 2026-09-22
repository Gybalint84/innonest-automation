"""
bid_kereso.py — bejövő számla BID-jének kikeresése a Gmailből, számlaszám alapján.

Működés számlánként:
  1. Gmail-keresés a számlaszámra. Csak olyan levelet fogadunk el, ahol a számlaszám a
     TÁRGYBAN vagy a LEVÉL SZÖVEGÉBEN szerepel — így a könyvelő táblázatai (ahol csak a
     csatolmányban van benne) kiesnek. A saját elküldött leveleket kihagyjuk.
  2. Ha van PDF-csatolmány (pl. STO): kiolvassuk a szövegét, és megkeressük benne a BID-et.
  3. Ha nincs PDF, de van számlalink (pl. Billingo): megnyitjuk böngészőben és ott keressük.

Élőben ellenőrizve (2026-09-21):
  - STO: tárgy „Sto Számla 7150170649", két PDF csatolva, a PDF-en „BID-2026-238".
  - V-Clean (Billingo): tárgy „Számlája érkezett", a szövegben „Számla sorszáma:
    VClean_Sz-2026-131", link: app.billingo.hu/document-access/...

Env: a sheets_kliens Google-OAuth változói. A refresh tokennek a gmail.readonly
jogosultságot is tartalmaznia kell (oauth_token_szerzo.py).

Függőség: pypdf (requirements.txt). Ha nincs telepítve, a PDF-es ág kimarad, a linkes működik.
"""

import base64
import logging
import re
import time
from urllib.parse import urlparse

import requests

import sheets_kliens as sk

log = logging.getLogger("bid_kereso")

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Csak ismert számlázó rendszerek linkjeit nyitjuk meg — a szerver ne kövessen
# tetszőleges, levélben talált URL-t.
ENGEDELYEZETT_LINK_DOMAINEK = ("billingo.hu", "szamlazz.hu", "szamlazo.hu", "kulcs-szamla.hu",
                               "e-szamla.hu", "octopus8.hu", "nav.gov.hu")

LINK_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
BID_RE = re.compile(r"(?<![A-Za-z0-9])BID\s?-?\s?(\d{4})\s?-\s?(\d+)", re.IGNORECASE)

# Állapotok, amik a Bejövő BID lap „gmail_allapot" oszlopába kerülnek
TALALT_PDF = "talált (PDF)"
TALALT_LINK = "talált (link)"
NINCS_LEVEL = "nincs levél a számlaszámra"
NINCS_BID = "megvan a számla, de nincs rajta BID"
HIBA = "hiba"


def bid_a_szovegben(szoveg):
    m = BID_RE.search(szoveg or "")
    return f"BID-{m.group(1)}-{m.group(2)}" if m else ""


def _b64(adat):
    adat = (adat or "").replace("-", "+").replace("_", "/")
    return base64.b64decode(adat + "=" * (-len(adat) % 4))


# ---------------------------------------------------------------- Gmail REST

def _gmail(ut, params=None):
    fejlec = {"Authorization": "Bearer " + sk.access_token()}
    for probalkozas in range(4):
        r = requests.get(f"{GMAIL_API}{ut}", headers=fejlec, params=params, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(1.5 * (probalkozas + 1))
            continue
        if r.status_code == 403:
            reszlet = r.text[:200].replace("\n", " ")
            if "has not been used" in r.text or "disabled" in r.text:
                raise RuntimeError(f"Gmail API nincs engedélyezve a Google Cloud projektben: {reszlet}")
            raise RuntimeError(f"Gmail 403 — valószínűleg hiányzik a gmail.readonly hatókör a refresh "
                               f"tokenből (futtasd újra az oauth_token_szerzo.py-t). Google válasza: {reszlet}")
        if r.status_code >= 400:
            raise RuntimeError(f"Gmail {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError("Gmail: az újrapróbálások elfogytak")


def _reszek(payload):
    """A MIME-fa összes része, laposan."""
    ki = [payload]
    for p in payload.get("parts", []) or []:
        ki.extend(_reszek(p))
    return ki


def uzenet(uzenet_id):
    """Egy levél feldolgozva: tárgy, feladó, szöveg, PDF-csatolmányok, linkek, címkék."""
    m = _gmail(f"/messages/{uzenet_id}", {"format": "full"})
    payload = m.get("payload", {})
    fejlecek = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    szoveg, pdfek = [], []
    for r in _reszek(payload):
        mime = (r.get("mimeType") or "").lower()
        body = r.get("body") or {}
        fajlnev = (r.get("filename") or "").lower()
        if mime in ("text/plain", "text/html") and body.get("data"):
            try:
                szoveg.append(_b64(body["data"]).decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
        elif mime == "application/pdf" or fajlnev.endswith(".pdf"):
            if body.get("attachmentId"):
                pdfek.append({"id": body["attachmentId"], "nev": r.get("filename") or ""})
            elif body.get("data"):
                pdfek.append({"adat": _b64(body["data"]), "nev": r.get("filename") or ""})
    teljes = "\n".join(szoveg)
    return {
        "id": uzenet_id,
        "targy": fejlecek.get("subject", ""),
        "felado": fejlecek.get("from", ""),
        "szoveg": teljes,
        "pdfek": pdfek,
        "linkek": linkek_kinyerese(teljes),
        "cimkek": m.get("labelIds", []) or [],
    }


def csatolmany(uzenet_id, csatolmany_id):
    adat = _gmail(f"/messages/{uzenet_id}/attachments/{csatolmany_id}")
    return _b64(adat.get("data", ""))


def linkek_kinyerese(szoveg):
    """A szövegben lévő számlalinkek — csak az engedélyezett domainekről."""
    ki = []
    for url in LINK_RE.findall(szoveg or ""):
        url = url.rstrip(".,;")
        host = (urlparse(url).hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in ENGEDELYEZETT_LINK_DOMAINEK):
            if url not in ki:
                ki.append(url)
    return ki


def _normal(s):
    return re.sub(r"\s+", " ", (s or "")).lower()


def szamla_levele(szamlaszam):
    """A számlához tartozó bejövő levél, vagy None.

    Csak azt a levelet fogadjuk el, ahol a számlaszám a tárgyban vagy a levél szövegében
    szerepel (a könyvelő táblázataiban csak a csatolmányban van — azokat így kizárjuk),
    és ami nem a saját elküldött levelünk."""
    szam = (szamlaszam or "").strip()
    if not szam:
        return None
    talalat = _gmail("/messages", {"q": f'"{szam}" -in:sent -in:chats', "maxResults": 10})
    jeloltek = []
    for m in talalat.get("messages", []) or []:
        u = uzenet(m["id"])
        if "SENT" in u["cimkek"]:
            continue
        if _normal(szam) not in _normal(u["targy"]) and _normal(szam) not in _normal(u["szoveg"]):
            continue
        jeloltek.append(u)
    if not jeloltek:
        return None
    # ha több is van, az előnyös, amelyiken PDF vagy számlalink van
    jeloltek.sort(key=lambda u: (not u["pdfek"], not u["linkek"]))
    return jeloltek[0]


# ---------------------------------------------------------------- PDF

def pdf_szoveg(adat):
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("[BID-KERESO] pypdf nincs telepítve — a PDF-csatolmányokat nem tudom olvasni")
        return ""
    import io
    try:
        olvaso = PdfReader(io.BytesIO(adat))
        return "\n".join((o.extract_text() or "") for o in olvaso.pages[:6])
    except Exception as e:  # noqa: BLE001
        log.warning("[BID-KERESO] PDF nem olvasható: %s", str(e)[:120])
        return ""


def bid_a_pdfekbol(u):
    for p in u["pdfek"]:
        adat = p.get("adat") or csatolmany(u["id"], p["id"])
        b = bid_a_szovegben(pdf_szoveg(adat))
        if b:
            return b
    return ""


# ---------------------------------------------------------------- link (Playwright)

async def bid_a_linkrol(context, url):
    """Megnyitja a számlalinket. Ha közvetlenül PDF-et ad, azt olvassa; ha oldalt,
    annak szövegében keres, és ha ott nincs, az oldalon lévő PDF-letöltést próbálja."""
    try:
        valasz = await context.request.get(url, timeout=25000)
        tipus = (valasz.headers.get("content-type") or "").lower()
        if "pdf" in tipus:
            return bid_a_szovegben(pdf_szoveg(await valasz.body()))
    except Exception:  # noqa: BLE001 — oldalként próbáljuk
        pass

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2500)
        b = bid_a_szovegben(await page.inner_text("body"))
        if b:
            return b
        hrefek = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)")
        for h in hrefek:
            hl = h.lower()
            if ".pdf" in hl or "download" in hl or "letolt" in hl:
                try:
                    v = await context.request.get(h, timeout=25000)
                    if "pdf" in (v.headers.get("content-type") or "").lower():
                        b = bid_a_szovegben(pdf_szoveg(await v.body()))
                        if b:
                            return b
                except Exception:  # noqa: BLE001
                    continue
        return ""
    finally:
        await page.close()


# ---------------------------------------------------------------- vezérlés

async def bid_kereses_async(szamlaszamok, max_db=40):
    """szamlaszamok: [str] → {számlaszám: {"bid", "allapot"}}.

    Legfeljebb max_db számlát néz meg egy futásban, hogy a HTTP-kérés ne fusson túl hosszan;
    a többit a következő frissítés folytatja (az eredmények a Bejövő BID lapon megmaradnak)."""
    eredmeny, linkesek = {}, []
    for szam in szamlaszamok[:max_db]:
        try:
            u = szamla_levele(szam)
            if not u:
                eredmeny[szam] = {"bid": "", "allapot": NINCS_LEVEL}
                continue
            b = bid_a_pdfekbol(u) if u["pdfek"] else ""
            if b:
                eredmeny[szam] = {"bid": b, "allapot": TALALT_PDF}
            elif u["linkek"]:
                linkesek.append((szam, u["linkek"]))
            else:
                eredmeny[szam] = {"bid": "", "allapot": NINCS_BID}
        except Exception as e:  # noqa: BLE001
            eredmeny[szam] = {"bid": "", "allapot": f"{HIBA}: {str(e)[:120]}"}

    if linkesek:
        from playwright.async_api import async_playwright
        from innonest_core import make_browser_args
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=make_browser_args())
            context = await browser.new_context()
            for szam, linkek in linkesek:
                b = ""
                for url in linkek[:3]:
                    try:
                        b = await bid_a_linkrol(context, url)
                    except Exception as e:  # noqa: BLE001
                        log.warning("[BID-KERESO] %s link hiba: %s", szam, str(e)[:120])
                    if b:
                        break
                eredmeny[szam] = {"bid": b, "allapot": TALALT_LINK if b else NINCS_BID}
            await browser.close()

    return eredmeny
