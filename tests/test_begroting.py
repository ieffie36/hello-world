"""Offline tests met opgeslagen pagina's van beide bronnen."""

from pathlib import Path

import pytest

from begroting import bronnen, service
from begroting.bronnen import (
    OfficieleBekendmakingenScraper,
    RijksfinancienScraper,
    naam_uit_titel,
)
from begroting.tables import SOORT_AGENTSCHAPPEN, SOORT_DEPARTEMENTAAL, parse_bedrag, parse_html, parse_xml

FIX = Path(__file__).parent / "fixtures"


class Antwoord:
    def __init__(self, status_code=200, text="", content=None):
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")


class NepHttp:
    """Beantwoordt verzoeken op basis van een URL-fragment → Antwoord."""

    def __init__(self, routes):
        self.routes = routes
        self.verzoeken = []

    def get(self, url, **kwargs):
        self.verzoeken.append(url)
        for fragment, antwoord in self.routes.items():
            if fragment in url:
                return antwoord
        return Antwoord(404, "niet gevonden")


# --------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "tekst, verwacht",
    [("4.560.128", 4560128), ("13 478 481", 13478481), ("6264 *", 6264), ("(7.107)", -7107), ("(1)", None), ("–", 0), ("nihil", 0), ("‒ 288 770", -288770), ("1 2", None), ("‒ 3.100", -3100), ("-1.234", -1234), ("0", 0), ("12,5", 12.5), ("", None), ("Totaal", None)],
)
def test_parse_bedrag(tekst, verwacht):
    assert parse_bedrag(tekst) == verwacht


def _controleer_bzk_2027_of_2025(tabellen):
    assert [t.soort for t in tabellen] == [SOORT_DEPARTEMENTAAL, SOORT_AGENTSCHAPPEN, SOORT_AGENTSCHAPPEN]
    dep = tabellen[0]
    assert dep.kolommen == [
        "Art.",
        "Omschrijving",
        "Vastgestelde begroting – Verplichtingen",
        "Vastgestelde begroting – Uitgaven",
        "Vastgestelde begroting – Ontvangsten",
    ]
    assert dep.label_kolommen == [0, 1]
    assert dep.rijen[0].soort == "totaal"
    blsd = tabellen[1]
    assert blsd.kolommen == ["Naam baten-lastenagentschap", "Baten", "Lasten", "Saldo"]
    assert any("Logius" in r.cellen[0] for r in blsd.rijen)
    return dep, blsd


def test_html_rijksfinancien():
    tabellen = parse_html((FIX / "rijksfinancien_2027_OWB_VII.html").read_text(encoding="utf-8"))
    dep, blsd = _controleer_bzk_2027_of_2025(tabellen)
    assert dep.totaal() == {
        "Vastgestelde begroting – Verplichtingen": 4560128,
        "Vastgestelde begroting – Uitgaven": 4810993,
        "Vastgestelde begroting – Ontvangsten": 1615066,
    }
    # Som van de artikelen is gelijk aan de totaalregel.
    uitgaven = [r["bedrag"] for r in dep.records() if r["rijsoort"] == "data" and r["kolom"].endswith("Uitgaven")]
    assert sum(uitgaven) == 4810993
    assert blsd.totaal()["Baten"] == 2063589


def test_xml_officielebekendmakingen():
    tabellen = parse_xml((FIX / "kst-36600-VII-1.xml").read_bytes())
    dep, _ = _controleer_bzk_2027_of_2025(tabellen)
    assert dep.totaal()["Vastgestelde begroting – Uitgaven"] == 5083939
    kapitaal = tabellen[2]
    assert kapitaal.label_kolommen == [0]
    assert parse_bedrag(kapitaal.rijen[0].cellen[1]) == -3100


