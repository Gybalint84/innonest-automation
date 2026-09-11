"""
szamlazz_adatkapcsolat.py — Számlázz.hu Online pénzügyi adatkapcsolat fogadó (kimenő + bejövő számlák)

Bekötés a server.py-ba (a többi modulhoz hasonlóan):
    from szamlazz_adatkapcsolat import register_szamlazz_routes
    register_szamlazz_routes(app)

Railway env változók:
    SZAMLAZZ_KEY                    – a Számlázz.hu fiókban beállított azonosító kulcs (utolsó 5 = rendszer-postfix)
    SZAMLAZZ_SHEET_ID               – a cél munkafüzet ID-je
    SZAMLAZZ_GOOGLE_CLIENT_ID       – OAuth kliens (Desktop app)
    SZAMLAZZ_GOOGLE_CLIENT_SECRET
    SZAMLAZZ_GOOGLE_REFRESH_TOKEN   – az oauth_token_szerzo.py adja

A Sheet írása KÖZVETLENÜL a Google Sheets API-n megy, nem Apps Script proxyn keresztül.
Ok: az Apps Script fiókszinten korlátozott és lassú (több másodperc/hívás), ami a
Számlázz.hu párhuzamos küldésénél időtúllépést okozott, és a többi SQM automatizmus
elől is elszívta a végrehajtási kapacitást.

Endpointok (ezeket kell megadni a Számlázz.hu regisztrációnál):
    POST /szamlazz/kimeno   – kimenő számlák (szamla.xsd)
    POST /szamlazz/bejovo   – bejövő számlák (szamlabe.xsd)
    GET  /szamlazz/health   – életjel

Működés:
    Számlázz.hu → POST application/xml (+ X-Szamlazzhu-Key fejléc) → parse → Sheets API upsert
    → válasz HTTP 200 + <szamlavalasz>/<szamlabevalasz> XML az <alap><id>-vel.
    Ha a Sheet-írás nem sikerül → HTTP 500 (nincs érvényes válasz) → a Számlázz.hu 72 órán át újrapróbálja.
    Ugyanaz a számla többször is jön (fizetettség / adatváltozás) → a Sheetben az <alap><id> alapján UPDATE.
"""

import logging
import os
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from flask import request, Response

import sheets_kliens as sk

log = logging.getLogger("szamlazz")

NS_KIMENO = "http://www.szamlazz.hu/szamla"
NS_BEJOVO = "http://www.szamlazz.hu/szamlabe"

PLACEHOLDER_HATARIDO = "2099-12-31"          # NAV-forrású számlánál, ha nincs határidő
FORRAS_NEVEK = {"26": "QUiCK", "28": "SMARTBooks", "34": "NAV Online Számla"}
TIPUS_NEVEK = {
    "SZ": "Számla", "SS": "Sztornó számla", "JS": "Jóváíró számla", "HS": "Helyesbítő számla",
    "ES": "Előlegszámla", "VS": "Végszámla", "D": "Díjbekérő", "SL": "Szállítólevél",
}
HUF_NEVEK = {"", "HUF", "FT", "Ft"}

# Az Apps Script web app oszlopsorrendje — ha bővíted, a webapp HEADER-jét is bővítsd!
OSZLOPOK = [
    "irany", "szamlazz_id", "szamlaszam", "tipus", "forras", "sztornozott", "hivszamlaszam",
    "kelt", "teljesites", "fizetesi_hatarido", "hatarido_ismeretlen", "fizmod",
    "partner_nev", "partner_adoszam", "partner_orszag",
    "devizanem", "arfolyam", "netto", "afa", "brutto", "netto_huf", "brutto_huf",
    "kifizetve", "kifizetve_huf", "utolso_kifizetes", "hatralek", "statusz",
    "rendelesszam", "megjegyzes", "iktatoszam", "penzforgalmi", "teszt", "frissitve",
]


# ---------------------------------------------------------------- XML segédek

def _txt(el, path, ns):
    """Szöveges mezőérték az elem alatt, '' ha hiányzik."""
    if el is None:
        return ""
    found = el.find(path, ns)
    return (found.text or "").strip() if found is not None and found.text is not None else ""


