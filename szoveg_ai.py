"""
szoveg_ai.py – AI árajánlat-szövegezés a webapp Szövegtárához (2026-09-29)
==========================================================================
A webapp (Árajánlat tételei, B ajánlat blokkjai) ezt a végpontot hívja:

  POST /szoveg-javaslat      X-API-Key header kötelező (ugyanaz az API_KEY,
                             mint a többi végpontnál)

Két mód:
  "uj"        – új szöveg egy tételhez / blokkhoz a projekt adataiból
  "igazitas"  – egy mentett (jóváhagyott) szöveg átírása az aktuális
                projekthez: szerkezet és hangvétel marad, a konkrétumok
                (mennyiség, körülmények, rétegrend) cserélődnek

A Claude API-t közvetlenül, `requests`-szel hívja (nem kell új Python-csomag).
A rendszer-prompt: a jóváhagyott mintaszövegek (sablonok/szoveg_mintak.md,
prompt cache-sel) + a webappban szerkeszthető stílusleírás (a kérés „stilus"
mezője; tartalék: sablonok/szoveg_stilus.md).

Környezeti változók (Railway → Variables):
  ANTHROPIC_API_KEY   – KÖTELEZŐ, a Claude API kulcs
  SZOVEG_AI_MODEL     – opcionális, alapértelmezés: claude-sonnet-5
  API_KEY             – már létezik (server.py is ezt használja)

Telepítés:
  1. szoveg_ai.py a repó gyökerébe; a sablonok/ mappába: szoveg_mintak.md és
     szoveg_stilus.md (a régi szoveg_tudasbazis.md törölhető)
  2. Dockerfile: a többi COPY sor mellé:   COPY szoveg_ai.py .
  3. server.py: a többi register_* hívás mellé:
       from szoveg_ai import register_szoveg_ai_routes
       register_szoveg_ai_routes(app)
  4. Railway → Variables: ANTHROPIC_API_KEY
"""

import os
import logging

import requests
from flask import request, jsonify

log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SZOVEG_AI_MODEL   = os.environ.get("SZOVEG_AI_MODEL", "claude-sonnet-5")
API_KEY           = os.environ.get("API_KEY", "titkos-kulcs")
_ANTHROPIC_URL    = "https://api.anthropic.com/v1/messages"

# 2026-09-29: a tudásbázis KÉT részre vált:
#   • STÍLUSLEÍRÁS — a webappban szerkeszthető (Szövegtár → AI stílusleírás,
#     Firestore: katalogus/szoveg_stilus); a webapp minden kérésben elküldi
#     („stilus" mező). Ha nem küldi, a sablonok/szoveg_stilus.md a tartalék.
#   • MINTASZÖVEGEK — itt, a GitHubon: sablonok/szoveg_mintak.md (a
#     felhasználó nem tervezi szerkeszteni). Ez a nagyobb rész, ezt cache-eljük.
# A `sablonok/` mappát a Dockerfile egészében másolja — nem kell külön COPY sor.
_SABLONOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sablonok")


def _olvas(nev: str) -> str:
    try:
        with open(os.path.join(_SABLONOK, nev), encoding="utf-8") as f:
            return f.read()
    except OSError:
        log.warning(f"[SZOVEG-AI] Nem található: sablonok/{nev}")
        return ""


_MINTAK = _olvas("szoveg_mintak.md")
_STILUS_TARTALEK = _olvas("szoveg_stilus.md")
_STILUS_MAX = 20000

_SZEREP = (
    "Az SQM Hungary Kft. (ipari padlóbevonatok, műgyanta és PU rendszerek, "
    "padlófelújítás) árajánlat-szövegeit írod magyarul. Lent egy STÍLUSLEÍRÁS "
    "(hangvétel, szókincs, kategóriánkénti felépítés — ez a mérvadó szabály) és "
    "jóváhagyott MINTASZÖVEGEK vannak. A mintákhoz igazodj hangvételben, de ne "
    "másold őket szó szerint (a „VERBATIM” jelölés a gyűjtemény belső jelölése, "
    "nem neked szól).\n\n"
    "SZIGORÚ KIMENETI SZABÁLY: kizárólag a kész szöveget add vissza — nincs "
    "bevezetés, nincs magyarázat, nincs cím, nincs idézőjel a szöveg körül, "
    "nincs felsorolás (ha nem kérik). Folyó szöveg. Ne találj ki számokat, "
    "anyagneveket vagy garanciát, ami nincs a megadott adatokban."
)

