"""
server.py – SQM Hungary Innonest Automatizáció
===============================================
Flask app belépési pont. Csak az inicializálást és a route regisztrációkat
tartalmazza — az üzleti logika külön modulokban van:
  innonest_core.py        – Playwright alap (login, session, js_fill, tételkinyerő)
  megrendeles_figyelő.py  – 30 perces polling, megrendelés feldolgozás
                              (SQM Megrendelés Feldolgozó Apps Script, webapp_v8.js)
  arajanlat_feltolto.py   – /create-arajanlat végpont
  pipedrive_addon.py      – Pipedrive webhook + visszajelzési rendszer
                              (SQM Email Küldő Apps Script, webapp_email_v1.js)
  arajanlat_pdf.py        – /pdf-tool, Innonest BID alapú PDF generátor
  pipedrive_webapp.py     – Pipedrive → webapp projekt import pipeline
  innonest_szamlalo.py    – /innonest-counters, Innonest darabszám-lekérdező
  dropbox_mappa_generator.py – /pipedrive-webhook/dropbox-mappa, Dropbox
                                ügyfélmappa auto-generátor deal stage-hez kötve
Környezeti változók (Railway → Variables):
  INNONEST_EMAIL          – Innonest bejelentkezési email
  INNONEST_PASSWORD       – Innonest jelszó
  API_KEY                 – Titkos kulcs az /create-arajanlat és /check-now végpontokhoz
  WEBAPP_SECRET           – Titkos kulcs a "SQM Megrendelés Feldolgozó" Apps
                             Scripthez (webapp_v8.js) — megrendelés-figyelő
                             Sheet-írás + setBidSzam fájlnév-visszaírás
  WEBAPP_URL              – A "SQM Megrendelés Feldolgozó" Apps Script Web App URL-je
  EMAIL_WEBAPP_SECRET     – Titkos kulcs az ÖNÁLLÓ "SQM Email Küldő" Apps
                             Scripthez (webapp_email_v1.js) — ez KÜLÖN projekt/
                             URL, nem ugyanaz, mint a WEBAPP_URL/WEBAPP_SECRET!
  EMAIL_WEBAPP_URL        – A "SQM Email Küldő" Apps Script Web App URL-je
                             (pipedrive_addon.py: sendEmail, PDFquotationSENDdealOWNER)
  PIPEDRIVE_API_TOKEN     – Pipedrive személyes API token
  PIPEDRIVE_BID_FIELD_KEY – BID szám custom mező API kulcsa
  WEBAPP_BASE_URL         – SQM kalkulátor webapp URL-je (pl. https://sqm-hungary.hu/kalkulator/index.html)
  PD_WEBAPP_URL_FIELD     – Pipedrive deal custom field API key a kalkulátor URL visszaíráshoz
  DROPBOX_APP_KEY             – Dropbox App key (App Console → Settings)
  DROPBOX_APP_SECRET          – Dropbox App secret (App Console → Settings)
  DROPBOX_REFRESH_TOKEN       – Dropbox refresh token (egyszeri OAuth flow eredménye)
  DROPBOX_PARENT_FOLDER       – szülőmappa, ahova az új ügyfélmappák kerülnek (="/Ügyfélképek")
  PIPEDRIVE_DROPBOX_FIELD_KEY – Pipedrive deal custom field API key a Dropbox URL visszaíráshoz
  WEBHOOK_SHARED_SECRET       – opcionális, védi a Dropbox webhook végpontot (?secret=... paraméter)

  MEGSZŰNT (nem használt többé, törölhető a Railway Variables-ből):
  GOOGLE_SHEET_ID         – a régi, Sheet-alapú visszajelzési rendszer emléke;
                             a jelenlegi kódban sehol nem történik rá hivatkozás
                             (a visszajelzési adatok Pipedrive note-okban élnek).
"""
import os
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)
# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)
API_KEY = os.environ.get("API_KEY", "titkos-kulcs")
# ── Alap végpontok ────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})
@app.route("/check-now", methods=["POST"])
def check_now():
    """Azonnali megrendelés-ellenőrzés kiváltása manuálisan."""
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        from innonest_core import run_in_loop
        from megrendeles_figyelő import check_megrendelesek
        run_in_loop(check_megrendelesek())
        return jsonify({"status": "ok", "message": "Ellenőrzés lefutott."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
# ── Modulok betöltése és route regisztráció ───────────────────────────────────
# 1. Megrendelés figyelő háttérszál indítása
from megrendeles_figyelő import start_figyelő
start_figyelő()
# 2. Árajánlat feltöltő végpont regisztrálása
from arajanlat_feltolto import register_arajanlat_routes
register_arajanlat_routes(app)
# 3. Pipedrive webhook + visszajelzési rendszer regisztrálása
from pipedrive_addon import register_pipedrive_routes
register_pipedrive_routes(app)
# 4. Árajánlat PDF generátor regisztrálása (/pdf-tool)
from arajanlat_pdf import register_pdf_routes
register_pdf_routes(app)
# 5. Pipedrive → webapp import pipeline regisztrálása
from pipedrive_webapp import register_pipedrive_webapp_routes
register_pipedrive_webapp_routes(app)
# 6. Innonest darabszám-lekérdező végpont regisztrálása (/innonest-counters)
from innonest_szamlalo import register_innonest_szamlalo_routes
register_innonest_szamlalo_routes(app)
# 7. Dropbox mappa-generáló webhook regisztrálása (/pipedrive-webhook/dropbox-mappa)
from dropbox_mappa_generator import register_dropbox_routes
register_dropbox_routes(app)
# ── Indítás ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
