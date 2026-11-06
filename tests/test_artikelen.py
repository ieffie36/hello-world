"""Tests voor het uitlezen van artikelregels en de SQL-opslag."""

import sqlite3
from pathlib import Path

from begroting import database
from begroting.artikelen import extraheer, normaliseer_artikelnummer, splits_kolom
from begroting.bronnen import Hoofdstuk, Resultaat
from begroting.tables import parse_html, parse_xml

FIX = Path(__file__).parent / "fixtures"


def tabel(html):
    (t,) = parse_html(f"<table>{html}</table>")
    return t


def test_splits_kolom():
    assert splits_kolom("Vastgestelde begroting – Uitgaven") == ("Vastgestelde begroting", "uitgaven")
    assert splits_kolom("Baten") == ("", "baten")
    assert splits_kolom("Saldo baten en lasten") == ("", "saldo")
    assert splits_kolom("Stand – Totaal kapitaal-uitgaven") == ("Stand", "kapitaaluitgaven")
    assert splits_kolom("Mutaties 1e suppletoire – V") == ("Mutaties 1e suppletoire", "verplichtingen")


def test_normalisatie_naam_en_stand():
    from begroting.artikelen import normaliseer_naam, normaliseer_stand

    assert normaliseer_naam("Duurzame economische ontwikkeling,handel") == "Duurzame economische ontwikkeling, handel"
    assert normaliseer_stand("Mutaties (+ of –)1 e suppletoire begroting ") == "Mutaties (+ of -) 1e suppletoire begroting"
    assert normaliseer_stand("Stand begroting (2e incidentele suppletoire begroting )") == "Stand begroting (2e incidentele suppletoire begroting)"


def test_artikelnummer():
    assert [normaliseer_artikelnummer(n) for n in ["01", "1.", "12", "6.1", "12a"]] == ["1", "1", "12", "6.1", "12a"]


def test_bzk_alle_artikelen_zonder_totalen():
    dep, blsd, kapitaal = parse_xml((FIX / "kst-36600-VII-1.xml").read_bytes())
    ex = extraheer(dep)
    artikelen = sorted({(r.artikelnummer, r.artikelnaam) for r in ex.regels}, key=lambda a: int(a[0]))
    assert [a[0] for a in artikelen] == ["1", "2", "6", "7", "11", "12", "13", "14", "15"]
    assert ("1", "Openbaar bestuur en democratie") in artikelen
    assert not any("totaal" in r.artikelnaam.lower() for r in ex.regels)
    assert {r.artikelsoort for r in ex.regels if r.artikelnummer == "11"} == {"niet-beleidsartikel"}
    assert {r.artikelsoort for r in ex.regels if r.artikelnummer == "1"} == {"beleidsartikel"}
    assert {r.bedragsoort for r in ex.regels} == {"verplichtingen", "uitgaven", "ontvangsten"}
    assert ex.controle and ex.controle_ok  # som artikelen = totaalregel
    diensten = extraheer(blsd)
    assert "Logius" in {r.artikelnaam for r in diensten.regels}
    assert not any(r.artikelnaam == "Totaal" for r in diensten.regels)
    assert diensten.controle_ok and extraheer(kapitaal).controle_ok


def test_sectiekoppen_met_bedragen_zijn_subtotalen():
    t = tabel(
        "<caption>Wijziging begrotingsstaat van het Ministerie van X (VIII) voor het jaar 2025</caption>"
        "<thead><tr><th></th><th></th><th>Uitgaven</th></tr></thead><tbody>"
        "<tr><td></td><td>Totaal</td><td>30</td></tr>"
        "<tr><td></td><td>Beleidsartikelen</td><td>25</td></tr>"
        "<tr><td>01</td><td>Primair onderwijs</td><td>10</td></tr>"
        "<tr><td>03</td><td>Voortgezet onderwijs</td><td>15</td></tr>"
        "<tr><td></td><td>Niet-beleidsartikelen</td><td>5</td></tr>"
        "<tr><td>91</td><td>Nog onverdeeld</td><td>5</td></tr></tbody>"
    )
    ex = extraheer(t)
    assert [(r.artikelnummer, r.artikelsoort, r.bedrag) for r in ex.regels] == [
        ("1", "beleidsartikel", 10),
        ("3", "beleidsartikel", 15),
        ("91", "niet-beleidsartikel", 5),
    ]
    assert ex.overgeslagen == {"totaal": 1, "subtotaal": 2}
    assert ex.controle_ok


