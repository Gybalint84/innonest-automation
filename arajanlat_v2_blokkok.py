"""
arajanlat_v2_blokkok.py — a V2 („prémium") árajánlat-sablon kitöltő segédfüggvényei
====================================================================================
Tedd az `arajanlat_pdf.py` mellé, és importáld belőle. A sablon
(`sablonok/arajanlat_sablon_v2.html`) kétféle helyőrzőt tartalmaz:

  1. egyszerű szöveges:  {{UGYFEL_NEV}}, {{KELTEZES}}, …   → sima csere, escape-elve
  2. HTML-blokk:         {{TETEL_SOROK}}, {{RETEGREND_SOROK}},
                         {{FELTETELEK_KIVONAT}}, {{HERO_BLOKK}}, {{BRUTTO_OSSZESEN}}
                         → az itteni függvények állítják elő

Használat:

    from arajanlat_v2_blokkok import build_arajanlat_html_v2
    html = build_arajanlat_html_v2(adatok, extra)   # extra = a webappos űrlap mezői
"""
import os
import re
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SABLON_V2 = os.path.join(BASE_DIR, "sablonok", "arajanlat_sablon_v2.html")


# ══════════════════════════════════════════════════════════════════════════════
# ALAPOK
# ══════════════════════════════════════════════════════════════════════════════

