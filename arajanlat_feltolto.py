"""
arajanlat_feltolto.py – Innonest árajánlat feltöltő
====================================================
Playwright-tal feltölti az árajánlatot az Innonestbe.
A /create-arajanlat Flask végpontot regisztrálja.

CALLBACK ARCHITEKTÚRA:
  1. Az Apps Script elküldi a kérést → azonnal 200 OK visszatér (~2 mp)
  2. A Playwright automatizáció a háttérben fut
  3. Ha kész a BID szám, Railway visszahívja a webapp_script.js Web App-ot
  4. A webapp_script.js átnevezi a fájlt [BID-XXXX-NNN]-re
  → Nincs többé timeout probléma
"""

import os
import re
import base64
import logging
import traceback
import asyncio

import requests as req_lib
from flask import request, jsonify
from playwright.async_api import async_playwright

from innonest_core import (
    run_in_loop, login, load_session, make_browser_args,
    js_fill, js_fill_nth, fill_nev, fill_tetel, upload_csatolmany
)

log = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "titkos-kulcs")


# ══════════════════════════════════════════════════════════════════════════════
# ÁRAJÁNLAT TÁRGYA – "ÁTNÉZÉSRE - " ELŐTAG
# ══════════════════════════════════════════════════════════════════════════════
# 2026.08.14: a webappból beküldött árajánlatok tárgya elé kötelezően bekerül
# az "ÁTNÉZÉSRE - " előtag, hogy az Innonest listájában azonnal látszódjon,
# melyik ajánlatot kell még emberi szemmel átnézni a kiküldés előtt.
#
# ⚠️ 2026-09-04: ez a funkció az élesen futó (Railway) verzióban megvolt, de a
# helyi másolatból hiányzott — a felhasználó feltöltötte az élő fájlt
# összevetésre, és ez volt az egyetlen érdemi eltérés. Itt visszapótolva.
TARGY_PREFIX = "ÁTNÉZÉSRE - "

# Idempotencia: ha a tárgy MÁR "ÁTNÉZÉSRE -"-vel kezdődik (bármilyen kis/nagy-
# betűvel, tetszőleges szóközökkel, sima kötőjellel vagy gondolatjellel), nem
# tesszük rá még egyszer. Ez azért kell, mert ugyanaz a projekt többször is
# beküldhető a webappból (pl. javítás után), és nem szeretnénk
# "ÁTNÉZÉSRE - ÁTNÉZÉSRE - ..." halmozódást.
_TARGY_PREFIX_RE = re.compile(r"^\s*ÁTNÉZÉSRE\s*[-–—]\s*", re.IGNORECASE)


def targy_atnezesre(targya: str) -> str:
    """Az árajánlat tárgya elé teszi az "ÁTNÉZÉSRE - " előtagot.

    - üres tárgy esetén csak "ÁTNÉZÉSRE" (nem hagyunk lógó kötőjelet),
    - ha már rajta van az előtag, változatlanul hagyja (lásd _TARGY_PREFIX_RE).
    """
    t = (targya or "").strip()
    if not t:
        return "ÁTNÉZÉSRE"
    if _TARGY_PREFIX_RE.match(t):
        return t
    return TARGY_PREFIX + t


# ══════════════════════════════════════════════════════════════════════════════
# EXTRA ANYAGSOROK (STO/Eurostep/Murexin) — RAKTÁRI TERMÉKKÉNT KIVÁLASZTÁS
# + ELREJTÉS AZ ÜGYFÉL ELŐL
# ══════════════════════════════════════════════════════════════════════════════
#
# 2026-09: a webapp a feladat-tételek után a Rendelési összesítőből extra
# anyagsorokat is felküld (STO/Eurostep/Murexin, beszerzési áron — lásd
# Arajanlat.jsx: InnonestButton). Ezek NEM az ügyfélnek szólnak, csak belső
# nyilvántartásra/beszerzésre — ezért a payload minden ilyen tételén
# `hideFromCustomer: true` jön.
#
# Két lépés történik minden ilyen soron, MINDIG ugyanarra a <tr> elemre
# hivatkozva (nem név alapján újra-keresve — lásd lejjebb, miért fontos ez):
#   1. `_select_material_from_autocomplete` — a nevet valódi gépeléssel újra
#      beírjuk, hogy az Innonest raktárkészlet-kereső legördülője megjelenjen,
#      és a pontosan egyező találatra kattintunk. E NÉLKÜL az Innonest az
#      egyszerű szövegből ÚJ (raktárkészlet nélküli) terméket hozna létre —
#      ezt a felhasználó élesben visszaigazolta (screenshot: "gh 205" beírás
#      → StoPox GH 205 találatok, a 25 kg-os raktáron lévő tétel kiválasztva).
#      Mivel a kiválasztás UTÁN az Innonest felülírhatja a mennyiség/ár
#      mezőket a katalógus-tétel saját adataival, a végén VISSZAÍRJUK a mi
#      (beszerzési) mennyiség/ár/egység/megjegyzés értékeinket
#      (`_reapply_row_values`) — így biztosan a mi számaink maradnak.
#   2. `_toggle_hide_from_customer` — rákattintunk az Innonest „ügyfél elől
#      elrejt" (áthúzott szem, kék) ikonjára a soron.
#
# FONTOS: az autocomplete-kiválasztás megváltoztathatja a name mező LÁTHATÓ
# értékét (pl. a katalógus saját elnevezésére), ezért a hide-lépés NEM kereshet
# újra név alapján — ugyanazt a <tr> elem-referenciát (`row`) adjuk át mindkét
# függvénynek, amit `_find_row_by_name`-mel az EREDETI (fill_tetel által
# beírt) néven találtunk meg, még az átírás előtt.
#
# ⚠️ Az autocomplete legördülő szerkezete (kártyák "Termék mennyiség" szöveggel)
# a felhasználó screenshotja alapján lett feltérképezve, de a pontos
# kattintható elem és az esetleges ár/mennyiség-felülírás élesben még nincs
# visszaigazolva — logolva van minden lépés, hogy Railway logból gyorsan
# lehessen pontosítani, ha valami nem talál.

