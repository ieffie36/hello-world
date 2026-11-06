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
| `SBS` | Septembersuppletoire begroting (Miljoenennota, Prinsjesdag; vanaf 2023) |
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
* **CSV** (`/begroting.csv?jaar=2025&fase=OWB&bron=rijksfinancien`): dezelfde artikelregels als de
  SQL-tabel (zonder totalen), puntkomma-gescheiden (opent direct in Excel-NL).
  Bedragen staan zoals in de wet (meestal × € 1.000, zie `tabel_titel`).
* **JSON** (`/api/begroting?jaar=2025&fase=OWB&bron=beide`): alle tabellen met kolomkoppen, rijen en
  totalen, plus de vergelijking.

Parameters: `jaar` (verplicht), `fase` (standaard `OWB`), `bron` (`rijksfinancien`,
`officielebekendmakingen` of `beide`; standaard `beide`) en `vernieuw=1` om de cache (1 uur) te omzeilen.

## SQL-database

Elke verse scrape wordt automatisch opgeslagen in een SQLite-database (`begroting.db` in de
projectmap; ander pad via de omgevingsvariabele `BEGROTING_DB`). Opnieuw ophalen van dezelfde
bron/jaar/fase vervangt de vorige gegevens. In bulk vullen:

```bash
flask --app app opslaan --jaar 2013 --tot 2027 --fase alle --bron beide
```

| Tabel / view | Inhoud |
| --- | --- |
| `begrotingsregel` | Eén rij per bedrag van een **artikel** of **baten-lastendienst**: `bron, jaar, fase, hoofdstuk, hoofdstuknaam, onderdeel, tabel_nr, tabel_titel, regelsoort, artikelnummer, artikelnaam, artikelsoort (beleidsartikel/niet-beleidsartikel), stand, bedragsoort, kolom, bedrag, eenheid, rij, url` |
| `artikel_breed` (view) | Per artikel en stand de bedragsoorten naast elkaar: `verplichtingen, uitgaven, ontvangsten, kapitaaluitgaven, kapitaalontvangsten, saldo, baten, lasten` |
| `hoofdstuk` | Per ophaling en hoofdstuk: naam, bron-URL, fout, aantal regels en de controle |
| `ophaling` | Wanneer welke bron/jaar/fase is opgehaald |

**Zonder totalen en subtotalen.** Weggelaten worden: `Totaal`, `Subtotaal`/`Sub-totaal`, sectiekoppen
met bedragen (`Beleidsartikelen`, `Niet-beleidsartikelen`, dat zijn subtotalen) en de eindsaldo-regels
van begrotingsfondsen. Artikelnummers worden genormaliseerd (`01` en `1.` worden `1`).

**Controle.** Per tabel en kolom wordt de som van de opgeslagen artikelen vergeleken met de
(sub)totaalregel in de bron (`hoofdstuk.controle_ok`). Afwijkingen staan op `/database`; in de
gevallen die ik heb nagelopen zat de fout in de gepubliceerde tabel zelf.

Voorbeeld:

```sql
SELECT hoofdstuk, artikelnummer, artikelnaam, verplichtingen, uitgaven, ontvangsten
FROM artikel_breed
WHERE jaar = 2025 AND fase = 'OWB' AND bron = 'rijksfinancien.nl' AND regelsoort = 'artikel'
ORDER BY hoofdstuk, CAST(artikelnummer AS INTEGER);
```

Downloads: `/begroting.sql?jaar=2025&fase=OWB` (SQL-script met `CREATE TABLE` + `INSERT`, te importeren in
elke SQL-database) en `/database.sqlite` (het volledige SQLite-bestand).

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