def test_kopregel_in_body_wordt_kolomkop():
    html = """<table><caption>Wijziging begrotingsstaat van het Ministerie van X (VII) voor het jaar 2024</caption>
    <thead><tr><th>Art.</th><th>Omschrijving</th><th colspan="2">Vastgestelde begroting</th></tr></thead>
    <tbody><tr><td></td><td></td><td>Uitgaven</td><td>Ontvangsten</td></tr>
    <tr><td></td><td>Totaal</td><td>10</td><td>5</td></tr>
    <tr><td>1</td><td>Iets</td><td>10</td><td>5</td></tr></tbody></table>"""
    (t,) = parse_html(html)
    assert t.kolommen[2:] == ["Vastgestelde begroting – Uitgaven", "Vastgestelde begroting – Ontvangsten"]
    assert [r.soort for r in t.rijen] == ["totaal", "data"]
    assert t.onderdeel == "VII"


def test_cals_morerows_en_spans():
    xml = """<doc><table><title>Begrotingsstaat inzake de agentschappen</title><tgroup cols="3">
      <colspec colname="c1"/><colspec colname="c2"/><colspec colname="c3"/>
      <thead><row><entry morerows="1">Naam</entry><entry namest="c2" nameend="c3">Bedragen</entry></row>
             <row><entry colname="c2">Baten</entry><entry colname="c3">Lasten</entry></row></thead>
      <tbody><row><entry>Dienst A</entry><entry>1.000</entry><entry>900</entry></row></tbody>
    </tgroup></table></doc>"""
    (t,) = parse_xml(xml)
    assert t.kolommen == ["Naam", "Bedragen – Baten", "Bedragen – Lasten"]
    assert t.soort == SOORT_AGENTSCHAPPEN
    assert t.records()[0]["bedrag"] == 1000


@pytest.mark.parametrize(
    "titel, naam",
    [
        ("Vaststelling van de begrotingsstaten van het Ministerie van Defensie (X) voor het jaar 2025", "Ministerie van Defensie"),
        ("Vastgestelde begrotingsstaat voor Buitenlandse Handel en Ontwikkelingshulp voor het jaar 2025", "Buitenlandse Handel en Ontwikkelingshulp"),
        ("Vaststelling van de begrotingsstaat van het gemeentefonds voor het jaar 2024", "Gemeentefonds"),
        ("Wijziging van de begrotingstaat van het Ministerie van Financiën (IXB) voor het jaar 2024", "Ministerie van Financiën"),
        ("Jaarverslag en slotwet Ministerie van Binnenlandse Zaken en Koninkrijksrelaties 2024", "Ministerie van Binnenlandse Zaken en Koninkrijksrelaties"),
    ],
)
def test_naam_uit_titel(titel, naam):
    assert naam_uit_titel(titel) == naam


# -------------------------------------------------------------------- scrapers


def test_rijksfinancien_scraper():
    http = NepHttp(
        {
            "/memorie-van-toelichting/2027/OWB": Antwoord(text=(FIX / "mvt_2027_OWB.html").read_text(encoding="utf-8")),
            "/voorstel-van-wet/2027/OWB/VII": Antwoord(
                text=(FIX / "rijksfinancien_2027_OWB_VII.html").read_text(encoding="utf-8")
            ),
            "/voorstel-van-wet/2027/OWB/XX": Antwoord(403, "geen toegang"),
        }
    )
    res = RijksfinancienScraper(http).scrape(2027, "OWB")
    codes = [h.code for h in res.hoofdstukken]
    assert len(codes) == 27 and codes[:3] == ["I", "IIA", "IIB"] and "XXIII" in codes
    vii = next(h for h in res.hoofdstukken if h.code == "VII")
    assert vii.naam == "Ministerie van Binnenlandse Zaken en Koninkrijksrelaties"
    assert len(vii.tabellen) == 3 and vii.fout is None
    xx = next(h for h in res.hoofdstukken if h.code == "XX")
    assert "403" in xx.fout