def _num_eq(a, b, tol=0.01) -> bool:
    """Két, magyar formátumú (szóköz ezres-tagoló, vessző tizedes) számot
    hasonlít össze, tűréssel — mert az Innonest kerekíthet/formázhat."""
    try:
        fa = float(str(a).replace(" ", "").replace("\xa0", "").replace(",", "."))
        fb = float(str(b).replace(" ", "").replace("\xa0", "").replace(",", "."))
        return abs(fa - fb) < tol
    except Exception:
        return False


def _search_term_fallbacks(name: str) -> list:
    """Egyre rövidebb keresési kifejezéseket állít elő a teljes katalógus-
    névből. FONTOS: a teljes, hosszú név (vesszőkkel, méret/kiszerelés
    résszel) élesben gyakran NEM adott találatot az Innonest keresőjében —
    a felhasználó is csak egy rövid részletet ("gh 205") írt be manuálisan,
    nem a teljes nevet. Ezért a kereséshez egyre rövidebb változatokat is
    kipróbálunk, de a TALÁLAT ELFOGADÁSA mindig a TELJES eredeti névvel
    történő egyezés-ellenőrzésen alapul (lásd _select_material_from_autocomplete),
    így egy túl rövid/általános kereső-kifejezés miatt sem választhatunk
    véletlenül rossz terméket."""
    name = (name or "").strip()
    terms = [name] if name else []

    if "," in name:
        head = name.split(",")[0].strip()
        if head and head not in terms:
            terms.append(head)

    words = name.split()
    for n in (3, 2):
        if len(words) > n:
            short = " ".join(words[:n]).strip().rstrip(",")
            if short and short not in terms:
                terms.append(short)

    return terms


async def _find_row_by_name(page, megnevezes: str):
    """Visszaadja annak a <tr>-nek az ElementHandle-jét, aminek productsName
    mezője éppen a megadott névvel egyezik. `fill_tetel` után hívjuk, amíg a
    név még a nyers, beírt szöveg (autocomplete-kiválasztás előtt) — utána
    már NEM szabad név alapján újra keresni, mert a kiválasztás megváltoztatja
    a mező értékét."""
    handle = await page.evaluate_handle(
        r"""
        (nev) => {
            const rows = document.querySelectorAll('tbody.items-box tr.items');
            for (const tr of rows) {
                const inp = tr.querySelector('input[name^="productsName"]');
                if (inp && inp.value.trim() === nev) return tr;
            }
            return null;
        }
        """,
        megnevezes,
    )
    el = handle.as_element()
    if el is None:
        return None
    return el


