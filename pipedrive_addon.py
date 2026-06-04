"""
pipedrive_addon.py – Pipedrive webhook + visszajelzési rendszer
===============================================================
Importáld a server.py végére, a meglévő kódot NEM kell módosítani.

A server.py aljára add hozzá ezt a 3 sort:
    from pipedrive_addon import register_pipedrive_routes
    register_pipedrive_routes(app)
    # (ez az if __name__ == "__main__" sor ELÉ kerüljön)
"""

import os
import json
import secrets
import datetime
import re
import logging
import requests
from flask import Blueprint, request, jsonify

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# KONFIGURÁCIÓ
# ─────────────────────────────────────────────
PIPEDRIVE_API_TOKEN  = os.environ.get("PIPEDRIVE_API_TOKEN", "")
PIPEDRIVE_BID_FIELD  = os.environ.get("PIPEDRIVE_BID_FIELD_KEY", "")
WEBAPP_URL           = os.environ.get("WEBAPP_URL", "")
WEBAPP_SECRET        = os.environ.get("WEBAPP_SECRET", "")
BASE_URL             = "https://" + os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
GOOGLE_SHEET_ID      = os.environ.get("GOOGLE_SHEET_ID", "")

SABLONOK_MAPPA = os.path.join(os.path.dirname(__file__), "sablonok")

