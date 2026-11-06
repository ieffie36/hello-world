"""Flask-app: begrotingsstaten uit de begrotingswetten scrapen.

Starten::

    pip install -r requirements.txt
    flask --app app run        # of: python app.py

Routes:

* ``/``                      formulier (jaar, fase, bron)
* ``/begroting``             resultaat als HTML
* ``/begroting.csv``         resultaat als CSV (lange vorm)
* ``/api/begroting``         resultaat als JSON
* ``/begroting.sql``         artikelregels uit de database als SQL-script
* ``/database``              overzicht van wat in de SQL-database staat
* ``/database.sqlite``       download van de volledige SQLite-database

Elke verse scrape wordt opgeslagen in de SQLite-database ``begroting.db``
(pad instelbaar met de omgevingsvariabele ``BEGROTING_DB``). In bulk vullen::

    flask --app app opslaan --jaar 2013 --tot 2027 --fase alle --bron beide

Querystring-parameters: ``jaar`` (verplicht), ``fase`` (OWB, 1SUPP, SBS, 2SUPP, JV;
standaard OWB), ``bron`` (rijksfinancien, officielebekendmakingen of beide;
standaard beide) en ``vernieuw=1`` om de cache te omzeilen.
"""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor

from contextlib import closing
from pathlib import Path

import click
from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from begroting import database, service
from begroting.artikelen import extraheer_hoofdstuk
from begroting.bronnen import BRONNEN, FASEN
from begroting.service import aantal_agentschappen, haal_op, naar_csv, vergelijk
from begroting.tables import parse_bedrag

EERSTE_JAAR = 2013  # rijksfinancien.nl en de XML-publicaties gaan terug tot 2013

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


def laatste_jaar() -> int:
    return dt.date.today().year + 1


@app.template_global()
def controle(h) -> dict:
    """Aantal artikelregels en of de som van de artikelen klopt met het totaal van de bron."""
    regels, fouten = 0, []
    for nr, ex in enumerate(extraheer_hoofdstuk(h.tabellen), start=1):
        regels += len(ex.regels)
        fouten += [f"tabel {nr} {c['kolom']}: som {c['som_artikelen']} ≠ {c['totaal']}" for c in ex.controle if not c["ok"]]
    return {"regels": regels, "ok": not fouten, "toelichting": "; ".join(fouten)}


@app.template_global()
def is_bedrag(waarde: str) -> bool:
    return parse_bedrag(waarde) is not None


def lees_parameters() -> tuple[int, str, list[str], bool]:
    try:
        jaar = int(request.args.get("jaar", ""))
    except ValueError:
        abort(400, "Geef een geldig jaar op, bijvoorbeeld ?jaar=2025")
    if not EERSTE_JAAR <= jaar <= laatste_jaar():
        abort(400, f"Jaar moet tussen {EERSTE_JAAR} en {laatste_jaar()} liggen")
    fase = request.args.get("fase", "OWB").upper()
    if fase not in FASEN:
        abort(400, f"Onbekende fase; kies uit {', '.join(FASEN)}")
    if jaar < FASEN[fase].get("vanaf", EERSTE_JAAR):
        abort(400, f"{FASEN[fase]['naam']} bestaat pas vanaf {FASEN[fase]['vanaf']}")
    bron = request.args.get("bron", "beide")
    if bron == "beide":
        bronnen = list(BRONNEN)
    elif bron in BRONNEN:
        bronnen = [bron]
    else:
        abort(400, f"Onbekende bron; kies uit {', '.join(BRONNEN)} of beide")
    vernieuw = request.args.get("vernieuw") in ("1", "true", "ja")
    return jaar, fase, bronnen, vernieuw


def scrape(jaar: int, fase: str, bronnen: list[str], vernieuw: bool):
    with ThreadPoolExecutor(len(bronnen)) as pool:
        return list(pool.map(lambda b: haal_op(b, jaar, fase, vernieuw), bronnen))


@app.route("/")
def index():
    return render_template(
        "index.html",
        fasen=FASEN,
        bronnen=BRONNEN,
        eerste_jaar=EERSTE_JAAR,
        laatste_jaar=laatste_jaar(),
        standaard_jaar=dt.date.today().year,
    )


@app.route("/begroting")
def begroting():
    jaar, fase, bronnen, vernieuw = lees_parameters()
    resultaten = scrape(jaar, fase, bronnen, vernieuw)
    return render_template(
        "resultaat.html",
        jaar=jaar,
        fase=fase,
        fasen=FASEN,
        bronnen=BRONNEN,
        gekozen_bron=request.args.get("bron", "beide"),
        resultaten=resultaten,
        overzicht=vergelijk(resultaten),
        aantal_agentschappen=aantal_agentschappen,
        eerste_jaar=EERSTE_JAAR,
        laatste_jaar=laatste_jaar(),
    )


@app.route("/begroting.csv")
def begroting_csv():
    jaar, fase, bronnen, vernieuw = lees_parameters()
    resultaten = scrape(jaar, fase, bronnen, vernieuw)
    vergelijk(resultaten)  # vult ontbrekende hoofdstuknamen aan vanuit de andere bron
    naam = f"begrotingsstaten_{jaar}_{fase}_{request.args.get('bron', 'beide')}.csv"
    return Response(
        "﻿" + naar_csv(resultaten),  # BOM zodat Excel UTF-8 herkent
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{naam}"'},
    )