async def _select_material_from_autocomplete(page, row, megnevezes: str) -> bool:
    """A `row` sorának név mezőjébe valódi gépeléssel újra beírja a nevet
    (hogy az Innonest raktárkészlet-kereső legördülője megjelenjen), majd
    rákattint a pontosan (vagy részlegesen) egyező találatra — így az
    Innonest a meglévő raktári terméket társítja, nem egy új, raktárkészlet
    nélküli tételt hoz létre a puszta szövegből."""
    try:
        name_input = await row.query_selector('input[name^="productsName"]')
        if not name_input:
            log.warning(f"[ANYAG-VALASZTAS] Nem található productsName mező a sorban: '{megnevezes}'")
            return False

        # ⚠️ 2026-09-04, javítás: élesben a TELJES katalógusnév (vesszőkkel,
        # méret/kiszerelés résszel) beírása 0 találatot adott az Innonest
        # keresőjében a legtöbb anyagnál — csak az egyszerűbb "StoPox GH 205"
        # esetén jött vissza eredmény. Ezért most egyre rövidebb keresési
        # kifejezéseket próbálunk (_search_term_fallbacks), amíg találunk
        # egyezést. A találat ELFOGADÁSA mindig a TELJES eredeti névvel
        # (`megnevezes`) történő egyezés-ellenőrzésen alapul, tehát egy
        # rövidebb, általánosabb keresés miatt sem választhatunk véletlenül
        # rossz terméket.
        search_terms = _search_term_fallbacks(megnevezes)
        last_diag = None
        last_counts = (0, 0)

        for attempt, term in enumerate(search_terms):
            await name_input.click()
            await name_input.fill("")
            await name_input.type(term, delay=60)

            # Várakozás a legördülő találatokra (keresés-debounce + XHR
            # válasz) — legfeljebb kb. 2,4 mp-et pollozunk próbálkozásonként.
            candidate_count = 0
            for _ in range(8):
                await page.wait_for_timeout(300)
                candidate_count = await page.evaluate(
                    r"""
                    () => [...document.querySelectorAll('*')].filter(el =>
                        el.textContent && el.textContent.includes('Termék mennyiség') &&
                        el.getBoundingClientRect().width > 0
                    ).length
                    """
                )
                if candidate_count > 0:
                    break

            pick_result = await page.evaluate(
                r"""
                (nev) => {
                    const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    const target = norm(nev);

                    // 1) Levél-szintű "stat" elemek, amik szó szerint
                    //    tartalmazzák a "Termék mennyiség" feliratot.
                    const statEls = [...document.querySelectorAll('*')].filter(el => {
                        if (!el.textContent || !el.textContent.includes('Termék mennyiség')) return false;
                        const rect = el.getBoundingClientRect();
                        if (!(rect.width > 0 && rect.height > 0)) return false;
                        return ![...el.children].some(c => c.textContent && c.textContent.includes('Termék mennyiség'));
                    });

                    // 2) Minden stat-elemtől felfelé lépkedünk (max. 4 szint),
                    //    amíg egy érdemben nagyobb (a nevet is tartalmazó)
                    //    konténert nem találunk.
                    const seen = new Set();
                    const cards = [];
                    for (const stat of statEls) {
                        let card = stat;
                        for (let hop = 0; hop < 4; hop++) {
                            if (!card.parentElement) break;
                            card = card.parentElement;
                            if (card.textContent.trim().length > stat.textContent.trim().length + 15) break;
                        }
                        if (!seen.has(card)) { seen.add(card); cards.push(card); }
                    }

                    let exact = null, partial = null;
                    for (const card of cards) {
                        const firstLine = norm((card.textContent.split('\n')[0] || '').split('Termék')[0]);
                        if (firstLine === target) { exact = card; break; }
                        if (!partial && firstLine.includes(target)) partial = card;
                    }
                    const hit = exact || partial;
                    if (!hit) {
                        const diag = statEls.slice(0, 3).map(el => ({
                            tag: el.tagName,
                            cls: el.className || '',
                            text: el.textContent.trim().slice(0, 80),
                            parentTag: el.parentElement ? el.parentElement.tagName : null,
                            parentCls: el.parentElement ? (el.parentElement.className || '') : '',
                            parentText: el.parentElement ? el.parentElement.textContent.trim().slice(0, 160) : '',
                        }));
                        return { picked: false, candidates: cards.length, statCount: statEls.length, diag };
                    }
                    hit.click();
                    return { picked: true, exact: !!exact, text: hit.textContent.trim().slice(0, 160) };
                }
                """,
                megnevezes,
            )

            if pick_result.get("picked"):
                await page.wait_for_timeout(300)
                log.info(
                    f"[ANYAG-VALASZTAS] Kiválasztva ({'pontos' if pick_result.get('exact') else 'részleges'} "
                    f"egyezés, keresőszó: '{term}'): '{megnevezes}' -> {pick_result.get('text')}"
                )
                return True

            last_diag = pick_result.get("diag") or []
            last_counts = (pick_result.get("candidates", candidate_count), pick_result.get("statCount", 0))
            log.info(
                f"[ANYAG-VALASZTAS] {attempt + 1}/{len(search_terms)}. próbálkozás ('{term}') nem hozott "
                f"egyezést '{megnevezes}'-hez ({last_counts[0]} kártya, {last_counts[1]} stat-elem) — "
                + ("következő, rövidebb keresőszó próbálása..." if attempt + 1 < len(search_terms) else "nincs több próbálkozás.")
            )

        # Egyik próbálkozás sem talált egyezést — a mezőt visszaállítjuk a
        # TELJES eredeti névre (különben az utolsó, legrövidebb keresőszó
        # maradna benne, ami félrevezető lenne az Innonest felületén).
        try:
            await name_input.click()
            await name_input.fill("")
            await name_input.type(megnevezes, delay=30)
        except Exception:
            pass

        log.warning(
            f"[ANYAG-VALASZTAS] Egyik keresőszóval sem találtam egyező legördülő találatot: "
            f"'{megnevezes}' (próbált kifejezések: {search_terms}) — a mező a teljes eredeti "
            f"névvel maradt, ELLENŐRZÉS SZÜKSÉGES (lehet, hogy új anyagként jön létre)."
        )
        for d in last_diag or []:
            log.warning(
                f"[ANYAG-VALASZTAS]   diag: stat=<{d.get('tag')} class='{d.get('cls')}'> "
                f"'{d.get('text')}' | szülő=<{d.get('parentTag')} class='{d.get('parentCls')}'> "
                f"'{d.get('parentText')}'"
            )
        return False
    except Exception as e:
        log.warning(f"[ANYAG-VALASZTAS] Hiba autocomplete-kiválasztás közben ('{megnevezes}'): {e}")
        return False


_REAPPLY_JS = r"""
(args) => {
    const [tr, vals] = args;
    const setVal = (el, v) => {
        if (!el) return;
        el.value = v;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    };
    const qtyInput   = tr.querySelector('input[name^="productsQty"]');
    const priceInput = tr.querySelector('input[name^="productsPrice"]');
    const taInput    = tr.querySelector('textarea');

    setVal(qtyInput, vals.mennyiseg);
    setVal(priceInput, vals.egysegar);
    setVal(taInput, vals.megjegyzes);

    if (priceInput) {
        const allInputs = [...tr.querySelectorAll('input:not([type="checkbox"])')];
        const idx = allInputs.indexOf(priceInput);
        if (idx >= 1) setVal(allInputs[idx - 1], vals.egyseg);
    }
}
"""

_READ_QTY_PRICE_JS = r"""
(tr) => {
    const qtyInput   = tr.querySelector('input[name^="productsQty"]');
    const priceInput = tr.querySelector('input[name^="productsPrice"]');
    return {
        mennyiseg: qtyInput ? qtyInput.value.trim() : null,
        egysegar: priceInput ? priceInput.value.trim() : null,
    };
}
"""