# ─────────────────────────────────────────────
# GOOGLE SHEETS – token tárolás
# ─────────────────────────────────────────────
def _sheets_post(action: str, payload: dict) -> dict:
    """Minden Sheets műveletet az Apps Script Web App-on keresztül intézünk."""
    try:
        r = requests.post(WEBAPP_URL, json={
            "secret": WEBAPP_SECRET,
            "action": action,
            **payload
        }, timeout=30)
        return r.json()
    except Exception as e:
        log.error(f"[SHEETS] {action} hiba: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────────
# INNONEST – tételek lekérése
# ─────────────────────────────────────────────
def _innonest_adatok_leker(bid: str) -> dict:
    """
    Az Innonestből lekéri a BID szám alapján az árajánlat adatait.
    A server.py innonest_adatok_leker() függvényét hívja.
    """
    try:
        from server import innonest_adatok_leker
        return innonest_adatok_leker(bid)
    except Exception as e:
        log.error(f"[INNONEST] Import/hívás hiba: {e}")
        return {
            "tetelek": [],
            "netto_osszeg": "",
            "penznem": "HUF",
            "fizetesi_feltetelek": "–",
            "ervenyes_ig": "–",
        }


def token_ment(token: str, deal_adatok: dict):
    _sheets_post("saveToken", {
        "token": token,
        "bid": deal_adatok.get("bid_szam", ""),
        "dealJson": json.dumps(deal_adatok, ensure_ascii=False),
        "letrehozva": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })


def token_beolvas(token: str) -> dict | None:
    resp = _sheets_post("getToken", {"token": token})
    if resp.get("success") and resp.get("dealJson"):
        try:
            return json.loads(resp["dealJson"])
        except Exception:
            return None
    return None


def token_bekuldve(token: str):
    _sheets_post("markTokenSent", {
        "token": token,
        "bekuldve": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })


# ─────────────────────────────────────────────
# PIPEDRIVE API
# ─────────────────────────────────────────────
def _pd_get(endpoint: str) -> dict | None:
    url = f"https://api.pipedrive.com/v1/{endpoint}"
    try:
        r = requests.get(url, params={"api_token": PIPEDRIVE_API_TOKEN}, timeout=15)
        data = r.json()
        if data.get("success"):
            return data["data"]
    except Exception as e:
        log.error(f"[PD] {endpoint}: {e}")
    return None


def _elso(lista, kulcs="value"):
    if isinstance(lista, list) and lista:
        return lista[0].get(kulcs, "")
    return ""


def _szam_fmt(ertek) -> str:
    try:
        return f"{int(float(str(ertek).replace(' ','').replace(',','.'))):,}".replace(",", " ")
    except Exception:
        return str(ertek)


def _datum_fmt(s: str) -> str:
    honapok = ["január","február","március","április","május","június",
               "július","augusztus","szeptember","október","november","december"]
    try:
        d = datetime.datetime.fromisoformat(s.split("T")[0])
        return f"{d.year}. {honapok[d.month-1]} {d.day}."
    except Exception:
        return s


def deal_adatok_osszerak(deal_id: int) -> dict | None:
    deal   = _pd_get(f"deals/{deal_id}")
    if not deal:
        return None

    bid = deal.get(PIPEDRIVE_BID_FIELD, "")
    if not bid:
        log.warning(f"[PD] Nincs BID szám a deal-ben: {deal_id}")
        return None

    person_id = (deal.get("person_id") or {}).get("value")
    org_id    = (deal.get("org_id")    or {}).get("value")
    owner_id  = (deal.get("user_id")   or {}).get("id")

    person = _pd_get(f"persons/{person_id}")  if person_id else {}
    org    = _pd_get(f"organizations/{org_id}") if org_id  else {}
    owner  = _pd_get(f"users/{owner_id}")     if owner_id else {}

    cim_reszek = [org.get("address_route",""), org.get("address_locality",""),
                  org.get("address_country","")] if org else []
    cim = ", ".join(r for r in cim_reszek if r) or (org or {}).get("address","–")

    won_time = deal.get("won_time") or deal.get("update_time","")
    datum    = _datum_fmt(won_time) if won_time else datetime.datetime.now().strftime("%Y. %m. %d.")

    # Innonest tételek lekérése Playwright-tal
    log.info(f"[INNONEST] Tételek lekérése BID alapján: {bid}")
    innonest = _innonest_adatok_leker(bid)

    return {
        "deal_id":                deal_id,
        "bid_szam":               bid,
        "datum":                  datum,
        "deal_nev":               deal.get("title","–"),
        "deal_ertek":             _szam_fmt(deal.get("value", 0)),
        "penznem":                innonest.get("penznem") or deal.get("currency","HUF"),
        "cegnev":                 (org or {}).get("name", deal.get("org_name","–")),
        "kapcsolattarto_nev":     (person or {}).get("name","–"),
        "kapcsolattarto_email":   _elso((person or {}).get("email",[])),
        "kapcsolattarto_telefon": _elso((person or {}).get("phone",[])) or "–",
        "cim":                    cim if cim and cim != "None" else "–",
        "owner_nev":              (owner or {}).get("name","–"),
        "owner_email":            (owner or {}).get("email",""),
        "owner_telefon":          _elso((owner or {}).get("phone",[])),
        "tetelek":                innonest.get("tetelek", []),
        "netto_osszeg":           innonest.get("netto_osszeg") or _szam_fmt(deal.get("value", 0)),
        "fizetesi_feltetelek":    innonest.get("fizetesi_feltetelek", "–"),
        "ervenyes_ig":            innonest.get("ervenyes_ig", "–"),
    }


# ─────────────────────────────────────────────
# SABLON KITÖLTÉS
# ─────────────────────────────────────────────
def sablon_betolt(nev: str) -> str:
    with open(os.path.join(SABLONOK_MAPPA, nev), encoding="utf-8") as f:
        return f.read()


def tetelek_email_html(tetelek: list) -> str:
    html = ""
    for i, t in enumerate(tetelek):
        bg = "#ffffff" if i % 2 == 0 else "#fafafa"
        html += (
            f'<tr style="background:{bg};border-bottom:1px solid #f0f0f0;">'
            f'<td style="padding:9px 10px;font-family:Arial,sans-serif;font-size:11px;color:#999;">{t.get("sorszam","")}</td>'
            f'<td style="padding:9px 10px;font-family:Arial,sans-serif;font-size:13px;color:#444;">{t.get("megnevezes","")}</td>'
            f'<td align="right" style="padding:9px 10px;font-family:Arial,sans-serif;font-size:13px;color:#444;white-space:nowrap;">{t.get("mennyiseg","")}</td>'
            f'<td align="right" style="padding:9px 10px;font-family:Arial,sans-serif;font-size:13px;color:#444;white-space:nowrap;">{t.get("egysegar","")}</td>'
            f'<td align="right" style="padding:9px 10px;font-family:Arial,sans-serif;font-size:13px;font-weight:600;color:#1a1a1a;white-space:nowrap;">{t.get("osszesen","")}</td>'
            f'</tr>'
        )
    return html


def tetelek_web_html(tetelek: list) -> str:
    html = ""
    for t in tetelek:
        html += (
            f'<tr>'
            f'<td>{t.get("sorszam","")}</td>'
            f'<td>{t.get("megnevezes","")}</td>'
            f'<td>{t.get("mennyiseg","")}</td>'
            f'<td>{t.get("egysegar","")}</td>'
            f'<td>{t.get("osszesen","")}</td>'
            f'</tr>'
        )
    return html


def sablon_kitolt(html: str, adatok: dict, token: str = "") -> str:
    owner_tel_sor = (
        f'{adatok["owner_telefon"]}<br>'
        if adatok.get("owner_telefon") else ""
    )
    visszajelzes_url = f"{BASE_URL}/visszajelzes/{token}" if token else ""

    csere = {
        "{{BID_SZAM}}":               adatok.get("bid_szam",""),
        "{{DATUM}}":                  adatok.get("datum",""),
        "{{DEAL_NEV}}":               adatok.get("deal_nev",""),
        "{{DEAL_ERTEK}}":             adatok.get("deal_ertek",""),
        "{{PENZNEM}}":                adatok.get("penznem","HUF"),
        "{{CEGNEV}}":                 adatok.get("cegnev",""),
        "{{KAPCSOLATTARTO_NEV}}":     adatok.get("kapcsolattarto_nev",""),
        "{{KAPCSOLATTARTO_EMAIL}}":   adatok.get("kapcsolattarto_email",""),
        "{{KAPCSOLATTARTO_TELEFON}}": adatok.get("kapcsolattarto_telefon","–"),
        "{{CIM}}":                    adatok.get("cim",""),
        "{{OWNER_NEV}}":              adatok.get("owner_nev",""),
        "{{OWNER_EMAIL}}":            adatok.get("owner_email",""),
        "{{OWNER_TELEFON_SOR}}":      owner_tel_sor,
        "{{OWNER_TELEFON_SOR_HTML}}":   f'<strong>Tel:</strong> {adatok["owner_telefon"]}<br>\n        ' if adatok.get("owner_telefon") else "",
        "{{NETTO_OSSZEG}}":           adatok.get("netto_osszeg",""),
        "{{FIZETESI_FELTETELEK}}":    adatok.get("fizetesi_feltetelek","–"),
        "{{ERVENYES_IG}}":            adatok.get("ervenyes_ig","–"),
        "{{TOKEN}}":                  token,
        "{{VISSZAJELZES_URL}}":       visszajelzes_url,
        "{{TETELEK_EMAIL_HTML}}":     tetelek_email_html(adatok.get("tetelek",[])),
        "{{TETELEK_HTML}}":           tetelek_web_html(adatok.get("tetelek",[])),
        "{{CALENDLY_LINK}}":          "#",
    }
    for k, v in csere.items():
        html = html.replace(k, str(v))
    return html


# ─────────────────────────────────────────────
# EMAIL KÜLDÉS – Apps Script-en keresztül
# ─────────────────────────────────────────────
def email_kuld(cimzett: str, targy: str, html_body: str) -> bool:
    try:
        r = requests.post(WEBAPP_URL, json={
            "secret":   WEBAPP_SECRET,
            "action":   "sendEmail",
            "to":       cimzett,
            "subject":  targy,
            "htmlBody": html_body
        }, timeout=30)
        resp = r.json()
        if resp.get("success"):
            log.info(f"[EMAIL] Elküldve → {cimzett}")
            return True
        else:
            log.error(f"[EMAIL] Apps Script hiba: {resp}")
            return False
    except Exception as e:
        log.error(f"[EMAIL] Küldési hiba: {e}")
        return False


# ─────────────────────────────────────────────
# OWNER ÖSSZESÍTŐ EMAIL
# ─────────────────────────────────────────────
def owner_email_html(deal_adatok: dict, idopontok: list,
                     kovetelmenyek: list, megjegyzes: str) -> str:
    ido_sorok = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#999;font-size:12px;">{i+1}. opció</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;font-weight:600;">{d}</td></tr>'
        for i, d in enumerate(idopontok)
    ) or '<tr><td colspan="2" style="padding:8px 12px;color:#aaa;">Nem adott meg időpontot.</td></tr>'

    kov_sorok = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;">✓ {k["label"]}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#666;">{k.get("reszlet","") or "–"}</td></tr>'
        for k in kovetelmenyek
    )

    megjegyzes_blokk = (
        f'<tr><td colspan="2" style="padding:20px 28px;border-bottom:1px solid #eee;">'
        f'<p style="margin:0 0 8px;font-size:10px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;">Egyéb megjegyzés</p>'
        f'<p style="margin:0;font-size:13px;color:#444;line-height:1.6;">{megjegyzes}</p></td></tr>'
        if megjegyzes else ""
    )

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#e8e8e8;font-family:Arial,sans-serif;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#e8e8e8;">
<tr><td align="center" style="padding:24px 16px;">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#fff;max-width:600px;">
  <tr><td style="background:#1a1a1a;border-bottom:3px solid #f0a500;padding:18px 28px;">
    <p style="margin:0;font-size:18px;font-weight:700;color:#fff;letter-spacing:2px;text-transform:uppercase;">SQM HUNGARY</p>
    <p style="margin:4px 0 0;font-size:10px;color:#888;letter-spacing:2px;text-transform:uppercase;">Ügyfél visszajelzés érkezett</p>
  </td></tr>
  <tr><td style="padding:24px 28px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 4px;font-size:11px;color:#f0a500;font-weight:700;letter-spacing:2px;text-transform:uppercase;">Deal</p>
    <p style="margin:0;font-size:16px;font-weight:700;color:#1a1a1a;">{deal_adatok.get("deal_nev","")}</p>
    <p style="margin:4px 0 0;font-size:12px;color:#888;">{deal_adatok.get("bid_szam","")} · {deal_adatok.get("cegnev","")} · {deal_adatok.get("kapcsolattarto_nev","")}</p>
  </td></tr>
  <tr><td style="padding:20px 28px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 12px;font-size:10px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;">Preferált időpontok</p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%">{ido_sorok}</table>
  </td></tr>
  {"<tr><td style='padding:20px 28px;border-bottom:1px solid #eee;'><p style='margin:0 0 12px;font-size:10px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;'>Belépési / helyszíni követelmények</p><table cellpadding='0' cellspacing='0' border='0' width='100%'>" + kov_sorok + "</table></td></tr>" if kov_sorok else ""}
  {megjegyzes_blokk}
  <tr><td style="background:#f9f9f9;padding:16px 28px;">
    <p style="margin:0;font-size:11px;color:#aaa;">Beküldve: {now} · {deal_adatok.get("bid_szam","")}</p>
  </td></tr>
