# SQM Hungary – Innonest Automatizáció

Automatizált munkafolyamat-kezelő rendszer ipari padlóburkolási projektek kivitelezési előkészítéséhez. A rendszer integrálja a Pipedrive CRM-et, az Innonest árajánlat-kezelőt, a Google Sheets-et és a Gmailt egy összefüggő automatizációs lánccá.

---

## Tartalomjegyzék

- [Áttekintés](#áttekintés)
- [Rendszerarchitektúra](#rendszerarchitektúra)
- [Fájlstruktúra](#fájlstruktúra)
- [Környezeti változók](#környezeti-változók)
- [Telepítés és beállítás](#telepítés-és-beállítás)
- [Működés részletesen](#működés-részletesen)
- [API végpontok](#api-végpontok)
- [Google Apps Script (webapp_script_v6.js)](#google-apps-script)
- [Email sablonok](#email-sablonok)
- [Google Sheets struktúra](#google-sheets-struktúra)
- [Pipedrive beállítás](#pipedrive-beállítás)
- [Hibaelhárítás](#hibaelhárítás)

---

## Áttekintés

A rendszer két fő automatizációt tartalmaz:

### 1. Megrendelés figyelő (eredeti funkció)
Az Innonest megrendelőlapjait figyeli 5 percenként. Ha egy tétel „Megrendelt" státuszra vált, automatikusan:
- Átnevezi a kapcsolódó Google Sheet fájlt (hozzáfűzi: `- MEGRENDELVE`)
- Beírja az adatokat a `QUiCK API` sheet `Megrendelt projektek másolata` lapfülére

### 2. Pipedrive → Kivitelezési tájékoztató (új funkció)
Ha egy Pipedrive deal „Nyert" státuszra kerül, automatikusan:
- Lekéri a deal és az ügyfél adatait a Pipedrive API-ból
- Playwright-tal bescrapeli az Innonestből a BID-hez tartozó árajánlat tételeit
- Egyedi visszajelzési linket generál
- Brandelt HTML emailt küld az ügyfélnek (tételek, feltételek, visszajelzési link)
- Az ügyfél böngészőben kitölti a visszajelzési adatlapot (időpontok, helyszíni előírások)
- A beküldött adatokból összesítő emailt küld az ownernek és az ügyfélnek
- Ha több alvállalkozó szerepel a kalkulátorban, minden alvállalkozónak külön emailt generál a saját tételeivel

---

## Rendszerarchitektúra

```
Pipedrive (deal nyert)
    ↓ Automation webhook
Railway – server.py + pipedrive_addon.py
    ├── Pipedrive API (deal adatok)
    ├── Innonest Playwright scraping (tételek)
    └── Google Apps Script Web App
            ├── Gmail küldés
            ├── Google Sheets (token tárolás, duplikáció védelem)
            └── Google Drive (BID sheet keresés)

Ügyfél böngészője
    ↓ visszajelzés-submit POST
Railway – /visszajelzes-submit
    ├── Google Sheets (alvállalkozói kalkuláció lekérés)
    ├── Owner összesítő email (Gmail via Apps Script)
    ├── Kivitelezőnkénti emailek (Gmail via Apps Script)
    └── Ügyfél visszaigazoló email (Gmail via Apps Script)
```

---

## Fájlstruktúra

```
innonest-automation/
│
├── server.py                    # Flask app, Playwright alapfunkciók,
│                                # megrendelés figyelő, árajánlat feltöltő
│
├── pipedrive_addon.py           # Pipedrive webhook + visszajelzési rendszer
│                                # (importálva a server.py-ból)
│
├── sablonok/
│   ├── email_kikuldo.html       # Email-safe HTML: az ügyfélnek küldött levél
│   └── visszajelzes_oldal.html  # Böngészős interaktív adatlap (Railway-en hostolva)
│
├── Dockerfile                   # Playwright + Python konténer
├── requirements.txt             # Python függőségek
│
└── docs/
    └── sqm_automatizacio.pdf    # Folyamatábra (ez a dokumentum)
```

---

## Környezeti változók

A Railway **Variables** fülén kell beállítani:

| Változó | Leírás | Kötelező |
|---|---|---|
| `INNONEST_EMAIL` | Innonest bejelentkezési email | ✅ |
| `INNONEST_PASSWORD` | Innonest jelszó | ✅ |
| `API_KEY` | Titkos kulcs a `/check-now` és `/create-arajanlat` végpontokhoz | ✅ |
| `WEBAPP_SECRET` | Titkos kulcs a Google Apps Script Web App-hoz | ✅ |
| `WEBAPP_URL` | Google Apps Script Web App URL-je (`/exec` végű) | ✅ |
| `PIPEDRIVE_API_TOKEN` | Pipedrive személyes API token | ✅ |
| `PIPEDRIVE_BID_FIELD_KEY` | A BID szám custom mező API kulcsa Pipedrive-ban | ✅ |
| `GOOGLE_SHEET_ID` | A fő Google Sheet azonosítója (URL-ből, `/d/` és `/edit` között) | ✅ |
| `RAILWAY_PUBLIC_DOMAIN` | Railway automatikusan tölti ki (pl. `sqm-visszajelzes.up.railway.app`) | 🤖 auto |

> **Fontos:** A `RAILWAY_PUBLIC_DOMAIN`-t a Railway automatikusan állítja be — nem kell kézzel megadni. Ebből rakja össze a rendszer a visszajelzési linkeket (`https://DOMAIN/visszajelzes/TOKEN`).

---

## Telepítés és beállítás

### 1. Railway deployment
A repo automatikusan deployolódik minden GitHub push után. A Dockerfile kezeli a Playwright + Chromium telepítését.

### 2. Google Apps Script beállítás
1. Nyisd meg: [script.google.com](https://script.google.com)
2. Nyisd meg az **„Innonest megrendelve – excel név módosítás"** projektet
3. Másold be a `webapp_script_v6.js` teljes tartalmát (Ctrl+A → töröl → beillesztés)
4. Mentés (💾)
5. **Deploy → Manage deployments → ceruza → New version → Deploy**
6. Az URL marad ugyanaz — nem kell máshol frissíteni

### 3. Pipedrive Automation beállítás
**Settings → Automatizálás → Új automatizáció:**

```
Trigger:  Üzlet frissítve → Üzlet állapota
Feltétel: Üzlet állapota erre változott: nyert
Művelet:  Webhook kérés küldése
  Webhook:  SQM Visszajelzés
  Metódus:  POST
  Tartalom: Kulcs-érték
    Kulcs:  dealId
    Érték:  [Üzletazonosító chip]
```

### 4. Pipedrive Webhook beállítás (alternatív)
**Settings → Tools and apps → Webhooks → Add webhook:**
- Event: `updated.deal`
- URL: `https://sqm-visszajelzes.up.railway.app/pipedrive-webhook/`
- Method: POST

> A kód mindkét formátumot kezeli.

---

## Működés részletesen

### A. Megrendelés figyelő (5 percenként fut)

```
1. Playwright bejelentkezik az Innonestbe (session cache-sel)
2. Lekéri az összes „Megrendelt" státuszú tételt a megrendelőlapokról
3. Minden új BID számra:
   a. Duplikáció ellenőrzés (JSON fájl + Sheets)
   b. Apps Script-en keresztül:
      - Google Drive-on megkeresi a BID számhoz tartozó sheetet
      - Átnevezi: "[eredeti név] - MEGRENDELVE"
      - Kiolvas a G11 cellából (Árajánlat lapfül)
      - Beírja az adatokat a QUiCK API sheetbe
4. Következő ellenőrzés 5 perc múlva
```

### B. Pipedrive webhook → Ügyfél email

```
1. Pipedrive Automation tüzeli a webhookot (deal nyert)
2. In-memory lock: azonnali duplikáció védelem
3. Pipedrive API: valóban "won" státusz? (double check)
4. Google Sheets: küldtünk-e már emailt ehhez a dealhez?
5. Pipedrive API: cég, kapcsolattartó, owner adatok lekérése
6. Innonest Playwright: BID alapján megkeresi az árajánlatot,
   megnyitja a szerkesztőt (/bids/change/ID), kinyeri:
   - Tételek (megnevezés, mennyiség, egységár, nettó összesen)
   - Nettó végösszeg (input.fullTotalNett)
   - Fizetési feltétel
   - Pénznem
7. Egyedi token generálása (16 bájt, URL-safe)
8. Token + deal adatok → Google Sheets ("Visszajelzés tokenek" lapfül)
9. email_kikuldo.html sablon kitöltése az adatokkal
10. Apps Script → Gmail: tájékoztató email az ügyfélnek
11. Google Sheets: "elküldve" feljegyzés a dealhez
```

### C. Ügyfél visszajelzési folyamat

```
1. Ügyfél megnyitja a linket: /visszajelzes/{token}
2. Railway kiszolgálja a visszajelzes_oldal.html-t (adatokkal kitöltve)
3. Ügyfél kitölti:
   - Preferált időpontok (tól-ig, Flatpickr naptárral)
   - Helyszíni előírások (pipálós kártyák részletezővel)
   - Egyéb megjegyzés
4. "Visszajelzés küldése" gomb → POST /visszajelzes-submit
5. Railway:
   a. Google Sheets: "Alvállalkozó díjkalkulátor" lapfül lekérése
   b. Kivitelezők csoportosítása ("Kitől kell megrendelni" oszlop)
   c. Owner összesítő email küldése (minden tétel + kalkuláció)
   d. Ha több kivitelező: kivitelezőnkénti email (csak saját tételek)
   e. Ügyfél visszaigazoló email küldése
   f. Google Sheets: token "beküldve" időbélyeg
```

---

## API végpontok

| Végpont | Metódus | Auth | Leírás |
|---|---|---|---|
| `/health` | GET | — | Szerver állapot |
| `/check-now` | POST | `X-API-Key` | Azonnali megrendelés ellenőrzés |
| `/create-arajanlat` | POST | `X-API-Key` | Árajánlat feltöltés Innonestbe |
| `/pipedrive-webhook` | POST | — | Pipedrive Automation hívja |
| `/visszajelzes/<token>` | GET | — | Visszajelzési adatlap kiszolgálása |
| `/visszajelzes-submit` | POST | — | Visszajelzés beküldése |

### `/create-arajanlat` payload példa
```json
{
  "ugyfel_nev": "Példa Kft.",
  "tetelek": [
    {
      "megnevezes": "Műgyanta bevonat",
      "mennyiseg": 250,
      "egyseg": "m2",
      "egysegar": 4800
    }
  ]
}
```

---

## Google Apps Script

**Fájl:** `webapp_script_v6.js`  
**Projekt neve:** Innonest megrendelve – excel név módosítás

### Action-ok (doPost routing)

| `action` | Leírás |
|---|---|
| *(nincs action)* | Eredeti megrendelés feldolgozás (BID, sheet átnevezés, Sheets írás) |
| `sendEmail` | Gmail küldés (`to`, `subject`, `htmlBody`) |
| `saveToken` | Token + deal JSON mentése a sheetbe |
| `getToken` | Token alapján deal JSON visszaolvasása |
| `markTokenSent` | Token „beküldve" időbélyeg |
| `checkDealSent` | Küldtünk-e már emailt a dealhez? |
| `markDealSent` | Deal „elküldve" feljegyzés |
| `getKalkulacio` | Alvállalkozó díjkalkulátor adatok BID alapján |

### Szükséges OAuth scope-ok (`appsscript.json`)
```json
{
  "oauthScopes": [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/script.external_request"
  ]
}
```

---

## Email sablonok

### `sablonok/email_kikuldo.html`
Az ügyfélnek küldött email. Email-safe `<table>`-alapú HTML, minden kliensnél (Gmail, Outlook, Exchange) működik.

**Változók:**
| Változó | Forrás |
|---|---|
| `{{BID_SZAM}}` | Pipedrive custom mező |
| `{{DATUM}}` | Pipedrive `won_time` |
| `{{NETTO_OSSZEG}}` | Innonest scraping |
| `{{PENZNEM}}` | Innonest scraping |
| `{{CEGNEV}}`, `{{KAPCSOLATTARTO_NEV}}`, `{{KAPCSOLATTARTO_EMAIL}}`, `{{KAPCSOLATTARTO_TELEFON}}` | Pipedrive |
| `{{OWNER_NEV}}`, `{{OWNER_EMAIL}}`, `{{OWNER_TELEFON_SOR}}` | Pipedrive deal owner |
| `{{TETELEK_EMAIL_HTML}}` | Innonest scraping (generált `<tr>` sorok) |
| `{{FIZETESI_FELTETELEK}}`, `{{ERVENYES_IG}}` | Innonest scraping |
| `{{VISSZAJELZES_URL}}` | `BASE_URL/visszajelzes/TOKEN` |

### `sablonok/visszajelzes_oldal.html`
A böngészős interaktív adatlap. Flatpickr naptár a dátumválasztáshoz.

**Interaktív elemek:**
- Tól-ig dátumválasztó (Flatpickr range picker, 3 opció)
- Pipálós kártyák részletező szövegmezőkkel (belépési adatok, felszerelés, oktatás, szállítólevél)
- Egyéb megjegyzés szövegmező
- Sikeres beküldés → köszönőképernyő (az oldal újratöltés nélkül cserélődik)

---

## Google Sheets struktúra

**Fő sheet:** `QUiCK API működő, bejövő számlák szerkesztve`

| Lapfül | Tartalom |
|---|---|
| `Megrendelt projektek másolata` | Megrendelések adatai (cég, tárgy, pénznem, nettó, G11) |
| `Feldolgozott_BID` | Már feldolgozott BID számok (duplikáció védelem) |
| `Visszajelzés tokenek` | Visszajelzési tokenek + deal JSON adatok |
| `Elküldött emailek` | Deal ID → token → küldési időbélyeg |

**BID-hez tartozó sheet** (Google Drive-on, a deal száma alapján keresve):

| Lapfül | Tartalom |
|---|---|
| `Árajánlat` | Az árajánlat adatai (G11 = alvállalkozói díj összege) |
| `Alvállalkozó díjkalkulátor` | Feladatok, mennyiségek, árak, napok, emberek, kivitelező neve (C–L oszlopok, 34. sortól) |

---

## Pipedrive beállítás

### BID szám custom mező
- **Típus:** Text
- **Név:** pl. „BID szám"
- **API kulcs:** `PIPEDRIVE_BID_FIELD_KEY` Railway változóba kell beírni
- Az API kulcs megtalálható: `Settings → Data fields → Deals → [mező neve] → API key`

### Deal owner adatok
A rendszer a deal ownerétől (felelős kolléga) veszi az email és telefonszámot, ezek jelennek meg az email láblécében és a visszajelzési oldalon.

---

## Hibaelhárítás

### Dupla email küldés
A rendszer háromrétegű védelemmel rendelkezik:
1. **In-memory lock** – ugyanaz a deal ID nem futhat párhuzamosan
2. **Pipedrive API check** – csak valódi `won` státusznál fut le
3. **Sheets ellenőrzés** – ha már elküldtük, kihagyja

Ha mégis dupla email megy ki, nézd meg a **„Elküldött emailek"** lapfület a sheetben.

### Innonest scraping nem talál tételeket
A `/bids/change/ID` URL-t nyitja meg a szerkesztő. Ha a tételek üresek a logban:
- Ellenőrizd hogy a BID szám létezik-e az Innonestben
- A `[TETELEK] Kinyert tételek száma: 0` log sor után nézd meg az URL-t
- A `input[name^="productsName"]` szelektor a tételneveket keresi — ha az Innonest frissítette a HTML struktúrát, a szelektor frissítést igényelhet

### Apps Script jogosultsági hiba
Ha `does not have permission` hibát látsz:
1. Apps Script → Project Settings → `appsscript.json` megjelenítése
2. Add hozzá a hiányzó scope-ot (leggyakrabban: `gmail.send`)
3. Mentés → új verzió deploy → fogadd el az új engedélyeket

### Railway build hiba
A Dockerfile `mcr.microsoft.com/playwright/python:v1.44.0-jammy` image-t használ — ez tartalmaz minden Playwright függőséget. Ha a build meghiúsul, ellenőrizd a `requirements.txt`-et.

---

## Verzióhistória

| Verzió | Változások |
|---|---|
| v1 | Innonest megrendelés figyelő + Google Sheets írás |
| v2 | Pipedrive webhook + tájékoztató email küldés |
| v3 | Visszajelzési adatlap (böngészős, Railway-en hostolva) |
| v4 | Alvállalkozói díjkalkulátor az owner emailben |
| v5 | Kivitelezőnkénti email szétválasztás |
| v6 (aktuális) | Flatpickr naptár, dupla email védelem, ügyfél visszaigazoló email |

---

*SQM Hungary Kft. — Belső dokumentáció — Bizalmasan kezelendő*