async def _reapply_row_values(page, row, item: dict) -> bool:
    """Az autocomplete-kiválasztás után visszaírja a mi (beszerzési)
    mennyiség/egységár/egység/megjegyzés értékeinket a sorba, arra az esetre,
    ha az Innonest a katalógus-tétel saját adataival felülírta volna őket.
    Az egység mezőt ugyanúgy azonosítjuk, mint az arajanlat_pdf.py scraper:
    a productsPrice mező előtti (nem checkbox) input.

    ⚠️ 2026-09-04: élesben a mennyiség minden törzsadat-linkelt (autocomplete-
    ből kiválasztott) sornál 1-re, az ár a katalógus saját árára állt vissza —
    ANNAK ELLENÉRE, hogy ez a függvény lefutott és logolta a sikert. Ennek
    valószínű oka, hogy az Innonest a termékválasztás után egy ASZINKRON
    (AJAX) újraszámítást indít, ami a mi visszaírásunk UTÁN futott le, és
    felülírta azt. Ezért most nem elég egyszer visszaírni: beírás után
    VISSZAOLVASSUK az értéket, és ha nem egyezik a kívánttal, újra beírjuk
    (max. 3x, növekvő várakozással) — amíg vagy stabilizálódik, vagy
    feladjuk és figyelmeztetünk."""
    megnevezes = item.get("megnevezes", "")
    desired = {
        "mennyiseg": item.get("mennyiseg", ""),
        "egysegar": item.get("egysegar", ""),
        "egyseg": item.get("egyseg", ""),
        "megjegyzes": item.get("megjegyzes", ""),
    }
    try:
        for attempt in range(3):
            await page.evaluate(_REAPPLY_JS, [row, desired])
            await page.wait_for_timeout(500 + attempt * 400)
            current = await page.evaluate(_READ_QTY_PRICE_JS, row)
            if _num_eq(current.get("mennyiseg"), desired["mennyiseg"]) and \
               _num_eq(current.get("egysegar"), desired["egysegar"]):
                log.info(
                    f"[ANYAG-VALASZTAS] Mennyiség/ár visszaírva és visszaigazolva "
                    f"({attempt + 1}. próbálkozásra): '{megnevezes}' — "
                    f"{desired['mennyiseg']} {desired['egyseg']} / {desired['egysegar']}"
                )
                return True
            log.info(
                f"[ANYAG-VALASZTAS] {attempt + 1}. visszaírás után az érték eltér a kívánttól "
                f"(jelenleg: menny={current.get('mennyiseg')}, ár={current.get('egysegar')}; "
                f"kívánt: menny={desired['mennyiseg']}, ár={desired['egysegar']}) — újrapróbálom"
                if attempt < 2 else ""
            )

        log.warning(
            f"[ANYAG-VALASZTAS] A mennyiség/ár NEM stabilizálódott a kívánt értékre "
            f"({desired['mennyiseg']} {desired['egyseg']} / {desired['egysegar']}) 3 próbálkozás "
            f"után sem — valószínűleg az Innonest a törzsadat-kiválasztás után aszinkron módon "
            f"felülírja azokat. '{megnevezes}' — ELLENŐRZÉS SZÜKSÉGES az Innonestben."
        )
        return False
    except Exception as e:
        log.warning(f"[ANYAG-VALASZTAS] Hiba az érték-visszaírásnál ('{megnevezes}'): {e}")
        return False


