"""
megrendeles_figyelő.py – Innonest megrendelőlap figyelő
========================================================
30 percenként ellenőrzi az Innonest megrendelőlapjait.
Ha új "Megrendelt" státuszú tétel jelenik meg:
  - elküldi az adatokat a Google Apps Script Web App-nak
  - az Apps Script átnevezi a sheetet ("- MEGRENDELVE")
  - beírja az adatokat a QUiCK API sheetbe
  - (ÚJ, 2026-09-11) beírja a webapp Firestore-jába is, hogy az adott
    BID-számú ajánlatot megrendelte az ügyfél

⚠️ EZ A FÁJL A "Webapp szerkesztés" React-projekt oldaláról készült PATCH —
nem ebben a repóban él az eredeti (az az innonest-automation / Railway repo
saját fájlja). A változásokat keresd a "# === ÚJ (2026-09-11) ===" jelölésű
blokkokban; minden más sor változatlan az eredetihez képest. Másold be ezt a
tartalmat a valódi megrendeles_figyelő.py helyére (vagy emeld át kézzel a
jelölt részeket), és nézd meg a fájl alján lévő TELEPÍTÉSI LÉPÉSEK részt.
"""

import os
import json
import re
import time
import logging
import threading
from datetime import date  # === ÚJ (2026-09-11) ===

import requests
from playwright.async_api import async_playwright

from innonest_core import (
    run_in_loop, login, load_session, make_browser_args
)

log = logging.getLogger(__name__)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
WEBAPP_SECRET  = os.environ.get("WEBAPP_SECRET", "")
WEBAPP_URL     = os.environ.get(
    "WEBAPP_URL",
    "https://script.google.com/macros/s/AKfycbyy1PQmHyBSlnWpXQR9bygVfFV_g2gJI9_7UjDI5zHm2xXElIX1DvsszM_UJu8l7too/exec"
)
PROCESSED_FILE = "/tmp/feldolgozott_megrendelesek.json"
CHECK_INTERVAL = 1800  # 30 perc

# === ÚJ (2026-09-11): Firestore (SQM webapp) integráció ═══════════════════════
# A React webapp (sqm-app-react) "Mentett projektek" listáján az 5. jelzőlámpa
# ("Megrendelve") a `projects/{id}` dokumentum `snap.meta.megrendelve` mezőjét
# (dátum-string, pl. "2026-09-11") mutatja — pontosan úgy, mint az
# `ajKikuld`/`pdfKikuld` mezőket. Ezt a mezőt ez a blokk írja be, amikor egy
# BID "Megrendelt"-té válik. A Firebase Admin SDK-s írás MEGKERÜLI a Firestore
# security rules-t (szolgáltatásfiók = teljes jogosultság), ezért a webapp
# oldalán NEM kell semmilyen szabály-módosítás.
#
# TELEPÍTÉS (lásd részletesen a fájl alján is):
#   1. `pip install firebase-admin` (requirements.txt-be is fel kell venni)
#   2. Firebase Console → Project settings → Service accounts →
#      "Generate new private key" → a letöltött JSON TELJES tartalmát tedd be
#      egyetlen Railway env var-ba: FIREBASE_SERVICE_ACCOUNT_JSON
#   3. Amíg a fenti env var nincs beállítva, ez a blokk csendben kihagyja a
#      Firestore-írást (figyelmeztetést logol egyszer) — a meglévő
#      Sheet/Drive/QUiCK-folyamat változatlanul működik.

_firestore_db = None
_firestore_init_tried = False


def _get_firestore_db():
    """Lusta inicializálás — csak akkor importál/kapcsolódik, ha tényleg
    kell, és csak egyszer próbálkozik (utána a memoizált eredményt adja)."""
    global _firestore_db, _firestore_init_tried
    if _firestore_init_tried:
        return _firestore_db
    _firestore_init_tried = True

    cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not cred_json:
        log.warning(
            "FIREBASE_SERVICE_ACCOUNT_JSON nincs beállítva – a webapp "
            "Firestore 'Megrendelve' jelzője NEM frissül (a Sheet/Drive/"
            "QUiCK folyamat ettől függetlenül változatlanul fut)."
        )
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        cred = credentials.Certificate(json.loads(cred_json))
        firebase_admin.initialize_app(cred)
        _firestore_db = firestore.client()
        log.info("Firestore (SQM webapp) kapcsolat inicializálva.")
    except Exception as e:
        log.error(f"Firestore inicializálás sikertelen: {e}")
        _firestore_db = None

    return _firestore_db


