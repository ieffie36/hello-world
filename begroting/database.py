"""Opslag van begrotingsregels in een SQL-database (SQLite).

Tabellen:

* ``ophaling``        één rij per opgehaalde bron/jaar/fase;
* ``hoofdstuk``       per ophaling de hoofdstukken (naam, bron-URL, eventuele fout);
* ``begrotingsregel`` één rij per bedrag van een artikel of baten-lastendienst,
                      zonder totalen en subtotalen.

View ``artikel_breed`` zet verplichtingen/uitgaven/ontvangsten (en baten/lasten/
saldo/kapitaal) naast elkaar per artikel en stand.

Opnieuw opslaan van dezelfde bron/jaar/fase vervangt de vorige gegevens.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .artikelen import BEDRAGSOORTEN, extraheer_hoofdstuk
from .bronnen import BRONNEN, Resultaat

STANDAARD_PAD = os.environ.get("BEGROTING_DB", str(Path(__file__).resolve().parent.parent / "begroting.db"))

_BEDRAGSOORT_NAMEN = [naam for naam, _ in BEDRAGSOORTEN]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS ophaling (
    id            INTEGER PRIMARY KEY,
    bron          TEXT    NOT NULL,
    jaar          INTEGER NOT NULL,
    fase          TEXT    NOT NULL,
    opgehaald_op  TEXT    NOT NULL,
    fout          TEXT,
    UNIQUE (bron, jaar, fase)
);

CREATE TABLE IF NOT EXISTS hoofdstuk (
    id            INTEGER PRIMARY KEY,
    ophaling_id   INTEGER NOT NULL REFERENCES ophaling(id) ON DELETE CASCADE,
    hoofdstuk     TEXT    NOT NULL,
    naam          TEXT,
    url           TEXT,
    fout          TEXT,
    aantal_regels INTEGER NOT NULL DEFAULT 0,
    controle_ok   INTEGER,          -- 1 = som artikelen = totaalregel, 0 = afwijking, NULL = geen totaalregel
    controle      TEXT              -- toelichting bij afwijkingen
);

CREATE TABLE IF NOT EXISTS begrotingsregel (
    id             INTEGER PRIMARY KEY,
    ophaling_id    INTEGER NOT NULL REFERENCES ophaling(id) ON DELETE CASCADE,
    bron           TEXT    NOT NULL,   -- rijksfinancien.nl | officielebekendmakingen.nl
    jaar           INTEGER NOT NULL,
    fase           TEXT    NOT NULL,   -- OWB | 1SUPP | SBS | 2SUPP | JV
    hoofdstuk      TEXT    NOT NULL,   -- bijv. VII, IXB, A
    hoofdstuknaam  TEXT,
    onderdeel      TEXT,               -- code van de staat, bijv. IIIB of H (BES-fonds) binnen IV
    tabel_nr       INTEGER NOT NULL,
    tabel_titel    TEXT,
    regelsoort     TEXT    NOT NULL,   -- artikel | baten-lastendienst
    artikelnummer  TEXT,               -- leeg bij baten-lastendiensten
    artikelnaam    TEXT    NOT NULL,   -- artikel- of dienstnaam
    artikelsoort   TEXT,               -- beleidsartikel | niet-beleidsartikel
    stand          TEXT,               -- bijv. 'Vastgestelde begroting', 'Mutaties 1e suppletoire begroting'
    bedragsoort    TEXT,               -- verplichtingen, uitgaven, ontvangsten, baten, lasten, ...
    kolom          TEXT    NOT NULL,   -- volledige kolomkop uit de bron
    bedrag         NUMERIC NOT NULL,   -- zoals in de wet, meestal x € 1.000
    eenheid        TEXT,
    rij            INTEGER NOT NULL,   -- rijnummer in de brontabel (volgorde)
    url            TEXT
);

CREATE INDEX IF NOT EXISTS ix_regel_zoek ON begrotingsregel (jaar, fase, hoofdstuk, artikelnummer);
CREATE INDEX IF NOT EXISTS ix_regel_ophaling ON begrotingsregel (ophaling_id);

DROP VIEW IF EXISTS artikel_breed;
CREATE VIEW artikel_breed AS
SELECT bron, jaar, fase, hoofdstuk, MAX(hoofdstuknaam) AS hoofdstuknaam, onderdeel, regelsoort,
       artikelnummer, artikelnaam, MAX(artikelsoort) AS artikelsoort, stand, MAX(eenheid) AS eenheid,
       {", ".join(f"SUM(CASE WHEN bedragsoort = '{n}' THEN bedrag END) AS {n}" for n in _BEDRAGSOORT_NAMEN)},
       MIN(tabel_nr) AS tabel_nr, MIN(rij) AS rij
FROM begrotingsregel
GROUP BY ophaling_id, hoofdstuk, onderdeel, regelsoort, artikelnummer, artikelnaam, stand
ORDER BY bron, jaar, fase, hoofdstuk, MIN(tabel_nr), MIN(rij);
"""