# ✅ 2026-09-04: a felhasználó élesben (böngésző DevTools) visszaigazolta,
# hogy a gomb megnyomásakor a tétel sorának (<tr class="uppercase items">)
# osztálylistájához hozzáadódik a `hideElementsRow` class. Ez egy MEGBÍZHATÓ,
# ellenőrizhető jel — ezért a kattintás UTÁN ezt a class-t nézzük meg: ha nem
# jelenik meg, tudjuk, hogy rossz elemre kattintottunk (nem csak feltételezzük,
# hogy sikerült). Magát a kattintandó ikont továbbra is kulcsszó alapján
# keressük (ezt nem sikerült még visszaigazolni) — ha a log szerint ez a
# lépés hibázik, onnantól már csak az ikon-keresést kell pontosítani, a
# sikerkritérium (hideElementsRow) biztosan jó.
#
# `row`: ha adott (ElementHandle), ezt a KONKRÉT sort használjuk — ez azért
# fontos, mert az autocomplete-kiválasztás (lásd fent) megváltoztathatja a
# név mező értékét, tehát név alapján már nem biztos, hogy megtalálnánk a
# sort. Ha `row` nincs megadva, a régi, név-alapú keresést használjuk
# (visszafelé kompatibilis, pl. ha a hívó nem rendelkezik row-referenciával).
async def _toggle_hide_from_customer(page, megnevezes: str, row=None) -> bool:
    try:
        if row is not None:
            click_result = await page.evaluate(
                r"""
                (tr) => {
                    if (tr.classList.contains('hideElementsRow')) {
                        return { clicked: false, already: true, reason: 'már rejtett volt' };
                    }
                    const candidates = [...tr.querySelectorAll('button, a, span, i, svg')];
                    const kw = /eye|szem|rejt|hide|visibility_off|slash|elrejt/i;
                    let hit = candidates.find(el => {
                        const t = (el.getAttribute('title') || '') + ' ' +
                                  (el.getAttribute('aria-label') || '') + ' ' +
                                  (el.className && el.className.baseVal !== undefined ? el.className.baseVal : (el.className || '')) + ' ' +
                                  (el.innerHTML || '');
                        return kw.test(t);
                    });
                    if (!hit) return { clicked: false, reason: 'ikon nem található a sorban' };
                    let btn = hit;
                    if (['i', 'svg', 'path'].includes(btn.tagName.toLowerCase())) {
                        btn = btn.closest('button, a') || btn.parentElement;
                    }
                    if (!btn) return { clicked: false, reason: 'kattintható szülő nem található' };
                    btn.click();
                    return { clicked: true, reason: btn.outerHTML.slice(0, 120) };
                }
                """,
                row,
            )
        else:
            click_result = await page.evaluate(
                r"""
                (nev) => {
                    const rows = document.querySelectorAll('tbody.items-box tr.items');
                    let row = null;
                    for (const tr of rows) {
                        const inp = tr.querySelector('input[name^="productsName"]');
                        if (inp && inp.value.trim() === nev) { row = tr; break; }
                    }
                    if (!row) return { clicked: false, reason: 'sor nem található: ' + nev };

                    if (row.classList.contains('hideElementsRow')) {
                        return { clicked: false, already: true, reason: 'már rejtett volt' };
                    }

                    const candidates = [...row.querySelectorAll('button, a, span, i, svg')];
                    const kw = /eye|szem|rejt|hide|visibility_off|slash|elrejt/i;
                    let hit = candidates.find(el => {
                        const t = (el.getAttribute('title') || '') + ' ' +
                                  (el.getAttribute('aria-label') || '') + ' ' +
                                  (el.className && el.className.baseVal !== undefined ? el.className.baseVal : (el.className || '')) + ' ' +
                                  (el.innerHTML || '');
                        return kw.test(t);
                    });
                    if (!hit) return { clicked: false, reason: 'ikon nem található a sorban' };

                    let btn = hit;
                    if (['i', 'svg', 'path'].includes(btn.tagName.toLowerCase())) {
                        btn = btn.closest('button, a') || btn.parentElement;
                    }
                    if (!btn) return { clicked: false, reason: 'kattintható szülő nem található' };
                    btn.click();
                    return { clicked: true, reason: btn.outerHTML.slice(0, 120) };
                }
                """,
                megnevezes,
            )

        if click_result.get("already"):
            log.info(f"[HIDE] '{megnevezes}': már rejtett volt, nincs teendő")
            return True

        if not click_result.get("clicked"):
            log.warning(f"[HIDE] Nem sikerült rákattintani: '{megnevezes}' — {click_result.get('reason')}")
            return False

        # ── VISSZAIGAZOLÁS: tényleg megjelent-e a hideElementsRow class? ──
        await page.wait_for_timeout(300)
        if row is not None:
            verified = await page.evaluate("(tr) => tr.classList.contains('hideElementsRow')", row)
        else:
            verified = await page.evaluate(
                r"""
                (nev) => {
                    const rows = document.querySelectorAll('tbody.items-box tr.items');
                    for (const tr of rows) {
                        const inp = tr.querySelector('input[name^="productsName"]');
                        if (inp && inp.value.trim() === nev) {
                            return tr.classList.contains('hideElementsRow');
                        }
                    }
                    return false;
                }
                """,
                megnevezes,
            )
        if verified:
            log.info(f"[HIDE] Elrejtve az ügyfél elől (hideElementsRow visszaigazolva): '{megnevezes}'")
            return True
        log.warning(
            f"[HIDE] Rákattintottunk ({click_result.get('reason')}), de a 'hideElementsRow' "
            f"class NEM jelent meg — valószínűleg rossz elemre kattintottunk: '{megnevezes}'"
        )
        return False
    except Exception as e:
        log.warning(f"[HIDE] Hiba elrejtés közben ('{megnevezes}'): {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# MEGJEGYZÉS BEÍRÁSA A BID SORBA
# ══════════════════════════════════════════════════════════════════════════════

async def _write_megjegyzes(page, bid_szam: str, szoveg: str, bid_url: str = ""):
    """
    Megnyitja a BID sorának Megjegyzések paneljét, beírja a projekt URL-t,
    majd rákattint a Mehet! gombra.

    bid_url: a mentés utáni URL (pl. /bids/index/0/315723.html) — itt csak ez az
             egy BID látható, az ikonok biztosan elérhetők. Ha üres, a teljes
             listára navigál.
    """
    target = bid_url if bid_url else "https://app.innonest.hu/bids"
    log.info(f"[MEGJEGYZES] Navigálás: {target}")
    await page.goto(target, wait_until="networkidle")
    await page.wait_for_timeout(1500)

    # BID sor megkeresése — csak a sorszám cellában keressük,
    # hogy ne találjon más sor tárgyában szereplő BID hivatkozásra
    row = None
    try:
        row = page.locator(f"td:text-is('{bid_szam}')").first.locator("xpath=ancestor::tr").first
        if not await row.count():
            raise Exception("text-is nem talált")
    except Exception:
        bid_cell = page.locator(f"text={bid_szam}").first
        if not await bid_cell.count():
            log.warning(f"[MEGJEGYZES] Nem találtam a BID sort: {bid_szam}")
            return
        row = bid_cell.locator("xpath=ancestor::tr").first

    log.info(f"[MEGJEGYZES] BID sor megtalálva: {bid_szam}")

    # Megjegyzés ikon keresése a sorban (speech bubble / comment icon)
    # Az ikonok általában NEM button/a tagek, hanem span/i/div elemek!
    comment_btn = None

    # 1. title alapján (bármilyen tag)
    for sel in [
        '[title*="egjegyz"]', '[title*="omment"]',
    ]:
        loc = row.locator(sel).first
        if await loc.count() > 0:
            comment_btn = loc
            log.info(f"[MEGJEGYZES] Ikon megtalálva (title): {sel}")
            break

    # 2. class alapján (bármilyen tag — ikonok lehetnek span, i, div is)
    if comment_btn is None:
        for sel in [
            '[class*="comment"]', '[class*="speech"]',
            '[class*="bubble"]', '[class*="megjegyz"]',
        ]:
            loc = row.locator(sel).first
            if await loc.count() > 0:
                # ha az elem <i> tag, a kattintható szülőt keressük
                tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                if tag == 'i':
                    parent = loc.locator("xpath=..")
                    if await parent.count() > 0:
                        loc = parent
                        log.info(f"[MEGJEGYZES] Ikon (i szülője): {sel}")
                    else:
                        log.info(f"[MEGJEGYZES] Ikon (i tag): {sel}")
                else:
                    log.info(f"[MEGJEGYZES] Ikon megtalálva (class/{tag}): {sel}")
                comment_btn = loc
                break

    # 3. innerHTML alapján — minden button/a/span-t megvizsgálunk
    if comment_btn is None:
        candidates = row.locator('button, a, span, i')
        cnt = await candidates.count()
        log.info(f"[MEGJEGYZES] {cnt} elem a sorban, innerHTML vizsgálat...")
        for idx in range(min(cnt, 10)):
            el = candidates.nth(idx)
            try:
                inner = await el.inner_html()
                cls   = await el.get_attribute('class') or ''
                log.info(f"[MEGJEGYZES] Elem {idx}: class='{cls[:60]}', html='{inner[:80]}'")
                if any(kw in (inner + cls).lower() for kw in ['comment', 'speech', 'bubble', 'megjegyz']):
                    comment_btn = el
                    log.info(f"[MEGJEGYZES] Ikon innerHTML/class alapján: elem {idx}")
                    break
            except Exception:
                pass

    if comment_btn is None:
        log.warning("[MEGJEGYZES] Nem találtam a megjegyzés ikont")
        return

    await comment_btn.scroll_into_view_if_needed()
    await comment_btn.click()

    # Explicit várakozás: megvárjuk hogy a textarea tényleg megjelenjen
    try:
        await page.wait_for_selector('textarea[placeholder="Megjegyzés"]', state='visible', timeout=5000)
        log.info("[MEGJEGYZES] Textarea megjelent")
    except Exception:
        log.warning("[MEGJEGYZES] Textarea 5mp-en belül nem jelent meg, folytatás...")
        await page.wait_for_timeout(1000)

    # Visible textarea keresése (hogy ne egy rejtett mezőbe írjunk)
    textarea = page.locator('textarea[placeholder="Megjegyzés"]:visible').first
    if not await textarea.count():
        textarea = page.locator('textarea[placeholder="Megjegyzés"]').first
    if not await textarea.count():
        log.warning("[MEGJEGYZES] Nem találtam a megjegyzés textarea-t")
        return

    # Mehet! gomb keresése — class="comment-send" az ismert HTML alapján
    mehet = page.locator('button.comment-send, button:has-text("Mehet!")').first
    mehet_cnt = await mehet.count()
    if mehet_cnt > 0:
        try:
            mehet_html = await mehet.evaluate("el => el.outerHTML")
            log.info(f"[MEGJEGYZES] Mehet! gomb HTML: {mehet_html[:200]}")
            mehet_disabled = await mehet.is_disabled()
            log.info(f"[MEGJEGYZES] Mehet! disabled: {mehet_disabled}")
        except Exception as e:
            log.warning(f"[MEGJEGYZES] Mehet! inspect hiba: {e}")
    else:
        log.warning("[MEGJEGYZES] Mehet! gomb NEM látható a panel megnyitása után")

    # Textarea kitöltése + explicit JS event dispatch
    # fill() beírja az értéket, de egyes SPA frameworkök nem érzékelik —
    # ezért manuálisan is kiküldjük az input/change eventeket
    await textarea.click()
    await textarea.fill(szoveg)
    await textarea.evaluate("""el => {
        el.dispatchEvent(new Event('input',  { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }""")
    await page.wait_for_timeout(200)
    # Tab: blur a textarea-ból, hogy a JS tudja a mező elhagyva
    await textarea.press("Tab")
    await page.wait_for_timeout(200)

    # Ellenőrzés: valóban bekerült-e az érték?
    try:
        val = await textarea.input_value()
        log.info(f"[MEGJEGYZES] Textarea értéke beírás után: '{val[:80]}'")
    except Exception:
        pass

    # Mehet! kattintás
    if mehet_cnt > 0:
        # A Mehet! confirm() natív dialógot dob fel — előre regisztráljuk a kezelőt
        async def _accept_dialog(dialog):
            log.info(f"[MEGJEGYZES] Dialog elfogadva: '{dialog.message}'")
            await dialog.accept()

        page.once("dialog", _accept_dialog)

        await mehet.scroll_into_view_if_needed()
        await mehet.click()
        await page.wait_for_timeout(1500)

        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            await page.wait_for_timeout(2000)
        log.info(f"[MEGJEGYZES] Projekt URL beírva ({bid_szam}): {szoveg}")
    else:
        log.warning("[MEGJEGYZES] Nem találtam a Mehet! gombot — mentés kihagyva")


# ══════════════════════════════════════════════════════════════════════════════
# FŐ PLAYWRIGHT AUTOMATIZÁCIÓ
# ══════════════════════════════════════════════════════════════════════════════

async def run_automation(payload: dict):
    ugyfel = payload.get("ugyfel", {})
    targya_eredeti = payload.get("arajanlat_targya", "AI-ÁTNÉZÉSRE")
    # 2026.08.14: minden Innonestbe beküldött árajánlat tárgya elé "ÁTNÉZÉSRE - "
    targya = targy_atnezesre(targya_eredeti)
    if targya != targya_eredeti:
        log.info(f"Árajánlat tárgya előtaggal: {targya_eredeti!r} -> {targya!r}")
    items  = payload.get("items", [])
    log.info("Árajánlat: böngésző indítás...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=make_browser_args()
        )
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page    = await context.new_page()

        await load_session(context)
        await page.goto("https://app.innonest.hu", wait_until="networkidle")
        await page.wait_for_timeout(500)

        if "login" in page.url:
            await login(page)
            await page.goto("https://app.innonest.hu", wait_until="networkidle")
            await page.wait_for_timeout(500)
            if "login" in page.url:
                raise Exception("Bejelentkezés sikertelen!")

        await page.click("text=Munkavégzés")
        await page.wait_for_timeout(500)
        await page.click("text=Árajánlatok")
        await page.wait_for_load_state("networkidle")
        await page.click("text=Új árajánlat")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(800)

        # Screenshot eltávolítva – callback módban a Railway azonnal visszatér,
        # a screenshotok sosem kerülnek felhasználásra, csak lassítanak (~2mp/db)

        # Ügyféladatok
        await fill_nev(page, ugyfel.get("nev", ""))
        if ugyfel.get("adoszam"):
            await js_fill(page, 'input[placeholder="Adószám"]', ugyfel["adoszam"], "Adószám")
        if ugyfel.get("iranyitoszam"):
            await js_fill(page, 'input[placeholder="Irányítószám"]', ugyfel["iranyitoszam"], "Irányítószám")
        if ugyfel.get("telepules"):
            await js_fill(page, 'input[placeholder="Település"]', ugyfel["telepules"], "Település")
        if ugyfel.get("utca"):
            await js_fill(page, 'input[placeholder="Utca"]', ugyfel["utca"], "Utca")
        if ugyfel.get("kapcsolattarto"):
            await js_fill(page, 'input[placeholder="Kapcsolattartó neve"]', ugyfel["kapcsolattarto"], "Kapcsolattartó")

        await js_fill(page, 'input[placeholder="Árajánlat tárgya"]', targya, "Árajánlat tárgya")

        # Tételek
        # ── Innonest template sor viselkedése (tesztek alapján feltérképezve) ────
        #
        # Az "Új árajánlat" form 2 template sorral nyílik meg:
        #   - Sor 0 (data-id=0): SOHA nem mentődik el → hagyjuk üresen
        #   - Sor 1 (data-id>0): mentődik, és a PDF ELEJÉRE kerül
        #
        # Stratégia:
        #   - Sor 0: nem töltjük ki (üresen marad, nem lesz az árajánlatban)
        #   - Sor 1: items[0]-t töltjük be → PDF első helye ✓
        #   - Új sorok (gomb): items[1..n-1] → PDF 2., 3., ... helye ✓
        #   → Végső sorrend: helyes ✅

        log.info(f"{len(items)} tétel feltöltése...")

        # 1. tétel: az 1-es indexű template sorba (sor 0-t kihagyjuk)
        await fill_tetel(
            page, 1,
            megnevezes = items[0]["megnevezes"],
            mennyiseg  = items[0]["mennyiseg"],
            egyseg     = items[0]["egyseg"],
            egysegar   = items[0]["egysegar"],
            megjegyzes = items[0].get("megjegyzes", ""),
        )
        if items[0].get("hideFromCustomer"):
            row0 = await _find_row_by_name(page, items[0]["megnevezes"])
            if row0 is not None:
                await _select_material_from_autocomplete(page, row0, items[0]["megnevezes"])
                await _reapply_row_values(page, row0, items[0])
                await _toggle_hide_from_customer(page, items[0]["megnevezes"], row=row0)
            else:
                log.warning(f"[ANYAG-VALASZTAS] Sor nem található (1. tétel): '{items[0]['megnevezes']}'")

        # 2..n. tételek: minden tételnél új sort adunk hozzá
        for i, item in enumerate(items[1:], start=1):
            uj = page.locator('button:has-text("Új tétel hozzáadása")').first
            await uj.scroll_into_view_if_needed()
            await uj.click()
            await page.wait_for_timeout(800)

            await fill_tetel(
                page, i + 1,   # sor 0 kihagyva: items[1]→idx=2, items[2]→idx=3, stb.
                megnevezes = item["megnevezes"],
                mennyiseg  = item["mennyiseg"],
                egyseg     = item["egyseg"],
                egysegar   = item["egysegar"],
                megjegyzes = item.get("megjegyzes", ""),
            )
            # A STO/Eurostep/Murexin beszerzési-áras anyagsorokat raktári
            # termékként választjuk ki, majd elrejtjük az ügyfél elől (lásd
            # a fejléc-kommentet a fájl elején).
            if item.get("hideFromCustomer"):
                row_n = await _find_row_by_name(page, item["megnevezes"])
                if row_n is not None:
                    await _select_material_from_autocomplete(page, row_n, item["megnevezes"])
                    await _reapply_row_values(page, row_n, item)
                    await _toggle_hide_from_customer(page, item["megnevezes"], row=row_n)
                else:
                    log.warning(f"[ANYAG-VALASZTAS] Sor nem található: '{item['megnevezes']}'")

        await page.wait_for_timeout(300)

        # Mentés
        mentes_ok = False
        for sel in ['button:has-text("Mentés")', 'button[type="submit"]', '.btn-primary']:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.scroll_into_view_if_needed()
                    await loc.click()
                    mentes_ok = True
                    break
            except Exception:
                continue

        if not mentes_ok:
            raise Exception("Nem találtam Mentés gombot!")

        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)
        result_url = page.url
        log.info(f"Mentés utáni URL: {result_url}")

        # Debug: oldal tartalom logolása hogy lássuk mi jelent meg mentés után
        try:
            page_snippet = await page.evaluate(
                "() => document.body ? document.body.innerText.substring(0, 400) : '(üres)'"
            )
            log.info(f"Oldal tartalom mentés után: {page_snippet[:400]}")
        except Exception as e:
            log.warning(f"Oldal tartalom lekérés hiba: {e}")

        bid_szam = await page.evaluate("""
            () => {
                const text = document.body ? document.body.innerText : "";
                const m = text.match(/BID-[0-9]{4}-[0-9]+/);
                return m ? m[0] : null;
            }
        """)

        if bid_szam:
            log.info(f"BID szám: {bid_szam}")

        # Csatolmány feltöltés
        csatolmany = payload.get("csatolmany")
        if csatolmany and csatolmany.get("adat"):
            eredeti_nev = csatolmany.get("nev", "arajanlat.xlsx")
            biztonságos_nev = re.sub(r'[/\\:*?"<>|]', '_', eredeti_nev)
            if biztonságos_nev != eredeti_nev:
                log.info(f"Csatolmány fájlnév javítva: '{eredeti_nev}' → '{biztonságos_nev}'")
            csatolmany = dict(csatolmany)
            csatolmany["nev"] = biztonságos_nev
            await upload_csatolmany(page, csatolmany, bid_szam)

        # Projekt URL beírása a BID Megjegyzések mezőjébe
        # result_url-t adjuk át: ez az egy-BID-et mutató szűrt oldal
        # (pl. /bids/index/0/315723.html) — itt az ikonok biztosan elérhetők
        projekt_url = payload.get("projekt_url", "")
        if bid_szam and projekt_url:
            try:
                await _write_megjegyzes(page, bid_szam, projekt_url, bid_url=result_url)
            except Exception as e:
                log.warning(f"Megjegyzés írás hiba: {e}")

        await browser.close()

    return {
        "ok":       True,
        "url":      result_url,
        "bid_szam": bid_szam,
    }


