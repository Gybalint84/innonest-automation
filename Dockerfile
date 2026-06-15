# Microsoft hivatalos Playwright image – minden függőség előre telepítve van
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright böngésző telepítése
RUN playwright install chromium

COPY server.py .
COPY innonest_core.py .
COPY megrendeles_figyelő.py .
COPY arajanlat_feltolto.py .
COPY pipedrive_addon.py .
COPY arajanlat_pdf.py .
COPY sablonok/ ./sablonok/

EXPOSE 5000
CMD ["python", "server.py"]