_MAX = {"kulcsszavak": 600, "alapSzoveg": 4000, "jegyzet": 1200, "minta": 2500}

# 2026-09-29 — HANGOLÁS: élesben a „rövid TERC-sor" és a „részletes" beállítás
# szinte ugyanolyan hosszú (4 mondatos) szöveget adott. Okok: (1) a hossz csak
# egy sor volt a sok kontextus között, (2) a tudásbázis kategóriánkénti
# felépítési mintája (pl. anyagköltség: jelleg → rendszer → mit biztosít →
# zárás) eleve 4 mondatot „kér", (3) a mellékelt minták hosszú, részletes
# szövegek voltak, és a modell a hosszukat is utánozta, (4) a modell minden
# kapott adatot (páratartalom, tárcsa, m²) bele akart írni. Javítás: a rövid
# módnak saját, szigorú szabálya és saját (valódi SQM TERC-) példái vannak,
# a hosszú mintákat rövid módban nem küldjük, a kimenet hosszát a szerver
# ellenőrzi, és ha túl hosszú, egyszer rövidíttet.
_ROVID_MAX_SZO = 45
_ROVID_TURES_SZO = 60          # e fölött egyszer újrarövidíttetjük
_ROVID_MINTA_MAX_KAR = 350     # ennél hosszabb mentett mintát rövid módban nem küldünk

_ROVID_SZABALY = (
    "HOSSZ — SZIGORÚ: rövid TERC-tételsor. Legfeljebb 2 mondat, összesen legfeljebb "
    f"{_ROVID_MAX_SZO} szó. Költségvetési tételsor-szerkezet: mit, mivel, milyen technológiai "
    "lépésekkel — névszói, tömör megfogalmazás (pl. „… kivitelezése …, …-val, majd …-val.”). "
    "NE indokolj, NE sorolj anyagtulajdonságokat vagy előnyöket, NE írj „ez hozzájárul…”, "
    "„ezáltal…” típusú magyarázó mondatot, és a projekt-körülményeket (páratartalom, "
    "hőmérséklet stb.) NE említsd. Ilyenkor a tudásbázis kategóriánkénti felépítési mintáját "
    "NE kövesd — az a részletes leírásra vonatkozik.\n\n"
    "Valódi SQM TERC-példák a kívánt hosszra és szerkezetre:\n"
    "- Repedés javítása, a repedés feltárásával, gyantahabarccsal történő kitöltésével, majd "
    "kötést követő síkba csiszolással.\n"
    "- Olajszennyezett betonfelület feltárása a meglévő gyantaréteg eltávolításával, a szennyezett "
    "betonfelület HVP O olajeltávolító vegyszeres tisztításával, majd HVP O alapozógyantával "
    "történő alapozásával.\n"
    "- 3 rétegű műgyanta bevonatrendszer kivitelezése, a felület előkészítését követően alapozó, "
    "közbenső és fedőréteg technológiai sorrend szerinti felhordásával."
)

_RESZLETES_SZABALY = (
    "HOSSZ: részletes leírás, 3–6 mondat, a tudásbázis kategóriánkénti felépítési mintája szerint."
)


def _szoszam(s: str) -> int:
    return len((s or "").split())


def _vag(s, n):
    s = (s or "").strip() if isinstance(s, str) else ""
    return s[:n]