</table></td></tr></table></body></html>"""


def ugyfel_visszaigazolo_html(deal_adatok: dict, idopontok: list,
                              kovetelmenyek: list, megjegyzes: str) -> str:
    """Visszaigazoló email az ügyfélnek a beküldött adatokról."""

    ido_sorok = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#999;font-size:12px;">{i+1}. opció</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;font-weight:600;">{d}</td></tr>'
        for i, d in enumerate(idopontok)
    ) or '<tr><td colspan="2" style="padding:8px 12px;color:#aaa;">Nem adott meg időpontot.</td></tr>'

    kov_sorok = "".join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;">&#10003; {k["label"]}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#666;">{k.get("reszlet","") or "–"}</td></tr>'
        for k in kovetelmenyek
    )

    megjegyzes_blokk = (
        f'<tr><td colspan="2" style="padding:20px 28px;border-bottom:1px solid #eee;">'
        f'<p style="margin:0 0 8px;font-size:10px;font-weight:700;color:#1a1a1a;letter-spacing:2px;text-transform:uppercase;">Egyéb megjegyzés</p>'
        f'<p style="margin:0;font-size:13px;color:#444;line-height:1.6;">{megjegyzes}</p></td></tr>'
        if megjegyzes else ""
    )

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    bid = deal_adatok.get("bid_szam", "")
    owner_nev = deal_adatok.get("owner_nev", "kollégánk")

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#d8d8d8;font-family:Arial,sans-serif;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#d8d8d8;">
<tr><td align="center" style="padding:32px 16px;">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="background:#fff;max-width:600px;">

  <!-- Fejléc -->
  <tr><td style="background:#1a1a1a;border-bottom:3px solid #f0a500;padding:22px 32px;">
    <p style="margin:0;font-size:20px;font-weight:700;color:#fff;letter-spacing:2px;text-transform:uppercase;">SQM HUNGARY</p>
    <p style="margin:4px 0 0;font-size:9px;color:#888;letter-spacing:3px;text-transform:uppercase;">Visszajelzés visszaigazolása</p>
  </td></tr>

  <!-- Bevezető -->
  <tr><td style="padding:28px 32px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 12px;font-size:15px;color:#1a1a1a;">Tisztelt <strong>{deal_adatok.get("kapcsolattarto_nev","")}</strong>!</p>
    <p style="margin:0;font-size:14px;color:#555;line-height:1.7;">
      Köszönjük visszajelzését a <strong>{bid}</strong> számú árajánlatunkra vonatkozóan.<br>
      Az alábbiakban összefoglaltuk az Ön által megadott adatokat. Kollégánk, <strong>{owner_nev}</strong> hamarosan felveszi Önnel a kapcsolatot a végleges időpont egyeztetése érdekében.
    </p>
  </td></tr>

  <!-- Preferált időpontok -->
  <tr><td style="padding:20px 32px;border-bottom:1px solid #eee;">
    <p style="margin:0 0 12px;font-size:9px;font-weight:700;color:#f0a500;letter-spacing:2px;text-transform:uppercase;">Megadott preferált időpontok</p>
    <table cellpadding="0" cellspacing="0" border="0" width="100%">{ido_sorok}</table>
  </td></tr>

  <!-- Helyszíni követelmények -->
  {"<tr><td style='padding:20px 32px;border-bottom:1px solid #eee;'><p style='margin:0 0 12px;font-size:9px;font-weight:700;color:#f0a500;letter-spacing:2px;text-transform:uppercase;'>Jelzett helyszíni előírások</p><table cellpadding='0' cellspacing='0' border='0' width='100%'>" + kov_sorok + "</table></td></tr>" if kov_sorok else ""}

  <!-- Megjegyzés -->
  {megjegyzes_blokk}

  <!-- Lábléc -->
  <tr><td style="background:#1a1a1a;padding:20px 32px;">
    <table cellpadding="0" cellspacing="0" border="0" width="100%">
      <tr>
        <td style="vertical-align:top;">
          <p style="margin:0 0 3px;font-size:13px;font-weight:700;color:#fff;letter-spacing:1px;text-transform:uppercase;">SQM Hungary Kft.</p>
          <p style="margin:0;font-size:9px;color:#555;letter-spacing:2px;text-transform:uppercase;">Ipari padlóburkolás &amp; bevonatok</p>
        </td>
        <td align="right" style="vertical-align:top;">
          <p style="margin:0 0 2px;font-size:11px;color:#777;">{owner_nev}</p>
          <p style="margin:0 0 2px;font-size:11px;color:#777;"><a href="mailto:{deal_adatok.get('owner_email','')}" style="color:#f0a500;text-decoration:none;">{deal_adatok.get("owner_email","")}</a></p>
          <p style="margin:0;font-size:11px;"><a href="https://sqm-hungary.hu" style="color:#f0a500;text-decoration:none;">sqm-hungary.hu</a></p>
        </td>
      </tr>
    </table>
  </td></tr>

  <tr><td style="background:#0d0d0d;padding:9px 32px;">
    <p style="margin:0;font-size:9px;color:#444;">Beküldve: {now} &nbsp;·&nbsp; {bid} &nbsp;·&nbsp; © 2026 SQM Hungary Kft.</p>
  </td></tr>

</table></td></tr></table></body></html>"""


