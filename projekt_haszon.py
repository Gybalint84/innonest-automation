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

from flask import request, jsonify

import sheets_kliens as sk

log = logging.getLogger("projekt_haszon")

BID_RE = re.compile(r"BID-\s?(\d{4})-\s?(\d+)")
INVOICE_REF_RE = re.compile(r"#(BID-)?(\d{4}-\d+)")     # ugyanaz, mint innonest_szamlalo.INVOICE_REF_PATTERN

PROJEKT_LAP = "Projektek"
HOZZAR_LAP = "Hozzárendelések"
BEALL_LAP = "Beállítások"
NAPLO_ALAP_ID = "1XQO7-kq2dtPhMpZ5ew327vVq-ddW1IFSAReaVLAQQs0"

PROJEKT_OSZLOPOK = [
    "bid", "ugyfel", "vegszamlak", "elolegszamlak",
    "bevetel_netto_huf", "anyag_tenyleges", "munkadij_tenyleges", "haszon_tenyleges",
    "kalk_bevetel", "kalk_anyag", "kalk_munkadij", "kalk_haszon",
    "elteres_ft", "elteres_pct", "sajat_csapat", "allapot", "megjegyzes", "frissitve",
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

# A Beállítások lapról beolvasott lista; amíg None, az alapértelmezés érvényes.
AKTIV_ANYAG_LISTA = None


def anyag_szallito_e(partner_nev, lista=None):
    """Anyagbeszállító-e a partner. SZÓHATÁRRAL illesztünk, nem részstringgel:
    a sima 'in' a "Deli Store"-t is STO-nak vette (élő próbán bukott ki 2026-09-11)."""
    if lista is None:
        lista = AKTIV_ANYAG_LISTA if AKTIV_ANYAG_LISTA is not None else ANYAG_ALAP
    p = partner_nev or ""
    for k in lista:
        k = (k or "").strip()
        if k and re.search(r"(?<![\wáéíóöőúüű])" + re.escape(k) + r"(?![\wáéíóöőúüű])", p, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------- 1. kimenő → BID (Innonest /invoices)

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

def bejovo_bid_hozzarendeles(rekord, tetelek, beszerzesek, naplo_index):
    """
    Egy bejövő számla → (bid, módszer). beszerzesek: [{bid, beszallito, netto}] az Innonest-ből,
    naplo_index: {számlaszám: bid} az ellenőrző naplójából.
    """
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

    # c) számlaellenőrző napló
    b = naplo_index.get((rekord.get("szamlaszam") or "").strip())
    if b:
        return b, "ellenőrző napló"

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

def kalkulalt_lekeres(bid):
    """
    A Padló kalkulátor webappból: {'bevetel': ..., 'anyag': ..., 'munkadij': ..., 'sajat_csapat': bool, 'ugyfel': str}
    vagy None, ha nincs kalkuláció. Ide jön a Firestore/webapp-olvasás, ha ismert a tárolás szerkezete.
    """
    return None


# ---------------------------------------------------------------- 5. összeállítás (tiszta függvény — tesztelhető)

def kimutatas_osszeallitas(szamlak, tetelek, kimeno_bid, beszerzesek, naplo_index, kezi):
    """
    szamlak, tetelek: a Számlázz.hu-Sheet sorai dict-ként.
    kimeno_bid: {számlaszám: bid} az Innonest-ből. kezi: {számlaszám: (bid_kezi, kategoria_kezi)}.
    Visszaad: (projekt_sorok, hozzarendeles_sorok) — mindkettő [dict] a fenti oszlopokkal.
    """
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

        if irany == "Kimenő":
            bid_auto = kimeno_bid.get(szam, "")
            modszer = "Innonest számlalista" if bid_auto else ""
            kat_auto = "bevétel"
        else:
            bid_auto, modszer = bejovo_bid_hozzarendeles(r, tetel_index[(irany, str(r.get("szamlazz_id")))], beszerzesek, naplo_index)
            kat_auto = ""      # a BID ismeretében állítjuk be lentebb

        bid_kezi, kat_kezi = kezi.get(szam, ("", ""))
        bid_erv = bid_kezi or bid_auto
        if irany == "Bejövő":
            # Kategóriát csak akkor adunk, ha a számla projekthez köthető (akár kézzel) — különben
            # "egyéb" (rezsi, irodaszer, lízing), ami nem kerül be egyetlen projekt költségébe sem.
            kat_auto = ("anyag" if anyag_szallito_e(r.get("partner_nev")) else "munkadíj") if bid_erv else "egyéb"
        kat_erv = kat_kezi or kat_auto
        hozzar.append({
            "szamlaszam": szam, "irany": irany, "tipus": tipus, "kelt": r.get("kelt", ""),
            "partner_nev": r.get("partner_nev", ""), "netto_huf": round(netto, 2),
            "bid_auto": bid_auto, "modszer": modszer, "kategoria_auto": kat_auto,
            "bid_kezi": bid_kezi, "kategoria_kezi": kat_kezi,
            "bid_ervenyes": bid_erv, "kategoria_ervenyes": kat_erv,
        })

    # projektenkénti összegzés
    proj = defaultdict(lambda: {"vegszamlak": [], "elolegszamlak": [], "bevetel": 0.0, "anyag": 0.0,
                                "munkadij": 0.0, "ugyfel": ""})
    for h in hozzar:
        b = h["bid_ervenyes"]
        if not b:
            continue
        p = proj[b]
        if h["irany"] == "Kimenő":
            p["bevetel"] += h["netto_huf"]
            p["ugyfel"] = p["ugyfel"] or h["partner_nev"]
            (p["elolegszamlak"] if h["tipus"] == "Előlegszámla" else p["vegszamlak"]).append(h["szamlaszam"])
        elif h["kategoria_ervenyes"] == "anyag":
            p["anyag"] += h["netto_huf"]
        elif h["kategoria_ervenyes"] == "munkadíj":
            p["munkadij"] += h["netto_huf"]

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
            "elteres_ft": "", "elteres_pct": "", "sajat_csapat": "", "megjegyzes": "", "frissitve": most,
        }
        if not p["vegszamlak"]:
            sor["allapot"] = "csak előleg"
        elif kalk is None:
            sor["allapot"] = "nincs kalkuláció"
        else:
            kh = _num(kalk.get("bevetel")) - _num(kalk.get("anyag")) - _num(kalk.get("munkadij"))
            sor.update({
                "kalk_bevetel": round(_num(kalk.get("bevetel"))), "kalk_anyag": round(_num(kalk.get("anyag"))),
                "kalk_munkadij": round(_num(kalk.get("munkadij"))), "kalk_haszon": round(kh),
                "elteres_ft": round(haszon - kh),
                "elteres_pct": round((haszon - kh) / kh * 100, 1) if kh else "",
                "sajat_csapat": "saját csapat is volt kint" if kalk.get("sajat_csapat") else "",
                "allapot": "kiértékelt",
            })
            sor["ugyfel"] = sor["ugyfel"] or kalk.get("ugyfel", "")
        if p["anyag"] == 0 and p["munkadij"] == 0 and p["vegszamlak"]:
            sor["megjegyzes"] = "nincs hozzárendelt költség"
        projektek.append(sor)

    return projektek, hozzar


# ---------------------------------------------------------------- Innonest scrape (egy böngésző-munkamenet)

async def _innonest_gyujtes_async(kezdo_bidek):
    """Egy böngésző-menetben: kimenő számlalista → BID-ek, majd beszerzési megrendelőlapok
    (modált csak azokhoz a BID-ekhez nyitunk, amelyek a kimenő számlákon vagy a kézi listán szerepelnek)."""
    from playwright.async_api import async_playwright
    from innonest_core import login, load_session, make_browser_args
    from innonest_szamlalo import _scrape_rows, _invoices_page_url, LISTA_OLDALMERET, DATE_PATTERN
    from szamla_ellenorzo import _get_beszerzesi_sorok, _nyisd_meg_reszletek, ACQUISITION_URL

    ev = datetime.now().year
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        # -- kimenő számlák
        szamla_sorok, offset = [], 0
        for _ in range(50):
            sorok = await _scrape_rows(page, _invoices_page_url(offset))
            if "login" in page.url:
                await login(page)
                sorok = await _scrape_rows(page, _invoices_page_url(offset))
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
        await page.goto(ACQUISITION_URL, wait_until="networkidle")
        await page.wait_for_timeout(1000)
        lista = await _get_beszerzesi_sorok(page)
        beszerzesek = []
        for s in lista:
            if not s.get("bid") or s["bid"] not in erdekes_bidek:
                continue
            reszlet = await _nyisd_meg_reszletek(page, s["azonosito"])
            szoveg = (reszlet or {}).get("netto_szoveg", "")
            m = re.search(r"Nett[oó][^\d]{0,20}([\d\s]{3,})\s*(?:Ft|HUF)", szoveg, re.IGNORECASE) or \
                re.search(r"([\d][\d\s]{2,})\s*(?:Ft|HUF)", szoveg, re.IGNORECASE)
            beszerzesek.append({
                "bid": s["bid"],
                "beszallito": (reszlet or {}).get("beszallito") or s.get("beszallito_lista", ""),
                "netto": int(m.group(1).replace(" ", "")) if m else 0,
            })
        await browser.close()

    log.info("[HASZON] Innonest: %d számlasor, %d beszerzés az érdekes BID-ekhez", len(szamla_sorok), len(beszerzesek))
    return szamla_sorok, beszerzesek


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


def anyagszallitok_olvasas():
    """Az anyagbeszállítók listája a Beállítások lapról. Ha a lap még nincs meg,
    létrehozza az alapértelmezett listával — onnantól a Sheetben szerkeszthető,
    nem kell hozzá Railway-deploy."""
    cel = _cel_id()
    if BEALL_LAP not in sk.lapok(cel):
        sk.lap_letrehozas(BEALL_LAP, ["beallitas", "ertek", "leiras"], sheet_id=cel)
        sk.ir(f"'{BEALL_LAP}'!A2",
              [["Anyagbeszállító", n, "Ezek bejövő számlái számítanak ANYAGKÖLTSÉGNEK. "
                "Új beszállító: új sor, A oszlop = Anyagbeszállító. Minden más partner = munkadíj."]
               for n in ANYAG_ALAP], sheet_id=cel)
        log.info("[HASZON] Beállítások lap létrehozva az alapértelmezett beszállítókkal")
        return list(ANYAG_ALAP)

    lista = []
    for sor in sk.olvas(f"'{BEALL_LAP}'!A2:B", cel):
        if len(sor) >= 2 and str(sor[0]).strip().lower().startswith("anyagbeszáll") and str(sor[1]).strip():
            lista.append(str(sor[1]).strip())
    if not lista:
        log.warning("[HASZON] A Beállítások lapon nincs egyetlen anyagbeszállító sem — az alapértelmezést használom")
        return list(ANYAG_ALAP)
    return lista


def kezi_felulirasok_olvasas():
    """A Hozzárendelések lap kézi oszlopai — ezek túlélik a frissítést."""
    cel = _cel_id()
    if HOZZAR_LAP not in sk.lapok(cel):
        return {}
    d = _sorok_dict(sk.olvas(f"'{HOZZAR_LAP}'!A1:M", cel))
    return {str(s.get("szamlaszam", "")).strip(): (bid_normalizal(s.get("bid_kezi", "")), str(s.get("kategoria_kezi", "")).strip())
            for s in d if s.get("bid_kezi") or s.get("kategoria_kezi")}


VEDETT_LAPOK = {"Számlák", "Tételek", "Napló"}


def kimutatas_iras(projektek, hozzar):
    cel = _cel_id()
    if VEDETT_LAPOK & {PROJEKT_LAP, HOZZAR_LAP, BEALL_LAP}:      # biztosíték átnevezés ellen
        raise RuntimeError("A kimutatás lapneve ütközik a Számlázz.hu adatkapcsolat lapjaival")
    meglevo = sk.lapok(cel)
    for nev, fej in ((PROJEKT_LAP, PROJEKT_OSZLOPOK), (HOZZAR_LAP, HOZZAR_OSZLOPOK)):
        if nev not in meglevo:
            sk.lap_letrehozas(nev, fej, sheet_id=cel)
    sk.torol(f"'{PROJEKT_LAP}'!A2:Z", cel)
    sk.torol(f"'{HOZZAR_LAP}'!A2:Z", cel)
    if projektek:
        sk.ir(f"'{PROJEKT_LAP}'!A2", [[p.get(k, "") for k in PROJEKT_OSZLOPOK] for p in projektek], sheet_id=cel)
    if hozzar:
        sk.ir(f"'{HOZZAR_LAP}'!A2", [[h.get(k, "") for k in HOZZAR_OSZLOPOK] for h in hozzar], sheet_id=cel)


def frissites():
    """Teljes futás. Visszaad egy összegzést a végpontnak."""
    from innonest_core import run_in_loop

    global AKTIV_ANYAG_LISTA
    forras = os.environ.get("SZAMLAZZ_SHEET_ID")
    szamlak = _sorok_dict(sk.olvas("'Számlák'!A1:AG", forras))
    tetelek = _sorok_dict(sk.olvas("'Tételek'!A1:P", forras))
    kezi = kezi_felulirasok_olvasas()
    AKTIV_ANYAG_LISTA = anyagszallitok_olvasas()
    log.info("[HASZON] Anyagbeszállítók: %s", ", ".join(AKTIV_ANYAG_LISTA))

    naplo_index = {}
    try:
        naplo_id = os.environ.get("SZAMLAZZ_ELLENORZO_NAPLO_ID", NAPLO_ALAP_ID)
        elso_lap = next(iter(sk.lapok(naplo_id)))
        naplo_index = naplo_index_epites(sk.olvas(f"'{elso_lap}'!A1:Z", naplo_id))
    except Exception as e:  # noqa: BLE001 — a napló opcionális
        log.warning("[HASZON] Ellenőrző napló nem olvasható: %s", e)

    # Innonest (egy böngésző-menet): kimenő számlák → BID, majd beszerzések az érdekes BID-ekhez.
    # Kiinduló érdekes BID-ek: kézi hozzárendelések + a számlák szövegében talált BID-ek.
    kezdo_bidek = {b for b, _ in kezi.values() if b}
    for r in szamlak:
        b = bid_normalizal(r.get("megjegyzes", "") + " " + r.get("rendelesszam", ""))
        if b:
            kezdo_bidek.add(b)
    szamla_sorok, beszerzesek = run_in_loop(_innonest_gyujtes_async(kezdo_bidek))
    kimeno_bid = kimeno_bid_kinyeres(szamla_sorok)

    projektek, hozzar = kimutatas_osszeallitas(szamlak, tetelek, kimeno_bid, beszerzesek, naplo_index, kezi)
    kimutatas_iras(projektek, hozzar)

    return {
        "ok": True, "projektek": len(projektek), "szamlak": len(hozzar),
        "kimeno_bid_talalat": len(kimeno_bid),
        "bejovo_hozzarendelt": sum(1 for h in hozzar if h["irany"] == "Bejövő" and h["bid_ervenyes"]),
        "bejovo_nincs_bid": sum(1 for h in hozzar if h["irany"] == "Bejövő" and not h["bid_ervenyes"]),
        "kiertekelt": sum(1 for p in projektek if p["allapot"] == "kiértékelt"),
        "nincs_kalkulacio": sum(1 for p in projektek if p["allapot"] == "nincs kalkuláció"),
        "anyagszallitok": AKTIV_ANYAG_LISTA,
    }




# ---------------------------------------------------------------- DIAGNOSZTIKA

async def _diag_innonest_async(bid):
    """Nyersen visszaadja, mit lát az Innonest az adott BID-hez."""
    from playwright.async_api import async_playwright
    from innonest_core import login, load_session, make_browser_args
    from innonest_szamlalo import _scrape_rows, _invoices_page_url, LISTA_OLDALMERET
    from szamla_ellenorzo import _get_beszerzesi_sorok, _nyisd_meg_reszletek, ACQUISITION_URL

    ki = {"kimeno_szamlasorok": [], "beszerzesek": []}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=make_browser_args())
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await load_session(context)
        page = await context.new_page()

        offset = 0
        for _ in range(50):
            sorok = await _scrape_rows(page, _invoices_page_url(offset))
            if "login" in page.url:
                await login(page)
                sorok = await _scrape_rows(page, _invoices_page_url(offset))
            if not sorok:
                break
            for r in sorok:
                if bid.replace("BID-", "") in r.get("text", ""):
                    ki["kimeno_szamlasorok"].append({"id": r.get("id"), "text": r.get("text", "")[:400]})
            if len(sorok) < LISTA_OLDALMERET:
                break
            offset += LISTA_OLDALMERET

        await page.goto(ACQUISITION_URL, wait_until="networkidle")
        await page.wait_for_timeout(1000)
        lista = await _get_beszerzesi_sorok(page)
        ki["acquisition_osszes_sor"] = len(lista)
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