def _uzenet(d: dict) -> str:
    """A felhasználói üzenet összeállítása a webapp kéréséből."""
    mod        = d.get("mod") if d.get("mod") in ("uj", "igazitas") else "uj"
    kategoria  = _vag(d.get("kategoria"), 60) or "Műszaki megjegyzés"
    hossz      = "rovid" if d.get("hossz") == "rovid" else "reszletes"
    tetel      = d.get("tetel") or {}
    tetel_nev  = _vag(tetel.get("nev"), 200)
    menny      = tetel.get("menny")
    egyseg     = _vag(tetel.get("egyseg"), 20)
    retegek    = [_vag(r, 120) for r in (d.get("retegek") or [])[:12] if isinstance(r, str) and r.strip()]
    korulm     = [_vag(k, 120) for k in (d.get("korulmenyek") or [])[:15] if isinstance(k, str) and k.strip()]
    jegyzet    = _vag(d.get("jegyzet"), _MAX["jegyzet"])
    kulcsszo   = _vag(d.get("kulcsszavak"), _MAX["kulcsszavak"])
    alap       = _vag(d.get("alapSzoveg"), _MAX["alapSzoveg"])
    mintak     = [_vag(m, _MAX["minta"]) for m in (d.get("mintak") or [])[:3] if isinstance(m, str) and m.strip()]

    if hossz == "rovid":
        mintak = [m for m in mintak if len(m) <= _ROVID_MINTA_MAX_KAR]
    hossz_szabaly = _ROVID_SZABALY if hossz == "rovid" else _RESZLETES_SZABALY

    sorok = [hossz_szabaly, ""]
    if mod == "igazitas":
        sorok.append(
            "Az alábbi, korábban jóváhagyott SQM-szöveget igazítsd a jelenlegi projekthez. "
            "Tartsd meg a szerkezetét, a hangvételét és a hosszát; cseréld vagy hagyd el azokat a "
            "konkrétumokat (mennyiség, körülmény, rétegrend, helyszín), amelyek nem illenek a mostani "
            "projekthez, és építsd be a mostani projekt releváns adatait. Ha a szöveg már illik, "
            "csak minimálisan módosíts."
        )
        sorok.append(f"\nAZ ÁTÍRANDÓ SZÖVEG:\n{alap}")
    else:
        sorok.append("Írj ajánlati szöveget az alábbi tételhez az SQM stílusában.")

    sorok.append(f"\nKategória: {kategoria}")
    sorok.append(
        "Az alábbi projektadatok HÁTTÉRINFORMÁCIÓK: nem kell mindet beleírni. Csak azt használd, "
        "ami a tétel tartalmát ténylegesen meghatározza (anyag, technológia, mennyiség); a "
        "körülményeket csak akkor említsd, ha a kivitelezést érdemben befolyásolják."
    )
    if tetel_nev:
        m = f" — {menny} {egyseg}".rstrip() if menny not in (None, "") else ""
        sorok.append(f"Tétel: {tetel_nev}{m}")
    # 2026-09-30: a webappban ki/be kapcsolható, hogy az anyagok nevesítve
    # szerepeljenek-e a szövegben (anyagokBele: True / False; hiányzik = régi mód).
    anyagok_bele = d.get("anyagokBele")
    if retegek:
        sorok.append("A rétegrend / felhasznált anyagok: " + "; ".join(retegek))
    if anyagok_bele is True and retegek:
        sorok.append(
            "FELHASZNÁLT ANYAGOK: a szövegben nevezd meg a fenti felhasznált anyagokat "
            "(termék- vagy rendszernév szerint), természetesen, a mondatokba építve — ne "
            "felsorolásként. Rövid (TERC) módban is szerepeljenek, tömören."
        )
    elif anyagok_bele is False:
        sorok.append(
            "FELHASZNÁLT ANYAGOK: NE nevezz meg konkrét anyag-, termék- vagy márkanevet a "
            "szövegben; általános megnevezést használj (pl. „epoxi rendszeranyagok”, "
            "„alapozó”, „fedőréteg”)."
        )
    if korulm:
        sorok.append("Projekt-körülmények: " + "; ".join(korulm))
    if jegyzet:
        sorok.append(f"Felmérési / belső jegyzet a tételhez: {jegyzet}")
    if kulcsszo:
        sorok.append(
            "Nyers kulcsszavak a kollégától (köznyelvi is lehet — fordítsd szakszerű műszaki nyelvre, "
            f"és ami releváns, építsd be): {kulcsszo}"
        )
    if mintak and mod == "uj":
        sorok.append("\nHasonló tételekhez korábban jóváhagyott SQM-szövegek (hangvétel és részletesség mintájaként, NE másold):")
        for i, m in enumerate(mintak, 1):
            sorok.append(f"--- {i}. minta ---\n{m}")
    if hossz == "rovid":
        sorok.append(f"\nEMLÉKEZTETŐ: legfeljebb 2 mondat, legfeljebb {_ROVID_MAX_SZO} szó, TERC-stílus, magyarázat nélkül.")
    sorok.append("\nCsak a kész szöveget add vissza.")
    return "\n".join(sorok)


