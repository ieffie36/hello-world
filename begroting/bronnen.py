"""Scrapers voor rijksfinancien.nl en officielebekendmakingen.nl.

Beide halen per begrotingshoofdstuk de tabellen uit het *voorstel van wet*
(de begrotingswet): de departementale begrotingsstaat en de begrotingsstaat
inzake de agentschappen (baten-lastendiensten).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

from .tables import SOORT_OVERIG, Tabel, normaliseer_tekst, parse_html, parse_xml

log = logging.getLogger(__name__)

USER_AGENT = "begroting-scraper/1.0 (Flask; +https://github.com/ieffie36/hello-world)"
TIMEOUT = 30
MAX_WORKERS = 4

RIJKSFINANCIEN = "https://www.rijksfinancien.nl"
OB_ZOEK = "https://zoek.officielebekendmakingen.nl"

# Begrotingsmomenten. ``ob`` beschrijft hoe het wetsvoorstel op
# officielebekendmakingen.nl te vinden is: zoekterm(en) in de dossiertitel en
# de verschuiving van het vergaderjaar t.o.v. het begrotingsjaar.
FASEN: dict[str, dict] = {
    "OWB": {
        "naam": "Ontwerpbegroting (begrotingswet)",
        "ob": {"titel": ["Vaststelling van de begrotingsstaten"], "vergaderjaar_start": -1},
    },
    "1SUPP": {
        "naam": "Eerste suppletoire begroting (Voorjaarsnota)",
        "ob": {"titel": ["Wijziging van de begrotingsstaten", "Voorjaarsnota"], "vergaderjaar_start": -1},
    },
    "SBS": {
        "naam": "Septembersuppletoire begroting (Miljoenennota)",
        "vanaf": 2023,
        "ob": {"titel": ["Wijziging van de begrotingsstaten", "Miljoenennota"], "vergaderjaar_start": 0},
    },
    "2SUPP": {
        "naam": "Tweede suppletoire begroting (Najaarsnota)",
        "ob": {"titel": ["Wijziging van de begrotingsstaten", "Najaarsnota"], "vergaderjaar_start": 0},
    },
    "JV": {
        "naam": "Slotwet (jaarverslag)",
        "ob": {"titel": ["Jaarverslag en slotwet"], "vergaderjaar_start": 0},
    },
}

BRONNEN = {
    "rijksfinancien": "rijksfinancien.nl",
    "officielebekendmakingen": "officielebekendmakingen.nl",
}

# Volgorde van hoofdstukken zoals in de Miljoenennota.
HOOFDSTUK_VOLGORDE = [
    "I", "IIA", "IIB", "III", "IV", "V", "VI", "VII", "VIII", "IXA", "IXB", "IX", "X", "XI",
    "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX", "XXI", "XXII", "XXIII",
    "A", "B", "C", "F", "H", "J", "K", "L", "M",
]  # fmt: skip


def hoofdstuk_sorteersleutel(code: str) -> tuple[int, str]:
    try:
        return (HOOFDSTUK_VOLGORDE.index(code), code)
    except ValueError:
        return (len(HOOFDSTUK_VOLGORDE), code)


class ScraperFout(Exception):
    pass


@dataclass
class Hoofdstuk:
    code: str
    naam: str = ""
    url: str = ""
    tabellen: list[Tabel] = field(default_factory=list)
    fout: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "naam": self.naam,
            "url": self.url,
            "fout": self.fout,
            "tabellen": [t.to_dict() for t in self.tabellen],
        }


@dataclass
class Resultaat:
    bron: str
    jaar: int
    fase: str
    hoofdstukken: list[Hoofdstuk] = field(default_factory=list)
    fout: str | None = None

    def to_dict(self) -> dict:
        return {
            "bron": self.bron,
            "jaar": self.jaar,
            "fase": self.fase,
            "fase_naam": FASEN[self.fase]["naam"],
            "fout": self.fout,
            "hoofdstukken": [h.to_dict() for h in self.hoofdstukken],
        }


_TITEL_RES = [
    re.compile(
        r"begrotings?sta(?:at|ten)\s+(?:van|voor)\s+(?:het\s+|de\s+)?(?P<naam>.+?)\s*(?:\((?P<code>[^)]+)\)\s*)?voor het jaar",
        re.IGNORECASE,
    ),
    re.compile(
        r"slotwet\s+(?:en\s+jaarverslag\s+)?(?:van\s+|inzake\s+)?(?:het\s+|de\s+)?(?P<naam>.+?)\s*(?:\((?P<code>[^)]+)\)\s*)?(?:over\s+(?:het\s+jaar\s+)?|voor\s+het\s+jaar\s+)?\d{4}",
        re.IGNORECASE,
    ),
]


def naam_uit_titel(titel: str) -> str:
    """Haal de naam van het begrotingshoofdstuk uit een (dossier- of tabel)titel."""
    for patroon in _TITEL_RES:
        m = patroon.search(titel)
        if m:
            naam = m.group("naam").strip(" ,")
            return naam[0].upper() + naam[1:]
    return ""


def alleen_begrotingsstaten(tabellen: list[Tabel]) -> list[Tabel]:
    """Laat tabellen weg die geen begrotingsstaat zijn.

    Een enkele pagina bevat per abuis tabellen uit de memorie van toelichting
    (bijv. 'Budgettaire gevolgen van beleid'); die horen niet bij de wet.
    """
    return [t for t in tabellen if t.soort != SOORT_OVERIG]


# ----------------------------------------------------------------------- HTTP


class Http:
    """Kleine wrapper om ``requests`` met retries en een beleefde pauze."""

    def __init__(self, pauze: float = 0.2, pogingen: int = 3):
        self._lokaal = threading.local()
        self.pauze = pauze
        self.pogingen = pogingen

    @property
    def sessie(self) -> requests.Session:
        if not hasattr(self._lokaal, "sessie"):
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            self._lokaal.sessie = s
        return self._lokaal.sessie

    def get(self, url: str, **kwargs) -> requests.Response:
        laatste: Exception | None = None
        for poging in range(self.pogingen):
            try:
                r = self.sessie.get(url, timeout=TIMEOUT, **kwargs)
                if r.status_code in (429, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
                time.sleep(self.pauze)
                return r
            except requests.RequestException as e:
                laatste = e
                time.sleep(1.5 * (poging + 1))
        raise ScraperFout(f"Ophalen mislukt: {url} ({laatste})")


# ------------------------------------------------------------- rijksfinancien.nl


class RijksfinancienScraper:
    bron = "rijksfinancien"

    def __init__(self, http: Http | None = None):
        self.http = http or Http()

    def hoofdstukken(self, jaar: int, fase: str) -> list[str]:
        """Lees de hoofdstukcodes uit het overzicht van de memorie van toelichting."""
        url = f"{RIJKSFINANCIEN}/memorie-van-toelichting/{jaar}/{fase}"
        r = self.http.get(url)
        if r.status_code != 200:
            raise ScraperFout(f"Geen overzicht voor {jaar}/{fase} op rijksfinancien.nl (HTTP {r.status_code})")
        patroon = re.compile(rf"^/memorie-van-toelichting/{jaar}/{fase}/([A-Z]+)$")
        codes = {
            m.group(1)
            for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True)
            if (m := patroon.match(a["href"]))
        }
        return sorted(codes, key=hoofdstuk_sorteersleutel)

    def hoofdstuk(self, jaar: int, fase: str, code: str) -> Hoofdstuk:
        url = f"{RIJKSFINANCIEN}/voorstel-van-wet/{jaar}/{fase}/{code}"
        h = Hoofdstuk(code=code, url=url)
        try:
            r = self.http.get(url)
            if r.status_code != 200:
                h.fout = f"Pagina niet beschikbaar op rijksfinancien.nl (HTTP {r.status_code})"
                return h
            h.tabellen = alleen_begrotingsstaten(parse_html(r.text))
            soup = BeautifulSoup(r.text, "html.parser")
            kop = soup.find("h1")
            h.naam = naam_uit_titel(h.tabellen[0].titel) if h.tabellen else ""
            if not h.naam and kop:
                h.naam = normaliseer_tekst(kop.get_text(" "))
            if not h.tabellen:
                h.fout = "Geen begrotingsstaat gevonden in het voorstel van wet"
        except ScraperFout as e:
            h.fout = str(e)
        return h

    def scrape(self, jaar: int, fase: str) -> Resultaat:
        res = Resultaat(bron=self.bron, jaar=jaar, fase=fase)
        try:
            codes = self.hoofdstukken(jaar, fase)
        except ScraperFout as e:
            res.fout = str(e)
            return res
        with ThreadPoolExecutor(MAX_WORKERS) as pool:
            res.hoofdstukken = list(pool.map(lambda c: self.hoofdstuk(jaar, fase, c), codes))
        return res


# ------------------------------------------------------ officielebekendmakingen.nl


class OfficieleBekendmakingenScraper:
    bron = "officielebekendmakingen"

    def __init__(self, http: Http | None = None):
        self.http = http or Http()

    @staticmethod
    def zoekvraag(jaar: int, fase: str) -> str:
        cfg = FASEN[fase]["ob"]
        start = jaar + cfg["vergaderjaar_start"]
        delen = [
            '(c.product-area=="officielepublicaties")',
            '(dt.type=="Kamerstuk")',
            *[f'(w.dossiertitel="{t}")' for t in cfg["titel"]],
            f'(w.vergaderjaar=="{start}-{start + 1}")',
            '(w.documenttitel="Voorstel van wet")',
        ]
        return "and".join(delen)

    def _zoek(self, zoekvraag: str) -> list[str]:
        ids: list[str] = []
        for pagina in range(1, 6):
            params = {"q": zoekvraag, "pg": "50"}
            if pagina > 1:
                params["pagina"] = str(pagina)
            r = self.http.get(f"{OB_ZOEK}/resultaten?{urlencode(params)}")
            if r.status_code != 200 or "ongeldige karakters" in r.text:
                raise ScraperFout(f"Zoeken op officielebekendmakingen.nl mislukt (HTTP {r.status_code})")
            gevonden = dict.fromkeys(re.findall(r'href="/(kst-\d+-[A-Z]+-\d+)\.html"', r.text))
            nieuw = [i for i in gevonden if i not in ids]
            ids += nieuw
            if len(gevonden) < 50 or not nieuw:
                break
        return ids

    def documenten(self, jaar: int, fase: str) -> list[str]:
        """Zoek de identifiers (``kst-36600-VII-1``) van alle wetsvoorstellen.

        Stap 1 zoekt op dossiertitel en vergaderjaar. Omdat de metadata niet
        altijd consistent is (bijv. een afwijkend vergaderjaar of 'begrotingsstaat'
        in enkelvoud), wordt in stap 2 binnen de gevonden dossiernummers nog eens
        naar alle voorstellen van wet gezocht.

        Per hoofdstuk wordt het stuk met het laagste ondernummer gekozen (het
        oorspronkelijke voorstel van wet, niet een eventueel gewijzigd voorstel).
        """
        ids = self._zoek(self.zoekvraag(jaar, fase))
        dossiers = sorted({i.split("-")[1] for i in ids})
        for dossier in dossiers:
            vraag = (
                '(c.product-area=="officielepublicaties")and(dt.type=="Kamerstuk")'
                f'and(w.dossiernummer="{dossier}")and(w.documenttitel="Voorstel van wet")'
            )
            ids += [i for i in self._zoek(vraag) if i not in ids]
        per_hoofdstuk: dict[str, str] = {}
        for doc_id in ids:
            _, dossier, code, nr = doc_id.split("-")
            if dossier not in dossiers:
                continue
            huidig = per_hoofdstuk.get(code)
            if huidig is None or int(nr) < int(huidig.split("-")[3]):
                per_hoofdstuk[code] = doc_id
        return list(per_hoofdstuk.values())

    def hoofdstuk(self, jaar: int, doc_id: str) -> Hoofdstuk | None:
        code = doc_id.split("-")[2]
        h = Hoofdstuk(code=code, url=f"{OB_ZOEK}/{doc_id}.html")
        try:
            r = self.http.get(f"{OB_ZOEK}/{doc_id}.xml")
            if r.status_code == 404:
                h.url = f"{OB_ZOEK}/{doc_id}.pdf"
                h.fout = "Alleen als PDF gepubliceerd (geen XML met tabellen beschikbaar)"
                return h
            if r.status_code != 200:
                h.fout = f"HTTP {r.status_code}"
                return h
            wortel = ET.fromstring(r.content.lstrip(b"\xef\xbb\xbf"))
            titel_el = wortel.find(".//dossier/titel")
            dossiertitel = normaliseer_tekst(" ".join(titel_el.itertext())) if titel_el is not None else ""
            # Vergaderjaren kunnen dossiers van een ander begrotingsjaar bevatten.
            if dossiertitel and str(jaar) not in dossiertitel:
                return None
            h.tabellen = alleen_begrotingsstaten(parse_xml(r.content))
            h.naam = naam_uit_titel(dossiertitel) or (naam_uit_titel(h.tabellen[0].titel) if h.tabellen else "")
            if not h.tabellen:
                h.fout = "Geen begrotingsstaat gevonden in het voorstel van wet"
        except (ScraperFout, ET.ParseError) as e:
            h.fout = str(e)
        return h

    def scrape(self, jaar: int, fase: str) -> Resultaat:
        res = Resultaat(bron=self.bron, jaar=jaar, fase=fase)
        try:
            ids = self.documenten(jaar, fase)
        except ScraperFout as e:
            res.fout = str(e)
            return res
        if not ids:
            res.fout = "Geen wetsvoorstellen gevonden voor dit jaar en deze fase"
            return res
        with ThreadPoolExecutor(MAX_WORKERS) as pool:
            hs = [h for h in pool.map(lambda i: self.hoofdstuk(jaar, i), ids) if h is not None]
        res.hoofdstukken = sorted(hs, key=lambda h: hoofdstuk_sorteersleutel(h.code))
        return res


SCRAPERS = {
    "rijksfinancien": RijksfinancienScraper,
    "officielebekendmakingen": OfficieleBekendmakingenScraper,
}
