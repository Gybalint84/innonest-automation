"""
ajanlat_b_kepek.py — képek a „B ajánlat" egy kattintásos kitöltéséhez (2026-10)
==============================================================================
Az `ajanlat_b_ai.py` hívja a szöveges kitöltés UTÁN. Sorrend: ELŐSZÖR Dropbox,
ha ott nincs megfelelő, akkor a NET.

  • Hero fotó (borító): az ügyfél saját, áttekintő csarnokfotója.
      1. a deal Dropbox-mappája (Pipedrive „Dropbox" mező → /Ügyfélképek/<Cég> - dátum)
      2. /Ügyfélképek alatt a cégnévre illő mappa(k)
      3. net: az ügyfél honlapjának képei (og:image + nagy <img>-ek)
  • Referenciafotó: a kiválasztott referencia-deal kivitelezési fotója.
      1. /Fotók kivitelezésekről/<ügyfél …> mappa (előtte-utána kreatív előnyben)
      2. net: az SQM honlap (sqm-hungary.hu) képei
  • Partnerlogók: az AI által kiválasztott ügyfelek (nyert dealekből) logója.
      1. Dropbox-keresés: „<cég> logo" képfájl
      2. net: a cég honlapja — fejléc-logó (<img>/inline <svg>/svg ikon), végül og:image

A fotókból a Dropbox kis előnézeti képeit (get_thumbnail_batch) Claude-dal
(képfelismerés) választatjuk ki: melyik a legjobb hero / referenciafotó.
Minden kép data: URI-ként megy vissza; a webapp még lekicsinyíti (kepMeret.js).

Környezeti változók: DROPBOX_APP_KEY / _SECRET / _REFRESH_TOKEN (már megvannak,
a dropbox_mappa_generator.py is ezeket használja), PIPEDRIVE_DROPBOX_FIELD_KEY,
opcionális: DROPBOX_UGYFELKEPEK_FOLDER (alap: /Ügyfélképek),
DROPBOX_KIVITELEZES_FOLDER (alap: /Fotók kivitelezésekről), SQM_WEB (alap: https://sqm-hungary.hu).
"""

import base64
import json
import logging
import os
import re
import struct
import unicodedata
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Választható modellek (2026-10-16): a webapp a kulcsot küldi; ismeretlen kulcs → alapértelmezett.
# Ár: USD / millió token — bemenet, kimenet, cache-írás (5 perc), cache-olvasás
# (platform.claude.com árlista, 2026-10). FONTOS: mindhárom modell ELUTASÍTJA a kényszerített
# eszközhívást (tool_choice: tool) és mindig gondolkodik (adaptive thinking) — ezért
# strukturált kimenettel (output_config.format = json_schema) kérünk JSON-t, bőséges max_tokens-szel.
MODELLEK = {
    "sonnet": {"id": "claude-sonnet-5-5", "nev": "Sonnet 5.5", "ar": [2, 10, 2.5, 0.10]},
    "opus":   {"id": "claude-opus-5-5",   "nev": "Opus 5.5",   "ar": [4, 20, 5, 0.20]},
    "fable":  {"id": "claude-fable-5-1",  "nev": "Fable 5.1",  "ar": [10, 50, 12.5, 0.25]},
}
ALAP_MODELL = os.environ.get("B_KITOLTES_MODELL", "sonnet") if os.environ.get("B_KITOLTES_MODELL", "sonnet") in MODELLEK else "sonnet"


def modell(kulcs) -> dict:
    """{'kulcs', 'id', 'nev', 'ar'} — ismeretlen kulcsnál az alapértelmezett."""
    k = kulcs if kulcs in MODELLEK else ALAP_MODELL
    return {"kulcs": k, **MODELLEK[k]}


