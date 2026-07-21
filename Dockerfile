# Sima Python image (Docker Hub) – elkerüli a Microsoft Container Registry
# (mcr.microsoft.com) jelenlegi 401-es hibáját, ami a Playwright base image-eket érinti.
FROM python:3.11-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Playwright Chromium + szükséges rendszer-függőségek telepítése
RUN playwright install --with-deps chromium
COPY server.py .
COPY innonest_core.py .
COPY innonest_szamlalo.py .
COPY megrendeles_figyelő.py .
COPY arajanlat_feltolto.py .
COPY pipedrive_addon.py .
COPY arajanlat_pdf.py .
COPY pipedrive_webapp.py .
COPY dropbox_mappa_generator.py .
COPY sablonok/ ./sablonok/
EXPOSE 5000
CMD ["python", "server.py"]