def _claude(uzenet, max_tokens: int = 900, stilus: str = "") -> str:
    """`uzenet`: egy felhasználói üzenet (str), vagy kész üzenetlista.
    `stilus`: a webappból küldött stílusleírás (üresen a fájlbeli tartalék)."""
    messages = uzenet if isinstance(uzenet, list) else [{"role": "user", "content": uzenet}]
    system = [{"type": "text", "text": _SZEREP}]
    # A nagy, változatlan mintagyűjtemény ELŐL, cache-elve; a (szerkeszthető,
    # rövid) stílusleírás UTÁNA — így a stílus módosítása nem rontja a cache-t.
    if _MINTAK:
        system.append({"type": "text", "text": "JÓVÁHAGYOTT MINTASZÖVEGEK:\n\n" + _MINTAK,
                       "cache_control": {"type": "ephemeral"}})
    st = (stilus or "").strip()[:_STILUS_MAX] or _STILUS_TARTALEK
    if st:
        system.append({"type": "text", "text": "STÍLUSLEÍRÁS (mérvadó):\n\n" + st})
    r = requests.post(
        _ANTHROPIC_URL,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": SZOVEG_AI_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        },
        timeout=55,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
    data = r.json()
    szoveg = "".join(b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text").strip()
    # Biztonsági tisztítás: ha mégis idézőjelbe tenné az egészet.
    if len(szoveg) > 2 and szoveg[0] in "\"„'" and szoveg[-1] in "\"”'":
        szoveg = szoveg[1:-1].strip()
    return szoveg


def register_szoveg_ai_routes(app):
    """Hívd meg a server.py-ból: register_szoveg_ai_routes(app)"""

    @app.route("/szoveg-javaslat", methods=["POST"])
    def szoveg_javaslat():
        if request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        if not ANTHROPIC_API_KEY:
            return jsonify({"ok": False, "error": "Az AI még nincs bekötve (hiányzik az ANTHROPIC_API_KEY a Railway-en)."}), 503
        d = request.get_json(silent=True) or {}
        if d.get("mod") == "igazitas" and not (d.get("alapSzoveg") or "").strip():
            return jsonify({"ok": False, "error": "Igazításhoz hiányzik az alapszöveg"}), 400
        rovid = d.get("hossz") == "rovid"
        stilus = d.get("stilus") if isinstance(d.get("stilus"), str) else ""
        try:
            uzenet = _uzenet(d)
            szoveg = _claude(uzenet, max_tokens=300 if rovid else 900, stilus=stilus)
            # Rövid módban a hossz szerveroldali ellenőrzése: ha így is túl
            # hosszú lett, egyszer rövidíttetjük (a modell a saját szövegét kapja vissza).
            if rovid and szoveg and _szoszam(szoveg) > _ROVID_TURES_SZO:
                log.info(f"[SZOVEG-AI] rövid mód: {_szoszam(szoveg)} szó — újrarövidítés")
                rovidebb = _claude([
                    {"role": "user", "content": uzenet},
                    {"role": "assistant", "content": szoveg},
                    {"role": "user", "content":
                        f"Ez túl hosszú. Írd át TERC-tételsorrá: legfeljebb 2 mondat, legfeljebb "
                        f"{_ROVID_MAX_SZO} szó, csak mit/mivel/milyen lépésekkel, indoklás és "
                        "körülmények nélkül. Csak a kész szöveget add vissza."},
                ], max_tokens=300, stilus=stilus)
                if rovidebb:
                    szoveg = rovidebb
        except Exception as e:
            log.error(f"[SZOVEG-AI] Hiba: {e}")
            return jsonify({"ok": False, "error": "Az AI-hívás nem sikerült. Próbáld újra később."}), 502
        if not szoveg:
            return jsonify({"ok": False, "error": "Az AI üres választ adott. Próbáld újra."}), 502
        log.info(f"[SZOVEG-AI] {d.get('mod', 'uj')} | {'rövid' if rovid else 'részletes'} | {(d.get('tetel') or {}).get('nev', '')[:60]} | {_szoszam(szoveg)} szó")
        return jsonify({"ok": True, "szoveg": szoveg})

    log.info(f"[SZOVEG-AI] Végpont regisztrálva: /szoveg-javaslat (modell: {SZOVEG_AI_MODEL}, "
             f"minták: {len(_MINTAK)} kar., tartalék stílus: {len(_STILUS_TARTALEK)} kar., "
             f"kulcs: {'van' if ANTHROPIC_API_KEY else 'NINCS'})")