def _num(s, default=0.0):
    try:
        return float(str(s).replace(",", ".").strip()) if str(s).strip() != "" else default
    except ValueError:
        return default


def _bool(s):
    return str(s).strip().lower() in ("true", "1", "igen")


def _r2(x):
    return round(x + 0.0, 2)


# ---------------------------------------------------------------- feldolgozás

def parse_szamla(xml_bytes, irany):
    """
    Számlázz.hu számla-XML → lapos rekord (dict) + tételek (list of dict).
    irany: 'kimeno' vagy 'bejovo'. A PDF (base64) NEM kerül a rekordba.
    """
    ns_uri = NS_KIMENO if irany == "kimeno" else NS_BEJOVO
    ns = {"s": ns_uri}
    root = ET.fromstring(xml_bytes)

    alap = root.find("s:alap", ns)
    szallito = root.find("s:szallito", ns)
    vevo = root.find("s:vevo", ns)
    total = root.find("s:osszegek/s:totalossz", ns)

    szamlazz_id = _txt(alap, "s:id", ns)
    if not szamlazz_id:
        raise ValueError("hianyzo <alap><id>")

    # partner = kimenőnél a vevő, bejövőnél a szállító
    partner = vevo if irany == "kimeno" else szallito
    partner_orszag = _txt(partner, "s:cim/s:orszag", ns)
    if irany == "kimeno":
        lokacio = _txt(vevo, "s:lokacio", ns)
        partner_orszag = partner_orszag or {"1": "HU", "2": "EU", "3": "3. ország"}.get(lokacio, "")

    devizanem = _txt(alap, "s:devizanem", ns)
    is_huf = devizanem.upper() in {d.upper() for d in HUF_NEVEK}
    arfolyam = _num(_txt(alap, "s:devizaarf", ns), 1.0 if is_huf else 0.0)
    if is_huf:
        arfolyam = 1.0
        devizanem = "HUF"

    netto = _num(_txt(total, "s:netto", ns))
    afa = _num(_txt(total, "s:afa", ns))
    brutto = _num(_txt(total, "s:brutto", ns))
    huf_szorzo = 1.0 if is_huf else (arfolyam if arfolyam > 0 else 0.0)

    # kifizetések (számla devizanemében)
    kifizetve = 0.0
    utolso_kifizetes = ""
    for k in root.findall("s:kifizetesek/s:kifizetes", ns):
        kifizetve += _num(_txt(k, "s:osszeg", ns))
        d = _txt(k, "s:datum", ns)
        if d > utolso_kifizetes:
            utolso_kifizetes = d

    tipus = _txt(alap, "s:tipus", ns)
    sztornozott = _bool(_txt(alap, "s:sztornozott", ns))
    hatralek = _r2(brutto - kifizetve)

    if sztornozott or tipus == "SS":
        statusz = "Sztornó"
    elif tipus in ("D", "SL"):
        statusz = "Nem számla"          # díjbekérő / szállítólevél — nem kintlévőség
    elif brutto <= 0:
        statusz = "Jóváírás" if brutto < 0 else "Nulla összeg"
    elif kifizetve + 0.005 >= brutto:
        statusz = "Kifizetve"
    elif kifizetve > 0:
        statusz = "Részben fizetve"
    else:
        statusz = "Nyitott"

    hatarido = _txt(alap, "s:fizh", ns)
    hatarido_ismeretlen = hatarido == PLACEHOLDER_HATARIDO or hatarido == ""
    if hatarido_ismeretlen:
        hatarido = ""

    fizmod = _txt(alap, "s:fizmodunified", ns) or _txt(alap, "s:fizmod", ns)
    if fizmod in ("", "ismeretlen (NAV)"):
        fizmod = "ismeretlen"

    forras_kod = _txt(alap, "s:forras", ns)
    forras = FORRAS_NEVEK.get(forras_kod, "Számlázz.hu" if forras_kod == "" else f"egyéb ({forras_kod})")

    rekord = {
        "irany": "Kimenő" if irany == "kimeno" else "Bejövő",
        "szamlazz_id": szamlazz_id,
        "szamlaszam": _txt(alap, "s:szamlaszam", ns),
        "tipus": TIPUS_NEVEK.get(tipus, tipus),
        "forras": forras,
        "sztornozott": "igen" if sztornozott else "",
        "hivszamlaszam": _txt(alap, "s:hivszamlaszam", ns) or _txt(alap, "s:hivdijbekszam", ns),
        "kelt": _txt(alap, "s:kelt", ns),
        "teljesites": _txt(alap, "s:telj", ns),
        "fizetesi_hatarido": hatarido,
        "hatarido_ismeretlen": "igen" if hatarido_ismeretlen else "",
        "fizmod": fizmod,
        "partner_nev": _txt(partner, "s:nev", ns),
        "partner_adoszam": _txt(partner, "s:adoszam", ns) or _txt(partner, "s:adoszameu", ns),
        "partner_orszag": partner_orszag,
        "devizanem": devizanem,
        "arfolyam": arfolyam,
        "netto": _r2(netto),
        "afa": _r2(afa),
        "brutto": _r2(brutto),
        "netto_huf": _r2(netto * huf_szorzo),
        "brutto_huf": _r2(brutto * huf_szorzo),
        "kifizetve": _r2(kifizetve),
        "kifizetve_huf": _r2(kifizetve * huf_szorzo),
        "utolso_kifizetes": utolso_kifizetes,
        "hatralek": hatralek,
        "statusz": statusz,
        "rendelesszam": _txt(alap, "s:rendelesszam", ns),
        "megjegyzes": _txt(alap, "s:megjegyzes", ns),
        "iktatoszam": _txt(alap, "s:iktatoszam", ns),
        "penzforgalmi": "igen" if _bool(_txt(alap, "s:penzforg", ns)) else "",
        "teszt": "igen" if _bool(_txt(alap, "s:teszt", ns)) else "",
        "frissitve": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }

    tetelek = []
    for i, t in enumerate(root.findall("s:tetelek/s:tetel", ns), start=1):
        t_netto = _num(_txt(t, "s:netto", ns))
        tetelek.append({
            "irany": rekord["irany"],
            "szamlazz_id": szamlazz_id,
            "szamlaszam": rekord["szamlaszam"],
            "sorszam": _txt(t, "s:sztetordering", ns) or str(i),
            "megnevezes": _txt(t, "s:nev", ns),
            "mennyiseg": _num(_txt(t, "s:mennyiseg", ns)),
            "egyseg": _txt(t, "s:mennyisegiegyseg", ns),
            "egysegar": _num(_txt(t, "s:nettoegysegar", ns)),
            "afatipus": _txt(t, "s:afatipus", ns),
            "afakulcs": _num(_txt(t, "s:afakulcs", ns)),
            "netto": _r2(t_netto),
            "afa": _r2(_num(_txt(t, "s:afa", ns))),
            "brutto": _r2(_num(_txt(t, "s:brutto", ns))),
            "netto_huf": _r2(t_netto * huf_szorzo),
            "devizanem": devizanem,
            "megjegyzes": _txt(t, "s:megjegyzes", ns),
        })

    return rekord, tetelek