def _diag_firestore(bid):
    """Felderíti a webapp Firestore-tárolását: milyen collectionök vannak, és
    melyikben található az adott BID — mezőnevekkel együtt."""
    ki = {"elerheto": False}
    try:
        import json as _json
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError as e:
        ki["hiba"] = f"firebase_admin nincs telepítve: {e}"
        return ki

    try:
        if not firebase_admin._apps:
            nyers = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
            if not nyers:
                ki["hiba"] = "FIREBASE_SERVICE_ACCOUNT_JSON nincs beállítva a Railway-en"
                return ki
            firebase_admin.initialize_app(credentials.Certificate(_json.loads(nyers)))
        db = firestore.client()
        ki["elerheto"] = True

        ki["collectionok"] = []
        for coll in db.collections():
            nev = coll.id
            info = {"collection": nev, "minta_dokumentumok": [], "bid_talalat": None}
            for doc in coll.limit(3).stream():
                d = doc.to_dict() or {}
                info["minta_dokumentumok"].append({
                    "id": doc.id,
                    "mezok": sorted(d.keys()),
                    "ertek_minta": {k: str(v)[:120] for k, v in list(d.items())[:12]},
                })
            # a BID keresése: dokumentum-azonosítóként és néhány szokásos mezőnévben
            try:
                d = coll.document(bid).get()
                if d.exists:
                    adat = d.to_dict() or {}
                    info["bid_talalat"] = {"hol": "dokumentum-azonosító",
                                           "mezok": sorted(adat.keys()),
                                           "ertekek": {k: str(v)[:200] for k, v in adat.items()}}
            except Exception:
                pass
            if not info["bid_talalat"]:
                for mezo in ("bid", "BID", "bidSzam", "bid_szam", "projektBid", "azonosito", "projectId"):
                    try:
                        tal = list(coll.where(mezo, "==", bid).limit(1).stream())
                        if tal:
                            adat = tal[0].to_dict() or {}
                            info["bid_talalat"] = {"hol": f"mező: {mezo}", "doc_id": tal[0].id,
                                                   "mezok": sorted(adat.keys()),
                                                   "ertekek": {k: str(v)[:200] for k, v in adat.items()}}
                            break
                    except Exception:
                        continue
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
                              [t.get("megnevezes", "") for t in ts])
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


def diagnosztika(bid):
    from innonest_core import run_in_loop
    ki = {"bid": bid, "sheet": {}, "innonest": {}, "firestore": {}}
    try:
        ki["sheet"] = _diag_szamlak(bid)
    except Exception as e:  # noqa: BLE001
        ki["sheet"] = {"hiba": f"{type(e).__name__}: {e}"}
    try:
        ki["innonest"] = run_in_loop(_diag_innonest_async(bid))
    except Exception as e:  # noqa: BLE001
        ki["innonest"] = {"hiba": f"{type(e).__name__}: {e}"}
    ki["firestore"] = _diag_firestore(bid)
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
        bid = bid_normalizal(request.args.get("bid", ""))
        if not bid:
            return jsonify({"ok": False, "error": "hiányzó vagy hibás bid paraméter "
                                                  "(pl. ?bid=BID-2026-259)"}), 400
        try:
            return jsonify({"ok": True, **diagnosztika(bid)})
        except Exception as e:  # noqa: BLE001
            log.exception("[HASZON] diagnosztika hiba")
            return jsonify({"ok": False, "error": str(e)}), 500

    log.info("[HASZON] Végpontok regisztrálva: /projekt-haszon/frissit, /projekt-haszon/diagnosztika")
