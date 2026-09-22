"""
projekt_haszon.py — projektenkénti tényleges vs. kalkulált haszon

Bekötés a server.py-ba:
    from projekt_haszon import register_projekt_haszon_routes
    register_projekt_haszon_routes(app)

Dockerfile:
    COPY projekt_haszon.py .

Env változók (a sheets_kliens Google-OAuth változói mellett):
    SZAMLAZZ_SHEET_ID               – a Számlázz.hu adatkapcsolat munkafüzete (forrás ÉS alapból a cél is)
    SZAMLAZZ_HASZON_SHEET_ID        – OPCIONÁLIS: csak akkor, ha külön munkafüzetbe kell a kimutatás
    SZAMLAZZ_HASZON_SECRET          – a /projekt-haszon/frissit végpont titka
    SZAMLAZZ_ELLENORZO_NAPLO_ID     – a számlaellenőrző naplója (opcionális, alapértelmezés a skill szerinti ID)
(Az anyagbeszállítók listája NEM env változó: a cél munkafüzet "Beállítások" lapján van,
 ott bármikor szerkesztheted deploy nélkül. Az első futáskor jön létre az alapértelmezéssel.)

Végpont:
    GET/POST /projekt-haszon/frissit?secret=...   – teljes frissítés (Innonest-scrape + Sheet-írás), JSON összegzés

Folyamat:
    1. Kimenő számlák → BID:   Innonest /invoices lista, "[Árajánlat KIV #BID-…]" hivatkozás a sorban.
       (Az előleg- és végszámla is BID-et kap; a projekt csak akkor "kiértékelhető", ha van végszámlája.)
    2. Bejövő számlák → BID, három lépcsőben, a Hozzárendelések lapon kézi felülírással:
         a) a számla szövegében szereplő BID (tétel, megjegyzés, rendelésszám)
         b) Innonest beszerzési megrendelőlap: BID + beszállító + nettó (±2%)
         c) a számlaellenőrző naplója (számlaszám → BID)
    3. Kategória: anyag (SZAMLAZZ_ANYAG_SZALLITOK) vagy munkadíj. Minden egyéb bejövő számlát figyelmen kívül hagyunk.
    4. Kalkulált értékek: a Padló kalkulátor webappból — lásd kalkulalt_lekeres() (bekötendő).
    5. Eredmény: "Projektek" lap (BID-enként) + "Hozzárendelések" lap (számlánként, kézi felülírás oszloppal).
"""

import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

import requests
from flask import request, jsonify

import sheets_kliens as sk

log = logging.getLogger("projekt_haszon")

BID_RE = re.compile(r"(?<![A-Za-z0-9])BID\s?-?\s?(\d{4})\s?-\s?(\d+)", re.IGNORECASE)
KIV_RE = re.compile(r"KIV\s*#\s?(\d{4}-\d+)", re.IGNORECASE)   # megrendelőlap-szám a számlán
INVOICE_REF_RE = re.compile(r"#(BID-)?(\d{4}-\d+)")     # ugyanaz, mint innonest_szamlalo.INVOICE_REF_PATTERN

PROJEKT_LAP = "Projektek"
HOZZAR_LAP = "Hozzárendelések"          # régi lap — már nem írjuk, a két BID-lap váltotta
KIMENO_LAP = "Kimenő BID"
BEJOVO_LAP = "Bejövő BID"
BEALL_LAP = "Beállítások"

KIMENO_OSZLOPOK = ["szamlaszam", "kelt", "ugyfel", "tipus", "netto_huf",
                   "bid_auto", "forras", "bid_kezi", "bid"]
BEJOVO_OSZLOPOK = ["szamlaszam", "kelt", "partner", "kategoria", "netto_huf",
                   "bid_auto", "forras", "gmail_allapot", "bid_kezi", "bid", "figyelmeztetes"]

GMAIL_MAX_PER_FUTAS = int(os.environ.get("SZAMLAZZ_GMAIL_MAX", "40"))
NAPLO_ALAP_ID = "1XQO7-kq2dtPhMpZ5ew327vVq-ddW1IFSAReaVLAQQs0"

PROJEKT_OSZLOPOK = [
    "bid", "ugyfel", "vegszamlak", "elolegszamlak",
    "bevetel_netto_huf", "anyag_tenyleges", "munkadij_tenyleges", "haszon_tenyleges",
    "kalk_bevetel", "kalk_anyag", "kalk_munkadij", "kalk_haszon",
    "elteres_ft", "elteres_pct", "sajat_csapat",
    "anyag_beszallitonkent", "alvallalkozonkent", "allapot", "megjegyzes", "frissitve",
]
HOZZAR_OSZLOPOK = [
    "szamlaszam", "irany", "tipus", "kelt", "partner_nev", "netto_huf",
    "bid_auto", "modszer", "kategoria_auto", "bid_kezi", "kategoria_kezi", "bid_ervenyes", "kategoria_ervenyes",
]


# ---------------------------------------------------------------- segédek

def bid_normalizal(s):
    m = BID_RE.search(s or "")
    return f"BID-{m.group(1)}-{m.group(2)}" if m else ""


def _num(x):
    try:
        return float(str(x).replace(" ", "").replace(",", ".") or 0)
    except ValueError:
        return 0.0


def _sorok_dict(sorok, oszlopok=None):
    """Sheet-tartomány (első sor fejléc) → [dict]."""
    if not sorok:
        return []
    fej = [str(c).strip() for c in sorok[0]]
    ki = []
    for s in sorok[1:]:
        d = {fej[i]: (s[i] if i < len(s) else "") for i in range(len(fej))}
        ki.append(d)
    return ki


ANYAG_ALAP = ["STO", "Murexin", "MC Bauchemie", "Conica", "Eurostep"]
ALV_ALAP = ["V-Clean&Services Kft", "Kotán Építéstechnológia Kft.", "CHEMI BAU PLUSZ Kft.",
            "CSEH ÉS TÁRSA ÉPÍTŐ Kft.", "Epoxy Padló Kft.", "Padló Service Kft.",
            "Padlótechnika-Melegburkolati Kft.", "Fodor EP Floor Kft."]

# A Beállítások lapról beolvasott listák; amíg None, az alapértelmezés érvényes.
AKTIV_ANYAG_LISTA = None
AKTIV_ALV_LISTA = None

# A cégforma nem azonosít: "Kft." és "Korlátolt Felelősségű Társaság" ugyanaz.
_CEGFORMA = {"kft", "zrt", "nyrt", "bt", "kkt", "rt", "korlatolt", "felelossegu", "tarsasag",
             "reszvenytarsasag", "betéti", "beteti", "ag", "gmbh", "ltd", "sro"}


def _tokenek(nev):
    """Ékezet- és írásjel-független szótokenek, cégforma nélkül.
    'Sto Épitöanyag Kft.' → {'sto', 'epitoanyag'}"""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(nev or "")).encode("ascii", "ignore").decode().lower()
    return {t for t in re.split(r"[^a-z0-9]+", s) if t and t not in _CEGFORMA}


def ceg_egyezik(lista_nev, partner_nev):
    """Illeszkedik-e egy listában megadott cégnév a számla partnerére: a lista nevének
    minden szava szerepel a partner nevében, egész szóként.
    STO → 'Sto Épitöanyag Kft.' igen, de 'Deli Store Kft.' nem (a 'store' nem 'sto')."""
    lt, pt = _tokenek(lista_nev), _tokenek(partner_nev)
    return bool(lt) and lt <= pt


def anyag_szallito_e(partner_nev, lista=None):
    if lista is None:
        lista = AKTIV_ANYAG_LISTA if AKTIV_ANYAG_LISTA is not None else ANYAG_ALAP
    return any(ceg_egyezik(n, partner_nev) for n in lista if n)


def alvallalkozo_e(partner_nev, lista=None):
    if lista is None:
        lista = AKTIV_ALV_LISTA if AKTIV_ALV_LISTA is not None else ALV_ALAP
    return any(ceg_egyezik(n, partner_nev) for n in lista if n)


def partner_kategoria(partner_nev):
    """'anyag' / 'alvállalkozó' / '' (nem figyelt partner — kimarad a kimutatásból)."""
    if anyag_szallito_e(partner_nev):
        return "anyag"
    if alvallalkozo_e(partner_nev):
        return "alvállalkozó"
    return ""


# ---------------------------------------------------------------- 1. kimenő → BID (Innonest /invoices)

LISTA_OLDALMERET = 100          # az innonest_szamlalo-val azonos lapméret
ACQ_ALAP = "https://app.innonest.hu/acquisition"

# A beszerzési lista lapozása (Bálint megerősítette, 2026-09-18):
#   1. oldal: /acquisition/index/0/      2. oldal: /acquisition/index/100/
ACQ_OLDAL = ACQ_ALAP + "/index/{o}/"


