# Microsoft hivatalos Playwright image – minden függőség előre telepítve van
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright böngésző telepítése
RUN playwright install chromium

COPY server.py .

EXPOSE 5000

CMD ["python", "server.py"]