# ══════════════════════════════════════════════════════════════════════════════
# HÁTTÉRFUTÁS CALLBACK-KEL
# ══════════════════════════════════════════════════════════════════════════════

async def run_automation_background(payload: dict):
    """
    Háttérben futtatja az automatizációt, majd a BID számot
    visszaküldi a webapp_script.js Web App-nak (callback).
    """
    callback_url    = payload.get("callback_url")
    callback_secret = payload.get("callback_secret")
    spreadsheet_id  = payload.get("spreadsheet_id")

    try:
        result   = await run_automation(payload)
        bid_szam = result.get("bid_szam")
        log.info(f"Háttér automatizáció kész. BID: {bid_szam}")

        if not bid_szam:
            log.error("❌ Nem sikerült BID számot kinyerni az Innonestből!")
            return

        if not callback_url:
            log.warning("⚠️  callback_url nincs megadva – fájl átnevezés kihagyva.")
            return

        # Callback hívás a webapp_script.js-nek
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(None, lambda: req_lib.post(
                callback_url,
                json={
                    "secret":         callback_secret,
                    "action":         "setBidSzam",
                    "spreadsheet_id": spreadsheet_id,
                    "bid_szam":       bid_szam,
                },
                timeout=30
            ))
            log.info(f"Callback válasz: {resp.status_code} – {resp.text[:300]}")
        except Exception as cb_err:
            log.error(f"❌ Callback hiba: {cb_err}")

    except Exception as e:
        log.error(f"❌ Háttér automatizáció hiba: {e}")
        log.error(traceback.format_exc())