def esc(text) -> str:
    return (str(text if text is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _to_float(value) -> float:
    try:
        return float(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except Exception:
        return 0.0


def fmt_huf(value) -> str:
    """1234567 → '1 234 567 Ft'"""
    return f"{_to_float(value):,.0f}".replace(",", " ") + " Ft"


def fmt_datum(value) -> str:
    """'2026-07-07' / '2026.07.07' / '2026. 07. 07' → '2026. 07. 07.'

    Amit nem ismer fel, azt változatlanul visszaadja (jobb, mint eldobni).
    """
    s = str(value or "").strip()
    if not s:
        return ""
    m = re.match(r"^\s*(\d{4})[.\-/ ]+(\d{1,2})[.\-/ ]+(\d{1,2})\s*\.?\s*$", s)
    if m:
        ev, ho, nap = m.groups()
        return f"{ev}. {int(ho):02d}. {int(nap):02d}."
    return s


def fmt_terulet(items) -> str:
    """Az alcím területe: ha minden árazott tétel ugyanarra a m²-re vonatkozik, azt adja vissza.

    Különböző mennyiségeknél üres stringet ad — ilyenkor az alcímből egyszerűen kimarad.
    """
    mennyisegek = set()
    for it in items or []:
        if it.get("is_text_block"):
            continue
        egyseg = (it.get("egyseg") or "").strip().lower()
        if egyseg not in ("m²", "m2"):
            return ""
        szam = _to_float(re.sub(r"[^\d.,]", "", str(it.get("mennyiseg") or "")))
        if szam:
            mennyisegek.add(round(szam, 2))
    if len(mennyisegek) != 1:
        return ""
    n = mennyisegek.pop()
    egesz, tort = divmod(round(n * 100), 100)
    return f"{egesz:,}".replace(",", " ") + (f",{tort:02d}" if tort else "") + " m²"


def alcim(helyszin: str, terulet: str, rendszer: str) -> str:
    """A borító alcíme: a kitöltött elemek ' · ' jellel összefűzve."""
    return " · ".join(p for p in (helyszin, terulet, rendszer) if (p or "").strip())


# ══════════════════════════════════════════════════════════════════════════════
# BLOKKOK
# ══════════════════════════════════════════════════════════════════════════════

_HERO_URES = (
    '<div class="hero hero--empty">'
    '<svg width="25" height="21" viewBox="0 0 25 21" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<rect x="1" y="1" width="23" height="19" rx="2.5" stroke="#B4B2A9" stroke-width="1.4"/>'
    '<circle cx="8" cy="7.5" r="2.1" stroke="#B4B2A9" stroke-width="1.4"/>'
    '<path d="M2.5 16.5L9 10.5L13.5 14.5L17.5 11L22.5 16" stroke="#B4B2A9" '
    'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    '<div class="hero-cap">{cap}</div></div>'
)


def hero_html(kep_src: str = "", felirat: str = "Saját referenciafotó helye") -> str:
    """kep_src: data: URI vagy URL. Üresen szaggatott keretes helykitöltőt ad."""
    if (kep_src or "").strip():
        return f'<div class="hero"><img class="hero-img" src="{esc(kep_src)}" alt=""></div>'
    return _HERO_URES.format(cap=esc(felirat))


def retegrend_html(retegek) -> str:
    """[{cim, alcim}, …] felülről lefelé.

    Színezés: első réteg sárga (fő rendszer), utolsó sötét (meglévő aljzat),
    a köztesek világosszürkék. Cím nélküli réteg → alacsonyabb, egysoros sáv.
    """
    out, n = [], len(retegek or [])
    for i, r in enumerate(retegek or []):
        cls = "layer"
        if i == 0 and n > 1:
            cls += " layer--fo"
        elif i == n - 1 and n > 1:
            cls += " layer--aljzat"
        if not (r.get("cim") or "").strip():
            cls += " layer--egysoros"
        out.append(f'<div class="{cls}">'
                   f'<div class="layer-title">{esc(r.get("cim"))}</div>'
                   f'<div class="layer-sub">{esc(r.get("alcim"))}</div></div>')
    return "".join(out)


def feltetelek_html(pontok) -> str:
    return "".join(f"<li>{esc(t)}</li>" for t in (pontok or []) if str(t).strip())


def tetel_sorok_html(items) -> str:
    """Az árazási táblázat sorai — ugyanaz a tétel-struktúra, amit a scraper ad."""
    out = []
    for it in items or []:
        if it.get("is_text_block"):
            szoveg = (it.get("leiras") or "").strip()
            if szoveg:
                out.append(f'<div class="trow titem"><div class="ttext">{esc(szoveg)}</div></div>')
            continue
        egyseg = (it.get("egyseg") or "").strip()
        egysegar = it.get("egysegar_szoveg")
        if not egysegar:
            egysegar = fmt_huf(it.get("egysegar", 0))
            if egyseg:
                egysegar += f"/{egyseg}"
        osszesen = it.get("osszesen_szoveg") or fmt_huf(it.get("osszesen", 0))
        out.append(
            '<div class="trow titem">'
            f'<div class="tcell">{esc(it.get("megnevezes"))}'
            f'<div class="tnote">{esc(it.get("megjegyzes"))}</div>'
            f'<div class="tdesc">{esc(it.get("leiras"))}</div></div>'
            f'<div class="tcell tnum">{esc(it.get("mennyiseg"))}</div>'
            f'<div class="tcell tnum">{esc(egysegar)}</div>'
            f'<div class="tcell tnum">{esc(osszesen)}</div>'
            '</div>')
    return "".join(out)


def brutto_html(szoveg: str) -> str:
    """'76 839 651 Ft' → a pénznem kisebb betűvel a sárga dobozban."""
    s = esc(str(szoveg or "").strip())
    return f'{s[:-2].strip()} <span class="ft">Ft</span>' if s.endswith("Ft") else s


# ══════════════════════════════════════════════════════════════════════════════
# 4. OLDAL — ÜTEMEZÉS ÉS KÖVETKEZŐ LÉPÉSEK
# ══════════════════════════════════════════════════════════════════════════════

def lepesek_html(lepesek) -> str:
    """[{cim, leiras}, …] — a folyamatábra 4 lépése (a sorszám automatikus).

    Az első lépés számozó köre márkasárga, a többi halvány — ez CSS-ből jön
    (.lepes:first-child), tehát a sorrend elég.
    """
    out = []
    for i, l in enumerate(lepesek or [], start=1):
        out.append(
            '<div class="lepes">'
            f'<div class="lepes-num">{i}</div>'
            f'<div class="lepes-cim">{esc(l.get("cim"))}</div>'
            f'<div class="lepes-leiras">{esc(l.get("leiras"))}</div>'
            '</div>'
        )
    return "".join(out)


def monogram(nev: str) -> str:
    """'Gyenes Bálint' → 'GB'. Egy szóból az első két betű, üresből semmi."""
    reszek = [r for r in str(nev or "").split() if r]
    if not reszek:
        return ""
    if len(reszek) == 1:
        return reszek[0][:2].upper()
    return (reszek[0][0] + reszek[-1][0]).upper()


def qr_html(kep_src: str = "", felirat: str = "") -> str:
    """A kapcsolattartó-kártya jobb oldali QR-blokkja.

    ⚠️ 2026-09-04: a QR célja (mire mutasson) MÉG NINCS ELDÖNTVE, ezért kép
    nélkül a teljes blokk elmarad — üres fehér doboz csúnyán mutatna az
    ügyfélnek küldött PDF-ben. Ha később megvan a döntés, elég a `qr_kep`
    mezőt (data: URI vagy URL) átadni, és a blokk a helyére kerül; a sablon
    és a CSS (.kapcs-qr / .qr-doboz / .qr-felirat) már készen áll rá.
    """
    if not (kep_src or "").strip():
        return ""
    return (
        '<div class="kapcs-qr">'
        f'<div class="qr-doboz"><img class="qr-img" src="{esc(kep_src)}" alt=""></div>'
        f'<div class="qr-felirat">{esc(felirat)}</div>'
        '</div>'
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5. OLDAL — REFERENCIÁK
# ══════════════════════════════════════════════════════════════════════════════

# ugyanaz a kép-ikon, mint a borító hero-helykitöltőjén (egységes megjelenés)
_KEP_IKON = (
    '<svg width="25" height="21" viewBox="0 0 25 21" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<rect x="1" y="1" width="23" height="19" rx="2.5" stroke="#B4B2A9" stroke-width="1.4"/>'
    '<circle cx="8" cy="7.5" r="2.1" stroke="#B4B2A9" stroke-width="1.4"/>'
    '<path d="M2.5 16.5L9 10.5L13.5 14.5L17.5 11L22.5 16" stroke="#B4B2A9" '
    'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def ref_foto_html(kep_src: str = "", felirat: str = "Saját projektfotó — átadás után") -> str:
    """A referencia-kártya bal oldali fotója; kép nélkül szaggatott keretes helykitöltő."""
    if (kep_src or "").strip():
        return f'<div class="ref-foto"><img class="ref-foto-img" src="{esc(kep_src)}" alt=""></div>'
    return (f'<div class="ref-foto ref-foto--ures">{_KEP_IKON}'
            f'<div class="ref-foto-cap">{esc(felirat)}</div></div>')


def ref_sorok_html(sorok) -> str:
    """Az esettanulmány adatsorai: ['Terület: 7 227 m² · Rendszer: …', …]"""
    return "".join(f'<div class="ref-sor">{esc(s)}</div>' for s in (sorok or []) if str(s).strip())


def logo_racs_html(logok, hely_db: int = 6, hely_szoveg: str = "partnerlogó") -> str:
    """Partnerlogó-fal.

    `logok`: kép-forrás lista (data: URI vagy URL) — szürkeárnyalatosan,
    egységes dobozméretben jelennek meg (CSS: .logo-img filter:grayscale(1)).
    Ha üres, `hely_db` darab helykitöltő doboz kerül a helyükre — a
    felhasználó kérése (2026-09-04), amíg a valódi logók elő nem kerülnek.
    """
    logok = [l for l in (logok or []) if str(l).strip()]
    if logok:
        return "".join(
            f'<div class="logo-doboz"><img class="logo-img" src="{esc(l)}" alt=""></div>'
            for l in logok
        )
    return "".join(
        f'<div class="logo-doboz"><span class="logo-hely">{esc(hely_szoveg)}</span></div>'
        for _ in range(max(0, hely_db))
    )


# ══════════════════════════════════════════════════════════════════════════════
# 6. OLDAL — SZERZŐDÉSES FELTÉTELEK (MELLÉKLET)
# ══════════════════════════════════════════════════════════════════════════════

def kiemel(szoveg: str) -> str:
    """`**...**` → félkövér kiemelés. Előbb escape-el, így a felhasználó által
    beírt szöveg akkor is biztonságos, ha < vagy & van benne.

    Miért így: a feltételszövegben a határidők/díjak/tűrések félkövérek
    (a minta „kiemelt számai"), de a szöveg egyébként szabadon szerkeszthető
    a webappos űrlapról — a `**` a legegyszerűbb jelölés erre.
    """
    s = esc(szoveg)
    ki = re.split(r"\*\*", s)
    out = []
    for i, resz in enumerate(ki):
        out.append(f'<span class="kiemelt">{resz}</span>' if i % 2 else resz)
    return "".join(out)


def melleklet_html(blokkok) -> str:
    """[{cim, szoveg}, …] — sorfolytonosan tölti a kéthasábos rácsot
    (1. bal, 2. jobb, 3. bal, …), ahogy a mintában is."""
    out = []
    for b in blokkok or []:
        out.append(
            '<div class="mell-blokk">'
            f'<div class="mell-cim">{esc(b.get("cim"))}</div>'
            f'<div class="mell-szoveg">{kiemel(b.get("szoveg", ""))}</div>'
            '</div>'
        )
    return "".join(out)


# ══════════════════════════════════════════════════════════════════════════════
# SABLON KITÖLTÉS
# ══════════════════════════════════════════════════════════════════════════════

_ALAP_KIEMELESEK = [
    {"ertek": "3 év", "cimke": "rendszergarancia*"},
    {"ertek": "48–72 óra", "cimke": "alatt visszaadott felület"},
    {"ertek": "Hétvégi", "cimke": "kivitelezés, minimális leállás"},
]
_ALAP_LABJEGYZET = "* Jegyzőkönyvvel igazolt aljzatparaméterek esetén, a rendszerspecifikáció szerint."

# 4. oldal alapértelmezései — a minta szövegei. A webappos űrlapról bármelyik
# felülírható (`lepesek`, `utemezes_bekezdes`, `kapcsolat_szerep`).
_ALAP_LEPESEK = [
    {"cim": "Helyszíni bejárás", "leiras": "aljzatvizsgálat, egyeztetés az üzemeltetéssel"},
    {"cim": "Szerződéskötés",    "leiras": "ütemterv és szakaszolás rögzítése"},
    {"cim": "Kivitelezés",       "leiras": "szakaszolt, igény szerint hétvégi ütem"},
    {"cim": "Műszaki átadás",    "leiras": "jegyzőkönyv, garanciadokumentumok"},
]
_ALAP_UTEMEZES_BEKEZDES = (
    "Az ajánlat elfogadását követően 5 munkanapon belül egyeztetjük a helyszíni "
    "bejárás és a kivitelezés időpontját."
)
_ALAP_KAPCSOLAT_SZEREP = "projektfelelős"

# 5. oldal alapértelmezései (a minta szövegei). A logófal egyelőre
# helykitöltőkkel megy — a valódi logók a `logofal` mezőn keresztül jönnek majd.
_ALAP_REF_CHIP = "minta-esettanulmány"
_ALAP_REF_CIM = "Mélygarázs padlófelújítás — projektnév"
_ALAP_REF_SOROK = [
    "Terület: X m² · Rendszer: STO Crete OS",
    "Átfutás: X hétvége, üzemleállás nélkül",
    "Eredmény: 1–2 mondatos ügyfélvisszajelzés",
]
_ALAP_LOGOFAL_CIM = "Velük már együtt dolgoztunk"
_ALAP_LOGOFAL_MEGJEGYZES = (
    "Egységes méretű, szürkeárnyalatos logókezelés — a színes, vegyes minőségű logófal helyett."
)

# 6. oldal alapértelmezései — a jelenlegi feltételszöveg értékeivel (minta).
# A `**...**` közötti rész félkövéren jelenik meg (határidők, díjak, tűrések).
# A sorrend a kéthasábos rácsot tölti: 1. bal, 2. jobb, 3. bal, …
_ALAP_MELLEKLET_CIM = "Szerződéses feltételek — melléklet"
_ALAP_MELLEKLET_BLOKKOK = [
    {"cim": "Pótmunkák kezelése",
     "szoveg": "Nem látható, később felszínre kerülő munkákra **5 munkanapon** belül részletes "
               "ajánlat készül; a Megrendelőt **3 munkanapos** jóváhagyási határidő illeti meg."},
    {"cim": "Lemondás és kötbér",
     "szoveg": "Díjmentes lemondás a munkakezdés előtti 7. napig; ezen belül **30% bánatpénz**. "
               "Akadályoztatás és késedelem: **110 EUR + áfa/óra**, legfeljebb a vállalkozói díj 5%-a."},
    {"cim": "Munkaterületi feltételek",
     "szoveg": "Tiszta, zárt, huzatmentes terület; térítésmentes víz- és áramvétel; parkolás; "
               "géptárolás; a munkaterület használatból való kizárása a kivitelezés alatt."},
    {"cim": "Aljzatkövetelmények",
     "szoveg": "Nedvességtartalom max. **3–4 súly%** (CM-módszerrel), aljzathőmérséklet min. "
               "**+10 °C**, tapadószilárdság **≥ 1,5 N/mm²** — jegyzőkönyvvel igazolva."},
    {"cim": "Adminisztratív feltételek",
     "szoveg": "Az ár az előzetesen jelzett dokumentációs követelményekre vonatkozik; utólag "
               "közölt többletkövetelmény esetén a Vállalkozót elállási jog illeti meg."},
    {"cim": "Garancia és átadás",
     "szoveg": "Teljesítési igazolás **8 napon** belül; a garancia feltétele az igazolt "
               "aljzatparaméterek megléte. Kiszállási díj felesleges felvonulás esetén: "
               "**500 EUR + áfa**."},
]
_ALAP_MELLEKLET_MEGJEGYZES = (
    "A kiemelt számok a jelenlegi feltételszöveg értékei — a teljes jogi szöveg alcímenként "
    "tagolva, két hasábban követi ezt az összefoglalót."
)


def build_arajanlat_html_v2(adatok: dict, extra: dict = None, oldalak_szama: int = 6) -> str:
    """adatok = a scraper kimenete (Innonest + Pipedrive), extra = a webappos űrlap mezői."""
    extra = extra or {}
    items = adatok.get("tetelek", [])

    # ÁFA-kulcs: az Innonestből kiolvasott összegekből számoljuk, SOSEM a
    # webappos `extra`-ból (ott árral kapcsolatos mező nem is engedélyezett).
    _netto = adatok.get("netto_osszesen") or 0
    _afa   = adatok.get("afa_osszesen") or 0
    afa_kulcs = f"{round(_afa / _netto * 100)}%" if _netto else "27%"

    with open(SABLON_V2, encoding="utf-8") as f:
        html = f.read()

    # ── HTML-blokkok (nem escape-eljük, mert már escape-elt tartalomból épülnek)
    html = html.replace("{{HERO_BLOKK}}", hero_html(extra.get("hero_kep", "")))
    html = html.replace("{{RETEGREND_SOROK}}", retegrend_html(extra.get("retegrend", [])))
    html = html.replace("{{FELTETELEK_KIVONAT}}", feltetelek_html(extra.get("feltetelek_kivonat", [])))
    html = html.replace("{{TETEL_SOROK}}", tetel_sorok_html(items))
    html = html.replace("{{BRUTTO_OSSZESEN}}", brutto_html(fmt_huf(adatok.get("brutto_osszesen", 0))))
    # 4. oldal
    html = html.replace("{{LEPESEK}}", lepesek_html(extra.get("lepesek") or _ALAP_LEPESEK))
    html = html.replace("{{QR_BLOKK}}", qr_html(extra.get("qr_kep", ""), extra.get("qr_felirat", "")))
    # 5. oldal
    html = html.replace("{{REF_FOTO_BLOKK}}", ref_foto_html(extra.get("ref_foto", "")))
    html = html.replace("{{REF_SOROK}}", ref_sorok_html(extra.get("ref_sorok") or _ALAP_REF_SOROK))
    html = html.replace("{{LOGO_RACS}}", logo_racs_html(extra.get("logofal")))
    # 6. oldal
    html = html.replace("{{MELLEKLET_BLOKKOK}}",
                        melleklet_html(extra.get("melleklet_blokkok") or _ALAP_MELLEKLET_BLOKKOK))

    kiemelesek = (extra.get("kiemelesek") or _ALAP_KIEMELESEK) + _ALAP_KIEMELESEK
    ertekek = {}
    for i in range(3):
        ertekek[f"KIEMELES_{i+1}_ERTEK"] = kiemelesek[i].get("ertek", "")
        ertekek[f"KIEMELES_{i+1}_CIMKE"] = kiemelesek[i].get("cimke", "")

    szoveges = {
        # 1. oldal
        "BID_SZAM":            adatok.get("bid_szam", ""),
        "PROJEKT_CIM":         extra.get("projekt_cim") or adatok.get("targya", ""),
        "PROJEKT_ALCIM":       alcim(extra.get("helyszin", ""), fmt_terulet(items),
                                     extra.get("rendszer", "")),
        "UGYFEL_NEV":          adatok.get("ugyfel_nev", ""),
        "UGYFEL_CIM":          adatok.get("ugyfel_cim", ""),
        "UGYFEL_ADOSZAM":      adatok.get("ugyfel_adoszam", ""),
        "KELTEZES":            fmt_datum(adatok.get("keltezes", "")),
        "ERVENYES":            fmt_datum(adatok.get("ervenyes", "")),
        "KAPCSOLAT_NEV":       extra.get("kapcsolat_nev", "") or adatok.get("owner_name", ""),
        "KAPCSOLAT_TEL":       extra.get("kapcsolat_tel", ""),
        "KAPCSOLAT_EMAIL":     extra.get("kapcsolat_email", "") or adatok.get("owner_email", ""),
        "KIEMELES_LABJEGYZET": extra.get("kiemeles_labjegyzet", _ALAP_LABJEGYZET),
        "OLDALAK_SZAMA":       str(oldalak_szama),
        # 2. oldal
        "MUSZAKI_BEKEZDES_1":  extra.get("muszaki_bekezdes_1", ""),
        "MUSZAKI_BEKEZDES_2":  extra.get("muszaki_bekezdes_2", ""),
        "RETEGREND_MEGJEGYZES": extra.get("retegrend_megjegyzes", ""),
        "FELTETELEK_UTALAS":   extra.get("feltetelek_utalas", ""),
        # 3. oldal
        "AFA_KULCS":           afa_kulcs,
        "NETTO_OSSZESEN":      fmt_huf(adatok.get("netto_osszesen", 0)),
        "AFA_OSSZESEN":        fmt_huf(adatok.get("afa_osszesen", 0)),
        "FIZETESI_FELTETEL":   adatok.get("fizetesi_feltetel", ""),
        "FIZETESI_UTEMEZES":   extra.get("fizetesi_utemezes", ""),
        "ARAK_MEGJEGYZES":     extra.get("arak_megjegyzes", "forintban, egészre kerekítve"),
        # 4. oldal
        "KAPCSOLAT_SZEREP":    extra.get("kapcsolat_szerep", _ALAP_KAPCSOLAT_SZEREP),
        "KAPCSOLAT_MONOGRAM":  monogram(extra.get("kapcsolat_nev", "") or adatok.get("owner_name", "")),
        "UTEMEZES_BEKEZDES":   extra.get("utemezes_bekezdes", _ALAP_UTEMEZES_BEKEZDES),
        # 5. oldal
        "REF_CHIP":            extra.get("ref_chip", _ALAP_REF_CHIP),
        "REF_CIM":             extra.get("ref_cim", _ALAP_REF_CIM),
        "LOGOFAL_CIM":         extra.get("logofal_cim", _ALAP_LOGOFAL_CIM),
        "LOGOFAL_MEGJEGYZES":  extra.get("logofal_megjegyzes", _ALAP_LOGOFAL_MEGJEGYZES),
        # 6. oldal
        "MELLEKLET_CIM":        extra.get("melleklet_cim", _ALAP_MELLEKLET_CIM),
        "MELLEKLET_MEGJEGYZES": extra.get("melleklet_megjegyzes", _ALAP_MELLEKLET_MEGJEGYZES),
    }
    szoveges.update(ertekek)

    for kulcs, ertek in szoveges.items():
        html = html.replace("{{" + kulcs + "}}", esc(ertek))
    return html
