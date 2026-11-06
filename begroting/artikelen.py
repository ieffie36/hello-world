"""Zet begrotingsstaat-tabellen om in losse artikelregels, zonder (sub)totalen.

Elke regel is één bedrag van één artikel (of één baten-lastendienst) in één
kolom, met de kolom uitgesplitst in *stand* (bijv. "Vastgestelde begroting",
"Mutaties 1e suppletoire begroting") en *bedragsoort* (verplichtingen,
uitgaven, ontvangsten, baten, lasten, saldo, kapitaaluitgaven,
kapitaalontvangsten).

Weggelaten worden:

* totaalregels ("Totaal", "Subtotaal", "Sub-totaal", "Totaal beleidsartikelen");
* sectiekoppen met bedragen ("Beleidsartikelen", "Niet-beleidsartikelen"), dat
  zijn subtotalen;
* eindsaldo-regels van begrotingsfondsen ("Na-/Voordelig eindsaldo ...").

Ter controle wordt per kolom de som van de artikelen vergeleken met de
(sub)totaalregel van de bron.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tables import SOORT_AGENTSCHAPPEN, SOORT_DEPARTEMENTAAL, Tabel, parse_bedrag

_TOTAAL_RE = re.compile(r"^(sub[\s-]*)?totaal\b|^totalen\b", re.IGNORECASE)
_SUBTOTAAL_RE = re.compile(r"^sub[\s-]*totaal$", re.IGNORECASE)
_SECTIEKOP_RE = re.compile(r"^(niet[\s-]*)?beleids[\s-]*artikel(en)?$", re.IGNORECASE)
_EINDSALDO_RE = re.compile(r"eindsaldo", re.IGNORECASE)
_ARTIKELNR_RE = re.compile(r"^\d{1,3}[a-zA-Z]?(\.\d{1,2})?\.?$")
_ARTKOP_RE = re.compile(r"^art(\.|ikel)?\s*(nummer|nr\.?|no\.?)?$", re.IGNORECASE)

BEDRAGSOORTEN = [  # volgorde is van belang: kapitaal* vóór uitgaven/ontvangsten
    ("verplichtingen", re.compile(r"verplichting", re.I)),
    ("kapitaaluitgaven", re.compile(r"kapitaal[-\s]*uitgaven", re.I)),
    ("kapitaalontvangsten", re.compile(r"kapitaal[-\s]*ontvangsten", re.I)),
    ("uitgaven", re.compile(r"uitgaven", re.I)),
    ("ontvangsten", re.compile(r"ontvangsten", re.I)),
    ("saldo", re.compile(r"saldo", re.I)),
    ("baten", re.compile(r"\bbaten\b", re.I)),
    ("lasten", re.compile(r"\blasten\b", re.I)),
]
# Korte kolomkoppen in oudere staten ("V", "U", "O").
_LETTERS = {"v": "verplichtingen", "u": "uitgaven", "o": "ontvangsten"}


def bedragsoort(tekst: str) -> str:
    """Herken de bedragsoort in een kop of rijlabel ('' als die er niet in staat)."""
    t = re.sub(r"(?<=[a-z])-\s*(?=[a-z])", "", tekst.strip(), flags=re.I)  # "Verplicht-ingen", "kapitaal-uitgaven"
    if t.lower() in _LETTERS:
        return _LETTERS[t.lower()]
    if re.search(r"saldo", t, re.I):
        return "saldo"
    for naam, patroon in BEDRAGSOORTEN:
        if patroon.search(t):
            return naam
    return ""


def normaliseer_naam(tekst: str) -> str:
    """Gelijke witruimte rond leestekens: "ontwikkeling,handel" → "ontwikkeling, handel"."""
    tekst = re.sub(r",(?=[A-Za-zÀ-ÿ])", ", ", tekst)
    tekst = re.sub(r"\(\s+", "(", tekst)
    tekst = re.sub(r"\s+\)", ")", tekst)
    tekst = re.sub(r"\)(?=[\w(])", ") ", tekst)
    return re.sub(r"\s+", " ", tekst).strip()


def normaliseer_stand(stand: str) -> str:
    """Maak de schrijfwijze van een stand gelijk tussen bronnen ("1 e" → "1e", "(+ of –)" → "(+ of -)")."""
    stand = re.sub(r"\b(\d)\s+e\b", r"\1e", stand)
    stand = re.sub(r"\(\s*\+\s*of\s*[-–‒−]\s*\)", "(+ of -)", stand)
    stand = re.sub(r"\s*’\s*|\s*'\s*", "'", stand)
    return normaliseer_naam(stand)


def splits_kolom(kolom: str) -> tuple[str, str]:
    """``"Vastgestelde begroting – Uitgaven"`` → ``("Vastgestelde begroting", "uitgaven")``."""
    delen = [d.strip() for d in kolom.split(" – ") if d.strip()]
    if not delen:
        return "", ""
    soort = bedragsoort(delen[-1])
    if soort:
        return normaliseer_stand(" – ".join(delen[:-1])), soort
    return normaliseer_stand(" – ".join(delen)), ""


def is_totaal(tekst: str) -> bool:
    return bool(_TOTAAL_RE.match(tekst.strip()))


def normaliseer_artikelnummer(nr: str) -> str:
    """``"01"`` → ``"1"``, ``"1."`` → ``"1"``; ``"12a"`` en ``"6.1"`` blijven gelijk."""
    nr = nr.strip().rstrip(".")
    m = re.match(r"^0*(\d+)(.*)$", nr)
    return f"{m.group(1)}{m.group(2)}" if m else nr


@dataclass
class Regel:
    regelsoort: str  # "artikel" | "baten-lastendienst"
    artikelnummer: str
    artikelnaam: str
    artikelsoort: str  # beleidsartikel | niet-beleidsartikel | ""
    kolom: str
    stand: str
    bedragsoort: str
    bedrag: int | float
    rij: int


@dataclass
class Extractie:
    regels: list[Regel] = field(default_factory=list)
    overgeslagen: dict[str, int] = field(default_factory=dict)  # soort → aantal rijen
    controle: list[dict] = field(default_factory=list)  # per kolom: som artikelen vs. (sub)totaal

    @property
    def controle_ok(self) -> bool:
        return all(c["ok"] for c in self.controle)

    def sla_over(self, soort: str) -> None:
        self.overgeslagen[soort] = self.overgeslagen.get(soort, 0) + 1


def _label_indices(t: Tabel) -> tuple[int | None, int | None]:
    """Bepaal de kolom met het artikelnummer en de kolom met de omschrijving/naam."""
    labels = t.label_kolommen
    nr = next((i for i in labels if _ARTKOP_RE.match(t.kolommen[i].split(" – ")[0].strip())), None)
    if nr is None:
        # Geen kop "Art.": kies de labelkolom die vooral uit artikelnummers bestaat.
        for i in labels:
            waarden = [r.cellen[i] for r in t.rijen if i < len(r.cellen) and r.cellen[i]]
            if waarden and sum(bool(_ARTIKELNR_RE.match(w)) for w in waarden) > len(waarden) / 2:
                nr = i
                break
    # Omschrijving: de overige labelkolom met de meeste gevulde cellen (er kan een lege kolom tussen staan).
    overige = [i for i in labels if i != nr]
    naam = max(overige, key=lambda i: sum(1 for r in t.rijen if i < len(r.cellen) and r.cellen[i]), default=None)
    if naam is None and nr is not None and t.soort == SOORT_AGENTSCHAPPEN:
        naam, nr = nr, None
    return nr, naam


def _splits_nummer_en_naam(tekst: str) -> tuple[str, str]:
    """``"1 Openbaar bestuur"`` → ``("1", "Openbaar bestuur")`` (oudere staten)."""
    m = re.match(r"^(?:art(?:ikel|\.)?\s*)?(\d{1,3}[a-zA-Z]?(?:\.\d{1,2})?)\.?\s+(\D.*)$", tekst, re.I)
    if m:
        return m.group(1), m.group(2)
    return "", tekst


_DIENST_SOORTEN = {"baten", "lasten", "saldo", "kapitaaluitgaven", "kapitaalontvangsten"}


def _is_gekanteld(t: Tabel, naam_i: int | None) -> bool:
    """Oudere agentschapsstaten: per dienst een blok met een rij per bedragsoort (baten, lasten, ...)."""
    if naam_i is None:
        return False
    labels = [r.cellen[naam_i] for r in t.rijen if r.soort != "kop" and naam_i < len(r.cellen) and r.cellen[naam_i]]
    labels = [lab for lab in labels if not re.fullmatch(r"(sub[\s-]*)?totaal", lab, re.I)]
    soorten = [bedragsoort(lab) for lab in labels]
    return bool(soorten) and sum(s in _DIENST_SOORTEN for s in soorten) > 0.8 * len(soorten)


def _rijen_met_bedrag(t: Tabel) -> list[int]:
    return [n for n, r in enumerate(t.rijen) if r.soort != "kop"]


def _vervolg_koppeling(t: Tabel, vorige: Tabel | None) -> dict[int, int] | None:
    """Koppel de rijen van een vervolgtabel (alleen bedragkolommen) aan de rijen van de vorige tabel.

    Sommige staten lopen door in een tweede tabel zonder kolommen voor artikelnummer
    en omschrijving. Rijen worden op positie gekoppeld, of, als de vervolgtabel
    minder kopregels heeft, op volgorde van de rijen met bedragen.
    """
    if vorige is None or vorige.soort != t.soort:
        return None
    if not all(bedragsoort(k.split(" – ")[-1]) for k in t.kolommen):
        return None
    if len(vorige.rijen) == len(t.rijen):
        return {n: n for n in range(len(t.rijen))}
    hier, daar = _rijen_met_bedrag(t), _rijen_met_bedrag(vorige)
    if len(hier) == len(daar):
        return dict(zip(hier, daar))
    return None


def _zelfde(a: dict[int, float | None], b: dict[int, float]) -> bool:
    paren = [(v, b.get(i, 0)) for i, v in a.items() if v is not None]
    return bool(paren) and all(abs(x - y) <= max(3, 0.0005 * abs(x)) for x, y in paren)


def _is_groepsregel(kandidaten: list[dict], p: int) -> bool:
    """Is de ongenummerde regel op positie ``p`` het subtotaal van een groep artikelen?

    Dat is zo als de bedragen gelijk zijn aan de som van de direct volgende (of
    voorafgaande) genummerde artikelen. Alleen dan wordt de regel weggelaten,
    zodat een echt artikel zonder nummer nooit verdwijnt.
    """
    u = kandidaten[p]["bedragen"]
    if all(not v for v in u.values()):
        return False
    for richting in (range(p + 1, len(kandidaten)), range(p - 1, -1, -1)):
        som: dict[int, float] = {}
        aantal = 0
        for q in richting:
            k = kandidaten[q]
            if not k["nummer"]:
                continue
            for i, v in k["bedragen"].items():
                som[i] = som.get(i, 0) + (v or 0)
            aantal += 1
            if aantal >= 1 and _zelfde(u, som):
                return True
    return False


def extraheer(t: Tabel, vorige: Tabel | None = None) -> Extractie:
    """Haal artikel- of dienstregels uit één tabel; (sub)totalen worden weggelaten.

    ``vorige`` is de voorgaande tabel van hetzelfde hoofdstuk; die levert de
    artikelnummers en -namen als deze tabel daar zelf geen kolommen voor heeft.
    """
    ex = Extractie()
    if t.soort not in (SOORT_DEPARTEMENTAAL, SOORT_AGENTSCHAPPEN):
        return ex
    regelsoort = "artikel" if t.soort == SOORT_DEPARTEMENTAAL else "baten-lastendienst"
    koppeling = _vervolg_koppeling(t, vorige)
    vervolg = koppeling is not None
    if vervolg:
        label_bron, label_kolommen = vorige, vorige.label_kolommen
        bedrag_kolommen = list(range(len(t.kolommen)))
    else:
        label_bron, label_kolommen = t, t.label_kolommen
        bedrag_kolommen = [i for i in range(len(t.kolommen)) if i not in t.label_kolommen]
    nr_i, naam_i = _label_indices(label_bron)
    kolom_namen = {i: t.kolommen[i] for i in bedrag_kolommen}
    kolom_info = {i: splits_kolom(t.kolommen[i]) for i in bedrag_kolommen}
    gekanteld = _is_gekanteld(label_bron, naam_i)
    if gekanteld:
        regelsoort = "baten-lastendienst"

    sectie = ""
    # Bij gekantelde staten staat de naam van de eerste dienst soms in de kolomkop.
    kop_naam = label_bron.kolommen[naam_i].split(" – ") if naam_i is not None else [""]
    dienst = kop_naam[-1] if gekanteld and len(kop_naam) > 1 else ""
    kandidaten: list[dict] = []
    referentie: dict[str, dict[int, float | None]] = {}
    wachtende_kop: dict[int, str] = {}

    def afsluiten() -> None:
        nonlocal kandidaten, referentie
        _verwerk_segment(ex, kandidaten, referentie, regelsoort, kolom_namen, kolom_info, gekanteld)
        kandidaten, referentie = [], {}

    for rnr, rij in enumerate(t.rijen):
        label_rij = label_bron.rijen[koppeling[rnr]] if vervolg and rnr in koppeling else rij

        def cel(i: int | None, r=rij) -> str:
            return r.cellen[i].strip() if i is not None and i < len(r.cellen) else ""

        bedragen = {i: parse_bedrag(cel(i)) for i in bedrag_kolommen}
        heeft_bedrag = any(v is not None for v in bedragen.values())
        nummer, naam = cel(nr_i, label_rij), cel(naam_i, label_rij)
        label = f"{nummer} {naam}".strip()

        if not heeft_bedrag:
            koppen = {i: cel(i) for i in bedrag_kolommen if cel(i)}
            if koppen and not label.strip(" -–"):
                # Kopregel in de body; kan over meerdere rijen zijn afgebroken ("Verplich-" / "tingen").
                for i, tekst in koppen.items():
                    vorig = wachtende_kop.get(i, "")
                    wachtende_kop[i] = (vorig[:-1] + tekst) if vorig.endswith("-") else f"{vorig} {tekst}".strip()
                continue
            if len(koppen) >= 2 and all(bedragsoort(k) for k in koppen.values()):
                wachtende_kop.update(koppen)  # tussenkop met een label als "Naam" in de eerste kolom
                continue
            if _SECTIEKOP_RE.match(label):
                sectie = "niet-beleidsartikel" if label.lower().startswith("niet") else "beleidsartikel"
            elif gekanteld and label and not bedragsoort(label):
                dienst = label
            continue

        if wachtende_kop:
            nieuw = {i: wachtende_kop.get(i, "") for i in bedrag_kolommen}
            wachtende_kop = {}
            if sum(bool(bedragsoort(k)) for k in nieuw.values() if k) >= 2:
                if any(kolom_info[i][1] for i in bedrag_kolommen):
                    # Tussenkop: een tweede staat in dezelfde tabel (bijv. kapitaaluitgaven/-ontvangsten).
                    afsluiten()
                    kolom_namen = nieuw
                else:
                    # Aanvulling op de kolomkop, bijv. "(5) Realisatie" + "Verplichtingen".
                    kolom_namen = {i: " – ".join(x for x in (kolom_namen[i], nieuw[i]) if x) for i in bedrag_kolommen}
                kolom_info = {i: splits_kolom(kolom_namen[i]) for i in bedrag_kolommen}

        if gekanteld:
            if re.fullmatch(r"(sub[\s-]*)?totaal", label, re.I) or not dienst or is_totaal(dienst):
                ex.sla_over("totaal")
                continue
            kandidaten.append({"nummer": "", "naam": re.sub(r"^\d{1,2}\.\s+", "", normaliseer_naam(dienst)), "soort": bedragsoort(label), "bedragen": bedragen,
                               "sectie": "", "rij": rnr})  # fmt: skip
            continue

        if _SECTIEKOP_RE.match(naam or label) and not _ARTIKELNR_RE.match(nummer):
            # "Beleidsartikelen" mét bedragen = subtotaal van die sectie.
            sectie = "niet-beleidsartikel" if (naam or label).lower().startswith("niet") else "beleidsartikel"
            ex.sla_over("subtotaal")
            continue
        if is_totaal(naam) or is_totaal(nummer) or is_totaal(label):
            soort = "subtotaal" if _SUBTOTAAL_RE.match(naam or label) else "totaal"
            ex.sla_over(soort)
            if soort not in referentie and re.fullmatch(r"(sub[\s-]*)?totaal", naam or label, re.I):
                referentie[soort] = bedragen
            continue
        if _EINDSALDO_RE.search(label):
            ex.sla_over("eindsaldo")
            continue

        if regelsoort == "artikel":
            if not nummer and naam:
                nummer, naam = _splits_nummer_en_naam(naam)
            if not naam and nummer and not _ARTIKELNR_RE.match(nummer):
                nummer, naam = "", nummer
            if not naam and not nummer:
                ex.sla_over("zonder omschrijving")
                continue
            nummer = normaliseer_artikelnummer(nummer) if nummer else ""
        else:
            naam = naam or nummer
            nummer = ""
        naam = normaliseer_naam(naam)
        if regelsoort == "baten-lastendienst":
            naam = re.sub(r"^\d{1,2}\.\s+", "", naam)  # "2. Nationaal Archief" → "Nationaal Archief"
        kandidaten.append({"nummer": nummer, "naam": naam, "soort": None, "bedragen": bedragen,
                           "sectie": sectie if regelsoort == "artikel" else "", "rij": rnr})  # fmt: skip

    afsluiten()
    return ex


def _verwerk_segment(
    ex: Extractie,
    kandidaten: list[dict],
    referentie: dict,
    regelsoort: str,
    kolom_namen: dict[int, str],
    kolom_info: dict[int, tuple[str, str]],
    gekanteld: bool,
) -> None:
    genummerd = regelsoort == "artikel" and any(k["nummer"] for k in kandidaten)
    sommen: dict[int, float] = {}
    for p, k in enumerate(kandidaten):
        if genummerd and not k["nummer"] and _is_groepsregel(kandidaten, p):
            ex.sla_over("subtotaal")
            continue
        for i, bedrag in k["bedragen"].items():
            if bedrag is None:
                continue
            if gekanteld:
                stand, soort = normaliseer_stand(kolom_namen[i]), k["soort"]
            else:
                stand, soort = kolom_info[i]
                sommen[i] = sommen.get(i, 0) + bedrag
            ex.regels.append(
                Regel(
                    regelsoort=regelsoort,
                    artikelnummer=k["nummer"],
                    artikelnaam=k["naam"],
                    artikelsoort=k["sectie"],
                    kolom=kolom_namen[i],
                    stand=stand,
                    bedragsoort=soort,
                    bedrag=bedrag,
                    rij=k["rij"],
                )
            )
    if gekanteld:
        return
    # Controle: som van de artikelen tegen het subtotaal (fondsen: vóór eindsaldo) of het totaal.
    ref = referentie.get("subtotaal") or referentie.get("totaal") or {}
    for i, waarde in ref.items():
        if waarde is None:
            continue
        som = sommen.get(i, 0)
        ex.controle.append(
            {
                "kolom": kolom_namen[i],
                "som_artikelen": som,
                "totaal": waarde,
                # Afrondingsverschillen van enkele eenheden komen voor in de bron.
                "ok": abs(som - waarde) <= max(3, 0.0005 * abs(waarde)),
            }
        )


def extraheer_hoofdstuk(tabellen: list[Tabel]) -> list[Extractie]:
    """Extraheer alle tabellen van één hoofdstuk (in volgorde).

    Artikelen zonder nummer krijgen het nummer van een artikel met dezelfde naam
    uit een andere tabel van hetzelfde hoofdstuk (sommige staten tonen alleen de
    omschrijving).
    """
    uitkomsten = [extraheer(t, tabellen[n - 1] if n else None) for n, t in enumerate(tabellen)]
    nummers: dict[str, set[str]] = {}
    for ex in uitkomsten:
        for r in ex.regels:
            if r.regelsoort == "artikel" and r.artikelnummer:
                nummers.setdefault(_naamsleutel(r.artikelnaam), set()).add(r.artikelnummer)
    for ex in uitkomsten:
        for r in ex.regels:
            if r.regelsoort == "artikel" and not r.artikelnummer:
                kandidaten = nummers.get(_naamsleutel(r.artikelnaam), set())
                if len(kandidaten) == 1:
                    r.artikelnummer = next(iter(kandidaten))
    return uitkomsten


def _naamsleutel(naam: str) -> str:
    return re.sub(r"[^a-z0-9]", "", naam.lower())