BESZERZES_KIOLVASO = """
() => {
    const eredmeny = [];
    document.querySelectorAll('table.table-softservice tr').forEach(tr => {
        const szoveg = (tr.innerText || '').trim();
        if (!szoveg) return;
        const pdfLink = tr.querySelector('a[href*="worksheets_pdf/open/"]');
        const modalBtn = tr.querySelector('a.my-modal, .my-modal');
        eredmeny.push({ szoveg: szoveg,
                        pdf_href: pdfLink ? pdfLink.getAttribute('href') : '',
                        van_modal_gomb: !!modalBtn });
    });
    return eredmeny;
}
"""


async def beszerzesi_sorok_az_oldalrol(page):
    """A beszerzési lista sorai az ÉPPEN MEGNYITOTT oldalról.

    Nem a szamla_ellenorzo._get_beszerzesi_sorok-ot használjuk, mert az a belsejében
    maga navigál az /acquisition első oldalára — lapozásnál ezért mindig ugyanazt a
    100 sort adta vissza (élő próbán derült ki, 2026-09-22)."""
    try:
        await page.wait_for_selector("table.table-softservice tr", timeout=10000)
    except Exception:  # noqa: BLE001 — üres oldal is lehet
        pass
    nyers = await page.evaluate(BESZERZES_KIOLVASO)
    tetelek = []
    for sor in nyers:
        szoveg = sor.get("szoveg", "")
        pdf_href = sor.get("pdf_href", "")
        if not pdf_href or not sor.get("van_modal_gomb"):
            continue
        m = re.search(r"worksheets_pdf/open/(\d+)", pdf_href)
        if not m:
            continue
        sorok_lista = [x.strip() for x in szoveg.splitlines() if x.strip()]
        targya = sorok_lista[2] if len(sorok_lista) > 2 else ""
        tetelek.append({
            "bid": bid_normalizal(targya) or bid_normalizal(szoveg),
            "targya": targya,
            "beszallito_lista": sorok_lista[3] if len(sorok_lista) > 3 else "",
            "azonosito": m.group(1),
        })
    return tetelek


SOR_KIOLVASO = """
() => {
    const out = [];
    document.querySelectorAll('table.table-softservice tbody tr').forEach(tr => {
        const link = tr.querySelector('td.left.bold a');
        if (!link) return;
        out.push({ id: link.innerText.trim(), text: tr.innerText });
    });
    return out;
}
"""


async def _lista_sorok(page, url, timeout=25000):
    """Egy Innonest listaoldal sorai. Nem dob kivételt: hiba esetén (None) tér vissza.

    A dashboard _scrape_rows függvénye networkidle-t vár — ez az oldalon néha sosem
    következik be, és időtúllépéssel megöli a futást. Itt domcontentloaded után
    megvárjuk, hogy a táblázat megjelenjen."""
    if not await _acq_betolt(page, url, timeout=timeout):
        return None
    try:
        await page.wait_for_selector("table.table-softservice tbody tr", timeout=10000)
    except Exception:  # noqa: BLE001 — üres lista is lehet, nem hiba
        pass
    try:
        return await page.evaluate(SOR_KIOLVASO)
    except Exception as e:  # noqa: BLE001
        log.warning("[HASZON] Sorok kiolvasása sikertelen (%s): %s", url, str(e)[:120])
        return None