# ─────────────────────────────────────────────
# IN-MEMORY LOCK – dupla email védelem
# ─────────────────────────────────────────────
_feldolgozott_dealek: set = set()


# ─────────────────────────────────────────────
# FLASK BLUEPRINT – 3 új végpont
# ─────────────────────────────────────────────
pd_bp = Blueprint("pipedrive", __name__)


@pd_bp.route("/pipedrive-webhook", methods=["POST"])
@pd_bp.route("/pipedrive-webhook/", methods=["POST"])
def pipedrive_webhook():
    """
    Pipedrive Automation hívja, ha egy deal won-ra változik.
    """
    adat = request.get_json(silent=True)
    if not adat:
        return jsonify({"ok": False, "hiba": "Üres payload"}), 400

    log.info(f"[WEBHOOK] Payload: {str(adat)[:300]}")

    # ── Deal ID kinyerése ──
    deal_id = None
    if "current" in adat:
        deal_id = adat["current"].get("id")
    elif "deal" in adat:
        deal_id = adat["deal"].get("id")
    elif "dealId" in adat:
        deal_id = adat["dealId"]
    elif "id" in adat:
        deal_id = adat["id"]

    if not deal_id:
        log.warning(f"[WEBHOOK] Nem találtam deal ID-t: {adat}")
        return jsonify({"ok": False, "hiba": "Nincs deal ID"}), 400

    deal_id = int(deal_id)

    # ── 1. In-memory lock – azonnali duplikáció védelem ──
    if deal_id in _feldolgozott_dealek:
        log.info(f"[WEBHOOK] Deal {deal_id} – már feldolgozás alatt, kihagyva.")
        return jsonify({"ok": True, "info": "Már feldolgozás alatt"}), 200
    _feldolgozott_dealek.add(deal_id)

    # ── 2. Pipedrive API státusz ellenőrzés ──
    log.info(f"[WEBHOOK] Deal ID: {deal_id} – státusz ellenőrzés...")
    deal_ellenorzes = _pd_get(f"deals/{deal_id}")

    if not deal_ellenorzes:
        _feldolgozott_dealek.discard(deal_id)
        log.warning(f"[WEBHOOK] Deal nem található: {deal_id}")
        return jsonify({"ok": False, "hiba": "Deal nem található"}), 404

    aktualis_status = deal_ellenorzes.get("status", "")
    log.info(f"[WEBHOOK] Deal {deal_id} státusz: '{aktualis_status}'")

    if aktualis_status != "won":
        _feldolgozott_dealek.discard(deal_id)
        log.info(f"[WEBHOOK] Nem 'won', kihagyva.")
        return jsonify({"ok": True, "info": f"Státusz: {aktualis_status}"}), 200

    # ── 3. Sheets duplikáció védelem ──
    mar_kuldott = _sheets_post("checkDealSent", {"dealId": str(deal_id)})
    if mar_kuldott.get("sent"):
        log.info(f"[WEBHOOK] Deal {deal_id} – Sheets szerint már elküldve.")
        return jsonify({"ok": True, "info": "Email már elküldve"}), 200

    log.info(f"[WEBHOOK] Deal megnyerve, email küldés indul: {deal_id}")

    adatok = deal_adatok_osszerak(deal_id)
    if not adatok:
        _feldolgozott_dealek.discard(deal_id)
        return jsonify({"ok": False, "hiba": "Adatok lekérése sikertelen"}), 500

    token = secrets.token_urlsafe(16)
    token_ment(token, adatok)

    try:
        email_html = sablon_kitolt(sablon_betolt("email_kikuldo.html"), adatok, token)
        targy = f"Kivitelezési tájékoztató – {adatok['bid_szam']}"
        siker = email_kuld(adatok["kapcsolattarto_email"], targy, email_html)
        if siker:
            _sheets_post("markDealSent", {"dealId": str(deal_id), "token": token})
            log.info(f"[WEBHOOK] Email elküldve: {adatok['kapcsolattarto_email']}")
    except Exception as e:
        _feldolgozott_dealek.discard(deal_id)
        log.error(f"[WEBHOOK] Email hiba: {e}")
        return jsonify({"ok": False, "hiba": str(e)}), 500

    return jsonify({"ok": True, "token": token, "bid": adatok["bid_szam"]}), 200