def valasz_xml(irany, szamlazz_id=None, iktatoszam=None, hibakod=None):
    """A Számlázz.hu által elvárt válasz-XML (szamlavalasz / szamlabevalasz)."""
    gyoker = "szamlavalasz" if irany == "kimeno" else "szamlabevalasz"
    ns_uri = f"http://www.szamlazz.hu/{gyoker}"
    reszek = [f'<?xml version="1.0" encoding="UTF-8"?>', f'<{gyoker} xmlns="{ns_uri}">']
    if szamlazz_id:
        reszek.append("<alap>")
        reszek.append(f"<id>{szamlazz_id}</id>")
        if iktatoszam:
            reszek.append(f"<iktatoszam>{iktatoszam}</iktatoszam>")
        reszek.append("</alap>")
    if hibakod:
        reszek.append(f"<hibakod>{hibakod}</hibakod>")
    reszek.append(f"</{gyoker}>")
    return "".join(reszek)


# ---------------------------------------------------------------- Sheet írás (közvetlen Sheets API)

SZAMLA_LAP, TETEL_LAP, NAPLO_LAP = "Számlák", "Tételek", "Napló"

TETEL_OSZLOPOK = ["irany", "szamlazz_id", "szamlaszam", "sorszam", "megnevezes", "mennyiseg", "egyseg",
                  "egysegar", "afatipus", "afakulcs", "netto", "afa", "brutto", "netto_huf",
                  "devizanem", "megjegyzes"]