async def _acq_betolt(page, url, timeout=20000):
    """Oldalbetöltés, ami nem dönti el a futást. True, ha sikerült."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(800)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("[HASZON] Beszerzési oldal nem tölthető (%s): %s", url, str(e)[:120])
        return False


async def _acq_lista(page, get_sorok, max_oldal=30):
    """A beszerzési lista összes sora, oldalanként 100-asával.
    Ha egy oldal nem tölthető be, a addig összegyűjtött sorokkal tér vissza,
    figyelmeztetéssel — a frissítés ettől nem áll meg."""
    lista, latott = [], set()
    for oldal in range(max_oldal):
        url = ACQ_OLDAL.format(o=oldal * LISTA_OLDALMERET)
        sorok = await get_sorok(page, url)
        if sorok is None:
            gond = f"a beszerzési lista {oldal + 1}. oldala nem tölthető be ({url})"
            if oldal == 0:
                return [], gond
            return lista, gond + f" — csak {len(lista)} sort látunk, az anyagköltség hiányos lehet"
        ujak = [x for x in sorok if x.get("azonosito") and x["azonosito"] not in latott]
        latott.update(x["azonosito"] for x in ujak)
        lista.extend(ujak)
        if len(sorok) < LISTA_OLDALMERET or not ujak:
            break
    else:
        return lista, f"a beszerzési lista {max_oldal} oldalnál sem ért véget — lehet, hogy van még"
    return lista, ""


def kimeno_bid_kinyeres(innonest_szamla_sorok):
    """
    Innonest /invoices nyers sorok [{id: számlaszám, text: teljes sor}] → {számlaszám: BID}.
    Egy sorban több hivatkozás is lehet (BID + megrendelésszám); a BID-est vesszük.
    """
    ki = {}
    for r in innonest_szamla_sorok:
        szam = (r.get("id") or "").strip()
        if not szam:
            continue
        bidek = set()
        for m in INVOICE_REF_RE.finditer(r.get("text", "")):
            if m.group(1):
                bidek.add("BID-" + m.group(2))
        if len(bidek) == 1:
            ki[szam] = bidek.pop()
        elif len(bidek) > 1:
            ki[szam] = sorted(bidek)[0]
            log.warning("[HASZON] %s több BID-re hivatkozik: %s — az elsőt vesszük", szam, sorted(bidek))
    return ki


# ---------------------------------------------------------------- 2. bejövő → BID

def bejovo_bid_hozzarendeles(rekord, tetelek, beszerzesek, naplo_index, kiv_index=None, gmail=None):
    """
    Egy bejövő számla → (bid, módszer). beszerzesek: [{bid, beszallito, netto}] az Innonest-ből,
    naplo_index: {számlaszám: bid} az ellenőrző naplójából.
    kiv_index: {megrendelőlap-szám: bid} az Innonest beszerzési listájából.
    """
    kiv_index = kiv_index or {}
    # a) szöveges BID a számlán
    szovegek = [rekord.get("megjegyzes", ""), rekord.get("rendelesszam", "")]
    szovegek += [t.get("megnevezes", "") + " " + t.get("megjegyzes", "") for t in tetelek]
    for sz in szovegek:
        b = bid_normalizal(sz)
        if b:
            return b, "számla szövege"

    # b) Innonest beszerzési megrendelőlap: beszállító + nettó ±2%
    partner = (rekord.get("partner_nev") or "").lower()
    netto = _num(rekord.get("netto_huf"))
    if netto > 0:
        jeloltek = []
        for b in beszerzesek:
            if not b.get("bid") or not b.get("netto"):
                continue
            besz = (b.get("beszallito") or "").lower()
            kulcsszo = besz.split()[0] if besz else ""
            if kulcsszo and kulcsszo in partner and abs(_num(b["netto"]) - netto) <= 0.02 * netto:
                jeloltek.append(b["bid"])
        if len(set(jeloltek)) == 1:
            return jeloltek[0], "Innonest megrendelőlap"

    # b2) megrendelőlap-szám a számlán (pl. "KIV #2026-92 számú megrendelő alapján")
    for sz in szovegek:
        k = KIV_RE.search(sz or "")
        if k:
            b = kiv_index.get(k.group(1))
            if b:
                return b, "megrendelőlap-szám"

    # c) számlaellenőrző napló
    b = naplo_index.get((rekord.get("szamlaszam") or "").strip())
    if b:
        return b, "ellenőrző napló"

    # d) Gmail: a számla PDF-je vagy linkje (bid_kereso.py; az eredmény a Bejövő BID lapon marad)
    g = (gmail or {}).get((rekord.get("szamlaszam") or "").strip())
    if g and g[0]:
        return g[0], "Gmail " + ("PDF" if "PDF" in (g[1] or "") else "link")

    return "", ""


def naplo_index_epites(naplo_sorok):
    """Az ellenőrző napló lapja → {számlaszám: BID}. A fejlécet név alapján keresi, hogy oszlopsorrend-független legyen."""
    d = _sorok_dict(naplo_sorok)
    if not d:
        return {}
    kulcsok = list(d[0].keys())
    szam_k = next((k for k in kulcsok if "számlaszám" in k.lower() or "szamlaszam" in k.lower()), None)
    bid_k = next((k for k in kulcsok if k.lower().strip() in ("bid", "bid szám", "bid_szam") or k.lower().startswith("bid")), None)
    if not szam_k or not bid_k:
        log.warning("[HASZON] Ellenőrző napló: nem találom a számlaszám/BID oszlopot (fejléc: %s)", kulcsok)
        return {}
    ki = {}
    for s in d:
        b = bid_normalizal(s.get(bid_k, ""))
        szam = str(s.get(szam_k, "")).strip()
        if b and szam:
            ki[szam] = b
    return ki


# ---------------------------------------------------------------- 4. kalkulált (webapp) — BEKÖTENDŐ

# A webapp Firestore-szerkezete (élőben feltérképezve, 2026-09-18):
#   projects/{id}: id, name, createdAt, savedAt, tasks, snap
#   snap.meta.bid      → "BID-2026-258"   (ez a kapocs a számlákhoz)
#   snap.meta.ceg      → ügyfél neve
#   snap.meta.nev      → projekt megnevezése
#   snap.ALV.lastAppliedDij → {"Roliék": 13807000, "Saját csapat": 7006748}  — alvállalkozói díjak
#   snap.ALV.dijKi     → a kiválasztott kivitelező neve ("Chemibau Kft")
#   snap.totals.*      → az összesített kalkulált értékek (a webappnak MENTENIE KELL, lásd lent)
#
# FONTOS: a snap a kalkulátor BEMENETEIT tárolja (mennyiségek, rétegrendek, egységárak),
# nem a kiszámolt végösszegeket. A kalkulált ANYAGKÖLTSÉG ezért nem olvasható ki közvetlenül —
# ahhoz a webappnak mentéskor ki kell írnia egy összesítőt (snap.totals). Amíg ez nincs meg,
# az alvállalkozói díj rendelkezésre áll, az anyagköltség nem.

SAJAT_CSAPAT_KULCS = "Saját csapat"


# A kalkulátorban használt nevek és a számlán szereplő cégnevek nem egyeznek
# ("Roliék" → "V-Clean&Services Kft"). A megfeleltetést a Beállítások lap tartalmazza,
# hogy Sheetből szerkeszthető legyen, deploy nélkül.
AKTIV_NEVMEGFELELTETES = None

NEVMEGFELELTETES_ALAP = {"roliék": "V-Clean"}


def _nev_kulcs(n):
    return " ".join(str(n or "").lower().split())


def szamlazo_nev(kalk_nev, megfeleltetes=None):
    """Kalkulátorbeli név → a számlán keresendő névrészlet."""
    if megfeleltetes is None:
        megfeleltetes = AKTIV_NEVMEGFELELTETES if AKTIV_NEVMEGFELELTETES is not None else NEVMEGFELELTETES_ALAP
    return megfeleltetes.get(_nev_kulcs(kalk_nev), kalk_nev or "")


def nev_egyezik(kalk_nev, partner_nev, megfeleltetes=None):
    """Illeszkedik-e a kalkulátorbeli név a számla partnernevére (Roliék → V-Clean)."""
    return ceg_egyezik(szamlazo_nev(kalk_nev, megfeleltetes), partner_nev)


def _totals_tetelek(totals, kulcs):
    """snap.totals.anyag / .alvallalkozo tömb → egységes [dict]. Tűri a régi,
    objektumos alakot is ({"STO": 1240000})."""
    nyers = (totals or {}).get(kulcs)
    ki = []
    if isinstance(nyers, list):
        for t in nyers:
            if not isinstance(t, dict):
                continue
            osszeg = t.get("osszeg")
            if not isinstance(osszeg, (int, float)) or isinstance(osszeg, bool):
                continue
            ki.append({"nev": (t.get("beszallito") or t.get("nev") or "").strip(),
                       "osszeg": float(osszeg),
                       "sajat": bool(t.get("sajat"))})
    elif isinstance(nyers, dict):
        for nev, osszeg in nyers.items():
            if isinstance(osszeg, (int, float)) and not isinstance(osszeg, bool):
                ki.append({"nev": str(nev).strip(), "osszeg": float(osszeg),
                           "sajat": str(nev).strip() == SAJAT_CSAPAT_KULCS})
    return ki


def _kalk_alvallalkozoi(alv):
    """Az ALKALMAZOTT alvállalkozói díj és hogy saját csapattal számoltunk-e.
    Visszaad: (díj vagy None, sajat_csapat bool, megjegyzés)."""
    dijak = (alv or {}).get("lastAppliedDij") or {}
    dijak = {k: v for k, v in dijak.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if not dijak:
        return None, False, "nincs alvállalkozói díj a kalkulációban"
    valasztott = (alv or {}).get("dijKi") or ""
    # 1) a kiválasztott kivitelező neve szerepel a díjak között
    for k in dijak:
        if valasztott and k.strip().lower() == valasztott.strip().lower():
            return dijak[k], k == SAJAT_CSAPAT_KULCS, ""
    # 2) csak egy díj van → az az alkalmazott
    if len(dijak) == 1:
        k = next(iter(dijak))
        return dijak[k], k == SAJAT_CSAPAT_KULCS, ""
    # 3) több díj, és a kiválasztott nincs köztük → nem tippelünk
    return None, SAJAT_CSAPAT_KULCS in dijak, \
        f"több alvállalkozói díj ({', '.join(dijak)}), a kiválasztott '{valasztott}' nincs köztük"


def kalkulalt_lekeres(bid):
    """A kalkuláció a webapp Firestore-jából, snap.meta.bid alapján.
    None, ha nincs ilyen projekt vagy nincs Firestore-hozzáférés."""
    if not bid or not os.environ.get("SZAMLAZZ_FIREBASE_PROJECT_ID"):
        return None
    try:
        fejlec = {"Authorization": "Bearer " + sk.access_token()}
        r = requests.post(_firestore_ut() + ":runQuery", headers=fejlec, timeout=30, json={
            "structuredQuery": {
                "from": [{"collectionId": "projects"}],
                "where": {"fieldFilter": {"field": {"fieldPath": "snap.meta.bid"},
                                          "op": "EQUAL", "value": {"stringValue": bid}}},
                "limit": 20}})
        if r.status_code >= 400:
            log.warning("[HASZON] Firestore kalkuláció-lekérdezés (%s) %s: %s",
                        bid, r.status_code, r.text[:300])
            return None
        dokok = [_fs_dok(x["document"]) for x in r.json() if x.get("document")]
        if not dokok:
            return None
        # rendezés itt, nem a lekérdezésben (lásd fent: összetett index kellene hozzá)
        dokok.sort(key=lambda x: str(x.get("savedAt") or x.get("createdAt") or ""), reverse=True)
        d = dokok[0]                      # a legutóbb mentett verzió
        snap = d.get("snap") or {}
        meta = snap.get("meta") or {}
        totals = snap.get("totals") or {}

        anyag_tetelek = _totals_tetelek(totals, "anyag")
        alv_tetelek = _totals_tetelek(totals, "alvallalkozo")

        anyag = sum(t["osszeg"] for t in anyag_tetelek) if anyag_tetelek else \
            totals.get("anyagOsszesen", totals.get("anyagKalk"))
        if alv_tetelek:
            munkadij = sum(t["osszeg"] for t in alv_tetelek)
            sajat = any(t["sajat"] for t in alv_tetelek)
            alv_megj = ""
        else:
            munkadij, sajat, alv_megj = _kalk_alvallalkozoi(snap.get("ALV"))
            munkadij = totals.get("alvOsszesen", totals.get("alvKalk", munkadij))
        bevetel = totals.get("ajanlatNetto", totals.get("bevetel"))

        hianyzo = []
        if anyag is None:
            hianyzo.append("kalkulált anyagköltség (a webapp nem mentette)")
        if munkadij is None:
            hianyzo.append(alv_megj or "kalkulált alvállalkozói díj")

        return {"bevetel": bevetel, "anyag": anyag, "munkadij": munkadij,
                "anyag_tetelek": anyag_tetelek, "alv_tetelek": alv_tetelek,
                "sajat_csapat": sajat, "ugyfel": meta.get("ceg") or "",
                "projekt_nev": meta.get("nev") or d.get("name") or "",
                "devizanem": totals.get("devizanem") or "HUF",
                "margin": totals.get("margin") or (snap.get("AJ") or {}).get("margin"),
                "szamitva": totals.get("szamitva"),
                "tobb_verzio": len(dokok) > 1, "hianyzo": hianyzo}
    except Exception as e:  # noqa: BLE001
        log.warning("[HASZON] kalkulált érték nem olvasható (%s): %s", bid, e)
        return None


# ---------------------------------------------------------------- 5. összeállítás (tiszta függvény — tesztelhető)

def _ft(x):
    """Ezres elválasztós szám — csak a számra, nem az egész mondatra."""
    return f"{x:,.0f}".replace(",", " ")


def _beszallitonkenti_elteres(kalk_tetelek, tenyleges):
    """kalk_tetelek: [{nev, osszeg, sajat}], tenyleges: {partner_nev: összeg}.
    Név alapján párosít (a Beállítások lap megfeleltetésével), és megmutatja,
    kinél mennyi a terv és a tény."""
    sorok, felhasznalt = [], set()
    for t in sorted(kalk_tetelek, key=lambda x: -x["osszeg"]):
        if t.get("sajat"):
            sorok.append(f"{t['nev']}: terv {_ft(t['osszeg'])} / tény — (saját csapat, nincs számla)")
            continue
        tny, kik = 0.0, []
        for partner, osszeg in tenyleges.items():
            if nev_egyezik(t["nev"], partner):
                tny += osszeg
                kik.append(partner)
                felhasznalt.add(partner)
        elteres = tny - t["osszeg"]
        pct = f" ({elteres / t['osszeg'] * 100:+.0f}%)" if t["osszeg"] else ""
        jelzes = "" if kik else " [nincs hozzá számla]"
        sorok.append(f"{t['nev']}: terv {_ft(t['osszeg'])} / tény {_ft(tny)}{pct}{jelzes}")
    for partner, osszeg in sorted(tenyleges.items(), key=lambda x: -x[1]):
        if partner not in felhasznalt:
            sorok.append(f"{partner}: terv — / tény {_ft(osszeg)} (nem volt kalkulálva)")
    return " | ".join(sorok)


def duplikatum_jelzes(hozzar):
    """Ugyanarra a BID-re, ugyanattól a partnertől, ugyanakkora összeggel több számla —
    tipikusan elrontott és újrakiállított számla, aminek a sztornója nem érkezett meg.
    A sztornó (negatív, azonos abszolút összegű) kioltja az egyik pozitívat."""
    csoport = defaultdict(list)
    for h in hozzar:
        if h["irany"] != "Bejövő" or not h["bid_ervenyes"]:
            continue
        kulcs = (h["bid_ervenyes"], frozenset(_tokenek(h["partner_nev"])), round(abs(h["netto_huf"])))
        csoport[kulcs].append(h)
    for (bid, _, osszeg), tagok in csoport.items():
        pozitiv = [t for t in tagok if t["netto_huf"] > 0]
        negativ = [t for t in tagok if t["netto_huf"] < 0]
        if len(pozitiv) - len(negativ) > 1:
            szamok = ", ".join(t["szamlaszam"] for t in pozitiv)
            for t in pozitiv:
                t["figyelmeztetes"] = (f"{len(pozitiv)} db azonos összegű számla ({szamok}) erre a BID-re "
                                       f"— hiányzó sztornó? A költség többszörösen számolódhat.")


def kimutatas_osszeallitas(szamlak, tetelek, kimeno_bid, beszerzesek, naplo_index, kezi,
                           kiv_index=None, gmail=None):
    """
    szamlak, tetelek: a Számlázz.hu-Sheet sorai dict-ként.
    kimeno_bid: {számlaszám: bid} az Innonest-ből. kezi: {számlaszám: (bid_kezi, kategoria_kezi)}.
    gmail: {számlaszám: (bid, állapot)} — a Gmailből korábban vagy most kiolvasott BID-ek.
    Visszaad: (projekt_sorok, hozzarendeles_sorok).

    Bejövő számla CSAK akkor kerül be, ha a partnere szerepel a Beállítások lap
    anyagbeszállító- vagy alvállalkozó-listáján — minden más (eMAG, Telekom, lízing) kimarad.
    """
    kiv_index = kiv_index or {}
    gmail = gmail or {}
    tetel_index = defaultdict(list)
    for t in tetelek:
        tetel_index[(t.get("irany"), str(t.get("szamlazz_id")))].append(t)

    hozzar = []
    for r in szamlak:
        if r.get("teszt") == "igen" or r.get("sztornozott") == "igen":
            continue
        tipus = r.get("tipus", "")
        if tipus in ("Díjbekérő", "Szállítólevél"):
            continue
        szam = str(r.get("szamlaszam", "")).strip()
        irany = r.get("irany")
        netto = _num(r.get("netto_huf"))
        if tipus == "Sztornó számla":
            netto = -abs(netto)

        bid_kezi, kat_kezi = kezi.get(szam, ("", ""))
        if irany == "Kimenő":
            bid_auto = kimeno_bid.get(szam, "")
            modszer = "Innonest számlalista" if bid_auto else ""
            kat_auto = "bevétel"
        else:
            kat_auto = partner_kategoria(r.get("partner_nev"))
            if not kat_auto and not kat_kezi:
                continue          # nem figyelt partner — nem anyag, nem alvállalkozó
            bid_auto, modszer = bejovo_bid_hozzarendeles(
                r, tetel_index[(irany, str(r.get("szamlazz_id")))], beszerzesek, naplo_index,
                kiv_index, gmail)

        bid_erv = bid_kezi or bid_auto
        kat_erv = kat_kezi or kat_auto
        hozzar.append({
            "szamlaszam": szam, "irany": irany, "tipus": tipus, "kelt": r.get("kelt", ""),
            "partner_nev": r.get("partner_nev", ""), "netto_huf": round(netto, 2),
            "bid_auto": bid_auto, "modszer": modszer, "kategoria_auto": kat_auto,
            "bid_kezi": bid_kezi, "kategoria_kezi": kat_kezi,
            "bid_ervenyes": bid_erv, "kategoria_ervenyes": kat_erv,
            "gmail_allapot": (gmail.get(szam) or ("", ""))[1] if irany == "Bejövő" else "",
            "figyelmeztetes": "",
        })

    duplikatum_jelzes(hozzar)

    # projektenkénti összegzés
    proj = defaultdict(lambda: {"vegszamlak": [], "elolegszamlak": [], "bevetel": 0.0, "anyag": 0.0,
                                "munkadij": 0.0, "ugyfel": "", "anyag_partner": {}, "alv_partner": {}})
    for h in hozzar:
        b = h["bid_ervenyes"]
        if not b:
            continue
        p = proj[b]
        if h["irany"] == "Kimenő":
            p["bevetel"] += h["netto_huf"]
            p["ugyfel"] = p["ugyfel"] or h["partner_nev"]
            (p["elolegszamlak"] if h["tipus"] == "Előlegszámla" else p["vegszamlak"]).append(h["szamlaszam"])
        elif h["kategoria_ervenyes"] in ("anyag", "alvállalkozó", "munkadíj"):
            mezo = "anyag" if h["kategoria_ervenyes"] == "anyag" else "munkadij"
            if h.get("figyelmeztetes"):
                p.setdefault("figyelmeztetesek", []).append(f"{h['szamlaszam']}: {h['figyelmeztetes']}")
            p[mezo] += h["netto_huf"]
            reszletek = p["anyag_partner" if mezo == "anyag" else "alv_partner"]
            nev = h["partner_nev"] or "(névtelen)"
            reszletek[nev] = reszletek.get(nev, 0.0) + h["netto_huf"]

    most = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    projektek = []
    for b in sorted(proj, key=lambda x: [int(n) for n in re.findall(r"\d+", x)]):
        p = proj[b]
        haszon = p["bevetel"] - p["anyag"] - p["munkadij"]
        kalk = kalkulalt_lekeres(b)
        sor = {
            "bid": b, "ugyfel": p["ugyfel"],
            "vegszamlak": ", ".join(p["vegszamlak"]), "elolegszamlak": ", ".join(p["elolegszamlak"]),
            "bevetel_netto_huf": round(p["bevetel"]), "anyag_tenyleges": round(p["anyag"]),
            "munkadij_tenyleges": round(p["munkadij"]), "haszon_tenyleges": round(haszon),
            "kalk_bevetel": "", "kalk_anyag": "", "kalk_munkadij": "", "kalk_haszon": "",
            "elteres_ft": "", "elteres_pct": "", "sajat_csapat": "",
            "anyag_beszallitonkent": "", "alvallalkozonkent": "", "megjegyzes": "", "frissitve": most,
        }
        if not p["vegszamlak"]:
            sor["allapot"] = "csak előleg"
        elif kalk is None:
            sor["allapot"] = "nincs kalkuláció"
        else:
            sor["ugyfel"] = sor["ugyfel"] or kalk.get("ugyfel", "")
            sor["sajat_csapat"] = "saját csapat is volt kint" if kalk.get("sajat_csapat") else ""
            if kalk.get("anyag_tetelek"):
                sor["anyag_beszallitonkent"] = _beszallitonkenti_elteres(kalk["anyag_tetelek"], p["anyag_partner"])
            if kalk.get("alv_tetelek"):
                sor["alvallalkozonkent"] = _beszallitonkenti_elteres(kalk["alv_tetelek"], p["alv_partner"])
            for cel, forras_kulcs in (("kalk_bevetel", "bevetel"), ("kalk_anyag", "anyag"),
                                      ("kalk_munkadij", "munkadij")):
                if kalk.get(forras_kulcs) is not None:
                    sor[cel] = round(_num(kalk[forras_kulcs]))
            if kalk.get("hianyzo"):
                # Részleges kalkuláció: a haszon-összehasonlítás félrevezető lenne.
                sor["allapot"] = "részleges kalkuláció"
                sor["megjegyzes"] = "hiányzik: " + "; ".join(kalk["hianyzo"])
            else:
                kh = _num(kalk["bevetel"]) - _num(kalk["anyag"]) - _num(kalk["munkadij"])
                sor.update({"kalk_haszon": round(kh), "elteres_ft": round(haszon - kh),
                            "elteres_pct": round((haszon - kh) / kh * 100, 1) if kh else "",
                            "allapot": "kiértékelt"})
            if kalk.get("tobb_verzio"):
                sor["megjegyzes"] = (sor["megjegyzes"] + " | " if sor["megjegyzes"] else "") + \
                    "több mentett kalkuláció, a legutóbbit vettem"
        if p["anyag"] == 0 and p["munkadij"] == 0 and p["vegszamlak"]:
            sor["megjegyzes"] = (sor["megjegyzes"] + " | " if sor["megjegyzes"] else "") + \
                "nincs hozzárendelt költség"
        if p.get("figyelmeztetesek"):
            sor["megjegyzes"] = (sor["megjegyzes"] + " | " if sor["megjegyzes"] else "") + \
                "FIGYELEM — " + "; ".join(sorted(set(p["figyelmeztetesek"])))
        projektek.append(sor)

    return projektek, hozzar


# ---------------------------------------------------------------- Innonest scrape (egy böngésző-munkamenet)

async def _innonest_gyujtes_async(kezdo_bidek):
    """Egy böngésző-menetben: kimenő számlalista → BID-ek, majd beszerzési megrendelőlapok
    (modált csak azokhoz a BID-ekhez nyitunk, amelyek a kimenő számlákon vagy a kézi listán szerepelnek)."""
    from playwright.async_api import async_playwright
    from innonest_core import login, load_session, make_browser_args
    from innonest_szamlalo import _invoices_page_url, DATE_PATTERN
    from szamla_ellenorzo import _nyisd_meg_reszletek

    ev = datetime.now().year
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        # -- kimenő számlák
        szamla_sorok, offset, szamla_gond = [], 0, ""
        for _ in range(50):
            sorok = await _lista_sorok(page, _invoices_page_url(offset))
            if sorok is not None and "login" in page.url:
                await login(page)
                sorok = await _lista_sorok(page, _invoices_page_url(offset))
            if sorok is None:
                szamla_gond = f"a kimenő számlalista {offset // LISTA_OLDALMERET + 1}. oldala nem olvasható"
                break
            if not sorok:
                break
            elozo_ev = False
            for r in sorok:
                d = DATE_PATTERN.search(r["text"])
                if d and int(d.group(1)[:4]) != ev:
                    elozo_ev = True
                    break
                szamla_sorok.append(r)
            if elozo_ev or len(sorok) < LISTA_OLDALMERET:
                break
            offset += LISTA_OLDALMERET

        erdekes_bidek = set(kezdo_bidek) | set(kimeno_bid_kinyeres(szamla_sorok).values())

        # -- beszerzési megrendelőlapok
        async def beszerzesi_oldal(p, url):
            if not await _acq_betolt(p, url):
                return None
            try:
                return await beszerzesi_sorok_az_oldalrol(p)
            except Exception as e:  # noqa: BLE001
                log.warning("[HASZON] Beszerzési sorok olvasása sikertelen: %s", str(e)[:120])
                return None

        lista, lapozas_gond = await _acq_lista(page, beszerzesi_oldal)
        if lapozas_gond:
            log.warning("[HASZON] %s", lapozas_gond)
        if szamla_gond:
            lapozas_gond = (lapozas_gond + " | " if lapozas_gond else "") + szamla_gond
        log.info("[HASZON] Beszerzési megrendelőlapok: %d sor", len(lista))

        beszerzesek = []
        for s in lista:
            if not s.get("bid") or s["bid"] not in erdekes_bidek:
                continue
            kiv_m = KIV_RE.search(s.get("targya", "") or "")
            s["kiv"] = kiv_m.group(1) if kiv_m else ""
            reszlet = await _nyisd_meg_reszletek(page, s["azonosito"])
            szoveg = (reszlet or {}).get("netto_szoveg", "")
            m = re.search(r"Nett[oó][^\d]{0,20}([\d\s]{3,})\s*(?:Ft|HUF)", szoveg, re.IGNORECASE) or \
                re.search(r"([\d][\d\s]{2,})\s*(?:Ft|HUF)", szoveg, re.IGNORECASE)
            beszerzesek.append({
                "bid": s["bid"],
                "kiv": s.get("kiv", ""),
                "beszallito": (reszlet or {}).get("beszallito") or s.get("beszallito_lista", ""),
                "netto": int(m.group(1).replace(" ", "")) if m else 0,
            })
        await browser.close()

    log.info("[HASZON] Innonest: %d számlasor, %d beszerzés az érdekes BID-ekhez",
             len(szamla_sorok), len(beszerzesek))
    return szamla_sorok, beszerzesek, lapozas_gond


# ---------------------------------------------------------------- Sheet I/O

def _cel_id():
    """A kimutatás célja. Alapértelmezésben UGYANAZ a munkafüzet, ahova a Számlázz.hu
    adatkapcsolat ír — így minden egy helyen van. A Projektek / Hozzárendelések /
    Beállítások lap nem ütközik a Számlák / Tételek / Napló lappal.
    SZAMLAZZ_HASZON_SHEET_ID csak akkor kell, ha külön munkafüzetbe akarod tenni."""
    sid = os.environ.get("SZAMLAZZ_HASZON_SHEET_ID") or os.environ.get("SZAMLAZZ_SHEET_ID")
    if not sid:
        raise RuntimeError("Sem SZAMLAZZ_HASZON_SHEET_ID, sem SZAMLAZZ_SHEET_ID nincs beállítva")
    return sid


def beallitasok_olvasas():
    """A Beállítások lap: anyagbeszállítók, alvállalkozók, névmegfeleltetés.

    Sorok:  A="Anyagbeszállító",  B=cégnév (ahogy a számlán szerepel)
            A="Alvállalkozó",     B=cégnév
            A="Névmegfeleltetés", B=kalkulátorbeli név, C=a számlán szereplő név
    Ha a lap nincs meg, létrehozza; ha nincs rajta Alvállalkozó-sor, hozzáfűzi az
    alapértelmezett listát, hogy legyen mit szerkeszteni."""
    cel = _cel_id()
    if BEALL_LAP not in sk.lapok(cel):
        sk.lap_letrehozas(BEALL_LAP, ["beallitas", "ertek", "leiras"], sheet_id=cel)
        sk.ir(f"'{BEALL_LAP}'!A2", [["", "", "Anyagbeszállító / Alvállalkozó: B = a cégnév, ahogy a számlán "
                                              "szerepel. Csak ezek bejövő számlái kerülnek a kimutatásba."]],
              sheet_id=cel)
    sorok = sk.olvas(f"'{BEALL_LAP}'!A2:C", cel)

    def gyujt(elotag):
        return [str(r[1]).strip() for r in sorok
                if len(r) >= 2 and str(r[0]).strip().lower().startswith(elotag) and str(r[1]).strip()]

    anyag, alv = gyujt("anyagbeszáll"), gyujt("alvállalkoz")
    megf = {_nev_kulcs(r[1]): str(r[2]).strip() for r in sorok
            if len(r) >= 3 and str(r[0]).strip().lower().startswith("névmegfeleltet")
            and str(r[1]).strip() and str(r[2]).strip()}

    potlas = []
    if not anyag:
        potlas += [["Anyagbeszállító", n, ""] for n in ANYAG_ALAP]
        anyag = list(ANYAG_ALAP)
    if not alv:
        potlas += [["Alvállalkozó", n, "alapértelmezés — ellenőrizd, egészítsd ki"] for n in ALV_ALAP]
        alv = list(ALV_ALAP)
    if not megf:
        potlas += [["Névmegfeleltetés", k, v] for k, v in NEVMEGFELELTETES_ALAP.items()]
        megf = dict(NEVMEGFELELTETES_ALAP)
    if potlas:
        sk.hozzafuz(f"'{BEALL_LAP}'!A1", potlas, sheet_id=cel)
        log.info("[HASZON] Beállítások lap kiegészítve %d alapértelmezett sorral", len(potlas))
    return anyag, alv, megf


def _lap_sorai(nev):
    cel = _cel_id()
    if nev not in sk.lapok(cel):
        return []
    return _sorok_dict(sk.olvas(f"'{nev}'!A1:Z", cel))


def korabbi_allapot_olvasas():
    """A két BID-lap korábbi tartalma: a kézi javítások és a Gmail-keresések eredménye.
    Ezek túlélik a frissítést — a Gmailt nem kell újra végigkeresni."""
    kezi, gmail = {}, {}
    for sor in _lap_sorai(KIMENO_LAP) + _lap_sorai(BEJOVO_LAP):
        szam = str(sor.get("szamlaszam", "")).strip()
        if not szam:
            continue
        b = bid_normalizal(sor.get("bid_kezi", ""))
        if b:
            kezi[szam] = (b, "")
        allapot = str(sor.get("gmail_allapot", "")).strip()
        # a "hiba" állapotúakat újrapróbáljuk, a többit nem
        if allapot and not allapot.startswith("hiba"):
            gmail[szam] = (bid_normalizal(sor.get("bid_auto", "")) if "talált" in allapot else "", allapot)
    # a régi Hozzárendelések lap kézi BID-jei is éljenek tovább, ha még nem írtad át
    for sor in _lap_sorai(HOZZAR_LAP):
        szam = str(sor.get("szamlaszam", "")).strip()
        b = bid_normalizal(sor.get("bid_kezi", ""))
        if szam and b and szam not in kezi:
            kezi[szam] = (b, str(sor.get("kategoria_kezi", "")).strip())
    return kezi, gmail


VEDETT_LAPOK = {"Számlák", "Tételek", "Napló"}


def _lapot_ir(nev, oszlopok, sorok):
    cel = _cel_id()
    if nev in VEDETT_LAPOK:
        raise RuntimeError(f"A(z) {nev} lapot a Számlázz.hu adatkapcsolat használja — nem írom felül")
    if nev not in sk.lapok(cel):
        sk.lap_letrehozas(nev, oszlopok, sheet_id=cel)
    sk.torol(f"'{nev}'!A2:Z", cel)
    if sorok:
        sk.ir(f"'{nev}'!A2", [[r.get(k, "") for k in oszlopok] for r in sorok], sheet_id=cel)


def bid_lapok(hozzar):
    """A belső hozzárendelés-sorokból a két lap sorai."""
    kimeno = [{"szamlaszam": h["szamlaszam"], "kelt": h["kelt"], "ugyfel": h["partner_nev"],
               "tipus": h["tipus"], "netto_huf": h["netto_huf"], "bid_auto": h["bid_auto"],
               "forras": h["modszer"], "bid_kezi": h["bid_kezi"], "bid": h["bid_ervenyes"]}
              for h in hozzar if h["irany"] == "Kimenő"]
    bejovo = [{"szamlaszam": h["szamlaszam"], "kelt": h["kelt"], "partner": h["partner_nev"],
               "kategoria": h["kategoria_ervenyes"], "netto_huf": h["netto_huf"],
               "bid_auto": h["bid_auto"], "forras": h["modszer"],
               "gmail_allapot": h.get("gmail_allapot", ""), "bid_kezi": h["bid_kezi"],
               "bid": h["bid_ervenyes"], "figyelmeztetes": h.get("figyelmeztetes", "")}
              for h in hozzar if h["irany"] == "Bejövő"]
    kimeno.sort(key=lambda x: str(x["kelt"]), reverse=True)
    bejovo.sort(key=lambda x: str(x["kelt"]), reverse=True)
    return kimeno, bejovo


def gmail_jeloltek(hozzar, gmail_cache):
    """Azok a bejövő számlák, amikhez a Gmailben kell keresni: nincs BID-jük semmilyen
    más forrásból, nincs kézi BID, és még nem kerestük őket (vagy hibára futottak).
    A legfrissebbek elöl — így a folyamatban lévő projektek előbb teljesednek ki."""
    ki = [h for h in hozzar
          if h["irany"] == "Bejövő" and not h["bid_ervenyes"] and h["szamlaszam"] not in gmail_cache]
    ki.sort(key=lambda h: str(h["kelt"]), reverse=True)
    return [h["szamlaszam"] for h in ki]


def frissites():
    """Teljes futás. Visszaad egy összegzést a végpontnak."""
    from innonest_core import run_in_loop
    import bid_kereso as bk

    global AKTIV_ANYAG_LISTA, AKTIV_ALV_LISTA, AKTIV_NEVMEGFELELTETES
    forras = os.environ.get("SZAMLAZZ_SHEET_ID")
    szamlak = _sorok_dict(sk.olvas("'Számlák'!A1:AG", forras))
    tetelek = _sorok_dict(sk.olvas("'Tételek'!A1:P", forras))
    AKTIV_ANYAG_LISTA, AKTIV_ALV_LISTA, AKTIV_NEVMEGFELELTETES = beallitasok_olvasas()
    kezi, gmail_cache = korabbi_allapot_olvasas()
    log.info("[HASZON] Anyagbeszállítók: %d, alvállalkozók: %d, kézi BID: %d, Gmail-gyorsítótár: %d",
             len(AKTIV_ANYAG_LISTA), len(AKTIV_ALV_LISTA), len(kezi), len(gmail_cache))

    naplo_index = {}
    try:
        naplo_id = os.environ.get("SZAMLAZZ_ELLENORZO_NAPLO_ID", NAPLO_ALAP_ID)
        elso_lap = next(iter(sk.lapok(naplo_id)))
        naplo_index = naplo_index_epites(sk.olvas(f"'{elso_lap}'!A1:Z", naplo_id))
    except Exception as e:  # noqa: BLE001 — a napló opcionális
        log.warning("[HASZON] Ellenőrző napló nem olvasható: %s", e)

    kezdo_bidek = {b for b, _ in kezi.values() if b}
    for r in szamlak:
        b = bid_normalizal(r.get("megjegyzes", "") + " " + r.get("rendelesszam", ""))
        if b:
            kezdo_bidek.add(b)
    szamla_sorok, beszerzesek, figyelmeztetes = run_in_loop(_innonest_gyujtes_async(kezdo_bidek))
    kimeno_bid = kimeno_bid_kinyeres(szamla_sorok)
    kiv_index = {b["kiv"]: b["bid"] for b in beszerzesek if b.get("kiv") and b.get("bid")}

    # 1. kör: minden forrás a Gmail nélkül → kiderül, hol hiányzik még a BID
    _, hozzar = kimutatas_osszeallitas(szamlak, tetelek, kimeno_bid, beszerzesek, naplo_index,
                                       kezi, kiv_index, gmail_cache)
    jeloltek = gmail_jeloltek(hozzar, gmail_cache)

    # 2. Gmail-keresés a hiányzókra (korlátozott számban; a többi a következő futásra marad)
    gmail = dict(gmail_cache)
    gmail_uj = {}
    if jeloltek:
        try:
            gmail_uj = run_in_loop(bk.bid_kereses_async(jeloltek, max_db=GMAIL_MAX_PER_FUTAS))
        except Exception as e:  # noqa: BLE001
            log.warning("[HASZON] Gmail-keresés nem sikerült: %s", e)
            figyelmeztetes = (figyelmeztetes + " | " if figyelmeztetes else "") + f"Gmail-keresés hiba: {e}"
        for szam, e in gmail_uj.items():
            gmail[szam] = (e["bid"], e["allapot"])

    # 3. kör: végleges összeállítás
    projektek, hozzar = kimutatas_osszeallitas(szamlak, tetelek, kimeno_bid, beszerzesek, naplo_index,
                                               kezi, kiv_index, gmail)
    kimeno_sorok, bejovo_sorok = bid_lapok(hozzar)
    _lapot_ir(KIMENO_LAP, KIMENO_OSZLOPOK, kimeno_sorok)
    _lapot_ir(BEJOVO_LAP, BEJOVO_OSZLOPOK, bejovo_sorok)
    _lapot_ir(PROJEKT_LAP, PROJEKT_OSZLOPOK, projektek)

    hatravan = max(0, len(jeloltek) - GMAIL_MAX_PER_FUTAS)
    return {
        "ok": True,
        "kimeno_szamlak": len(kimeno_sorok),
        "kimeno_bid_nelkul": sum(1 for r in kimeno_sorok if not r["bid"]),
        "bejovo_szamlak": len(bejovo_sorok),
        "bejovo_bid_nelkul": sum(1 for r in bejovo_sorok if not r["bid"]),
        "gmail_most_keresve": len(gmail_uj),
        "gmail_most_talalt": sum(1 for e in gmail_uj.values() if e["bid"]),
        "gmail_hatravan": hatravan,
        "duplikatum_gyanu": sum(1 for r in bejovo_sorok if r["figyelmeztetes"]),
        "projektek": len(projektek),
        "allapotok": {a: sum(1 for p in projektek if p["allapot"] == a)
                      for a in sorted({p["allapot"] for p in projektek})},
        "anyagszallitok": AKTIV_ANYAG_LISTA,
        "alvallalkozok": AKTIV_ALV_LISTA,
        "figyelmeztetes": (figyelmeztetes or "") +
                          (f" | Még {hatravan} számla vár Gmail-keresésre — futtasd újra a frissítést."
                           if hatravan else ""),
    }



# ---------------------------------------------------------------- DIAGNOSZTIKA

async def _diag_innonest_async(bid):
    """Nyersen visszaadja, mit lát az Innonest az adott BID-hez."""
    from playwright.async_api import async_playwright
    from innonest_core import login, load_session, make_browser_args
    from innonest_szamlalo import _invoices_page_url
    from szamla_ellenorzo import _nyisd_meg_reszletek

    ki = {"kimeno_szamlasorok": [], "beszerzesek": []}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        offset = 0
        for _ in range(50):
            sorok = await _lista_sorok(page, _invoices_page_url(offset))
            if sorok is not None and "login" in page.url:
                await login(page)
                sorok = await _lista_sorok(page, _invoices_page_url(offset))
            if not sorok:
                break
            for r in sorok:
                if bid.replace("BID-", "") in r.get("text", ""):
                    ki["kimeno_szamlasorok"].append({"id": r.get("id"), "text": r.get("text", "")[:400]})
            if len(sorok) < LISTA_OLDALMERET:
                break
            offset += LISTA_OLDALMERET

        async def beszerzesi_oldal(p, url):
            if not await _acq_betolt(p, url):
                return None
            try:
                return await beszerzesi_sorok_az_oldalrol(p)
            except Exception as e:  # noqa: BLE001
                log.warning("[HASZON] Beszerzési sorok olvasása sikertelen: %s", str(e)[:120])
                return None

        lista, lapozas_gond = await _acq_lista(page, beszerzesi_oldal)
        ki["lapozas_gond"] = lapozas_gond or ""
        ki["acquisition_osszes_sor"] = len(lista)
        ki["acquisition_bid_lista"] = sorted({x["bid"] for x in lista if x.get("bid")})[:200]
        ki["acquisition_bid_talalatok"] = sum(1 for s in lista if s.get("bid"))
        for s in lista:
            if s.get("bid") != bid:
                continue
            reszlet = await _nyisd_meg_reszletek(page, s["azonosito"]) or {}
            szoveg = reszlet.get("netto_szoveg", "")
            m = re.search(r"Nett[oó][^\d]{0,20}([\d\s]{3,})\s*(?:Ft|HUF)", szoveg, re.IGNORECASE) or \
                re.search(r"([\d][\d\s]{2,})\s*(?:Ft|HUF)", szoveg, re.IGNORECASE)
            ki["beszerzesek"].append({
                "azonosito": s["azonosito"],
                "targya": s.get("targya", "")[:200],
                "beszallito_lista": s.get("beszallito_lista", ""),
                "modal_beszallito": reszlet.get("beszallito", ""),
                "kiolvasott_netto": int(m.group(1).replace(" ", "")) if m else None,
                "modal_szoveg_eleje": szoveg[:1200],
                "modal_szoveg_hossz": len(szoveg),
                "osszes_szam_a_modalban": re.findall(r"[\d][\d\s]{2,}\s*(?:Ft|HUF)", szoveg)[:20],
            })
        await browser.close()
    return ki


FIRESTORE_API = "https://firestore.googleapis.com/v1"


def _firestore_ut(utvonal=""):
    pid = os.environ.get("SZAMLAZZ_FIREBASE_PROJECT_ID")
    if not pid:
        raise RuntimeError("SZAMLAZZ_FIREBASE_PROJECT_ID nincs beállítva")
    return f"{FIRESTORE_API}/projects/{pid}/databases/(default)/documents{utvonal}"


def _fs_ertek(v):
    """Firestore REST érték → sima Python érték."""
    if not isinstance(v, dict):
        return v
    for k, ki in (("stringValue", str), ("integerValue", int), ("doubleValue", float),
                  ("booleanValue", bool), ("timestampValue", str)):
        if k in v:
            try:
                return ki(v[k])
            except (TypeError, ValueError):
                return v[k]
    if "nullValue" in v:
        return None
    if "arrayValue" in v:
        return [_fs_ertek(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: _fs_ertek(x) for k, x in v["mapValue"].get("fields", {}).items()}
    return v


def _fs_dok(d):
    return {k: _fs_ertek(v) for k, v in (d.get("fields") or {}).items()}


def _fs_kereses(obj, keresett=("bid",), ut=""):
    """Rekurzívan megkeresi, HOL van a kulcs a dokumentumban. [(útvonal, érték)]"""
    talalt = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            uj = f"{ut}.{k}" if ut else k
            if any(x.lower() == k.lower() for x in keresett):
                talalt.append((uj, v if not isinstance(v, (dict, list)) else str(v)[:300]))
            talalt.extend(_fs_kereses(v, keresett, uj))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:20]):
            talalt.extend(_fs_kereses(v, keresett, f"{ut}[{i}]"))
    return talalt


def _fs_szerkezet(obj, ut="", melyseg=0, max_melyseg=4):
    """A dokumentum szerkezete: útvonal → típus és rövid értékminta."""
    ki = {}
    if melyseg > max_melyseg:
        return {ut: "…(mélyebb szint)"}
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:60]:
            uj = f"{ut}.{k}" if ut else k
            if isinstance(v, (dict, list)):
                ki.update(_fs_szerkezet(v, uj, melyseg + 1, max_melyseg))
            else:
                ki[uj] = v if not isinstance(v, str) else v[:150]
    elif isinstance(obj, list):
        ki[f"{ut}[]"] = f"{len(obj)} elem"
        if obj:
            ki.update(_fs_szerkezet(obj[0], f"{ut}[0]", melyseg + 1, max_melyseg))
    else:
        ki[ut] = obj
    return ki


def _diag_projekt(projekt_id):
    """Egy konkrét kalkulátor-projekt TELJES tartalma — ebből tudjuk meg, hol
    vannak a kalkulált összegek és a saját csapat jelölés."""
    ki = {"projekt_id": projekt_id}
    try:
        fejlec = {"Authorization": "Bearer " + sk.access_token()}
        r = requests.get(_firestore_ut(f"/projects/{projekt_id}"), headers=fejlec, timeout=30)
        if r.status_code >= 400:
            ki["hiba"] = f"Firestore {r.status_code}: {r.text[:300]}"
            return ki
        adat = _fs_dok(r.json())
        ki["felso_szintu_mezok"] = sorted(adat.keys())
        ki["name"] = adat.get("name")
        ki["bid_talalatok"] = [{"utvonal": u, "ertek": v} for u, v in
                               _fs_kereses(adat, ("bid", "bidSzam", "bid_szam"))]
        ki["meta"] = (adat.get("snap") or {}).get("meta")
        ki["snap_szerkezet"] = _fs_szerkezet(adat.get("snap"), "snap")
        # a pénzügyileg érdekes mezők: aminek a neve összegre utal
        ki["osszegre_utalo_mezok"] = {u: v for u, v in ki["snap_szerkezet"].items()
                                      if isinstance(v, (int, float)) and not isinstance(v, bool)
                                      and abs(v or 0) >= 1000}
        ki["logikai_mezok"] = {u: v for u, v in ki["snap_szerkezet"].items() if isinstance(v, bool)}
    except Exception as e:  # noqa: BLE001
        ki["hiba"] = f"{type(e).__name__}: {e}"
    return ki


def _diag_firestore(bid):
    """Felderíti a webapp Firestore-tárolását: milyen collectionök vannak, és
    melyikben található az adott BID — mezőnevekkel együtt."""
    ki = {"elerheto": False}
    try:
        fejlec = {"Authorization": "Bearer " + sk.access_token()}
    except Exception as e:  # noqa: BLE001
        ki["hiba"] = f"OAuth token nem szerezhető: {e}"
        return ki

    try:
        # gyökér-collectionök listázása
        r = requests.post(_firestore_ut() + ":listCollectionIds", headers=fejlec,
                          json={"pageSize": 100}, timeout=30)
        if r.status_code == 403:
            ki["hiba"] = ("403 — a refresh tokenhez hiányzik a datastore jogosultság. "
                          "Futtasd újra az oauth_token_szerzo.py-t a bővített hatókörrel.")
            return ki
        if r.status_code >= 400:
            ki["hiba"] = f"Firestore {r.status_code}: {r.text[:300]}"
            return ki
        ki["elerheto"] = True
        collection_nevek = r.json().get("collectionIds", [])
        ki["collection_nevek"] = collection_nevek

        ki["collectionok"] = []
        for nev in collection_nevek[:25]:
            info = {"collection": nev, "minta_dokumentumok": [], "bid_talalat": None}
            m = requests.get(_firestore_ut(f"/{nev}"), headers=fejlec,
                             params={"pageSize": 3}, timeout=30)
            if m.status_code < 400:
                for d in m.json().get("documents", []):
                    adat = _fs_dok(d)
                    info["minta_dokumentumok"].append({
                        "id": d.get("name", "").rsplit("/", 1)[-1],
                        "mezok": sorted(adat.keys()),
                        "ertek_minta": {k: str(v)[:120] for k, v in list(adat.items())[:15]},
                    })
            # BID mint dokumentum-azonosító
            d = requests.get(_firestore_ut(f"/{nev}/{bid}"), headers=fejlec, timeout=30)
            if d.status_code < 400:
                adat = _fs_dok(d.json())
                info["bid_talalat"] = {"hol": "dokumentum-azonosító", "mezok": sorted(adat.keys()),
                                       "ertekek": {k: str(v)[:200] for k, v in adat.items()}}
            else:
                for mezo in ("snap.meta.bid", "meta.bid", "bid", "BID", "bidSzam",
                             "bid_szam", "projektBid", "azonosito", "projectId"):
                    q = requests.post(_firestore_ut() + ":runQuery", headers=fejlec, timeout=30, json={
                        "structuredQuery": {
                            "from": [{"collectionId": nev}],
                            "where": {"fieldFilter": {"field": {"fieldPath": mezo}, "op": "EQUAL",
                                                      "value": {"stringValue": bid}}},
                            "limit": 1}})
                    if q.status_code >= 400:
                        continue
                    tal = [x["document"] for x in q.json() if x.get("document")]
                    if tal:
                        adat = _fs_dok(tal[0])
                        info["bid_talalat"] = {"hol": f"mező: {mezo}",
                                               "doc_id": tal[0].get("name", "").rsplit("/", 1)[-1],
                                               "name": adat.get("name"),
                                               "meta": (adat.get("snap") or {}).get("meta"),
                                               "mezok": sorted(adat.keys())}
                        break
            ki["collectionok"].append(info)
    except Exception as e:  # noqa: BLE001
        ki["hiba"] = f"{type(e).__name__}: {e}"
    return ki


def _diag_szamlak(bid):
    """Mit mond a Sheet: mely számlák kapcsolódnak a BID-hez, és a BID nélküli
    bejövő számlák közül melyek jöhetnének szóba (szállító + összeg szerint)."""
    forras = os.environ.get("SZAMLAZZ_SHEET_ID")
    szamlak = _sorok_dict(sk.olvas("'Számlák'!A1:AG", forras))
    tetelek = _sorok_dict(sk.olvas("'Tételek'!A1:P", forras))
    tetel_index = defaultdict(list)
    for t in tetelek:
        tetel_index[(t.get("irany"), str(t.get("szamlazz_id")))].append(t)

    kimeno, bejovo_bides, bejovo_nelkul = [], [], []
    for r in szamlak:
        ts = tetel_index[(r.get("irany"), str(r.get("szamlazz_id")))]
        szovegek = " | ".join([r.get("megjegyzes", ""), r.get("rendelesszam", "")] +
                              [(t.get("megnevezes", "") + " " + t.get("megjegyzes", "")).strip() for t in ts])
        sor = {"szamlaszam": r.get("szamlaszam"), "partner": r.get("partner_nev"),
               "netto_huf": _num(r.get("netto_huf")), "kelt": r.get("kelt"),
               "tipus": r.get("tipus"), "tetelszovegek": szovegek[:300]}
        if r.get("irany") == "Kimenő":
            if bid_normalizal(szovegek) == bid:
                kimeno.append(sor)
        else:
            if bid_normalizal(szovegek) == bid:
                bejovo_bides.append(sor)
            elif _num(r.get("netto_huf")) > 50000 and r.get("tipus") == "Számla":
                bejovo_nelkul.append(sor)
    bejovo_nelkul.sort(key=lambda x: -x["netto_huf"])
    return {"kimeno_szoveges_bid_talalat": kimeno,
            "bejovo_szoveges_bid_talalat": bejovo_bides,
            "bid_nelkuli_nagy_bejovo_szamlak": bejovo_nelkul[:40],
            "megjegyzes": "A 'bid_nelkuli...' lista csak 50.000 Ft feletti bejövő SZÁMLÁKAT mutat, "
                          "csökkenő nettó szerint — ezek a jelöltek a kézi hozzárendeléshez."}


def _diag_kalkulacio(bid):
    """Mit ad a snap.meta.bid szerinti lekérdezés — nyersen, hibaüzenettel együtt."""
    ki = {"bid": bid}
    try:
        fejlec = {"Authorization": "Bearer " + sk.access_token()}
        r = requests.post(_firestore_ut() + ":runQuery", headers=fejlec, timeout=30, json={
            "structuredQuery": {
                "from": [{"collectionId": "projects"}],
                "where": {"fieldFilter": {"field": {"fieldPath": "snap.meta.bid"},
                                          "op": "EQUAL", "value": {"stringValue": bid}}},
                "limit": 20}})
        ki["http_statusz"] = r.status_code
        if r.status_code >= 400:
            ki["valasz"] = r.text[:600]
            return ki
        dokok = [_fs_dok(x["document"]) for x in r.json() if x.get("document")]
        ki["talalt_projektek"] = [{"name": d.get("name"), "savedAt": d.get("savedAt"),
                                   "van_totals": bool((d.get("snap") or {}).get("totals")),
                                   "totals_kulcsok": sorted(((d.get("snap") or {}).get("totals") or {}).keys())}
                                  for d in dokok]
        ki["feldolgozott"] = kalkulalt_lekeres(bid)
    except Exception as e:  # noqa: BLE001
        ki["hiba"] = f"{type(e).__name__}: {e}"
    return ki


def _diag_bidlista():
    """Végigolvassa a projects collectiont, és kigyűjti, milyen BID-ek szerepelnek
    a kalkulátorban. Ebből derül ki, hogy formátumkülönbség vagy hiányzó mentés
    okozza-e a párosítás hiányát."""
    ki = {"projektek": 0, "van_bid": 0, "nincs_bid": 0, "van_totals": 0}
    bidek, mintak_bid_nelkul = {}, []
    try:
        fejlec = {"Authorization": "Bearer " + sk.access_token()}
        token = None
        for _ in range(40):
            par = {"pageSize": 300}
            if token:
                par["pageToken"] = token
            r = requests.get(_firestore_ut("/projects"), headers=fejlec, params=par, timeout=40)
            if r.status_code >= 400:
                ki["hiba"] = f"Firestore {r.status_code}: {r.text[:300]}"
                break
            adat = r.json()
            for nyers in adat.get("documents", []):
                d = _fs_dok(nyers)
                snap = d.get("snap") or {}
                meta = snap.get("meta") or {}
                ki["projektek"] += 1
                if snap.get("totals"):
                    ki["van_totals"] += 1
                b = str(meta.get("bid") or "").strip()
                if b:
                    ki["van_bid"] += 1
                    bidek.setdefault(b, []).append(d.get("name") or "")
                else:
                    ki["nincs_bid"] += 1
                    if len(mintak_bid_nelkul) < 15:
                        mintak_bid_nelkul.append({"name": d.get("name"), "savedAt": d.get("savedAt")})
            token = adat.get("nextPageToken")
            if not token:
                break
    except Exception as e:  # noqa: BLE001
        ki["hiba"] = f"{type(e).__name__}: {e}"
    ki["bidek"] = sorted(bidek)
    ki["tobbszor_szereplo_bidek"] = {b: len(v) for b, v in sorted(bidek.items()) if len(v) > 1}
    ki["bid_nelkuli_minta"] = mintak_bid_nelkul
    return ki


def diagnosztika(bid, projekt_id=None):
    from innonest_core import run_in_loop
    ki = {"bid": bid, "sheet": {}, "innonest": {}, "firestore": {}}
    if projekt_id:
        # Csak a projekt-dump kell — ez gyors, nem indít böngészőt.
        return {"projekt": _diag_projekt(projekt_id)}
    try:
        ki["sheet"] = _diag_szamlak(bid)
    except Exception as e:  # noqa: BLE001
        ki["sheet"] = {"hiba": f"{type(e).__name__}: {e}"}
    try:
        ki["innonest"] = run_in_loop(_diag_innonest_async(bid))
    except Exception as e:  # noqa: BLE001
        ki["innonest"] = {"hiba": f"{type(e).__name__}: {e}"}
    ki["firestore"] = _diag_firestore(bid)
    ki["kalkulacio"] = _diag_kalkulacio(bid)
    return ki


# ---------------------------------------------------------------- Flask

def register_projekt_haszon_routes(app):
    @app.route("/projekt-haszon/frissit", methods=["GET", "POST"])
    def projekt_haszon_frissit():
        titok = os.environ.get("SZAMLAZZ_HASZON_SECRET", "")
        adott = request.args.get("secret") or (request.get_json(silent=True) or {}).get("secret")
        if not titok or adott != titok:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        try:
            return jsonify(frissites())
        except Exception as e:  # noqa: BLE001
            log.exception("[HASZON] frissítés hiba")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/projekt-haszon/diagnosztika", methods=["GET"])
    def projekt_haszon_diag():
        titok = os.environ.get("SZAMLAZZ_HASZON_SECRET", "")
        if not titok or request.args.get("secret") != titok:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if request.args.get("bidlista"):
            return jsonify({"ok": True, "bidlista": _diag_bidlista()})
        projekt_id = (request.args.get("projekt") or "").strip()
        if projekt_id:
            return jsonify({"ok": True, **diagnosztika("", projekt_id)})
        bid = bid_normalizal(request.args.get("bid", ""))
        if not bid:
            return jsonify({"ok": False, "error": "hiányzó vagy hibás paraméter — "
                                                  "?bid=BID-2026-259, ?projekt=mu6rw7nzoi6d "
                                                  "vagy ?bidlista=1"}), 400
        try:
            return jsonify({"ok": True, **diagnosztika(bid)})
        except Exception as e:  # noqa: BLE001
            log.exception("[HASZON] diagnosztika hiba")
            return jsonify({"ok": False, "error": str(e)}), 500

    log.info("[HASZON] Végpontok regisztrálva: /projekt-haszon/frissit, /projekt-haszon/diagnosztika")