@pd_bp.route("/visszajelzes/<token>", methods=["GET"])
def visszajelzes_oldal(token):
    """Az ügyfél böngészőben nyitja meg."""
    adatok = token_beolvas(token)
    if not adatok:
        return "<h2 style='font-family:sans-serif;padding:40px;color:#c00;'>Ez a link már nem érvényes.</h2>", 404
    try:
        html = sablon_kitolt(sablon_betolt("visszajelzes_oldal.html"), adatok, token)
        return html, 200
    except Exception as e:
        log.error(f"[OLDAL] Render hiba: {e}")
        return "<h2 style='font-family:sans-serif;padding:40px;'>Hiba. Vegye fel a kapcsolatot kollégánkkal.</h2>", 500


@pd_bp.route("/visszajelzes-submit", methods=["POST"])
def visszajelzes_submit():
    """Az ügyfél beküldi a kitöltött adatlapot."""
    adat = request.get_json(silent=True)
    if not adat:
        return jsonify({"ok": False}), 400

    token = adat.get("token", "")
    deal  = token_beolvas(token)
    if not deal:
        return jsonify({"ok": False, "hiba": "Érvénytelen token"}), 404

    idopontok     = adat.get("idopontok", [])
    kovetelmenyek = adat.get("kovetelmenyek", [])
    megjegyzes    = adat.get("megjegyzes", "")

    # ── Owner összesítő email ──
    o_html = owner_email_html(deal, idopontok, kovetelmenyek, megjegyzes)
    o_targy = f"[Visszajelzés] {deal.get('cegnev','')} – {deal.get('bid_szam','')}"
    owner_siker = email_kuld(deal.get("owner_email", ""), o_targy, o_html)

    # ── Ügyfél visszaigazoló email ──
    u_html = ugyfel_visszaigazolo_html(deal, idopontok, kovetelmenyek, megjegyzes)
    u_targy = f"Visszajelzés visszaigazolása – {deal.get('bid_szam','')} | SQM Hungary"
    email_kuld(deal.get("kapcsolattarto_email", ""), u_targy, u_html)

    if owner_siker:
        token_bekuldve(token)
        return jsonify({"ok": True}), 200
    else:
        return jsonify({"ok": False}), 500


def register_pipedrive_routes(app):
    """Hívd meg a server.py-ból: register_pipedrive_routes(app)"""
    app.register_blueprint(pd_bp)
    log.info("[PIPEDRIVE] Végpontok regisztrálva: /pipedrive-webhook, /visszajelzes/<token>, /visszajelzes-submit")