def _norm_bid(s: str) -> str:
    """Pontos mása a webapp `domain/projects.js: normBid()` függvényének —
    ha az ott megváltozik, ezt IS frissíteni kell, különben a két oldal
    eltérő BID-eket fog "ugyanannak" tekinteni. Kisbetűsít, levágja a "BID"
    előtagot (kötőjellel/szóközzel), és minden nem alfanumerikus karaktert
    kiszűr — így "BID-2026-251", "2026-251" és "2026251" is egyenlő lesz."""
    s = (s or "").lower()
    s = re.sub(r"^bid[-\s]*", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def update_firestore_megrendelve(bid: str, has_bid: bool) -> None:
    """Megkeresi a webapp `projects` gyűjteményében azt a projektet, aminek
    `snap.meta.bid` mezője (normalizálva) egyezik a paraméterrel, és beírja
    a mai dátumot a `snap.meta.megrendelve` mezőbe — hacsak már nincs
    kitöltve (idempotens: nem ír felül egy korábban rögzített dátumot, és
    nem piszkálja feleslegesen a `savedAt`-ot, ha a Railway-figyelő a
    /tmp-beli `processed` állomány elvesztése miatt (újraindítás) újra
    "újnak" látná ugyanazt a BID-et).

    A "SORSZAM-YYYY-NNN" fallback formátumnál (amikor a sor szövegében nem
    volt szabályos "BID-..." minta) a "SORSZAM-" előtagot levágjuk
    egyeztetés előtt, mert a webapp oldalán soha nem ez a formátum szerepel.
    """
    db = _get_firestore_db()
    if db is None:
        return

    bid_for_match = bid[len("SORSZAM-"):] if (not has_bid and bid.startswith("SORSZAM-")) else bid
    target = _norm_bid(bid_for_match)
    if not target:
        log.warning(f"update_firestore_megrendelve: üres/érvénytelen BID ({bid!r}), kihagyva.")
        return

    try:
        talalt = 0
        for doc in db.collection("projects").stream():
            data = doc.to_dict() or {}
            if data.get("deleted"):
                continue
            meta = ((data.get("snap") or {}).get("meta")) or {}
            if _norm_bid(meta.get("bid", "")) != target:
                continue

            talalt += 1
            if (meta.get("megrendelve") or "").strip():
                log.info(f"Firestore: {bid} projektje már meg van jelölve megrendelve-ként ({doc.id}), nem írom felül.")
                continue

            doc.reference.update({
                "snap.meta.megrendelve": date.today().isoformat(),
                "savedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            })
            log.info(f"✅ Firestore: {bid} → 'Megrendelve' beírva a(z) {doc.id} projekthez.")

        if talalt == 0:
            log.warning(f"Firestore: nem található projekt ehhez a BID-hez: {bid} (a webapp meta.bid mezője alapján).")
        elif talalt > 1:
            log.warning(f"Firestore: {talalt} projekt is egyezett a(z) {bid} BID-re — mindegyiket megjelöltem, érdemes ellenőrizni az adatot.")

    except Exception as e:
        log.error(f"Firestore írás hiba ({bid}): {e}")

# ═══════════════════════════════════════════════════════════════════════════


# ── Feldolgozott BID-ek tárolása ──────────────────────────────────────────────

def load_processed() -> set:
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_processed(processed: set):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(processed), f)


# ── Megrendelőlapok lekérése ──────────────────────────────────────────────────

async def get_megrendelt_tetelek(page) -> list:
    """
    Megnyitja a Megrendelőlapok listát és visszaadja
    a Megrendelt státuszú tételek adatait.
    """
    log.info("Megrendelőlapok ellenőrzése...")
    await page.goto("https://app.innonest.hu/ordersheets", wait_until="networkidle")
    await page.wait_for_timeout(2000)

    sorok_raw = await page.evaluate(
        "() => { "
        "  var eredmeny = []; "
        "  var sorok = document.querySelectorAll('tr, .list-item'); "
        "  sorok.forEach(function(sor) { "
        "    var szoveg = sor.innerText || ''; "
        "    var rowId = sor.getAttribute('data-id') || ''; "
        "    var linkek = []; "
        "    var as = sor.querySelectorAll('a[href]'); "
        "    for (var i=0; i<as.length; i++) { "
        "      var href = as[i].getAttribute('href') || ''; "
        "      if (href.indexOf('pdf') === -1 && href.indexOf('print') === -1) { "
        "        linkek.push(href); "
        "      } "
        "    } "
        "    eredmeny.push({szoveg: szoveg, row_id: rowId, link: linkek[0] || ''}); "
        "  }); "
        "  return eredmeny; "
        "}"
    )

    tetelek = []
    seen_bid = set()

    for sor in sorok_raw:
        szoveg = sor.get("szoveg", "")
        row_id = sor.get("row_id", "")
        link   = sor.get("link", "")

        bid_match = re.search(r"BID-[0-9]{4}-[0-9]+", szoveg)
        has_bid = bid_match is not None

        if has_bid:
            bid = bid_match.group(0)
        else:
            sorszam_match = re.search(r"[0-9]{4}-[0-9]+", szoveg)
            bid = "SORSZAM-" + sorszam_match.group(0) if sorszam_match else ""
            if not bid:
                continue

        if "megrendelt" not in szoveg.lower():
            continue

        if bid in seen_bid:
            continue
        seen_bid.add(bid)

        sorok_lista = [s.strip() for s in szoveg.splitlines() if s.strip()]

        ertelmes_sorok = []
        for s in sorok_lista:
            if re.match(r"^\d{4}-\d{2}-\d{2}", s): continue
            if re.match(r"^\d{4}-\d+$", s): continue
            if re.search(r"HUF|EUR|USD|GBP|CHF", s): continue
            if re.search(r"megrendelt|piszkozat|elküldve", s, re.IGNORECASE): continue
            if re.match(r"^[\d\s\.,]+$", s): continue
            if len(s) <= 5 and s.isupper(): continue
            ertelmes_sorok.append(s)

        targya = ertelmes_sorok[0] if len(ertelmes_sorok) >= 1 else ""
        cegnev = ertelmes_sorok[1] if len(ertelmes_sorok) >= 2 else ""

        penznem = "HUF"
        netto = ""
        arfolyam = "1"
        osszes_osszeg = []
        for sor_r in szoveg.splitlines():
            sor_r = sor_r.strip()
            penz_m = re.search(r"([0-9][0-9 ]{3,}[0-9])\s*(HUF|EUR|USD|GBP|CHF)", sor_r)
            if penz_m:
                szam = penz_m.group(1).replace(" ", "")
                if len(szam) >= 4:
                    osszes_osszeg.append((int(szam), penz_m.group(2)))

        if osszes_osszeg:
            netto = str(osszes_osszeg[0][0])
            penznem = osszes_osszeg[0][1]
            if penznem == "HUF":
                arfolyam = "1"
            else:
                huf_osszeg = next((s for s, p in osszes_osszeg if p == "HUF"), None)
                if huf_osszeg and osszes_osszeg[0][0] > 0:
                    arfolyam = str(round(huf_osszeg / osszes_osszeg[0][0]))
                else:
                    arfolyam = ""

        if not row_id:
            row_id = bid

        log.info(f"  → {bid}: cég='{cegnev}', tárgy='{targya[:40]}', "
                 f"pénznem={penznem}, nettó={netto}, árfolyam={arfolyam}")

        tetelek.append({
            "row_id": row_id, "bid": bid, "has_bid": has_bid,
            "cegnev": cegnev, "targya": targya,
            "penznem": penznem, "netto": netto, "arfolyam": arfolyam,
            "link": link,
        })

    log.info(f"Talált Megrendelt tételek: {len(tetelek)}")
    return tetelek


# ── Fő ellenőrző funkció ──────────────────────────────────────────────────────

async def check_megrendelesek():
    """Bejelentkezik, lekéri a megrendelőlapokat, feldolgozza az újakat."""
    processed = load_processed()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=make_browser_args()
        )
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        await page.goto("https://app.innonest.hu/ordersheets", wait_until="networkidle")
        await page.wait_for_timeout(1500)

        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu/ordersheets", wait_until="networkidle")
            await page.wait_for_timeout(1000)

        tetelek = await get_megrendelt_tetelek(page)

        for tetel in tetelek:
            row_id = tetel["row_id"]
            bid    = tetel["bid"]

            if row_id in processed:
                log.info(f"{bid} már feldolgozva – kihagyom.")
                continue

            log.info(f"Új megrendelés feldolgozása: {bid} (BID: {tetel.get('has_bid')})")

            try:
                response = requests.post(
                    WEBAPP_URL,
                    json={
                        "secret":   WEBAPP_SECRET,
                        "bid":      bid,
                        "has_bid":  tetel.get("has_bid", False),
                        "cegnev":   tetel.get("cegnev", ""),
                        "targya":   tetel.get("targya", ""),
                        "penznem":  tetel.get("penznem", "HUF"),
                        "netto":    tetel.get("netto", ""),
                        "arfolyam": tetel.get("arfolyam", "1"),
                    },
                    timeout=30
                )
                result = response.json()
                log.info(f"Web App válasz ({bid}): {result}")

                if result.get("success"):
                    log.info(f"✅ {bid} sikeresen feldolgozva")
                    # === ÚJ (2026-09-11): webapp Firestore "Megrendelve" jelző ===
                    try:
                        update_firestore_megrendelve(bid, tetel.get("has_bid", False))
                    except Exception as e:
                        log.error(f"Firestore-frissítés hiba ({bid}): {e}")
                    # ══════════════════════════════════════════════════════════
                    processed.add(row_id)
                    save_processed(processed)
                else:
                    log.warning(f"⚠️ {bid}: {result.get('error')}")

            except Exception as e:
                log.error(f"Web App hívás hiba ({bid}): {e}")

        await browser.close()


# ── Háttérszál indítása ───────────────────────────────────────────────────────

def megrendeles_figyelő():
    """Háttérszál: 30 percenként ellenőrzi az Innonest megrendelőlapokat."""
    log.info("Megrendelés figyelő elindult.")
    while True:
        try:
            run_in_loop(check_megrendelesek())
        except Exception as e:
            log.error(f"Figyelő hiba: {e}")
        log.info(f"Következő ellenőrzés {CHECK_INTERVAL // 60} perc múlva...")
        time.sleep(CHECK_INTERVAL)


def start_figyelő():
    """Elindítja a figyelőt egy daemon szálban. Hívd meg a server.py-ból."""
    threading.Thread(target=megrendeles_figyelő, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════
# TELEPÍTÉSI LÉPÉSEK (2026-09-11, webapp "Megrendelve" jelzőlámpa)
# ═══════════════════════════════════════════════════════════════════════════
#
# 1) requirements.txt (a Railway repóban) — vedd fel, ha még nincs:
#      firebase-admin
#
# 2) Firebase szolgáltatásfiók:
#    - Firebase Console → ⚙️ Project settings → Service accounts fül
#    - "Generate new private key" gomb → letölt egy .json fájlt
#    - A JSON TELJES tartalmát (egy sorban, escapelve nem kell) másold be
#      Railway-en egy új env var-ba: FIREBASE_SERVICE_ACCOUNT_JSON
#      (Railway → a szolgáltatás → Variables → New Variable)
#
# 3) Semmi mást nem kell módosítani a Firestore security rules-on — a
#    szolgáltatásfiókos (Admin SDK) írás megkerüli a rules-t.
#
# 4) Ellenőrzés bevezetés után: a Railway logban keresd a
#    "Firestore (SQM webapp) kapcsolat inicializálva." és a
#    "✅ Firestore: BID-... → 'Megrendelve' beírva..." sorokat.
#    Ha a "FIREBASE_SERVICE_ACCOUNT_JSON nincs beállítva" warning jön újra és
#    újra, az env var nem érte el a futó szolgáltatást (Railway redeploy
#    kellhet a változó felvétele után).
#
# 5) Ismert korlát (nem ebben a patch-ben javított, de érdemes tudni róla):
#    a PROCESSED_FILE a /tmp alatt van, ami Railway-újraindításkor kiürül —
#    ilyenkor minden aktuálisan "Megrendelt" tétel újra "újnak" fog látszani,
#    és újra lefut rájuk a teljes lánc (Sheet/Drive/QUiCK ÉS Firestore is).
#    A Firestore-oldalt ez nem veszélyezteti (a fenti idempotencia-ellenőrzés
#    miatt nem ír felül egy már kitöltött `megrendelve` dátumot), de a
#    Sheet/Drive/QUiCK oldalon ez már korábban is megvolt — ez a patch nem
#    változtat ezen a viselkedésen.
