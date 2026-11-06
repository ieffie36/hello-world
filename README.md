# Begrotingsstaten-scraper

Flask-applicatie die voor een opgegeven begrotingsjaar de **begrotingsstaten uit de
begrotingswetten** ophaalt, voor **alle begrotingshoofdstukken** (ministeries, Hoge Colleges
van Staat, begrotingsfondsen) en voor de **baten-lastendiensten (agentschappen)**.

Bronnen:

| Bron | Wat wordt gelezen |
| --- | --- |
| [rijksfinancien.nl](https://www.rijksfinancien.nl) | `/voorstel-van-wet/{jaar}/{fase}/{hoofdstuk}` (HTML); de lijst met hoofdstukken komt uit `/memorie-van-toelichting/{jaar}/{fase}` |
| [officielebekendmakingen.nl](https://zoek.officielebekendmakingen.nl) | Het Kamerstuk *Voorstel van wet* per hoofdstuk (XML), gevonden via de zoekdienst op dossiertitel + vergaderjaar |

Per hoofdstuk worden alle tabellen uit het wetsvoorstel opgehaald:

* de **departementale begrotingsstaat** (verplichtingen, uitgaven, ontvangsten per artikel), inclusief
  eventuele aparte staten zoals IIIB Kabinet van de Koning, IXA Nationale Schuld of H BES-fonds;
* de **begrotingsstaat inzake de agentschappen** (baten, lasten, saldo en kapitaaluitgaven/-ontvangsten
  per baten-lastendienst).

## Begrotingsmomenten

| Code | Moment |
| --- | --- |
| `OWB` | Ontwerpbegroting (begrotingswet, Prinsjesdag) |
| `1SUPP` | Eerste suppletoire begroting (Voorjaarsnota) |
| `2SUPP` | Tweede suppletoire begroting (Najaarsnota) |
| `JV` | Slotwet (bij het jaarverslag) |

Beschikbaar vanaf begrotingsjaar 2013.

## Installeren en starten

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask --app app run        # of: python app.py
```

Open daarna <http://localhost:5000>, kies een jaar, een begrotingsmoment en een bron.

## Gebruik

* **Webinterface** (`/begroting?jaar=2025&fase=OWB&bron=beide`): overzicht van alle hoofdstukken met
  het aantal baten-lastendiensten, en per hoofdstuk de volledige tabellen. Met bron `beide` worden de
  totaalregels van beide bronnen vergeleken (`gelijk` / `verschil` / `onvolledig`).
* **CSV** (`/begroting.csv?jaar=2025&fase=OWB&bron=rijksfinancien`): lange vorm, één regel per bedrag,
  puntkomma-gescheiden (opent direct in Excel-NL). Kolommen: `bron, jaar, fase, hoofdstuk, hoofdstuknaam,
  onderdeel, tabel_nr, tabel_soort, tabel_titel, rij, rijsoort, label_1, label_2, kolom, bedrag`.
  Bedragen staan zoals in de wet (meestal × € 1.000, zie `tabel_titel`).
* **JSON** (`/api/begroting?jaar=2025&fase=OWB&bron=beide`): alle tabellen met kolomkoppen, rijen en
  totalen, plus de vergelijking.

Parameters: `jaar` (verplicht), `fase` (standaard `OWB`), `bron` (`rijksfinancien`,
`officielebekendmakingen` of `beide`; standaard `beide`) en `vernieuw=1` om de cache (1 uur) te omzeilen.

## Opmerkingen

* Een enkele pagina op rijksfinancien.nl geeft `HTTP 403` (bijv. 2024 OWB hoofdstuk VII); kies dan
  `beide` of officielebekendmakingen.nl.
* Sommige oudere slotwetten staan op officielebekendmakingen.nl alleen als PDF; die worden gemeld met
  een link naar de PDF.
* De scraper doet maximaal 4 gelijktijdige verzoeken per bron met een korte pauze ertussen.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

De tests draaien offline op opgeslagen pagina's in `tests/fixtures/`.
