"""Flask-app: begrotingsstaten uit de begrotingswetten scrapen.

Starten::

    pip install -r requirements.txt
    flask --app app run        # of: python app.py

Routes:

* ``/``                      formulier (jaar, fase, bron)
* ``/begroting``             resultaat als HTML
* ``/begroting.csv``         resultaat als CSV (lange vorm)
* ``/api/begroting``         resultaat als JSON

Querystring-parameters: ``jaar`` (verplicht), ``fase`` (OWB, 1SUPP, 2SUPP, JV;
standaard OWB), ``bron`` (rijksfinancien, officielebekendmakingen of beide;
standaard beide) en ``vernieuw=1`` om de cache te omzeilen.
"""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, abort, jsonify, render_template, request

from begroting.bronnen import BRONNEN, FASEN
from begroting.service import aantal_agentschappen, haal_op, naar_csv, vergelijk
from begroting.tables import parse_bedrag

EERSTE_JAAR = 2013  # rijksfinancien.nl en de XML-publicaties gaan terug tot 2013

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


def laatste_jaar() -> int:
    return dt.date.today().year + 1


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


@app.errorhandler(400)
def foute_invoer(e):
    if request.path.startswith("/api/"):
        return jsonify({"fout": e.description}), 400
    return render_template("fout.html", melding=e.description), 400


if __name__ == "__main__":
    app.run(debug=True)
