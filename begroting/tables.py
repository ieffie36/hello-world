"""Gedeeld tabelmodel en parsers voor begrotingsstaten.

Beide bronnen publiceren dezelfde tabellen uit de begrotingswet:

* rijksfinancien.nl als HTML-``<table>`` (met ``colspan``/``rowspan``);
* officielebekendmakingen.nl als CALS-XML-``<table>`` (met ``colspec``,
  ``namest``/``nameend`` en ``morerows``).

Beide worden omgezet naar :class:`Tabel`, zodat de rest van de applicatie
bron-onafhankelijk is.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, Tag

SOORT_DEPARTEMENTAAL = "departementaal"
SOORT_AGENTSCHAPPEN = "agentschappen"
SOORT_OVERIG = "overig"

# Kolomkoppen die een omschrijvende kolom aanduiden (geen bedragen).
_LABEL_KOP_RE = re.compile(
    r"^(art(\.|ikel)?(nummer|nr\.?)?|omschrijving|naam|.*agentschap.*|.*dienst.*)$",
    re.IGNORECASE,
)
_ONDERDEEL_RE = re.compile(r"\(([IVX]+[A-C]?|H?[IVX]+|[A-M])\)")
_MIN = "-−–‒—"
_GETAL_RE = re.compile(rf"^[{_MIN}]?\s*(\d{{1,3}}(\.\d{{3}})*|\d+)(,\d+)?$")


def normaliseer_tekst(tekst: str | None) -> str:
    if not tekst:
        return ""
    return re.sub(r"\s+", " ", tekst.replace("\xa0", " ")).strip()


def parse_bedrag(tekst: str | None) -> float | int | None:
    """Zet een Nederlands opgemaakt bedrag (``1.234.567`` / ``-12,5``) om naar een getal."""
    t = normaliseer_tekst(tekst)
    if not t or not _GETAL_RE.match(t):
        return None
    negatief = t[0] in _MIN
    t = t.lstrip(_MIN + " ").replace(".", "").replace(",", ".")
    waarde: float | int = float(t) if "." in t else int(t)
    return -waarde if negatief else waarde


def classificeer(titel: str) -> str:
    t = titel.lower()
    if "agentschap" in t or "baten-lasten" in t or "baten–lasten" in t:
        return SOORT_AGENTSCHAPPEN
    if re.search(r"begrotings?sta(at|ten)", t):
        return SOORT_DEPARTEMENTAAL
    return SOORT_OVERIG


@dataclass
class Rij:
    cellen: list[str]
    soort: str = "data"  # data | kop | totaal


@dataclass
class Tabel:
    titel: str
    kolommen: list[str]
    rijen: list[Rij] = field(default_factory=list)
    soort: str = SOORT_OVERIG
    label_kolommen: list[int] = field(default_factory=list)
    onderdeel: str = ""  # hoofdstukcode uit de titel, bijv. "IIIB" of "H"

    def bepaal_metadata(self) -> None:
        self._promoveer_kopregels()
        self.soort = classificeer(self.titel)
        m = _ONDERDEEL_RE.search(self.titel)
        self.onderdeel = m.group(1) if m else ""
        self.label_kolommen = self._label_kolommen()
        for rij in self.rijen:
            rij.soort = self._rijsoort(rij)

    def _promoveer_kopregels(self) -> None:
        """Sommige tabellen hebben (een deel van) de kolomkoppen in de body staan.

        Zolang de eerste bodyrij geen enkel bedrag bevat maar wel meerdere
        gevulde cellen, en de kolomkoppen nog niet uniek zijn, wordt die rij aan
        de kop toegevoegd.
        """
        while self.rijen:
            eerste = self.rijen[0].cellen
            gevuld = [c for c in eerste if c]
            uniek = len(set(self.kolommen)) == len(self.kolommen) and self.kolommen
            if uniek or len(gevuld) < 2 or any(parse_bedrag(c) is not None for c in eerste):
                return
            kop_rijen = [k.split(" – ") for k in self.kolommen] if self.kolommen else []
            nieuwe = []
            for i, cel in enumerate(eerste):
                delen = list(kop_rijen[i]) if i < len(kop_rijen) and not kop_rijen[i][0].startswith("kolom ") else []
                if cel and (not delen or delen[-1] != cel):
                    delen.append(cel)
                nieuwe.append(" – ".join(delen) or f"kolom {i + 1}")
            self.kolommen = nieuwe
            self.rijen.pop(0)

    def _label_kolommen(self) -> list[int]:
        labels = []
        for i, kop in enumerate(self.kolommen):
            laatste_kop = kop.split(" – ")[-1]
            if _LABEL_KOP_RE.match(laatste_kop):
                labels.append(i)
                continue
            waarden = [r.cellen[i] for r in self.rijen if i < len(r.cellen) and r.cellen[i]]
            if waarden and sum(parse_bedrag(w) is None for w in waarden) > len(waarden) / 2:
                labels.append(i)
        return labels or [0]

    def _rijsoort(self, rij: Rij) -> str:
        label = " ".join(rij.cellen[i] for i in self.label_kolommen if i < len(rij.cellen)).strip()
        bedragen = [
            c for i, c in enumerate(rij.cellen) if i not in self.label_kolommen and parse_bedrag(c) is not None
        ]
        if label.lower().startswith("totaal"):
            return "totaal"
        if not bedragen:
            return "kop"
        return "data"

    @property
    def data_rijen(self) -> list[Rij]:
        return [r for r in self.rijen if r.soort != "kop" or any(r.cellen)]

    def totaal(self) -> dict[str, float | int | None]:
        """Bedragen van de eerste totaalrij, per kolomkop."""
        for rij in self.rijen:
            if rij.soort == "totaal":
                return {
                    self.kolommen[i]: parse_bedrag(c)
                    for i, c in enumerate(rij.cellen)
                    if i not in self.label_kolommen and i < len(self.kolommen)
                }
        return {}

    def records(self) -> list[dict]:
        """Lange ('tidy') vorm: één record per bedragcel."""
        uit = []
        for nr, rij in enumerate(self.rijen):
            if rij.soort == "kop":
                continue
            labels = [rij.cellen[i] for i in self.label_kolommen if i < len(rij.cellen)]
            for i, cel in enumerate(rij.cellen):
                if i in self.label_kolommen:
                    continue
                bedrag = parse_bedrag(cel)
                if bedrag is None:
                    continue
                uit.append(
                    {
                        "rij": nr,
                        "rijsoort": rij.soort,
                        "label_1": labels[0] if labels else "",
                        "label_2": " ".join(labels[1:]),
                        "kolom": self.kolommen[i] if i < len(self.kolommen) else f"kolom {i + 1}",
                        "bedrag": bedrag,
                    }
                )
        return uit

    def to_dict(self) -> dict:
        d = asdict(self)
        d["totaal"] = self.totaal()
        return d


def _combineer_koppen(kop_rijen: list[list[str]], aantal: int) -> list[str]:
    koppen = []
    for i in range(aantal):
        delen: list[str] = []
        for rij in kop_rijen:
            waarde = rij[i] if i < len(rij) else ""
            if waarde and (not delen or delen[-1] != waarde):
                delen.append(waarde)
        koppen.append(" – ".join(delen) or f"kolom {i + 1}")
    return koppen


def _vul_raster(rijen: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """Expandeer (tekst, colspan, rowspan)-cellen naar een rechthoekig raster.

    Een overspannende cel wordt herhaald in alle kolommen/rijen die hij beslaat;
    zo krijgt elke bedragkolom de juiste (samengestelde) kop.
    """
    raster: list[list[str | None]] = []
    for r, rij in enumerate(rijen):
        while len(raster) <= r:
            raster.append([])
        k = 0
        for tekst, colspan, rowspan in rij:
            while k < len(raster[r]) and raster[r][k] is not None:
                k += 1
            for dr in range(rowspan):
                while len(raster) <= r + dr:
                    raster.append([])
                doel = raster[r + dr]
                while len(doel) < k + colspan:
                    doel.append(None)
                for dc in range(colspan):
                    doel[k + dc] = tekst
            k += colspan
    breedte = max((len(r) for r in raster), default=0)
    return [[c or "" for c in r] + [""] * (breedte - len(r)) for r in raster]


# --------------------------------------------------------------------------- HTML


def _span(cel: Tag, attr: str) -> int:
    try:
        return max(1, int(cel.get(attr, 1)))
    except (TypeError, ValueError):
        return 1


def parse_html_tabel(tabel: Tag) -> Tabel:
    caption = tabel.find("caption")
    titel = normaliseer_tekst(caption.get_text(" ") if caption else tabel.get("summary", ""))

    def rijen_van(sectie: Tag | None) -> list[list[tuple[str, int, int]]]:
        if sectie is None:
            return []
        return [
            [
                (normaliseer_tekst(c.get_text(" ")), _span(c, "colspan"), _span(c, "rowspan"))
                for c in tr.find_all(["th", "td"], recursive=False)
            ]
            for tr in sectie.find_all("tr", recursive=False)
        ]

    thead = tabel.find("thead")
    tbody_rijen: list[list[tuple[str, int, int]]] = []
    for tbody in tabel.find_all("tbody", recursive=False) or [tabel]:
        tbody_rijen += rijen_van(tbody)
    kop = _vul_raster(rijen_van(thead))
    body = _vul_raster(tbody_rijen)
    breedte = max([len(r) for r in kop + body] or [0])
    t = Tabel(titel=titel, kolommen=_combineer_koppen(kop, breedte), rijen=[Rij(r) for r in body])
    t.bepaal_metadata()
    return t


def parse_html(html: str) -> list[Tabel]:
    soup = BeautifulSoup(html, "html.parser")
    wortel = soup.find("main") or soup
    return [parse_html_tabel(t) for t in wortel.find_all("table")]


# ---------------------------------------------------------------------- CALS-XML


def _xml_tekst(e: ET.Element) -> str:
    return normaliseer_tekst(" ".join(e.itertext()))


def _cals_rijen(rows: list[ET.Element], kolnamen: dict[str, int]) -> list[list[str]]:
    """Zet CALS-rijen om in een raster; ``colname``/``namest`` bepalen de positie."""
    aantal = max(kolnamen.values(), default=-1) + 1
    raster: list[list[str | None]] = [[None] * aantal for _ in rows]
    bezet: list[list[bool]] = [[False] * aantal for _ in rows]
    for r, row in enumerate(rows):
        k = 0
        for entry in row.findall("entry"):
            start = entry.get("namest") or entry.get("colname")
            if start in kolnamen:
                k = kolnamen[start]
            else:
                while k < aantal and bezet[r][k]:
                    k += 1
            eind = kolnamen.get(entry.get("nameend", ""), k)
            morerows = int(entry.get("morerows", 0) or 0)
            tekst = _xml_tekst(entry)
            for dr in range(morerows + 1):
                if r + dr >= len(rows):
                    break
                for c in range(k, min(eind, aantal - 1) + 1):
                    raster[r + dr][c] = tekst
                    bezet[r + dr][c] = True
            k = eind + 1
    return [[c or "" for c in rij] for rij in raster]


def parse_xml_tabel(tabel: ET.Element) -> Tabel:
    titel_el = tabel.find("title")
    titel = _xml_tekst(titel_el) if titel_el is not None else ""
    kolommen: list[str] = []
    rijen: list[Rij] = []
    for tgroup in tabel.findall("tgroup"):
        specs = tgroup.findall("colspec")
        kolnamen = {}
        for i, spec in enumerate(specs):
            nr = int(spec.get("colnum", i + 1)) - 1
            kolnamen[spec.get("colname", f"col{nr + 1}")] = nr
        aantal = int(tgroup.get("cols", len(specs) or 1))
        for i in range(aantal):
            kolnamen.setdefault(f"col{i + 1}", i)
        thead = tgroup.find("thead")
        tbody = tgroup.find("tbody")
        kop = _cals_rijen(thead.findall("row") if thead is not None else [], kolnamen)
        body = _cals_rijen(tbody.findall("row") if tbody is not None else [], kolnamen)
        if not kolommen:
            kolommen = _combineer_koppen(kop, aantal)
        rijen += [Rij(r) for r in body]
    t = Tabel(titel=titel, kolommen=kolommen, rijen=rijen)
    t.bepaal_metadata()
    return t


def parse_xml(xml: bytes | str) -> list[Tabel]:
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    wortel = ET.fromstring(xml.lstrip(b"\xef\xbb\xbf"))
    return [parse_xml_tabel(t) for t in wortel.iter("table")]