# ══════════════════════════════════════════════════════════════════════════════
# FLASK VÉGPONT REGISZTRÁCIÓ
# ══════════════════════════════════════════════════════════════════════════════

def register_arajanlat_routes(app):
    """Hívd meg a server.py-ból: register_arajanlat_routes(app)"""

    @app.route("/create-arajanlat", methods=["POST"])
    def create_arajanlat():
        if request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401

        payload = request.get_json()
        if not payload:
            return jsonify({"error": "Hiányzó JSON"}), 400

        from innonest_core import _loop

        if payload.get("callback_url"):
            # ── ÚJ: callback mód ──────────────────────────────────────────
            # Azonnal visszatér, a Playwright a háttérben fut.
            # A BID számot a webapp_script.js callback kapja meg.
            asyncio.run_coroutine_threadsafe(
                run_automation_background(payload),
                _loop
            )
            log.info("Árajánlat háttérbe indítva (callback mód).")
            return jsonify({
                "ok":     True,
                "status": "processing",
                "message": "Árajánlat elkészítése folyamatban. BID callback-en érkezik."
            })
        else:
            # ── RÉGI: szinkron mód (visszafelé kompatibilis) ───────────────
            try:
                from innonest_core import run_in_loop
                result = run_in_loop(run_automation(payload))
                return jsonify(result)
            except Exception as e:
                log.error(f"❌ /create-arajanlat szinkron hiba: {e}")
                log.error(traceback.format_exc())
                return jsonify({"error": str(e)}), 500

    log.info("[ARAJANLAT] Végpont regisztrálva: /create-arajanlat")