def szigoru(schema):
    """JSON-séma a strukturált kimenethez: minden objektum additionalProperties:false,
    minden mező kötelező; a ["x","null"] típus anyOf-ra alakul."""
    if isinstance(schema, list):
        return [szigoru(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    s = {k: szigoru(v) for k, v in schema.items() if k != "description"}
    if isinstance(s.get("type"), list):
        tipusok = s.pop("type")
        s = {"anyOf": [{"type": t} for t in tipusok], **s}
    if s.get("type") == "object":
        s["additionalProperties"] = False
        s["required"] = list((s.get("properties") or {}).keys())
    return s


def claude_json(modell_id, system, content, schema, effort="medium", max_tokens=16000, timeout=240,
                szamlalo=None, lepes="szoveg", cache=True):
    """Egy Claude-hívás strukturált (JSON) kimenettel. Visszaad: dict. Hibánál RuntimeError."""
    rendszer = [{"type": "text", "text": system, **({"cache_control": {"type": "ephemeral"}} if cache else {})}]
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": modell_id, "max_tokens": max_tokens, "system": rendszer,
              "messages": [{"role": "user", "content": content}],
              "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": szigoru(schema)}}},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
    valasz = r.json()
    if szamlalo:
        szamlalo(valasz.get("usage"), lepes)
    if valasz.get("stop_reason") == "max_tokens":
        raise RuntimeError("Az AI válasza túl hosszú lett (max_tokens) — próbáld újra, vagy válassz másik modellt.")
    if valasz.get("stop_reason") == "refusal":
        raise RuntimeError("Az AI elutasította a kérést.")
    szoveg = "".join(b.get("text", "") for b in (valasz.get("content") or []) if b.get("type") == "text").strip()
    try:
        return json.loads(szoveg)
    except ValueError:
        m = re.search(r"\{.*\}", szoveg, re.S)          # biztonsági háló: a JSON kiemelése a szövegből
        if m:
            return json.loads(m.group(0))
        raise RuntimeError("Az AI válasza nem értelmezhető JSON.")
DROPBOX_FIELD_KEY = os.environ.get("PIPEDRIVE_DROPBOX_FIELD_KEY", "8551443f0e9f59d2af653f3df5c12a05b0c432a7")
UGYFELKEPEK = unicodedata.normalize("NFC", os.environ.get("DROPBOX_UGYFELKEPEK_FOLDER", "/Ügyfélképek"))
KIVITELEZES = unicodedata.normalize("NFC", os.environ.get("DROPBOX_KIVITELEZES_FOLDER", "/Fotók kivitelezésekről"))
SQM_WEB = os.environ.get("SQM_WEB", "https://sqm-hungary.hu")
_API = "https://api.dropboxapi.com/2"
_CONTENT = "https://content.dropboxapi.com/2"
KEP_KIT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"}
MAX_JELOLT = 12          # ennyi előnézetet küldünk a képfelismerésnek kategóriánként
MAX_LOGO_BAJT = 400_000
MAX_FOTO_BAJT = 4_000_000


