"""
ajanlat_b_ai.py — a „B ajánlat adatai" oldal két új funkciója (2026-10)
======================================================================

1. EGY KATTINTÁSOS KITÖLTÉS (AI)
     POST /ajanlat-b-kitoltes           → {ok, job_id}       (X-API-Key)
     GET  /ajanlat-b-kitoltes/<job_id>  → {ok, allapot, ajv2, megjegyzesek, ...}
   A webapp elküldi a projekt adatait (feladatok, mennyiségek, rétegrend,
   árajánlat-sorok, körülmények, Pipedrive deal ID). A szerver kiegészíti a
   Pipedrive-ból (deal, jegyzetek, ügyfél-kapcsolattartó, szervezet, e-mailek,
   nyert dealek a referenciához), majd a Claude API-val kitölteti a B sablon
   szöveges mezőit az „sqm-b-arajanlat-pdf" skill szabályai szerint.
   Háttérben fut (job + polling), mert a Pipedrive + AI együtt 30–60 mp is lehet.

   2026-10-15 (Bálint): NINCSENEK rögzített szövegek és tiltott kifejezések —
   a skill VELUX-szövegei csak MINTÁK; az AI minden mezőt az adott projekthez
   szab (kiemelések, lábjegyzet, lépések, melléklet is). Adat marad: a
   kapcsolattartó telefonja (SQM_KAPCSOLATOK). ÁRAT, TÉTELT az AI sosem ír.
   KÉPEK: hero, referenciafotó és partnerlogók — először Dropbox, ha ott nincs
   megfelelő, a net (lásd ajanlat_b_kepek.py; a fotókat képfelismeréssel választja).

2. TELJES PDF-ELŐNÉZET MEZŐJELÖLÉSSEL
     POST /ajanlat-b-elonezet  → {ok, html}   (X-API-Key)
   A VALÓDI B sablonnal (arajanlat_v2_blokkok.build_arajanlat_html_v2) rendereli
   a webapp jelenlegi mezőit; minden szöveg köré címkét tesz, hogy honnan jön
   (B adatai / alapszöveg / üres / Innonest-Pipedrive). Az árak itt a webapp
   árajánlat-soraiból jönnek (az Innonest-feltöltés is ezekből készül) — a
   végleges PDF-ben az Innonestből.

Telepítés (Railway repó):
  1. ajanlat_b_ai.py a repó gyökerébe
  2. Dockerfile:   COPY ajanlat_b_ai.py .   és   COPY ajanlat_b_kepek.py .
  3. server.py, a többi register_* hívás mellé:
       from ajanlat_b_ai import register_ajanlat_b_routes
       register_ajanlat_b_routes(app)
  Környezeti változók: ANTHROPIC_API_KEY, PIPEDRIVE_API_TOKEN, API_KEY (már
  megvannak); opcionális: B_KITOLTES_MODEL (alapból a SZOVEG_AI_MODEL).
"""

import datetime
import html as htmlmod
import json
import logging
import os
import re
import threading
import time
import uuid

import requests
from flask import jsonify, request

log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
from ajanlat_b_kepek import MODELLEK, modell, claude_json  # noqa: E402  (közös modell-lista és JSON-hívó)
MODEL = modell(None)["id"]   # csak a naplóhoz: az alapértelmezett modell
API_KEY = os.environ.get("API_KEY", "titkos-kulcs")
PIPEDRIVE_API_TOKEN = os.environ.get("PIPEDRIVE_API_TOKEN", "")
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_PD = "https://api.pipedrive.com/v1"
_DROPBOX_MEZO = os.environ.get("PIPEDRIVE_DROPBOX_FIELD_KEY", "8551443f0e9f59d2af653f3df5c12a05b0c432a7")

# ══════════════════════════════════════════════════════════════════════════════
# MINTÁK ÉS ADATOK (az sqm-b-arajanlat-pdf skill alapján)
# ══════════════════════════════════════════════════════════════════════════════
# FONTOS (2026-10-15, Bálint): a skillben szereplő VELUX-szövegek NEM rögzített
# szabályok és nem tiltólisták — az egy konkrét projekthez lettek átszabva. Itt
# MINTAKÉNT szerepelnek: az AI minden mezőt az adott projekthez igazít (ha a
# projekt hétvégi, akkor hétvégi; ha más a kötési idő vagy a hőmérséklet, akkor az).

# SQM-oldali kapcsolattartók: a Pipedrive-ból csak név + e-mail jön, a telefon
# innen (ez ADAT, nem szöveg). A szerep csak javaslat — az AI felülírhatja.
SQM_KAPCSOLATOK = {
    "dudás mária": {"tel": "+36 20 210 6309", "szerep": "projektfelelős, nyugati régió"},
    "gyenes bálint": {"tel": "+36 20 210 6309", "szerep": "ügyvezető, projektfelelős"},
    "szűcs áron": {"tel": "+36 20 264 0237", "szerep": "projektfelelős"},
}