def test_officielebekendmakingen_scraper():
    zoek = '<a href="/kst-36600-VII-1.html">x</a><a href="/kst-36600-VII-4.html">y</a><a href="/kst-36600-B-1.html">z</a>'
    http = NepHttp(
        {
            "/resultaten?": Antwoord(text=zoek),
            "/kst-36600-VII-1.xml": Antwoord(content=(FIX / "kst-36600-VII-1.xml").read_bytes()),
        }
    )
    scraper = OfficieleBekendmakingenScraper(http)
    assert sorted(scraper.documenten(2025, "OWB")) == ["kst-36600-B-1", "kst-36600-VII-1"]
    res = scraper.scrape(2025, "OWB")
    vii = next(h for h in res.hoofdstukken if h.code == "VII")
    assert vii.naam == "Ministerie van Binnenlandse Zaken en Koninkrijksrelaties"
    assert len(vii.tabellen) == 3
    b = next(h for h in res.hoofdstukken if h.code == "B")
    assert "PDF" in b.fout
    # Zoekvraag bevat het juiste vergaderjaar
    assert 'w.vergaderjaar=="2024-2025"' in scraper.zoekvraag(2025, "OWB")
    assert 'w.vergaderjaar=="2025-2026"' in scraper.zoekvraag(2025, "JV")
    assert 'w.vergaderjaar=="2025-2026"' in scraper.zoekvraag(2025, "SBS")
    assert 'w.dossiertitel="Miljoenennota"' in scraper.zoekvraag(2025, "SBS")


# ------------------------------------------------------------------------ app


@pytest.fixture
def client(monkeypatch):
    rf = RijksfinancienScraper(
        NepHttp(
            {
                "/memorie-van-toelichting/2027/OWB": Antwoord(
                    text='<main><a href="/memorie-van-toelichting/2027/OWB/VII">VII</a></main>'
                ),
                "/voorstel-van-wet/2027/OWB/VII": Antwoord(
                    text=(FIX / "rijksfinancien_2027_OWB_VII.html").read_text(encoding="utf-8")
                ),
            }
        )
    )
    ob = OfficieleBekendmakingenScraper(NepHttp({"/resultaten?": Antwoord(text="geen resultaten")}))
    monkeypatch.setitem(bronnen.SCRAPERS, "rijksfinancien", lambda: rf)
    monkeypatch.setitem(bronnen.SCRAPERS, "officielebekendmakingen", lambda: ob)
    service.wis_cache()
    from app import app

    app.config["TESTING"] = True
    return app.test_client()


def test_app_routes(client):
    assert client.get("/").status_code == 200
    r = client.get("/begroting?jaar=2027&fase=OWB&bron=beide")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Logius" in html and "Geen wetsvoorstellen gevonden" in html

    csv = client.get("/begroting.csv?jaar=2027&bron=rijksfinancien").get_data(as_text=True)
    regels = csv.strip().splitlines()
    assert regels[0].lstrip("﻿").startswith("bron;jaar;fase;hoofdstuk")
    assert any("Logius" in r and ";baten;" in r for r in regels)
    assert not any(";Totaal;" in r for r in regels)

    data = client.get("/api/begroting?jaar=2027&bron=rijksfinancien").get_json()
    assert data["bronnen"][0]["hoofdstukken"][0]["code"] == "VII"

    sql = client.get("/begroting.sql?jaar=2027&bron=rijksfinancien").get_data(as_text=True)
    assert "INSERT INTO begrotingsregel" in sql and "'Logius'" in sql and "'Totaal'" not in sql
    db = client.get("/database").get_data(as_text=True)
    assert "2027" in db and "Opgeslagen ophalingen" in db


def test_app_validatie(client):
    assert client.get("/begroting?jaar=abc").status_code == 400
    assert client.get("/api/begroting?jaar=1990").get_json()["fout"].startswith("Jaar moet")
    assert client.get("/api/begroting?jaar=2025&fase=XYZ").status_code == 400
    assert "vanaf 2023" in client.get("/api/begroting?jaar=2022&fase=SBS").get_json()["fout"]
