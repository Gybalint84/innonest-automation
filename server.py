"""
server.py – SQM Hungary Innonest Automatizáció
===============================================
Flask app belépési pont. Csak az inicializálást és a route regisztrációkat
tartalmazza — az üzleti logika külön modulokban van:

  innonest_core.py        – Playwright alap (login, session, js_fill, tételkinyerő)
  megrendeles_figyelő.py  – 5 perces polling, megrendelés feldolgozás
  arajanlat_feltolto.py   – /create-arajanlat végpont
  pipedrive_addon.py      – Pipedrive webhook + visszajelzési rendszer

Környezeti változók (Railway → Variables):
  INNONEST_EMAIL          – Innonest bejelentkezési email
  INNONEST_PASSWORD       – Innonest jelszó
  API_KEY                 – Titkos kulcs az /create-arajanlat és /check-now végpontokhoz
  WEBAPP_SECRET           – Titkos kulcs a Google Apps Script Web App-hoz
  WEBAPP_URL              – Google Apps Script Web App URL-je
  PIPEDRIVE_API_TOKEN     – Pipedrive személyes API token
  PIPEDRIVE_BID_FIELD_KEY – BID szám custom mező API kulcsa
  GOOGLE_SHEET_ID         – Fő Google Sheet azonosítója
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


# ── Indítás ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