# A webapp alapértelmezett melléklet-blokkjai: a cég kereskedelmi feltételei
# (bánatpénz, díjak, határidők). Az AI ezekből indul ki, és csak azt igazítja,
# amit a projekt indokol (pl. aljzathőmérséklet, RAL/nyomvonal-jóváhagyás).
ALAP_MELLEKLET = [
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

RENDSZER_PROMPT = """Az SQM Hungary Kft. (ipari padlóbevonatok, műgyanta és PU rendszerek, vonalazás, \
padlófelújítás; ügyfelek: nagy ipari cégek) prémium, „B" típusú, 6 oldalas árajánlat-PDF-jének \
SZÖVEGES mezőit töltöd ki magyarul, a megadott projektadatokból. A kitöltést a megadott \
JSON-sémában add vissza (minden mező kötelező; amit nem tudsz kitölteni, legyen üres szöveg / üres lista).

ALAPELV: minden mezőt az ADOTT PROJEKTHEZ szabsz. Az alábbi példák egy korábbi projektből \
(VELUX, csarnokpadló-vonalazás, hétköznapi kivitelezés) valók — szerkezetet és hangvételt mutatnak, \
NEM kötelező szövegek és NEM tiltások. Ha a projekt adatai mást mondanak (pl. hétvégi vagy szakaszos \
kivitelezés, más kötési idő, más aljzathőmérséklet, más vonalszélesség), az adatok szerint írj.

HANGVÉTEL: tárgyilagos, mérnöki, tömör. Nincsenek marketing-jelzők („kiváló", „prémium", \
„tökéletes"). Ne találj ki számot, anyagnevet, garanciát vagy referenciát, ami nincs az adatokban \
vagy nem általános szakmai tény az adott rendszerre. Mennyiség csak az adatokból, magyar \
számformátumban (1 250 m², 340 fm, 12 db). Ne használj **csillagos** jelölést — kivéve a \
melléklet-blokkok szövegében, ahol a **…** félkövér kiemelést jelent (számok, határidők). \
Árat, összeget, egységárat NEM írsz sehova (az árak külön, a rendszerből kerülnek a PDF-be).

MEZŐK (zárójelben a VELUX-példa)
- projektCim: ügyfélbarát cím; a belső projektnév/tárgy ne legyen szó szerint átvéve \
(„Csarnokpadló közlekedési vonalazása").
- helyszin: a TÉNYLEGES munkavégzési hely(ek) — a Pipedrive deal címe / projekt helyszíne; eltérhet \
a számlázási címtől („Fertőd, VELUX gyár").
- rendszer: a fő rendszer neve az adatokból („oldószermentes epoxi vonalfesték").
- kiemelesek: PONTOSAN 3 borító-kártya {ertek, cimke}, a projekt legfontosabb tényeivel: \
1. a fő mennyiség, 2. mikor használható újra a felület, 3. a kivitelezés módja/ütemezése. \
(„1 250 fm" / „közlekedési vonal, RAL 1023"; „12–24 óra" / „alatt visszaadott felület*"; \
„Hétköznapi" / „kivitelezés, a terület egyben történő átadásával").
- kiemelesLabjegyzet: a *-os kártya feltétele, a rendszerhez illően („* +15 °C feletti \
aljzathőmérséklet mellett, gyalogos forgalomra értendő; targoncaforgalom 24 óra után.").
- muszakiBekezdes1: mi készül, milyen rendszerrel, KONKRÉT mennyiségekkel (2–3 mondat).
- muszakiBekezdes2: az ütemezés a projekt szerint: az ügyfél által megadott időszak, hétköznap vagy \
hétvége, egyben vagy szakaszosan átvett terület, mikor használható újra (2–3 mondat).
- retegrend: 2–5 réteg FELÜLRŐL LEFELÉ; az utolsó a meglévő aljzat. {cim, alcim}. Csak az \
adatokban szereplő anyagokból.
- retegrendMegjegyzes: 1 mondat a rétegrendhez/kivitelezéshez fontos kiegészítésről \
(„A vonalak szélessége 10 cm; a pontos nyomvonal a helyszíni bejáráson kerül véglegesítésre.").
- feltetelekKivonat: 4–6 rövid pont a projekthez illő kivitelezési feltételekről (pl. a munkaterület \
átadása, víz/áram, jóváhagyások, aljzathőmérséklet, színkód-egyeztetés, elszámolás módja).
- feltetelekUtalas: 1 mondat, hogy a teljes feltételrendszer a 6. oldali mellékletben van.
- fizetesiUtemezes: a projekthez/ügyfélhez illő ütemezés („Előleg nélkül, egy végszámla az \
átadás-átvételt követően." — nagyvállalatnál tipikus; ha az adatok mást mondanak, azt).
- arakMegjegyzes: rövid megjegyzés az árakról („nettó árak, forintban").
- kapcsolatSzerep: az SQM-kapcsolattartó szerepe (megadott javaslat, ha van).
- lepesek: PONTOSAN 4 lépés {cim, leiras (5–10 szó)} az ajánlattól az átadásig, a projekthez \
igazítva („Helyszíni bejárás", „Megrendelés és időpont — RAL-egyeztetés 2 héttel előtte", \
„Kivitelezés", „Műszaki átadás — jegyzőkönyv, 12–24 óra").
- utemezesBekezdes: 1–2 mondat a következő lépésről („Az ajánlat elfogadását követően 3 munkanapon \
belül egyeztetjük a helyszíni bejárás időpontját.").
- referencia: a megadott NYERT dealek közül a feladathoz LEGJOBBAN illő (azonos munkanem). \
refChip = munkanem-címke, refCim = „<Ügyfél> — <munka>", refSorok = 2–4 adatsor KIZÁRÓLAG a deal \
adataiból (pl. „Mennyiség: 1 200 fm"), referenciaDealId = a deal ID-ja. Ha nincs illő: üresek.
- logofalCim, logofalMegjegyzes: („Velük már együtt dolgoztunk"; „Ipari megrendelőink, akiknél \
<munkanem> munkát végeztünk. A logók a partnerek tulajdonát képezik.").
- logofalCegek: 6 ismert, nagy ügyfél a NYERT dealek közül (lehetőleg azonos munkanemben vagy \
iparágban), mindegyikhez a cég honlapjának domainje, ha ismered (pl. {"nev":"Richter Gedeon", \
"domain":"richter.hu"}); ismeretlen domainnél üres.
- ugyfelDomain: a megrendelő cég honlapjának domainje, ha ismered (különben üres).
- mellekletBlokkok: a megadott ALAP melléklet-blokkokból indulj ki: a kereskedelmi feltételek \
(bánatpénz, díjak, határidők, kötbér) MARADJANAK; csak azt igazítsd, amit a projekt indokol \
(munkaterület átadása, jóváhagyások, aljzathőmérséklet a rendszerhez stb.). 4–8 blokk {cim, szoveg}.
- mellekletMegjegyzes: 1 mondat a mellékletről („Jelen melléklet az ajánlat elválaszthatatlan része; \
a részletes vállalkozási feltételeket a megrendelést követően a szerződés tartalmazza.").
- megjegyzesek: MINDEN adateltérést és bizonytalanságot ide írj Bálintnak, röviden (pl. „Az ügyfél \
5 cm-es vonalat írt, a tétel 10 cm-es", „Két helyszín szerepel", „A deal értéke 0"). Ha nincs, üres lista."""

_SZOVEG = {"type": "string"}
_PAR = lambda a, b: {"type": "object", "properties": {a: _SZOVEG, b: _SZOVEG}, "required": [a, b]}  # noqa: E731
ESZKOZ = {
    "name": "b_ajanlat_mezok",
    "description": "A B árajánlat szöveges mezőinek kitöltése.",
    "input_schema": {
        "type": "object",
        "properties": {
            "projektCim": _SZOVEG, "helyszin": _SZOVEG, "rendszer": _SZOVEG,
            "kiemelesek": {"type": "array", "items": _PAR("ertek", "cimke")},
            "kiemelesLabjegyzet": _SZOVEG,
            "muszakiBekezdes1": _SZOVEG, "muszakiBekezdes2": _SZOVEG,
            "retegrend": {"type": "array", "items": _PAR("cim", "alcim")},
            "retegrendMegjegyzes": _SZOVEG,
            "feltetelekKivonat": {"type": "array", "items": _SZOVEG},
            "feltetelekUtalas": _SZOVEG, "fizetesiUtemezes": _SZOVEG, "arakMegjegyzes": _SZOVEG,
            "kapcsolatSzerep": _SZOVEG,
            "lepesek": {"type": "array", "items": _PAR("cim", "leiras")},
            "utemezesBekezdes": _SZOVEG,
            "refChip": _SZOVEG, "refCim": _SZOVEG, "refSorok": {"type": "array", "items": _SZOVEG},
            "referenciaDealId": {"type": ["integer", "null"]},
            "logofalCim": _SZOVEG, "logofalMegjegyzes": _SZOVEG,
            "logofalCegek": {"type": "array", "items": _PAR("nev", "domain")},
            "ugyfelDomain": _SZOVEG,
            "mellekletBlokkok": {"type": "array", "items": _PAR("cim", "szoveg")},
            "mellekletMegjegyzes": _SZOVEG,
            "megjegyzesek": {"type": "array", "items": _SZOVEG},
        },
        "required": ["projektCim", "helyszin", "rendszer", "kiemelesek", "kiemelesLabjegyzet",
                     "muszakiBekezdes1", "muszakiBekezdes2", "retegrend", "retegrendMegjegyzes",
                     "feltetelekKivonat", "fizetesiUtemezes", "lepesek", "utemezesBekezdes",
                     "refChip", "refCim", "refSorok", "logofalMegjegyzes", "logofalCegek",
                     "mellekletBlokkok", "mellekletMegjegyzes", "megjegyzesek"],
    },
}
# ══════════════════════════════════════════════════════════════════════════════
# PIPEDRIVE
# ══════════════════════════════════════════════════════════════════════════════


def _pd_get(path, **params):
    if not PIPEDRIVE_API_TOKEN:
        return None
    try:
        r = requests.get(_PD + path, params={"api_token": PIPEDRIVE_API_TOKEN, **params}, timeout=12)
        if r.status_code >= 400:
            log.warning(f"[B-AI] Pipedrive {path}: HTTP {r.status_code}")
            return None
        return r.json().get("data")
    except Exception as e:
        log.warning(f"[B-AI] Pipedrive {path}: {e}")
        return None


def _html_szoveg(s, n=1500):
    s = re.sub(r"<br\s*/?>|</p>|</div>", "\n", str(s or ""), flags=re.I)
    s = htmlmod.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s[:n]


_DEAL_MEZOK = {"at": 0, "map": {}}


def _deal_mezonevek():
    """custom field kulcs → név (1 órás cache)."""
    if time.time() - _DEAL_MEZOK["at"] < 3600 and _DEAL_MEZOK["map"]:
        return _DEAL_MEZOK["map"]
    d = _pd_get("/dealFields", limit=500) or []
    _DEAL_MEZOK["map"] = {f.get("key"): f.get("name") for f in d if isinstance(f, dict)}
    _DEAL_MEZOK["at"] = time.time()
    return _DEAL_MEZOK["map"]


def pipedrive_kontextus(deal_id) -> dict:
    """A deal + kapcsolódó adatok tömör, AI-nak szánt összefoglalója. Hiba esetén {}."""
    try:
        deal_id = int(str(deal_id).strip())
    except (TypeError, ValueError):
        return {}
    deal = _pd_get(f"/deals/{deal_id}")
    if not isinstance(deal, dict):
        return {}
    out = {"_deal": deal, "dealId": deal_id, "cim": deal.get("title"), "ertek": deal.get("value"),
           "penznem": deal.get("currency"), "statusz": deal.get("status")}
    owner = deal.get("user_id") if isinstance(deal.get("user_id"), dict) else {}
    out["sqmFelelos"] = {"nev": owner.get("name", ""), "email": owner.get("email", "")}
    # egyedi mezők (pl. a munkavégzés címe, BID) — csak a kitöltött, rövid szövegesek
    nevek = _deal_mezonevek()
    egyeb = {}
    for k, v in deal.items():
        if k == _DROPBOX_MEZO:
            continue          # a Dropbox-mappa linkje a képkereséshez kell, az AI-nak nem
        if len(k) == 40 and re.match(r"^[0-9a-f]{40}", k) and v not in (None, "", [], {}):
            if isinstance(v, (str, int, float)) and len(str(v)) < 300:
                egyeb[nevek.get(k.split("_")[0], k)] = v
    out["egyediMezok"] = egyeb
    person = deal.get("person_id") if isinstance(deal.get("person_id"), dict) else {}
    if person.get("value"):
        p = _pd_get(f"/persons/{person['value']}") or {}
        tel = [x.get("value") for x in (p.get("phone") or []) if isinstance(x, dict) and x.get("value")]
        out["ugyfelKapcsolattarto"] = {"nev": p.get("name", ""), "telefon": tel[:2]}
    org = deal.get("org_id") if isinstance(deal.get("org_id"), dict) else {}
    if org.get("value"):
        o = _pd_get(f"/organizations/{org['value']}") or {}
        web = next((v for k, v in o.items() if isinstance(v, str) and re.match(r"^(https?://)?(www\.)?[a-z0-9-]+\.[a-z.]{2,}(/.*)?$", v.strip(), re.I)
                    and ("web" in k.lower() or len(k) == 40)), "")
        out["szervezet"] = {"nev": o.get("name", ""), "cim": o.get("address", ""), "weboldal": web}
    notes = _pd_get("/notes", deal_id=deal_id, limit=15, sort="add_time DESC") or []
    out["jegyzetek"] = [_html_szoveg(n.get("content"), 1500) for n in notes if isinstance(n, dict)][:15]
    mails = _pd_get(f"/deals/{deal_id}/mailMessages", limit=10) or []
    levelek = []
    for m in mails:
        d = (m or {}).get("data") or {}
        if d.get("subject") or d.get("snippet"):
            levelek.append({"targy": d.get("subject", ""), "reszlet": _html_szoveg(d.get("snippet"), 400)})
    out["levelek"] = levelek[:10]
    return out


def nyert_dealek(n=250) -> list:
    """A referencia kiválasztásához: a legutóbbi nyert dealek (id, cím, ügyfél, érték)."""
    d = _pd_get("/deals", status="won", limit=n, sort="won_time DESC") or []
    out = []
    for x in d:
        if not isinstance(x, dict):
            continue
        org = x.get("org_id") if isinstance(x.get("org_id"), dict) else {}
        out.append({"id": x.get("id"), "cim": x.get("title", ""), "ugyfel": org.get("name", ""),
                    "ev": str(x.get("won_time") or "")[:4]})
    return out


# ══════════════════════════════════════════════════════════════════════════════
# TOKENSZÁMLÁLÓ (2026-10-16) — a webapp a gomb mellett mutatja
# ══════════════════════════════════════════════════════════════════════════════
# Becsült ár USD / millió token (bemenet, kimenet, cache-írás, cache-olvasás): a választott
# modellé (ajanlat_b_kepek.MODELLEK). Felülírható: B_AI_AR="2,10,2.5,0.10" (minden modellre).
def _arak(m):
    env = os.environ.get("B_AI_AR", "")
    if env:
        try:
            a = [float(x) for x in env.split(",")]
            if len(a) == 4:
                return a
        except ValueError:
            pass
    return list(m["ar"])


class TokenSzamlalo:
    """Az Anthropic-válaszok `usage` mezőit gyűjti, lépésenként (szöveg / képfelismerés)."""
    MEZOK = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

    def __init__(self, on_valtozas=None, modell_kulcs=None):
        self.lepesek = {}
        self.on_valtozas = on_valtozas
        self.modell = modell(modell_kulcs)

    def __call__(self, usage, lepes):
        if not isinstance(usage, dict):
            return
        sor = self.lepesek.setdefault(lepes, {m: 0 for m in self.MEZOK} | {"hivasok": 0})
        for m in self.MEZOK:
            try:
                sor[m] += int(usage.get(m) or 0)
            except (TypeError, ValueError):
                pass
        sor["hivasok"] += 1
        if self.on_valtozas:
            self.on_valtozas(self.osszesito())

    def osszesito(self) -> dict:
        ar = _arak(self.modell)
        ossz = {m: sum(l[m] for l in self.lepesek.values()) for m in self.MEZOK}
        usd = (ossz["input_tokens"] * ar[0] + ossz["output_tokens"] * ar[1]
               + ossz["cache_creation_input_tokens"] * ar[2] + ossz["cache_read_input_tokens"] * ar[3]) / 1e6
        return {
            "osszes": sum(ossz.values()),
            "bemenet": ossz["input_tokens"] + ossz["cache_creation_input_tokens"] + ossz["cache_read_input_tokens"],
            "kimenet": ossz["output_tokens"],
            "cacheOlvasas": ossz["cache_read_input_tokens"],
            "lepesek": {k: {"osszes": sum(v[m] for m in self.MEZOK), "kimenet": v["output_tokens"],
                            "hivasok": v["hivasok"]} for k, v in self.lepesek.items()},
            "becsultUsd": round(usd, 4),
            "modell": self.modell["nev"],
            "modellId": self.modell["id"],
        }


# ══════════════════════════════════════════════════════════════════════════════
# AI-HÍVÁS
# ══════════════════════════════════════════════════════════════════════════════

def _claude_eszkoz(uzenet, szamlalo):
    """A szöveges kitöltés (strukturált JSON-kimenettel, a választott modellel)."""
    return claude_json(szamlalo.modell["id"], RENDSZER_PROMPT, uzenet, ESZKOZ["input_schema"],
                       effort="medium", max_tokens=24000, timeout=280, szamlalo=szamlalo, lepes="szoveg")


def _str(x, n=600):
    return str(x if x is not None else "").strip()[:n]


def _parok(lista, a, b, max_db, na=200, nb=600):
    out = []
    for x in lista or []:
        if isinstance(x, dict) and (_str(x.get(a)) or _str(x.get(b))):
            out.append({a: _str(x.get(a), na), b: _str(x.get(b), nb)})
    return out[:max_db]


def ajv2_osszeallit(ki: dict, kapcsolat: dict) -> dict:
    """Az AI-kimenet → a webapp ajv2 mezői (camelCase). Üres mezőt nem adunk vissza
    (a webapp meglévő értéke marad)."""
    out = {
        "projektCim": _str(ki.get("projektCim"), 160),
        "helyszin": _str(ki.get("helyszin"), 160),
        "rendszer": _str(ki.get("rendszer"), 120),
        "kiemelesek": _parok(ki.get("kiemelesek"), "ertek", "cimke", 3, 30, 90),
        "kiemelesLabjegyzet": _str(ki.get("kiemelesLabjegyzet"), 300),
        "muszakiBekezdes1": _str(ki.get("muszakiBekezdes1"), 900),
        "muszakiBekezdes2": _str(ki.get("muszakiBekezdes2"), 900),
        "retegrend": _parok(ki.get("retegrend"), "cim", "alcim", 5, 120, 200),
        "retegrendMegjegyzes": _str(ki.get("retegrendMegjegyzes"), 300),
        "feltetelekKivonat": [_str(s, 220) for s in (ki.get("feltetelekKivonat") or []) if _str(s)][:6],
        "feltetelekUtalas": _str(ki.get("feltetelekUtalas"), 200),
        "fizetesiUtemezes": _str(ki.get("fizetesiUtemezes"), 160),
        "arakMegjegyzes": _str(ki.get("arakMegjegyzes"), 120),
        "kapcsolatSzerep": _str(ki.get("kapcsolatSzerep"), 80) or kapcsolat.get("szerep", ""),
        "lepesek": _parok(ki.get("lepesek"), "cim", "leiras", 4, 60, 140),
        "utemezesBekezdes": _str(ki.get("utemezesBekezdes"), 400),
        "refChip": _str(ki.get("refChip"), 40),
        "refCim": _str(ki.get("refCim"), 140),
        "refSorok": [_str(s, 160) for s in (ki.get("refSorok") or []) if _str(s)][:4],
        "logofalCim": _str(ki.get("logofalCim"), 80),
        "logofalMegjegyzes": _str(ki.get("logofalMegjegyzes"), 300),
        "mellekletBlokkok": _parok(ki.get("mellekletBlokkok"), "cim", "szoveg", 8, 80, 700),
        "mellekletMegjegyzes": _str(ki.get("mellekletMegjegyzes"), 300),
        "kapcsolatTel": kapcsolat.get("tel", ""),
    }
    if len(out["retegrend"]) < 2:
        out["retegrend"] = []
    if len(out["lepesek"]) < 2:
        out["lepesek"] = []
    return {k: v for k, v in out.items() if v not in ("", [], None)}


def sqm_kapcsolat(nev: str) -> dict:
    return SQM_KAPCSOLATOK.get(str(nev or "").strip().lower(), {})


def kitoltes(be: dict, allapot=None, szamlalo=None) -> dict:
    """A teljes kitöltés (szinkron; a job-szál hívja). `allapot(szoveg)`: haladásjelző,
    `szamlalo`: TokenSzamlalo (az AI-hívások tokenjeihez)."""
    jelez = allapot or (lambda _s: None)
    szamlalo = szamlalo or TokenSzamlalo(modell_kulcs=be.get("modell"))
    jelez("Pipedrive-adatok…")
    pd = pipedrive_kontextus(be.get("dealId")) if be.get("dealId") else {}
    dealek = nyert_dealek() if PIPEDRIVE_API_TOKEN else []
    felelos = (pd.get("sqmFelelos") or {}).get("nev") or ""
    kapcs = sqm_kapcsolat(felelos)
    adat = {
        "projekt": be.get("projekt") or {},
        "feladatok": (be.get("feladatok") or [])[:40],
        "arajanlatSorok": (be.get("tetelek") or [])[:60],
        "korulmenyek": (be.get("korulmenyek") or [])[:20],
        "pipedrive": {k: v for k, v in pd.items() if k != "_deal"} if pd
        else "nincs Pipedrive-kapcsolat (deal ID hiányzik vagy nem elérhető)",
        "sqmKapcsolattartoJavaslat": {"nev": felelos, **kapcs} if felelos else None,
        "alapMellekletBlokkok": ALAP_MELLEKLET,
        "nyertDealekReferenciahoz": dealek,
    }
    uzenet = ("A projekt adatai (JSON). Töltsd ki a B árajánlat szöveges mezőit, az adott projekthez szabva.\n\n"
              + json.dumps(adat, ensure_ascii=False, default=str)[:70000])
    jelez(f"Szövegek írása ({szamlalo.modell['nev']})…")
    ki = _claude_eszkoz(uzenet, szamlalo)

    megj = [_str(m, 400) for m in (ki.get("megjegyzesek") or []) if _str(m)]
    if not pd and be.get("dealId"):
        megj.append("A Pipedrive-adatokat nem sikerült lekérni — a kitöltés csak a webapp adataiból készült.")
    if not be.get("dealId"):
        megj.append("A projekthez nincs Pipedrive deal rendelve — helyszín, kapcsolattartó és jegyzetek nélkül készült.")
    if felelos and not kapcs:
        megj.append(f"A deal felelőse ({felelos}) telefonszáma nincs a listában — add meg kézzel.")
    ref_deal = next((d for d in dealek if d.get("id") == ki.get("referenciaDealId")), None)

    kepek = {"hero": "", "refFoto": "", "logok": [], "forrasok": {}}
    if be.get("kepek", True):
        jelez("Képek keresése (Dropbox, net)…")
        try:
            from ajanlat_b_kepek import kepek_beszerzese
            org = pd.get("szervezet") or {}
            munka = " · ".join(filter(None, [_str(ki.get("projektCim")), _str(ki.get("rendszer"))]))
            kepek = kepek_beszerzese(pd.get("_deal") or {}, org.get("nev") or (be.get("projekt") or {}).get("ugyfel", ""),
                                     _str(ki.get("ugyfelDomain")) or org.get("weboldal", ""), ref_deal,
                                     ki.get("logofalCegek") or [], munka, szamlalo=szamlalo)
            megj += kepek.pop("megjegyzesek", [])
        except Exception as e:
            log.exception("[B-AI] képbeszerzési hiba")
            megj.append(f"A képeket nem sikerült beszerezni ({str(e)[:120]}) — töltsd fel kézzel.")
    return {
        "ajv2": ajv2_osszeallit(ki, kapcs),
        "kepek": {k: kepek.get(k) for k in ("hero", "refFoto", "logok") if kepek.get(k)},
        "kepForrasok": kepek.get("forrasok") or {},
        "megjegyzesek": megj,
        "referenciaDeal": ref_deal,
        "felelos": felelos,
        "tokenek": szamlalo.osszesito(),
    }
_jobok = {}
_jobok_lock = threading.Lock()


def _job(job_id, **kw):
    with _jobok_lock:
        _jobok.setdefault(job_id, {"at": time.time()}).update(kw)
        regi = [k for k, v in _jobok.items() if time.time() - v.get("at", 0) > 3600]
        for k in regi:
            _jobok.pop(k, None)


def _job_futtat(job_id, be):
    try:
        _job(job_id, allapot="fut")
        szamlalo = TokenSzamlalo(lambda o: _job(job_id, tokenek=o), be.get("modell"))
        _job(job_id, allapot="kesz", eredmeny=kitoltes(be, lambda t: _job(job_id, lepes=t), szamlalo))
    except Exception as e:
        log.exception("[B-AI] kitöltési hiba")
        _job(job_id, allapot="hiba", hiba=str(e)[:300])


# ══════════════════════════════════════════════════════════════════════════════
# ELŐNÉZET MEZŐJELÖLÉSSEL
# ══════════════════════════════════════════════════════════════════════════════

# Privát Unicode-jelek: átmennek az escape-en és a sablon-kitöltésen, utána
# <span>-ra cseréljük őket. Formátum: kategória|kulcs érték 
_JE, _JK, _JV = "", "", ""

# Python-mezőnév → felirat az előnézetben (a webapp a camelCase alakra ugrik)
CIMKEK = {
    "helyszin": "Helyszín", "rendszer": "Rendszer", "projekt_cim": "Projektcím",
    "kapcsolat_tel": "Kapcsolattartó telefon", "kapcsolat_szerep": "Kapcsolattartó szerepe",
    "kiemelesek": "Kiemelések", "kiemeles_labjegyzet": "Kiemelés lábjegyzet",
    "muszaki_bekezdes_1": "Műszaki 1. bekezdés", "muszaki_bekezdes_2": "Műszaki 2. bekezdés",
    "retegrend": "Rétegrend", "retegrend_megjegyzes": "Rétegrend megjegyzés",
    "feltetelek_kivonat": "Feltételek kivonata", "feltetelek_utalas": "Utalás a mellékletre",
    "fizetesi_utemezes": "Fizetési ütemezés", "arak_megjegyzes": "Az árakról",
    "lepesek": "Lépések", "utemezes_bekezdes": "Záró bekezdés",
    "ref_chip": "Referencia címke", "ref_cim": "Referencia címe", "ref_sorok": "Adatsorok",
    "logofal_cim": "Logófal címe", "logofal_megjegyzes": "Logófal megjegyzés",
    "melleklet_cim": "Melléklet címe", "melleklet_blokkok": "Melléklet blokkok",
    "melleklet_megjegyzes": "Melléklet záró megjegyzés",
}
# Ha a mező hiányzik a webapp kéréséből, a Python-sablon EZT az alapértéket teszi be
# (az arajanlat_v2_blokkok konstansaiból) — az előnézet ugyanezt jelöli „alapszöveg"-ként.
_PY_ALAP = {
    "kiemelesek": "_ALAP_KIEMELESEK", "kiemeles_labjegyzet": "_ALAP_LABJEGYZET",
    "lepesek": "_ALAP_LEPESEK", "utemezes_bekezdes": "_ALAP_UTEMEZES_BEKEZDES",
    "kapcsolat_szerep": "_ALAP_KAPCSOLAT_SZEREP", "ref_chip": "_ALAP_REF_CHIP", "ref_cim": "_ALAP_REF_CIM",
    "ref_sorok": "_ALAP_REF_SOROK", "logofal_cim": "_ALAP_LOGOFAL_CIM",
    "logofal_megjegyzes": "_ALAP_LOGOFAL_MEGJEGYZES", "melleklet_cim": "_ALAP_MELLEKLET_CIM",
    "melleklet_blokkok": "_ALAP_MELLEKLET_BLOKKOK", "melleklet_megjegyzes": "_ALAP_MELLEKLET_MEGJEGYZES",
}
_PY_ALAP_LITERAL = {"arak_megjegyzes": "forintban, egészre kerekítve"}
_LISTA_MEZOK = {"kiemelesek", "retegrend", "feltetelek_kivonat", "lepesek", "ref_sorok", "melleklet_blokkok"}
_KEP_MEZOK = {"hero_kep", "ref_foto", "logofal", "qr_kep"}


def _jel(kat, kulcs, v):
    if isinstance(v, str):
        return f"{_JE}{kat}|{kulcs}{_JK}{v}{_JV}" if v.strip() else v
    if isinstance(v, list):
        return [_jel(kat, kulcs, x) for x in v]
    if isinstance(v, dict):
        return {k: _jel(kat, kulcs, x) for k, x in v.items()}
    return v


def _ures_jel(kulcs):
    return f"{_JE}ures|{kulcs}{_JK}{_JV}"


def jelolt_extra(extra: dict, mezok: list, alap_mezok: list, v2) -> dict:
    """A webapp extra-ját jelölőkkel látja el. `mezok`: a webapp által kezelt összes
    (Python-nevű) szöveges mező; `alap_mezok`: amelyek a webapp alapértékén állnak."""
    ki = {}
    for k, v in (extra or {}).items():
        if k in _KEP_MEZOK:
            ki[k] = v
            continue
        ki[k] = _jel("alap" if k in alap_mezok else "webapp", k, v)
    for k in mezok or []:
        if k in ki or k in _KEP_MEZOK or k not in CIMKEK:
            continue
        if k == "projekt_cim":
            continue      # üresen az Innonest tárgya megy (az adatokban jelölve)
        nev = _PY_ALAP.get(k)
        alap = getattr(v2, nev, None) if nev else _PY_ALAP_LITERAL.get(k)
        if alap:
            ki[k] = _jel("sablon", k, alap)
        elif k == "retegrend":
            ki[k] = [{"cim": "", "alcim": _ures_jel(k)}]
        elif k in _LISTA_MEZOK:
            ki[k] = [_ures_jel(k)]
        else:
            ki[k] = _ures_jel(k)
    return ki


def _fmt_datum(d):
    m = re.match(r"^\s*(\d{4})[.\-/ ]+(\d{1,2})[.\-/ ]+(\d{1,2})", str(d or ""))
    return f"{m.group(1)}. {int(m.group(2)):02d}. {int(m.group(3)):02d}." if m else str(d or "")


def elonezet_adatok(a: dict, pd: dict) -> dict:
    """A webapp adataiból „Innonest-szerű" adatok (az előnézethez)."""
    tetelek = []
    netto = 0.0
    for t in (a.get("tetelek") or [])[:80]:
        if not isinstance(t, dict):
            continue
        menny = t.get("menny") or 0
        try:
            menny_f = float(menny)
        except (TypeError, ValueError):
            menny_f = 0.0
        ear = float(t.get("egysegar") or 0)
        ossz = round(menny_f * ear)
        netto += ossz
        egys = str(t.get("egyseg") or "").strip()
        menny_sz = (f"{menny_f:,.2f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",") + (" " + egys if egys else ""))
        tetelek.append({"megnevezes": str(t.get("nev") or ""), "mennyiseg": menny_sz, "egyseg": egys,
                        "egysegar": ear, "osszesen": ossz, "leiras": str(t.get("leiras") or ""),
                        "is_text_block": False})
    afa = round(netto * 0.27)
    felelos = (pd or {}).get("sqmFelelos") or {}
    ma = datetime.date.today()
    jel_auto = lambda k, v: _jel("auto", k, v) if str(v or "").strip() else _ures_jel(k)  # noqa: E731
    return {
        "bid_szam": jel_auto("bid", a.get("bid") or "BID-…"),
        "targya": _jel("auto", "targya", a.get("targya") or ""),
        "ugyfel_nev": jel_auto("ugyfel_nev", a.get("ugyfelNev")),
        "ugyfel_cim": jel_auto("ugyfel_cim", a.get("ugyfelCim")),
        "ugyfel_adoszam": jel_auto("ugyfel_adoszam", a.get("adoszam")),
        "keltezes": _jel("auto", "keltezes", _fmt_datum(ma.isoformat())),
        # 2026-10-16: az előnézet úgy nézzen ki, ahogy az ügyfél látja — nincs „(Innonestből)"
        # helykitöltő; minta-érték megy (a címke mondja meg, hogy a PDF-ben az Innonestből jön)
        "ervenyes": _jel("auto", "ervenyes", _fmt_datum((ma + datetime.timedelta(days=30)).isoformat())),
        "fizetesi_feltetel": _jel("auto", "fizetesi_feltetel", "Átutalás"),
        "owner_name": felelos.get("nev") or "",
        "owner_email": felelos.get("email") or "",
        "tetelek": tetelek,
        "netto_osszesen": netto, "afa_osszesen": afa, "brutto_osszesen": netto + afa,
    }


AUTO_CIMKEK = {
    "bid": "BID (Innonest)", "targya": "Innonest tárgy = projekt neve", "ugyfel_nev": "Ügyfél neve (Innonest)",
    "ugyfel_cim": "Ügyfél címe (Innonest)", "ugyfel_adoszam": "Adószám (Innonest)",
    "keltezes": "Keltezés (Innonest; itt: mai nap)", "ervenyes": "Érvényesség (Innonest; itt: minta +30 nap)",
    "fizetesi_feltetel": "Fizetés módja (Innonest; itt: minta)",
}

_ELONEZET_CSS = """
<style id="av2-jelolo">
body{background:#e9e8e4}
.jel{position:relative;border-radius:2px;outline-offset:1px;cursor:pointer}
.jel-webapp{background:rgba(37,99,235,.10);outline:1.5px solid rgba(37,99,235,.55)}
.jel-alap{background:rgba(217,119,6,.12);outline:1.5px dashed rgba(217,119,6,.7)}
.jel-sablon{background:rgba(217,119,6,.12);outline:1.5px dashed rgba(217,119,6,.7)}
.jel-auto{background:rgba(5,150,105,.10);outline:1.5px solid rgba(5,150,105,.45)}
.jel-ures{display:inline-block;min-width:60px;min-height:1em;background:rgba(220,38,38,.10);outline:1.5px dashed rgba(220,38,38,.7)}
.jel-ures::after{content:"(üres)";color:#b91c1c;font-size:9px;font-family:Arial,sans-serif}
.jel::before{content:attr(data-cimke);position:absolute;left:-1px;top:-13px;white-space:nowrap;font:600 8px/11px Arial,sans-serif;
  letter-spacing:0;text-transform:none;color:#fff;padding:0 4px;border-radius:2px;z-index:5;pointer-events:none}
.jel-webapp::before{background:#2563eb}.jel-alap::before,.jel-sablon::before{background:#d97706}
.jel-auto::before{background:#059669}.jel-ures::before{background:#dc2626}
.jel-terulet{position:relative;outline:2px dashed rgba(5,150,105,.55);outline-offset:2px}
.jel-terulet.kep{outline-color:rgba(37,99,235,.6)}
.jel-cimke{position:absolute;right:4px;top:4px;font:600 8px/11px Arial,sans-serif;color:#fff;background:#059669;padding:0 4px;
  border-radius:2px;z-index:6;white-space:nowrap;cursor:pointer}
.jel-terulet.kep>.jel-cimke{background:#2563eb}
body.cimkek-ki .jel::before,body.cimkek-ki .jel-cimke{display:none}
body.cimkek-ki .jel,body.cimkek-ki .jel-terulet{outline:none!important;background:none!important}
body.cimkek-ki .jel-ures::after{content:""}
.jel.villan{animation:villan 1s 2}
@keyframes villan{50%{box-shadow:0 0 0 4px rgba(37,99,235,.5)}}
</style>
"""

_ELONEZET_JS = """
<script>
(function(){
  function teruletJelol(sel, felirat, mezo, kep){
    document.querySelectorAll(sel).forEach(function(el){
      if(el.querySelector(':scope > .jel-cimke')) return;
      el.classList.add('jel-terulet'); if(kep) el.classList.add('kep');
      var c=document.createElement('span'); c.className='jel-cimke'; c.textContent=felirat;
      if(mezo) c.setAttribute('data-mezo', mezo);
      if(getComputedStyle(el).position==='static') el.style.position='relative';
      el.appendChild(c);
    });
  }
  teruletJelol('.hero','Hero fotó (B adatai)','heroKep',true);
  teruletJelol('.ref-foto','Referenciafotó (B adatai)','refFoto',true);
  teruletJelol('.logo-racs','Partnerlogók (B adatai)','logofal',true);
  teruletJelol('.tbl','Tételek — Innonest (itt: a webapp árajánlat-sorai)','',false);
  teruletJelol('.totals','Összegek — Innonest (itt: a webapp alapján, 27% áfa)','',false);
  teruletJelol('.kapcs-adat','Kapcsolattartó neve/e-mail — Pipedrive deal felelőse','',false);
  document.addEventListener('click', function(e){
    var el=e.target.closest('[data-mezo]'); if(!el) return;
    var m=el.getAttribute('data-mezo'); if(!m) return;
    try{ parent.postMessage({type:'av2-ugras', mezo:m}, '*'); }catch(_){}
  });
  window.addEventListener('message', function(e){
    var d=e.data||{}; if(d.type==='av2-cimkek') document.body.classList.toggle('cimkek-ki', !d.be);
  });
})();
</script>
"""


def _camel(k):
    return re.sub(r"_([a-z0-9])", lambda m: m.group(1).upper(), k)


def jelolok_cserei(html: str) -> str:
    def csere(m):
        kat, kulcs, ertek = m.group(1), m.group(2), m.group(3)
        if kat == "auto":
            cimke = AUTO_CIMKEK.get(kulcs, kulcs)
            return f'<span class="jel jel-auto" data-cimke="{cimke}">{ertek}</span>'
        felirat = CIMKEK.get(kulcs, kulcs)
        felirat += {"webapp": "", "alap": " · alapszöveg", "sablon": " · sablon alapszöveg", "ures": ""}.get(kat, "")
        return (f'<span class="jel jel-{kat}" data-cimke="{htmlmod.escape(felirat)}" '
                f'data-mezo="{_camel(kulcs)}">{ertek}</span>')
    # a jelek egymásba nem ágyazódnak; a nem lezárt maradékot eltakarítjuk
    html = re.sub(f"{_JE}([a-z]+)\\|([a-z0-9_]+){_JK}(.*?){_JV}", csere, html, flags=re.S)
    html = re.sub(f"[{_JE}{_JK}{_JV}]", "", html)
    # <title>-ben nem lehet span
    html = re.sub(r"<title>.*?</title>", lambda m: re.sub(r"<[^>]+>", "", m.group(0)[7:-8]).join(("<title>", "</title>")), html, flags=re.S)
    return html


def elonezet(be: dict) -> str:
    import arajanlat_v2_blokkok as v2
    pd = pipedrive_kontextus(be.get("dealId")) if be.get("dealId") else {}
    adatok = elonezet_adatok(be.get("adatok") or {}, pd)
    extra = be.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    x = jelolt_extra(extra, be.get("mezok") or list(CIMKEK.keys()), be.get("alapMezok") or [], v2)
    html = v2.build_arajanlat_html_v2(adatok, x)
    html = jelolok_cserei(html)
    html = html.replace("</head>", _ELONEZET_CSS + "</head>", 1).replace("</body>", _ELONEZET_JS + "</body>", 1)
    return html


# ══════════════════════════════════════════════════════════════════════════════
# VÉGPONTOK
# ══════════════════════════════════════════════════════════════════════════════

def register_ajanlat_b_routes(app):
    """Hívd meg a server.py-ból: register_ajanlat_b_routes(app)"""

    def _auth():
        return request.headers.get("X-API-Key") == API_KEY

    @app.route("/ajanlat-b-kitoltes", methods=["POST"])
    def ajanlat_b_kitoltes():
        if not _auth():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        if not ANTHROPIC_API_KEY:
            return jsonify({"ok": False, "error": "Az AI nincs bekötve (hiányzik az ANTHROPIC_API_KEY)."}), 503
        be = request.get_json(silent=True) or {}
        if not (be.get("feladatok") or be.get("tetelek")):
            return jsonify({"ok": False, "error": "A projektben nincs feladat vagy árajánlat-sor — nincs miből kitölteni."}), 400
        job_id = str(uuid.uuid4())
        _job(job_id, allapot="indul")
        threading.Thread(target=_job_futtat, args=(job_id, be), daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id})

    @app.route("/ajanlat-b-kitoltes/<job_id>", methods=["GET"])
    def ajanlat_b_kitoltes_allapot(job_id):
        if not _auth():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        with _jobok_lock:
            j = dict(_jobok.get(job_id) or {})
        if not j:
            return jsonify({"ok": False, "error": "Ismeretlen feladat (a szerver újraindulhatott) — indítsd újra."}), 404
        out = {"ok": True, "allapot": j.get("allapot"), "lepes": j.get("lepes", ""), "tokenek": j.get("tokenek")}
        if j.get("allapot") == "kesz":
            out.update(j.get("eredmeny") or {})
        if j.get("allapot") == "hiba":
            out["error"] = j.get("hiba")
        return jsonify(out)

    @app.route("/ajanlat-b-elonezet", methods=["POST"])
    def ajanlat_b_elonezet():
        if not _auth():
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        be = request.get_json(silent=True) or {}
        try:
            return jsonify({"ok": True, "html": elonezet(be)})
        except Exception as e:
            log.exception("[B-AI] előnézeti hiba")
            return jsonify({"ok": False, "error": f"Az előnézet nem készült el: {e}"}), 500

    log.info(f"[B-AI] Végpontok: /ajanlat-b-kitoltes, /ajanlat-b-elonezet (modell: {MODEL}, "
             f"AI-kulcs: {'van' if ANTHROPIC_API_KEY else 'NINCS'}, Pipedrive: {'van' if PIPEDRIVE_API_TOKEN else 'NINCS'})")
