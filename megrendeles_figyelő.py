"""
megrendeles_figyelő.py – Innonest megrendelőlap figyelő
========================================================
30 percenként ellenőrzi az Innonest megrendelőlapjait.
Ha új "Megrendelt" státuszú tétel jelenik meg:
  - elküldi az adatokat a Google Apps Script Web App-nak
  - az Apps Script átnevezi a sheetet ("- MEGRENDELVE")
  - beírja az adatokat a QUiCK API sheetbe
  - (ÚJ, 2026-09-11) minden ellenőrzéskor frissíti a saját memóriájában
    (JSON fájl) a "jelenleg Megrendelt BID-ek" listáját, és egy ÚJ,
    egyszerű JSON-végponton (/megrendelt-bidek) elérhetővé teszi — ezt a
    webapp (sqm-app-react) kérdezi le a "Mentett projektek" oldalán, és ő
    maga írja be a Firestore-ba, hogy az adott BID-et megrendelte az ügyfél.

⚠️ 2026-09-11: EREDETILEG Firebase Admin SDK-s, KÖZVETLEN Firestore-írást
terveztünk ide (szolgáltatásfiók-kulccsal). Ezt ELVETETTÜK, mert a cég
Google Cloud szervezete tiltja az új szolgáltatásfiók-kulcsok létrehozását
("Disable service account key creation" org policy), és ezt nem lehetett
egyszerűen feloldani. EZ A VÁLTOZAT emiatt NEM használ semmilyen Firebase-t
vagy Google-kulcsot a Python oldalon — csak egy sima, API-kulccsal védett
JSON-végpontot ad, amit a MÁR bejelentkezett felhasználó böngészője hív le,
és ő ír a Firestore-ba a saját (már meglévő) jogosultságával. Pontosan az a
minta, amit a `/ertekesito-teljesitmeny` végpont is használ.

⚠️ EZ A FÁJL A "Webapp szerkesztés" React-projekt oldaláról készült PATCH —
nem ebben a repóban él az eredeti (az az innonest-automation / Railway repo
saját fájlja). A változásokat keresd a "# === ÚJ (2026-09-11) ===" jelölésű
blokkokban; minden más sor változatlan az eredetihez képest. Másold be ezt a
tartalmat a valódi megrendeles_figyelő.py helyére, és nézd meg a fájl alján
lévő TELEPÍTÉSI LÉPÉSEK részt — abban van egy pont, amit NEKED kell
véglegesítened (a Flask `app` objektum importja, mert az nálam nem látható,
csak a server.py-ban).
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

# === ÚJ (2026-09-11): webapp "Megrendelve" jelző — adattárolás + végpont ══════
# A React webapp (sqm-app-react) "Mentett projektek" listáján az 5. jelzőlámpa
# ("Megrendelve") azt mutatja, hogy az ügyfél megrendelte-e tőlünk az adott
# BID-számú ajánlatot. Ezt onnan tudjuk, hogy a BID megjelenik "Megrendelt"
# státusszal az Innonest megrendelőlapjai közt — amit ez a szkript már
# amúgy is figyel. Az alábbi blokk ezt a "jelenleg Megrendelt BID-ek" listát
# tartja karban egy /tmp-beli JSON fájlban, és egy Flask GET-végponton
# keresztül elérhetővé teszi a webapp számára.
#
# FONTOS KÜLÖNBSÉG a meglévő `PROCESSED_FILE`-hoz képest: a `processed` set
# csak azt jelöli, hogy egy adott sorra (row_id) már elküldtük-e a
# Sheet/Drive/QUiCK-láncot (hogy ne csináljuk meg kétszer) — ez viszont
# MINDEN ellenőrzéskor frissül, MINDEN aktuálisan "Megrendelt" tételre,
# függetlenül attól, hogy a Sheet-lánc már lefutott-e rá korábban. Ezért ha a
# Railway újraindul és a /tmp kiürül (ismert korlát, lásd lent), ez az
# adattár a KÖVETKEZŐ ellenőrzéskor magától helyreáll, amíg a BID az
# Innonest oldalán "Megrendelt" marad.

MEGRENDELT_BIDEK_FILE = "/tmp/megrendelt_bidek.json"

# Ugyanaz a kulcs, amit a webapp (services/megrendelesFigyelo.js) is használ
# a Railway API-hívásokhoz — pl. "X-API-Key" headerben várjuk. Ha nálatok ez
# másik env var/érték a szerveren, itt (és a webapp oldalon is) frissíteni
# kell, hogy egyezzenek.
API_KEY = os.environ.get("API_KEY", "389188")


def load_megrendelt_bidek() -> dict:
    """{ bid: "ISO dátum, amikor ELŐSZÖR észleltük Megrendeltként" }"""
    if os.path.exists(MEGRENDELT_BIDEK_FILE):
        try:
            with open(MEGRENDELT_BIDEK_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_megrendelt_bidek(bidek: dict):
    with open(MEGRENDELT_BIDEK_FILE, "w") as f:
        json.dump(bidek, f)


def frissits_megrendelt_bidek(tetelek: list):
    """Minden ellenőrzési körben hívva (nem csak az ÚJ tételekre!) — az
    aktuálisan Megrendelt BID-ekhez rögzíti az ELSŐ észlelés dátumát, ha még
    nincs eltárolva. Szándékosan NEM töröl bidet, ha időközben eltűnik a
    listából (pl. mert az Innonestben Teljesítettre váltott) — a webapp
    oldalán ez nem probléma, mert a `meta.megrendelve` mező úgyis csak egyszer
    íródik be (lásd domain/projects.js: matchMegrendeltBidek, idempotens)."""
    bidek = load_megrendelt_bidek()
    valtozott = False
    ma = date.today().isoformat()
    for tetel in tetelek:
        bid = tetel.get("bid", "")
        if not bid:
            continue
        if bid not in bidek:
            bidek[bid] = ma
            valtozott = True
    if valtozott:
        save_megrendelt_bidek(bidek)
    return bidek


def megrendelt_bidek_endpoint():
    """Flask view-függvény — regisztráld a szerveren, pl.:
        from megrendeles_figyelő import megrendelt_bidek_endpoint
        app.add_url_rule("/megrendelt-bidek", "megrendelt_bidek",
                          megrendelt_bidek_endpoint, methods=["GET"])
    (VAGY, ha nálatok Blueprint/dekorátor mintát használtok a többi
    endpointnál — pl. a /ertekesito-teljesitmeny-nél —, akkor UGYANAZT a
    mintát kövesd itt is; ezt a fájlt nem tudtam ahhoz igazítani, mert a
    server.py nincs nálam.)
    Válasz: {"ok": true, "items": {"<bid>": "<ISO dátum>", ...}}
    """
    from flask import request, jsonify  # helyi import, hogy a modul Flask nélkül is importálható maradjon
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"ok": False, "error": "Érvénytelen API-kulcs"}), 401
    return jsonify({"ok": True, "items": load_megrendelt_bidek()})

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

        # === ÚJ (2026-09-11): webapp "Megrendelve" adattár frissítése ===
        # Szándékosan MINDEN talált tételre lefut, nem csak az újakra (lásd a
        # frissits_megrendelt_bidek() docstringjét fentebb).
        try:
            frissits_megrendelt_bidek(tetelek)
        except Exception as e:
            log.error(f"megrendelt_bidek frissítés hiba: {e}")
        # ══════════════════════════════════════════════════════════════

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
# Ehhez a verzióhoz NEM kell Firebase-kulcs, NEM kell requirements.txt
# módosítás, és NEM kell Firestore security rules módosítás — csak egy új
# HTTP végpontot kell regisztrálni a meglévő Flask szerveren.
#
# 1) Nyisd meg a server.py-t (vagy ahol a Flask `app` létrejön és a többi
#    endpoint regisztrálva van — pl. ahol a `/ertekesito-teljesitmeny` van).
#
# 2) Adj hozzá egy sort, ami regisztrálja az új végpontot. Kétféle minta
#    lehet nálatok, nézd meg melyikhez hasonlít a meglévő kód:
#
#    a) Ha egyszerű "app.add_url_rule" vagy "@app.route" mintát használtok:
#         from megrendeles_figyelő import megrendelt_bidek_endpoint
#         app.add_url_rule("/megrendelt-bidek", "megrendelt_bidek",
#                           megrendelt_bidek_endpoint, methods=["GET"])
#
#    b) Ha a modulban magában van a dekorátor (mint gyanítom a
#       billingo_teljesitmeny.py-ban lehet a /ertekesito-teljesitmeny-nél),
#       akkor ide, a fájl tetején (ahol az "app" importálható) tedd:
#         from server import app
#         @app.route("/megrendelt-bidek", methods=["GET"])
#         def megrendelt_bidek_route():
#             return megrendelt_bidek_endpoint()
#
#    Ha egyik sem világos, küldd át a server.py-t (vagy a
#    billingo_teljesitmeny.py-t) is, és pontosítom ezt a lépést.
#
# 3) Ellenőrizd, hogy az API_KEY env var (vagy a fenti fallback "389188")
#    ugyanaz, mint amit a webapp (src/services/megrendelesFigyelo.js)
#    használ — ha nálatok az API-kulcs env var neve más, írd át a fenti
#    `API_KEY = os.environ.get("API_KEY", "389188")` sort.
#
# 4) Push a GitHub repóba → Railway automatikusan újraépíti és -indítja a
#    szolgáltatást.
#
# 5) Ellenőrzés: nyisd meg böngészőben (vagy Postmannel, X-API-Key headerrel)
#    a https://sqm-visszajelzes.up.railway.app/megrendelt-bidek címet — ha
#    van jelenleg Megrendelt BID az Innonestben, egy ilyesmit kell kapnod:
#    {"ok": true, "items": {"BID-2026-251": "2026-09-11"}}
#    Ha üres az "items", vagy még nem futott le a figyelő egy kört (max. 30
#    percet várhat), vagy tényleg nincs jelenleg Megrendelt tétel.
#
# 6) Ismert korlát (nem ebben a patch-ben javított, de érdemes tudni róla):
#    mind a PROCESSED_FILE, mind az ÚJ MEGRENDELT_BIDEK_FILE a /tmp alatt
#    van, ami Railway-újraindításkor kiürül. A MEGRENDELT_BIDEK_FILE ettől
#    nem szenved tartós adatvesztést, mert minden 30 perces körben újra
#    felépül minden AKTUÁLISAN "Megrendelt" tételből — legfeljebb egy környi
#    (max. 30 perc) késést okozhat, mire a webapp badge-e frissül egy
#    újraindítás után.