def norm(s) -> str:
    """Ékezet- és kisbetű-független összehasonlításhoz."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_CEG_ZAJ = {"kft", "zrt", "nyrt", "bt", "rt", "gmbh", "ag", "hungary", "magyarorszag", "magyar", "kereskedelmi",
            "es", "szolgaltato", "termelo", "gyarto", "ipari", "the", "group", "ltd", "inc", "co", "sa", "spa"}


def ceg_tokenek(nev) -> list:
    return [t for t in norm(nev).split() if len(t) >= 3 and t not in _CEG_ZAJ]


def egyezes(nev, mappanev) -> int:
    """Hány cégnév-token szerepel a mappanévben (0 = nem illik)."""
    m = norm(mappanev)
    return sum(1 for t in ceg_tokenek(nev) if t in m)


# ══════════════════════════════════════════════════════════════════════════════
# DROPBOX
# ══════════════════════════════════════════════════════════════════════════════

class Dropbox:
    def __init__(self):
        from dropbox_mappa_generator import dropbox_access_token_lekerese  # a meglévő token-kezelés
        self.token = dropbox_access_token_lekerese()

    def _post(self, path, body, timeout=20):
        r = requests.post(_API + path, headers={"Authorization": f"Bearer {self.token}",
                                                "Content-Type": "application/json"}, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def lista(self, path, max_db=2000):
        out, d = [], self._post("/files/list_folder", {"path": path, "limit": 1000})
        out += d.get("entries", [])
        while d.get("has_more") and len(out) < max_db:
            d = self._post("/files/list_folder/continue", {"cursor": d["cursor"]})
            out += d.get("entries", [])
        return out

    def megosztott_path(self, url):
        try:
            return self._post("/sharing/get_shared_link_metadata", {"url": url}).get("path_lower")
        except Exception:
            return None

    def kepek(self, path, rekurziv=False):
        try:
            if rekurziv:
                d = self._post("/files/list_folder", {"path": path, "recursive": True, "limit": 1000})
                ent = d.get("entries", [])
            else:
                ent = self.lista(path, 500)
        except Exception as e:
            log.warning(f"[B-KÉP] Dropbox lista hiba ({path}): {e}")
            return []
        return [e for e in ent if e.get(".tag") == "file" and e.get("name", "").lower().endswith(KEP_KIT)]

    def elonezetek(self, fajlok, meret="w256h256"):
        """{path_lower: jpeg bájtok} — a Dropbox maga konvertál (HEIC is)."""
        out = {}
        for i in range(0, len(fajlok), 25):
            resz = fajlok[i:i + 25]
            body = {"entries": [{"path": f["path_lower"], "format": "jpeg", "size": meret, "mode": "bestfit"}
                                for f in resz]}
            try:
                r = requests.post(_CONTENT + "/files/get_thumbnail_batch",
                                  headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                                  json=body, timeout=40)
                r.raise_for_status()
                for f, e in zip(resz, r.json().get("entries", [])):
                    if e.get(".tag") == "success" and e.get("thumbnail"):
                        out[f["path_lower"]] = base64.b64decode(e["thumbnail"])
            except Exception as e:
                log.warning(f"[B-KÉP] előnézet-hiba: {e}")
        return out

    def nagy_kep(self, path, meret="w1024h768"):
        r = requests.post(_CONTENT + "/files/get_thumbnail_v2",
                          headers={"Authorization": f"Bearer {self.token}",
                                   "Dropbox-API-Arg": json.dumps({"resource": {".tag": "path", "path": path},
                                                                  "format": "jpeg", "size": meret, "mode": "bestfit"})},
                          timeout=40)
        r.raise_for_status()
        return r.content

    def letolt(self, path):
        r = requests.post(_CONTENT + "/files/download",
                          headers={"Authorization": f"Bearer {self.token}",
                                   "Dropbox-API-Arg": json.dumps({"path": path})}, timeout=40)
        r.raise_for_status()
        return r.content

    def keres(self, query, kiterjesztesek=None, max_db=10):
        opts = {"max_results": max_db, "file_status": "active"}
        if kiterjesztesek:
            opts["file_extensions"] = kiterjesztesek
        try:
            d = self._post("/files/search_v2", {"query": query, "options": opts})
        except Exception as e:
            log.warning(f"[B-KÉP] Dropbox keresés hiba ({query}): {e}")
            return []
        out = []
        for m in d.get("matches", []):
            md = ((m.get("metadata") or {}).get("metadata")) or {}
            if md.get(".tag") == "file":
                out.append(md)
        return out


def illo_mappak(dbx, gyoker, cegnev, max_db=3):
    """A gyökér alatti, cégnévre leginkább illő mappák (legfrissebb elöl, ha a név dátummal végződik)."""
    try:
        ent = [e for e in dbx.lista(gyoker) if e.get(".tag") == "folder"]
    except Exception as e:
        log.warning(f"[B-KÉP] {gyoker} nem listázható: {e}")
        return []
    pont = [(egyezes(cegnev, e.get("name")), e.get("name", ""), e) for e in ent]
    pont = [p for p in pont if p[0] > 0]
    # több egyező token előbb; azonos pontszámnál a „későbbi" (dátumos) név elöl
    pont.sort(key=lambda p: (p[0], p[1]), reverse=True)
    return [p[2]["path_lower"] for p in pont[:max_db]]


# ══════════════════════════════════════════════════════════════════════════════
# NET
# ══════════════════════════════════════════════════════════════════════════════

def oldal(url, timeout=12):
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        if r.status_code < 400 and "html" in r.headers.get("content-type", ""):
            return r.text[:2_000_000], r.url
    except Exception as e:
        log.info(f"[B-KÉP] {url} nem elérhető: {e}")
    return "", url


def letolt_url(url, max_bajt):
    try:
        r = requests.get(url, headers=UA, timeout=15, stream=True)
        if r.status_code >= 400:
            return None, ""
        tipus = r.headers.get("content-type", "").split(";")[0].strip().lower()
        adat = b""
        for d in r.iter_content(65536):
            adat += d
            if len(adat) > max_bajt:
                return None, ""
        if not tipus.startswith("image/"):
            if adat.lstrip()[:5] in (b"<?xml", b"<svg ") or b"<svg" in adat[:400]:
                tipus = "image/svg+xml"
            else:
                return None, ""
        return adat, tipus
    except Exception:
        return None, ""


def _attr(tag, nev):
    m = re.search(r'\b' + nev + r'\s*=\s*["\']([^"\']+)["\']', tag, re.I)
    return m.group(1) if m else ""


def kep_meret(adat: bytes):
    """(szélesség, magasság) PNG/JPEG/GIF/WEBP fejlécből; ismeretlennél None."""
    try:
        if adat[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", adat[16:24])
        if adat[:3] == b"GIF":
            return struct.unpack("<HH", adat[6:10])
        if adat[:4] == b"RIFF" and adat[8:12] == b"WEBP":
            if adat[12:16] == b"VP8X":
                return (1 + int.from_bytes(adat[24:27], "little"), 1 + int.from_bytes(adat[27:30], "little"))
            return None
        if adat[:2] == b"\xff\xd8":
            i = 2
            while i < len(adat) - 9:
                if adat[i] != 0xFF:
                    i += 1
                    continue
                jel = adat[i + 1]
                if jel in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", adat[i + 5:i + 9])
                    return (w, h)
                i += 2 + struct.unpack(">H", adat[i + 2:i + 4])[0]
    except Exception:
        pass
    return None


def data_uri(adat: bytes, tipus: str) -> str:
    return f"data:{tipus};base64," + base64.b64encode(adat).decode("ascii")


def logo_jeloltek(html: str, alap: str) -> list:
    """Logó-jelöltek egy honlapból, valószínűség szerint rendezve: [(típus, érték)]."""
    out = []
    # 1) inline <svg> logó (class/id/aria-label „logo")
    for m in re.finditer(r"<svg\b[^>]*>.*?</svg>", html, re.S | re.I):
        nyito = html[m.start():m.start() + 400]
        elozo = html[max(0, m.start() - 300):m.start()]
        if re.search(r"logo", nyito, re.I) or re.search(r"logo[^>]*>\s*(<a[^>]*>\s*)?$", elozo, re.I):
            svg = m.group(0)
            if 300 < len(svg) < MAX_LOGO_BAJT:
                if "xmlns=" not in svg[:300]:
                    svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
                out.append(("svg", svg))
    # 2) <img> „logo" jelöléssel (fejléchez közeli találat előnyben)
    for m in re.finditer(r"<img\b[^>]*>", html, re.I):
        tag = m.group(0)
        if re.search(r"logo", tag, re.I):
            src = _attr(tag, "src") or _attr(tag, "data-src") or (_attr(tag, "srcset").split(" ")[0] if _attr(tag, "srcset") else "")
            if src and not src.startswith("data:"):
                out.append(("url", urljoin(alap, src)))
    # 3) svg ikon / apple-touch-icon / og:image
    for m in re.finditer(r"<link\b[^>]*>", html, re.I):
        tag = m.group(0)
        rel, href = _attr(tag, "rel").lower(), _attr(tag, "href")
        if href and "icon" in rel and href.lower().endswith(".svg"):
            out.append(("url", urljoin(alap, href)))
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]*>', html, re.I)
    if m and _attr(m.group(0), "content"):
        out.append(("url", urljoin(alap, _attr(m.group(0), "content"))))
    return out


def logo_netrol(domain: str):
    """(data URI, forrás) vagy (None, '')."""
    d = re.sub(r"^https?://", "", str(domain or "").strip()).strip("/")
    if not d or " " in d:
        return None, ""
    html, vegso = oldal("https://" + d)
    if not html:
        return None, ""
    for tipus, ertek in logo_jeloltek(html, vegso)[:8]:
        if tipus == "svg":
            return data_uri(ertek.encode("utf-8"), "image/svg+xml"), vegso + " (inline svg)"
        adat, mime = letolt_url(ertek, MAX_LOGO_BAJT)
        if not adat:
            continue
        if mime != "image/svg+xml":
            mm = kep_meret(adat)
            if not mm or mm[0] < 80 or mm[0] < mm[1] * 0.9:   # túl kicsi vagy ikon-szerű (álló)
                continue
        return data_uri(adat, mime), ertek
    return None, ""


def foto_jeloltek_netrol(url: str, max_db=8) -> list:
    """Nagyobb fotók egy honlapról: [(url, jpeg/png bájtok)] — a képfelismerés választ közülük."""
    html, vegso = oldal(url)
    if not html:
        return []
    urlek = []
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]*>', html, re.I)
    if m and _attr(m.group(0), "content"):
        urlek.append(urljoin(vegso, _attr(m.group(0), "content")))
    for m in re.finditer(r"<img\b[^>]*>", html, re.I):
        tag = m.group(0)
        src = _attr(tag, "src") or _attr(tag, "data-src")
        if src and not src.startswith("data:") and not re.search(r"logo|icon|sprite|\.svg", tag, re.I):
            urlek.append(urljoin(vegso, src))
    out, latott = [], set()
    for u in urlek:
        if u in latott:
            continue
        latott.add(u)
        adat, mime = letolt_url(u, MAX_FOTO_BAJT)
        if not adat or mime not in ("image/jpeg", "image/png", "image/webp"):
            continue
        mm = kep_meret(adat)
        if mm and mm[0] >= 700 and mm[0] >= mm[1]:
            out.append((u, adat, mime))
        if len(out) >= max_db:
            break
    return out


# ══════════════════════════════════════════════════════════════════════════════
# KÉPFELISMERÉS (Claude) — melyik a legjobb hero / referenciafotó
# ══════════════════════════════════════════════════════════════════════════════

VALASZTO = {
    "name": "kepvalasztas",
    "description": "A legjobb hero fotó és referenciafotó kiválasztása a jelöltek közül.",
    "input_schema": {"type": "object", "properties": {
        "hero": {"type": ["string", "null"], "description": "a hero jelölt azonosítója (pl. H3) vagy null"},
        "ref": {"type": ["string", "null"], "description": "a referencia jelölt azonosítója (pl. R2) vagy null"},
        "indok": {"type": "string"}}, "required": ["hero", "ref", "indok"]},
}

VALASZTO_PROMPT = (
    "Egy ipari padlóbevonó cég (SQM Hungary) árajánlatához választasz képet.\n"
    "HERO (H-jelöltek): az ÜGYFÉL saját, távoli, áttekintő fotója a munkaterületről (csarnok, raktár, "
    "üzem padlója, tágas, fekvő kép). NE válassz közelit, dokumentumot, képernyőképet, embert, járművet "
    "közelről, sötét/életlen képet. Ha egyik sem alkalmas: null.\n"
    "REFERENCIA (R-jelöltek): egy korábbi, KÉSZ SQM-munka fotója (kész padló/vonalazás); az előtte-utána "
    "összehasonlító kép kifejezetten jó. Ha egyik sem alkalmas: null.\n"
    "A választ a megadott JSON-sémában add vissza (hero, ref, indok).")


def kepvalasztas(hero_jeloltek: list, ref_jeloltek: list, munka: str, szamlalo=None) -> dict:
    """jeloltek: [(azonosító, jpeg bájtok, leírás)] → {'hero': 'H2'|None, 'ref': 'R1'|None}."""
    if not ANTHROPIC_API_KEY or not (hero_jeloltek or ref_jeloltek):
        return {"hero": None, "ref": None}
    tartalom = [{"type": "text", "text": f"A munka: {munka}"}]
    for az, adat, leiras in list(hero_jeloltek) + list(ref_jeloltek):
        tartalom.append({"type": "text", "text": f"{az} — {leiras}"})
        tartalom.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                      "data": base64.b64encode(adat).decode("ascii")}})
    try:
        m = getattr(szamlalo, "modell", None) or modell(None)
        return claude_json(m["id"], VALASZTO_PROMPT, tartalom, VALASZTO["input_schema"], effort="low",
                           max_tokens=4000, timeout=150, szamlalo=szamlalo, lepes="kepfelismeres", cache=False)
    except Exception as e:
        log.warning(f"[B-KÉP] képfelismerés hiba: {e}")
    return {"hero": None, "ref": None}


# ══════════════════════════════════════════════════════════════════════════════
# ÖSSZEFOGÓ
# ══════════════════════════════════════════════════════════════════════════════

def kepek_beszerzese(deal: dict, cegnev: str, ugyfel_web: str, ref_deal: dict, logo_cegek: list, munka: str,
                     szamlalo=None) -> dict:
    """Visszaad: {hero, refFoto, logok:[...], forrasok:{...}, megjegyzesek:[...]} — data URI-kkel."""
    ki = {"hero": "", "refFoto": "", "logok": [], "forrasok": {}, "megjegyzesek": []}
    try:
        dbx = Dropbox()
    except Exception as e:
        dbx = None
        log.warning(f"[B-KÉP] Dropbox nem elérhető: {e}")
        ki["megjegyzesek"].append("A Dropbox nem érhető el — a képeket csak a netről kerestem.")

    # ── jelöltek ──
    hero_fajlok, ref_fajlok = [], []
    if dbx:
        url = (deal or {}).get(DROPBOX_FIELD_KEY) or ""
        if url:
            p = dbx.megosztott_path(url)
            if p:
                hero_fajlok = dbx.kepek(p, rekurziv=True)
        if not hero_fajlok and cegnev:
            for p in illo_mappak(dbx, UGYFELKEPEK, cegnev, 2):
                hero_fajlok += dbx.kepek(p, rekurziv=True)
        if ref_deal:
            ref_nev = " ".join(filter(None, [ref_deal.get("ugyfel"), ref_deal.get("cim")]))
            for p in illo_mappak(dbx, KIVITELEZES, ref_deal.get("ugyfel") or ref_nev, 2):
                ref_fajlok += dbx.kepek(p, rekurziv=True)
        # előtte-utána kreatív előre, utána a legfrissebbek
        ref_fajlok.sort(key=lambda f: (0 if re.search(r"elotte|utana|overlay|before|after", norm(f.get("name"))) else 1,
                                       f.get("name", "")))
        hero_fajlok.sort(key=lambda f: f.get("client_modified", ""), reverse=True)
    hero_fajlok, ref_fajlok = hero_fajlok[:MAX_JELOLT], ref_fajlok[:MAX_JELOLT]

    elo = dbx.elonezetek(hero_fajlok + ref_fajlok) if dbx and (hero_fajlok or ref_fajlok) else {}
    hj = [(f"H{i + 1}", elo[f["path_lower"]], "Dropbox: " + f.get("name", ""), ("dbx", f["path_lower"]))
          for i, f in enumerate(hero_fajlok) if f["path_lower"] in elo]
    rj = [(f"R{i + 1}", elo[f["path_lower"]], "Dropbox: " + f.get("name", ""), ("dbx", f["path_lower"]))
          for i, f in enumerate(ref_fajlok) if f["path_lower"] in elo]

    # ── net, ha a Dropboxban nincs jelölt ──
    if not hj and ugyfel_web:
        for i, (u, adat, mime) in enumerate(foto_jeloltek_netrol(ugyfel_web if ugyfel_web.startswith("http")
                                                                 else "https://" + ugyfel_web)):
            if mime == "image/jpeg":
                hj.append((f"H{i + 1}", adat, "honlap: " + u, ("net", (adat, mime, u))))
    if not rj:
        for i, (u, adat, mime) in enumerate(foto_jeloltek_netrol(SQM_WEB)):
            if mime == "image/jpeg":
                rj.append((f"R{i + 1}", adat, "SQM honlap: " + u, ("net", (adat, mime, u))))

    valasz = kepvalasztas([(a, b, c) for a, b, c, _ in hj], [(a, b, c) for a, b, c, _ in rj], munka, szamlalo)

    def betolt(jeloltek, az, meret):
        for a, _, leiras, (forras, adat) in jeloltek:
            if a != az:
                continue
            try:
                if forras == "dbx":
                    return data_uri(dbx.nagy_kep(adat, meret), "image/jpeg"), leiras
                bajt, mime, _u = adat
                return data_uri(bajt, mime), leiras
            except Exception as e:
                log.warning(f"[B-KÉP] kép letöltés hiba ({leiras}): {e}")
        return "", ""

    if valasz.get("hero"):
        ki["hero"], ki["forrasok"]["hero"] = betolt(hj, valasz["hero"], "w1024h768")
    if valasz.get("ref"):
        ki["refFoto"], ki["forrasok"]["refFoto"] = betolt(rj, valasz["ref"], "w1024h768")
    if not ki["hero"]:
        ki["megjegyzesek"].append("Hero fotót nem találtam (az ügyfél Dropbox-mappájában és a honlapján sem volt alkalmas) — töltsd fel kézzel.")
    if not ki["refFoto"]:
        ki["megjegyzesek"].append("Referenciafotót nem találtam — töltsd fel kézzel.")

    # ── logók ──
    forras_logok = []
    for c in (logo_cegek or [])[:8]:
        nev, domain = str(c.get("nev") or "").strip(), str(c.get("domain") or "").strip()
        if not nev:
            continue
        uri, honnan = None, ""
        if dbx:
            for f in dbx.keres(f"{nev} logo", ["png", "svg", "jpg", "jpeg"], 5):
                if "logo" in norm(f.get("name")) and egyezes(nev, f.get("name")):
                    try:
                        adat = dbx.letolt(f["path_lower"])
                        if len(adat) <= MAX_LOGO_BAJT:
                            mime = "image/svg+xml" if f["name"].lower().endswith(".svg") else (
                                "image/png" if f["name"].lower().endswith(".png") else "image/jpeg")
                            uri, honnan = data_uri(adat, mime), "Dropbox: " + f.get("path_display", f["name"])
                            break
                    except Exception:
                        continue
        if not uri and domain:
            uri, honnan = logo_netrol(domain)
        if uri:
            ki["logok"].append(uri)
            forras_logok.append(f"{nev} — {honnan}")
        if len(ki["logok"]) >= 6:
            break
    ki["forrasok"]["logok"] = forras_logok
    if len(ki["logok"]) < 6:
        ki["megjegyzesek"].append(f"Partnerlogó: {len(ki['logok'])} db található — a hiányzókat töltsd fel kézzel.")
    return ki
