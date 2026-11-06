"""Servicelaag: caching, export en vergelijking tussen bronnen."""

from __future__ import annotations

import csv
import io
import threading
import time

from . import database
from .artikelen import extraheer_hoofdstuk
from .bronnen import BRONNEN, SCRAPERS, Hoofdstuk, Resultaat, hoofdstuk_sorteersleutel
from .tables import SOORT_AGENTSCHAPPEN

CACHE_TTL = 60 * 60  # seconden

# Verse scrape-resultaten worden automatisch in de SQL-database opgeslagen.
OPSLAAN = True
DB_PAD: str | None = None  # None = database.STANDAARD_PAD (omgevingsvariabele BEGROTING_DB)

_cache: dict[tuple[str, int, str], tuple[float, Resultaat]] = {}
_locks: dict[tuple[str, int, str], threading.Lock] = {}
_meta_lock = threading.Lock()


def haal_op(bron: str, jaar: int, fase: str, vernieuw: bool = False) -> Resultaat:
    """Scrape (of lees uit de cache) één bron voor een jaar en fase."""
    sleutel = (bron, jaar, fase)
    with _meta_lock:
        lock = _locks.setdefault(sleutel, threading.Lock())
    with lock:
        if not vernieuw and sleutel in _cache:
            tijd, res = _cache[sleutel]
            if time.time() - tijd < CACHE_TTL:
                return res
        res = SCRAPERS[bron]().scrape(jaar, fase)
        if not res.fout:
            _cache[sleutel] = (time.time(), res)
            if OPSLAAN:
                vul_namen_aan(res)
                res.opslag = database.sla_op(res, DB_PAD)
        return res


def wis_cache() -> None:
    _cache.clear()


def aantal_agentschappen(h: Hoofdstuk) -> int:
    for t in h.tabellen:
        if t.soort == SOORT_AGENTSCHAPPEN:
            return sum(1 for r in t.rijen if r.soort == "data")
    return 0


def vergelijk(resultaten: list[Resultaat]) -> list[dict]:
    """Zet de hoofdstukken van meerdere bronnen naast elkaar.

    Per hoofdstuk wordt gecontroleerd of de totaalregels van alle tabellen in
    beide bronnen gelijk zijn.
    """
    codes: dict[str, dict[str, Hoofdstuk]] = {}
    for res in resultaten:
        for h in res.hoofdstukken:
            codes.setdefault(h.code, {})[res.bron] = h
    rijen = []
    for code in sorted(codes, key=hoofdstuk_sorteersleutel):
        per_bron = codes[code]
        naam = next((h.naam for h in per_bron.values() if h.naam), "")
        for h in per_bron.values():
            if not h.naam:
                h.naam = naam
        bruikbaar = [h for h in per_bron.values() if not h.fout and h.tabellen]
        status = None
        if len(resultaten) > 1:
            if len(bruikbaar) < len(resultaten):
                status = "onvolledig"
            else:
                # Vergelijk op bedragen; kolomkoppen verschillen soms in schrijfwijze.
                totalen = [[list(t.totaal().values()) for t in h.tabellen] for h in bruikbaar]
                status = "gelijk" if all(t == totalen[0] for t in totalen) else "verschil"
        rijen.append({"code": code, "naam": naam, "per_bron": per_bron, "status": status})
    return rijen


_NAMEN: dict[tuple[int, str, str], str] = {}


def vul_namen_aan(res: Resultaat) -> None:
    """Hoofdstuknamen die in een bron ontbreken aanvullen uit eerder opgehaalde bronnen."""
    for h in res.hoofdstukken:
        sleutel = (res.jaar, res.fase, h.code)
        if h.naam:
            _NAMEN[sleutel] = h.naam
        else:
            h.naam = _NAMEN.get(sleutel, "")


CSV_KOLOMMEN = [
    "bron",
    "jaar",
    "fase",
    "hoofdstuk",
    "hoofdstuknaam",
    "onderdeel",
    "tabel_nr",
    "regelsoort",
    "artikelnummer",
    "artikelnaam",
    "artikelsoort",
    "stand",
    "bedragsoort",
    "kolom",
    "bedrag",
    "tabel_titel",
]


def naar_csv(resultaten: list[Resultaat]) -> str:
    """CSV met één regel per bedrag van een artikel of baten-lastendienst (zonder (sub)totalen).

    Puntkomma-gescheiden zodat Excel-NL het direct goed opent.
    """
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_KOLOMMEN, delimiter=";")
    w.writeheader()
    for res in resultaten:
        for h in res.hoofdstukken:
            for nr, (t, ex) in enumerate(zip(h.tabellen, extraheer_hoofdstuk(h.tabellen)), start=1):
                for r in ex.regels:
                    w.writerow(
                        {
                            "bron": BRONNEN[res.bron],
                            "jaar": res.jaar,
                            "fase": res.fase,
                            "hoofdstuk": h.code,
                            "hoofdstuknaam": h.naam,
                            "onderdeel": t.onderdeel or h.code,
                            "tabel_nr": nr,
                            "regelsoort": r.regelsoort,
                            "artikelnummer": r.artikelnummer,
                            "artikelnaam": r.artikelnaam,
                            "artikelsoort": r.artikelsoort,
                            "stand": r.stand,
                            "bedragsoort": r.bedragsoort,
                            "kolom": r.kolom,
                            "bedrag": r.bedrag,
                            "tabel_titel": t.titel,
                        }
                    )
    return buf.getvalue()