_ir_lock = threading.Lock()          # a sorindexek miatt egyszerre egy írás fusson
_index = {"kesz": False, "szamla": {}, "tetel": {}, "lap_id": {}}


def _kulcs(irany, szamlazz_id):
    return f"{irany}|{szamlazz_id}"


def _index_epites():
    """Egyszeri beolvasás induláskor: melyik számla melyik sorban van."""
    meglevo = sk.lapok()
    for nev, fejlec in ((SZAMLA_LAP, OSZLOPOK), (TETEL_LAP, TETEL_OSZLOPOK), (NAPLO_LAP, ["idopont", "tipus", "uzenet"])):
        if nev not in meglevo:
            sk.lap_letrehozas(nev, fejlec)
            meglevo = sk.lapok()
    _index["lap_id"] = meglevo

    _index["szamla"] = {}
    for i, sor in enumerate(sk.olvas(f"'{SZAMLA_LAP}'!A2:B"), start=2):
        if len(sor) >= 2 and sor[1]:
            _index["szamla"][_kulcs(sor[0], sor[1])] = i

    _index["tetel"] = {}
    for i, sor in enumerate(sk.olvas(f"'{TETEL_LAP}'!A2:B"), start=2):
        if len(sor) >= 2 and sor[1]:
            _index["tetel"].setdefault(_kulcs(sor[0], sor[1]), []).append(i)

    _index["kesz"] = True
    log.info("Sheet index felépítve: %d számla, %d tételcsoport",
             len(_index["szamla"]), len(_index["tetel"]))


def _blokkok(sorok):
    """[3,4,5,9,10] → [(3,3),(9,2)] — összefüggő szakaszok, hogy egy hívással törölhessünk."""
    ki = []
    for sz in sorted(sorok):
        if ki and sz == ki[-1][0] + ki[-1][1]:
            ki[-1] = (ki[-1][0], ki[-1][1] + 1)
        else:
            ki.append((sz, 1))
    return ki


def _eltolas(index_szotar, hatar, mennyivel):
    """Sortörlés után a nála nagyobb sorszámok elcsúsznak."""
    for k, v in index_szotar.items():
        if isinstance(v, list):
            index_szotar[k] = [x - mennyivel if x > hatar else x for x in v]
        elif v > hatar:
            index_szotar[k] = v - mennyivel


def sheet_upsert(rekord, tetelek):
    """Egy számla (és tételei) beírása vagy frissítése. Hiba esetén kivételt dob."""
    with _ir_lock:
        if not _index["kesz"]:
            _index_epites()

        kulcs = _kulcs(rekord["irany"], rekord["szamlazz_id"])
        ertekek = [[rekord.get(k, "") if rekord.get(k) is not None else "" for k in OSZLOPOK]]

        sor = _index["szamla"].get(kulcs)
        if sor:
            sk.ir(f"'{SZAMLA_LAP}'!A{sor}", ertekek)
            muvelet = "update"
        else:
            valasz = sk.hozzafuz(f"'{SZAMLA_LAP}'!A1", ertekek)
            sor = sk.sorszam_updated_range(valasz)
            _index["szamla"][kulcs] = sor
            muvelet = "insert"

        _tetelek_irasa(kulcs, rekord, tetelek)

        sk.hozzafuz(f"'{NAPLO_LAP}'!A1", [[
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), muvelet,
            f"{rekord['irany']} {rekord['szamlaszam']} {rekord['partner_nev']} "
            f"{rekord['brutto']} {rekord['devizanem']} → {rekord['statusz']}"]])

        return {"ok": True, "muvelet": muvelet, "sor": sor, "tetelek": len(tetelek)}