@app.route("/api/begroting")
def api_begroting():
    jaar, fase, bronnen, vernieuw = lees_parameters()
    resultaten = scrape(jaar, fase, bronnen, vernieuw)
    overzicht = vergelijk(resultaten)
    return jsonify(
        {
            "jaar": jaar,
            "fase": fase,
            "fase_naam": FASEN[fase]["naam"],
            "bronnen": [r.to_dict() for r in resultaten],
            "vergelijking": [
                {"code": r["code"], "naam": r["naam"], "status": r["status"], "bronnen": sorted(r["per_bron"])}
                for r in overzicht
            ],
        }
    )


def db_pad() -> str:
    return service.DB_PAD or database.STANDAARD_PAD


@app.route("/begroting.sql")
def begroting_sql():
    jaar, fase, bronnen, vernieuw = lees_parameters()
    # Alleen scrapen wat nog niet in de database staat (of bij vernieuw=1).
    ontbrekend = [b for b in bronnen if vernieuw or not database.heeft_ophaling(db_pad(), b, jaar, fase)]
    if ontbrekend:
        scrape(jaar, fase, ontbrekend, vernieuw)
    naam = f"begrotingsregels_{jaar}_{fase}.sql"
    return Response(
        database.als_sql_dump(db_pad(), jaar=jaar, fase=fase, bronnen=[BRONNEN[b] for b in bronnen]),
        mimetype="application/sql; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{naam}"'},
    )


@app.route("/database")
def database_overzicht():
    with closing(database.verbind(db_pad())) as con:
        ophalingen = con.execute(
            """SELECT o.*, COUNT(DISTINCT h.id) AS hoofdstukken,
                      (SELECT COUNT(*) FROM begrotingsregel r WHERE r.ophaling_id = o.id) AS regels,
                      SUM(h.controle_ok = 0) AS afwijkingen, SUM(h.fout IS NOT NULL) AS fouten
               FROM ophaling o LEFT JOIN hoofdstuk h ON h.ophaling_id = o.id
               GROUP BY o.id ORDER BY o.jaar DESC, o.fase, o.bron"""
        ).fetchall()
        afwijkingen = con.execute(
            """SELECT o.bron, o.jaar, o.fase, h.hoofdstuk, h.naam, h.url, h.controle
               FROM hoofdstuk h JOIN ophaling o ON o.id = h.ophaling_id
               WHERE h.controle_ok = 0 ORDER BY o.jaar DESC, o.fase, h.hoofdstuk"""
        ).fetchall()
    return render_template(
        "database.html",
        ophalingen=ophalingen,
        afwijkingen=afwijkingen,
        pad=db_pad(),
        broncode={naam: code for code, naam in BRONNEN.items()},
    )


@app.route("/database.sqlite")
def database_download():
    pad = Path(db_pad())
    if not pad.exists():
        abort(404)
    return send_file(pad, as_attachment=True, download_name="begroting.sqlite")


@app.cli.command("opslaan")
@click.option("--jaar", type=int, required=True, help="Begrotingsjaar (of eerste jaar met --tot).")
@click.option("--tot", type=int, default=None, help="Laatste jaar (inclusief).")
@click.option("--fase", default="OWB", show_default=True, help="OWB, 1SUPP, SBS, 2SUPP, JV of 'alle'.")
@click.option("--bron", default="beide", show_default=True, help="rijksfinancien, officielebekendmakingen of beide.")
def opslaan(jaar: int, tot: int | None, fase: str, bron: str) -> None:
    """Scrape begrotingsstaten en sla de artikelregels op in de SQL-database."""
    fasen = list(FASEN) if fase.lower() == "alle" else [fase.upper()]
    bronnen = list(BRONNEN) if bron == "beide" else [bron]
    for f in fasen:
        if f not in FASEN:
            raise click.BadParameter(f"onbekende fase {f}")
    for j in range(jaar, (tot or jaar) + 1):
        for f in fasen:
            if j < FASEN[f].get("vanaf", EERSTE_JAAR):
                continue
            for b in bronnen:
                res = haal_op(b, j, f, vernieuw=True)
                if res.fout:
                    click.echo(f"{j} {f:5} {BRONNEN[b]:27} FOUT: {res.fout}")
                    continue
                o = res.opslag or {}
                click.echo(
                    f"{j} {f:5} {BRONNEN[b]:27} {o.get('hoofdstukken', 0):3} hoofdstukken, "
                    f"{o.get('regels', 0):6} regels, {len(o.get('afwijkingen', []))} controle-afwijkingen"
                )
    click.echo(f"Database: {db_pad()}")


@app.errorhandler(400)
def foute_invoer(e):
    if request.path.startswith("/api/"):
        return jsonify({"fout": e.description}), 400
    return render_template("fout.html", melding=e.description), 400


if __name__ == "__main__":
    app.run(debug=True)