def verbind(pad: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(pad or STANDAARD_PAD, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def _eenheid(titel: str) -> str:
    m = re.search(r"x\s*€\s*([\d.]+|miljoen|miljard)", titel, re.I)
    if m:
        return f"x € {m.group(1)}"
    if re.search(r"in\s*€\s*miljoen", titel, re.I):
        return "x € 1 mln"
    return ""


def sla_op(res: Resultaat, pad: str | None = None, con: sqlite3.Connection | None = None) -> dict:
    """Schrijf één scrape-resultaat weg. Geeft een samenvatting terug."""
    eigen = con is None
    con = con or verbind(pad)
    try:
        with con:  # één transactie
            bron = BRONNEN[res.bron]
            con.execute("DELETE FROM ophaling WHERE bron = ? AND jaar = ? AND fase = ?", (bron, res.jaar, res.fase))
            cur = con.execute(
                "INSERT INTO ophaling (bron, jaar, fase, opgehaald_op, fout) VALUES (?, ?, ?, ?, ?)",
                (bron, res.jaar, res.fase, dt.datetime.now().isoformat(timespec="seconds"), res.fout),
            )
            ophaling_id = cur.lastrowid
            totaal_regels = 0
            afwijkingen = []
            for h in res.hoofdstukken:
                rijen = []
                controles = []
                for nr, (t, ex) in enumerate(zip(h.tabellen, extraheer_hoofdstuk(h.tabellen)), start=1):
                    eenheid = _eenheid(t.titel)
                    for r in ex.regels:
                        rijen.append(
                            (
                                ophaling_id, BRONNEN[res.bron], res.jaar, res.fase, h.code, h.naam,
                                t.onderdeel or h.code, nr, t.titel, r.regelsoort, r.artikelnummer,
                                r.artikelnaam, r.artikelsoort, r.stand, r.bedragsoort, r.kolom,
                                r.bedrag, eenheid, r.rij, h.url,
                            )
                        )  # fmt: skip
                    controles += [dict(c, tabel_nr=nr) for c in ex.controle]
                fouten = [c for c in controles if not c["ok"]]
                controle_ok = None if not controles else int(not fouten)
                toelichting = "; ".join(
                    f"tabel {c['tabel_nr']} {c['kolom']}: som {c['som_artikelen']} ≠ totaal {c['totaal']}" for c in fouten
                )
                if fouten:
                    afwijkingen.append((h.code, toelichting))
                con.execute(
                    "INSERT INTO hoofdstuk (ophaling_id, hoofdstuk, naam, url, fout, aantal_regels, controle_ok, controle)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ophaling_id, h.code, h.naam, h.url, h.fout, len(rijen), controle_ok, toelichting or None),
                )
                con.executemany(
                    "INSERT INTO begrotingsregel (ophaling_id, bron, jaar, fase, hoofdstuk, hoofdstuknaam, onderdeel,"
                    " tabel_nr, tabel_titel, regelsoort, artikelnummer, artikelnaam, artikelsoort, stand, bedragsoort,"
                    " kolom, bedrag, eenheid, rij, url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rijen,
                )
                totaal_regels += len(rijen)
            vul_artikelnummers_aan(con, res.jaar)
        return {
            "bron": res.bron,
            "jaar": res.jaar,
            "fase": res.fase,
            "regels": totaal_regels,
            "hoofdstukken": len(res.hoofdstukken),
            "afwijkingen": afwijkingen,
        }
    finally:
        if eigen:
            con.close()


def vul_artikelnummers_aan(con: sqlite3.Connection, jaar: int) -> int:
    """Vul ontbrekende artikelnummers aan uit andere bronnen/fasen van hetzelfde jaar en hoofdstuk.

    Sommige staten tonen alleen de omschrijving; het nummer wordt overgenomen als er
    precies één nummer bij dezelfde artikelnaam hoort.
    """
    cur = con.execute(
        """
        UPDATE begrotingsregel AS r
        SET artikelnummer = (
            SELECT MIN(a.artikelnummer) FROM begrotingsregel a
            WHERE a.jaar = r.jaar AND a.hoofdstuk = r.hoofdstuk AND a.regelsoort = 'artikel'
              AND a.artikelnummer <> '' AND lower(a.artikelnaam) = lower(r.artikelnaam)
            HAVING COUNT(DISTINCT a.artikelnummer) = 1
        )
        WHERE r.jaar = ? AND r.regelsoort = 'artikel' AND r.artikelnummer = ''
          AND EXISTS (
            SELECT 1 FROM begrotingsregel a
            WHERE a.jaar = r.jaar AND a.hoofdstuk = r.hoofdstuk AND a.regelsoort = 'artikel'
              AND a.artikelnummer <> '' AND lower(a.artikelnaam) = lower(r.artikelnaam)
            GROUP BY lower(a.artikelnaam) HAVING COUNT(DISTINCT a.artikelnummer) = 1
          )
        """,
        (jaar,),
    )
    return cur.rowcount


def heeft_ophaling(pad: str | None, bron: str, jaar: int, fase: str) -> bool:
    with closing(verbind(pad)) as con:
        rij = con.execute(
            "SELECT 1 FROM ophaling WHERE bron = ? AND jaar = ? AND fase = ? AND fout IS NULL",
            (BRONNEN.get(bron, bron), jaar, fase),
        ).fetchone()
        return rij is not None


def als_sql_dump(
    pad: str | None = None, jaar: int | None = None, fase: str | None = None, bronnen: list[str] | None = None
) -> str:
    """SQL-script (CREATE + INSERT) van de tabel begrotingsregel, eventueel gefilterd."""
    with closing(verbind(pad)) as con:
        kolommen = [r["name"] for r in con.execute("PRAGMA table_info(begrotingsregel)") if r["name"] not in ("id", "ophaling_id")]
        waar, args = [], []
        if jaar is not None:
            waar.append("jaar = ?")
            args.append(jaar)
        if fase:
            waar.append("fase = ?")
            args.append(fase)
        if bronnen:
            waar.append(f"bron IN ({', '.join('?' * len(bronnen))})")
            args += bronnen
        sql = f"SELECT {', '.join(kolommen)} FROM begrotingsregel"
        if waar:
            sql += " WHERE " + " AND ".join(waar)
        sql += " ORDER BY bron, jaar, fase, hoofdstuk, tabel_nr, rij, kolom"

        def lit(v):
            if v is None:
                return "NULL"
            if isinstance(v, (int, float)):
                return str(v)
            return "'" + str(v).replace("'", "''") + "'"

        regels = [
            "-- Begrotingsregels (artikelen en baten-lastendiensten, zonder totalen)",
            "CREATE TABLE IF NOT EXISTS begrotingsregel (",
            "    " + ",\n    ".join(
                f"{k} {'INTEGER' if k in ('jaar', 'tabel_nr', 'rij') else 'NUMERIC' if k == 'bedrag' else 'TEXT'}"
                for k in kolommen
            ),
            ");",
        ]
        for rij in con.execute(sql, args):
            regels.append(
                f"INSERT INTO begrotingsregel ({', '.join(kolommen)}) VALUES ({', '.join(lit(rij[k]) for k in kolommen)});"
            )
        return "\n".join(regels) + "\n"