def test_fonds_met_subtotaal_en_eindsaldo():
    t = tabel(
        "<caption>Vastgestelde begrotingsstaat van het Diergezondheidsfonds (F) voor het jaar 2025</caption>"
        "<thead><tr><th>Art.</th><th>Omschrijving</th><th>Uitgaven</th><th>Ontvangsten</th></tr></thead><tbody>"
        "<tr><td>1.</td><td>Bewaking en bestrijding</td><td>36.389</td><td>54.437</td></tr>"
        "<tr><td></td><td>Sub-totaal</td><td>36.389</td><td>54.437</td></tr>"
        "<tr><td></td><td>Na-/voordelig eindsaldo (cumulatief) 2025</td><td>18.048</td><td>0</td></tr>"
        "<tr><td></td><td>Totaal</td><td>54.437</td><td>54.437</td></tr></tbody>"
    )
    ex = extraheer(t)
    assert [(r.artikelnummer, r.artikelnaam, r.bedrag) for r in ex.regels] == [
        ("1", "Bewaking en bestrijding", 36389),
        ("1", "Bewaking en bestrijding", 54437),
    ]
    assert ex.overgeslagen == {"subtotaal": 1, "eindsaldo": 1, "totaal": 1}
    assert ex.controle_ok  # vergeleken met het subtotaal, niet met het totaal na eindsaldo


def test_twee_agentschapsstaten_in_een_tabel():
    t = tabel(
        "<caption>Vastgestelde begrotingsstaat inzake de baten-lastenagentschappen voor het jaar 2025</caption>"
        "<thead><tr><th>Naam</th><th>Baten</th><th>Lasten</th><th>Saldo</th></tr></thead><tbody>"
        "<tr><td>DUO</td><td>500</td><td>500</td><td>0</td></tr>"
        "<tr><td>Totaal</td><td>500</td><td>500</td><td>0</td></tr>"
        "<tr><td>Naam</td><td></td><td>Totaal kapitaaluitgaven</td><td>Totaal kapitaalontvangsten</td></tr>"
        "<tr><td>DUO</td><td></td><td>139</td><td>94</td></tr>"
        "<tr><td>Totaal</td><td></td><td>139</td><td>94</td></tr></tbody>"
    )
    ex = extraheer(t)
    assert [(r.artikelnaam, r.bedragsoort, r.bedrag) for r in ex.regels] == [
        ("DUO", "baten", 500), ("DUO", "lasten", 500), ("DUO", "saldo", 0),
        ("DUO", "kapitaaluitgaven", 139), ("DUO", "kapitaalontvangsten", 94),
    ]  # fmt: skip
    assert ex.controle_ok and len(ex.controle) == 5


def test_gekantelde_agentschapsstaat():
    t = tabel(
        "<caption>Samenvattende verantwoordingsstaat agentschappen</caption>"
        "<thead><tr><th>Naam Agentschap</th><th>Begroting</th><th>Realisatie</th></tr></thead><tbody>"
        "<tr><td colspan='3'>Agentschap CBG</td></tr>"
        "<tr><td>Totaal baten</td><td>40.000</td><td>48.565</td></tr>"
        "<tr><td>Totaal lasten</td><td>40.000</td><td>43.229</td></tr>"
        "<tr><td>Saldo van baten en lasten</td><td>0</td><td>5.336</td></tr></tbody>"
    )
    ex = extraheer(t)
    assert {(r.artikelnaam, r.stand, r.bedragsoort, r.bedrag) for r in ex.regels} == {
        ("Agentschap CBG", "Begroting", "baten", 40000), ("Agentschap CBG", "Realisatie", "baten", 48565),
        ("Agentschap CBG", "Begroting", "lasten", 40000), ("Agentschap CBG", "Realisatie", "lasten", 43229),
        ("Agentschap CBG", "Begroting", "saldo", 0), ("Agentschap CBG", "Realisatie", "saldo", 5336),
    }  # fmt: skip