def _tetelek_irasa(kulcs, rekord, tetelek):
    regi = _index["tetel"].get(kulcs, [])
    ujak = [[t.get(k, "") if t.get(k) is not None else "" for k in TETEL_OSZLOPOK] for t in tetelek]

    # Gyakori eset: ismételt küldésnél ugyanannyi tétel jön → helyben felülírjuk, nincs törlés.
    if regi and len(regi) == len(ujak) and _blokkok(regi) == [(min(regi), len(regi))]:
        sk.ir(f"'{TETEL_LAP}'!A{min(regi)}", ujak)
        return

    if regi:
        blokkok = _blokkok(regi)
        sk.sorok_torlese(_index["lap_id"][TETEL_LAP], blokkok)
        for kezd, db in sorted(blokkok, reverse=True):
            _eltolas(_index["tetel"], kezd + db - 1, db)
        _index["tetel"].pop(kulcs, None)

    if ujak:
        valasz = sk.hozzafuz(f"'{TETEL_LAP}'!A1", ujak)
        elso = sk.sorszam_updated_range(valasz)
        _index["tetel"][kulcs] = list(range(elso, elso + len(ujak)))


# ---------------------------------------------------------------- Flask route-ok

def _fogad(irany):
    kulcs = request.headers.get("X-Szamlazzhu-Key", "")
    vart = os.environ.get("SZAMLAZZ_KEY", "")
    if not vart or kulcs != vart:
        log.warning("Számlázz.hu %s: hibás kulcs (%s...)", irany, kulcs[:6])
        return Response(valasz_xml(irany, hibakod="KEY_ERR"), status=200, mimetype="application/xml")

    try:
        rekord, tetelek = parse_szamla(request.get_data(), irany)
    except (ET.ParseError, ValueError) as e:
        log.error("Számlázz.hu %s: érvénytelen XML: %s", irany, e)
        return Response("invalid xml", status=400, mimetype="text/plain")

    try:
        sheet_upsert(rekord, tetelek)
    except Exception as e:  # noqa: BLE001 — bármi hiba → 500, a Számlázz.hu újraküldi
        log.exception("Számlázz.hu %s: Sheet-írás sikertelen (%s): %s", irany, rekord["szamlaszam"], e)
        return Response("sheet write failed", status=500, mimetype="text/plain")

    log.info("Számlázz.hu %s OK: %s %s %s %s %s", irany, rekord["szamlaszam"], rekord["partner_nev"],
             rekord["brutto"], rekord["devizanem"], rekord["statusz"])
    return Response(valasz_xml(irany, rekord["szamlazz_id"], iktatoszam=f"SQM-{rekord['szamlazz_id']}"),
                    status=200, mimetype="application/xml")


def register_szamlazz_routes(app):
    @app.route("/szamlazz/kimeno", methods=["POST"])
    @app.route("/szamlazz/kimeno/", methods=["POST"])
    def szamlazz_kimeno():
        return _fogad("kimeno")

    @app.route("/szamlazz/bejovo", methods=["POST"])
    @app.route("/szamlazz/bejovo/", methods=["POST"])
    def szamlazz_bejovo():
        return _fogad("bejovo")

    @app.route("/szamlazz/health", methods=["GET"])
    def szamlazz_health():
        return {"ok": True,
                "key_set": bool(os.environ.get("SZAMLAZZ_KEY")),
                "sheet_set": bool(os.environ.get("SZAMLAZZ_SHEET_ID")),
                "oauth_set": all(os.environ.get(v) for v in (
                    "SZAMLAZZ_GOOGLE_CLIENT_ID", "SZAMLAZZ_GOOGLE_CLIENT_SECRET",
                    "SZAMLAZZ_GOOGLE_REFRESH_TOKEN")),
                "index_kesz": _index["kesz"],
                "szamlak_a_sheeten": len(_index["szamla"])}