def test_opslaan_in_database(tmp_path):
    pad = str(tmp_path / "b.db")
    h = Hoofdstuk(code="VII", naam="Ministerie van BZK", url="https://example", tabellen=parse_xml((FIX / "kst-36600-VII-1.xml").read_bytes()))
    res = Resultaat(bron="officielebekendmakingen", jaar=2025, fase="OWB", hoofdstukken=[h])
    samenvatting = database.sla_op(res, pad)
    assert samenvatting["regels"] > 0 and samenvatting["afwijkingen"] == []
    database.sla_op(res, pad)  # opnieuw opslaan vervangt, verdubbelt niet

    con = sqlite3.connect(pad)
    assert con.execute("SELECT COUNT(*) FROM ophaling").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM begrotingsregel").fetchone()[0] == samenvatting["regels"]
    assert con.execute("SELECT COUNT(*) FROM begrotingsregel WHERE artikelnaam LIKE '%totaal%'").fetchone()[0] == 0
    # Som van de artikelen per bedragsoort = totaal in de wet.
    som = con.execute(
        "SELECT SUM(bedrag) FROM begrotingsregel WHERE regelsoort='artikel' AND bedragsoort='uitgaven'"
    ).fetchone()[0]
    assert som == 5083939
    rij = con.execute(
        "SELECT verplichtingen, uitgaven, ontvangsten, eenheid FROM artikel_breed WHERE artikelnummer = '1'"
    ).fetchone()
    assert rij == (164462, 164462, 14765, "x € 1.000")
    assert con.execute("SELECT controle_ok FROM hoofdstuk").fetchone()[0] == 1

    dump = database.als_sql_dump(pad, jaar=2025)
    assert dump.startswith("-- Begrotingsregels") and "INSERT INTO begrotingsregel" in dump
    mem = sqlite3.connect(":memory:")
    mem.executescript(dump)  # het SQL-script is direct te importeren
    assert mem.execute("SELECT COUNT(*) FROM begrotingsregel").fetchone()[0] == samenvatting["regels"]


def test_artikelnummer_aanvullen_uit_andere_bron(tmp_path):
    pad = str(tmp_path / "b.db")
    met = tabel(
        "<caption>Vastgestelde begrotingsstaat van het Ministerie van Defensie (X) voor het jaar 2021</caption>"
        "<thead><tr><th>Art.</th><th>Omschrijving</th><th>Uitgaven</th></tr></thead>"
        "<tbody><tr><td>1</td><td>Inzet</td><td>10</td></tr></tbody>"
    )
    zonder = tabel(
        "<caption>Vastgestelde begrotingsstaat van het Ministerie van Defensie (X) voor het jaar 2021</caption>"
        "<thead><tr><th></th><th>Uitgaven</th></tr></thead>"
        "<tbody><tr><td>Inzet</td><td>10</td></tr></tbody>"
    )
    for bron, t in (("officielebekendmakingen", zonder), ("rijksfinancien", met)):
        database.sla_op(Resultaat(bron=bron, jaar=2021, fase="OWB", hoofdstukken=[Hoofdstuk("X", tabellen=[t])]), pad)
    nummers = sqlite3.connect(pad).execute("SELECT bron, artikelnummer FROM begrotingsregel ORDER BY bron").fetchall()
    assert nummers == [("officielebekendmakingen.nl", "1"), ("rijksfinancien.nl", "1")]
