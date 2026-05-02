#!/usr/bin/env python3
"""
seed_portfolios.py
Engångsscript: skapar portföljer och fyller innehav/transaktioner från historik_holdings_snapshot.

Steg som körs:
  1. ALTER TABLE – gör ticker nullable, lägger till fx_rate och senaste_kurs_manuell*
  2. Skapar portföljer 'Danske Bank' och 'Carnegie ISK' (om de saknas)
  3. Per rad i historik_holdings_snapshot 2026-04-28: INSERT innehav + INSERT transaktioner
  4. Verifierar anskaffningsvärden mot förväntade kontrollsummor
  5. Loggar STATUS.md med kända begränsningar

Idempotent: hoppar över innehav som redan finns (namn + portfolj_id).

Kurs-logik:
  kurs = gav_sek / antal  (alltid SEK, oavsett handelsvaluta)
  fx_rate = 1.0           (alltid)
  -> antal x kurs x fx_rate = gav_sek exakt
  Se KL-4 i STATUS.md for konsekvenser for icke-SEK-innehav.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()

BASE_DIR      = Path(__file__).parent
NOW           = datetime.now().strftime("%Y-%m-%d %H:%M")
SNAPSHOT_DATE = "2026-04-28"
OPENING_NOTE  = ("Oppningsbalans fran historik_holdings_snapshot 2026-04-28"
                 " - verkligt kopdatum okant")

DEPA_DANSKE   = "3023140659"
DEPA_CARNEGIE = "1687755"

TICKER_MAP = {
    "Investor AB B":              "INVB.ST",
    "iShares Physical Gold":      "IGLN.L",
    "Volvo B":                    "VOLV-B.ST",
    "Industrivarden C":           "INDU-C.ST",
    "AstraZeneca":                "AZN.ST",
    "Neste Oyj":                  "NESTE.HE",
    "Bonesupport Holding":        "BONE.ST",
    "iShares Euro Stoxx":         "EXW1.DE",
    "Castellum AB":               "CAST.ST",
    "Sinch Rg":                   "SINCH.ST",
    "Fastighets Balder B":        "BALD-B.ST",
    "Smart Eye":                  "SEYE.ST",
    "Novo Nordisk B":             "NOVO-B.CO",
    "Xtr Art USD1CAcc":           "XAIX.DE",
    "iShares S&P 500 USD":        "CSPX.L",
    "Atlas Copco B":              "ATCO-B.ST",
    "AAK AB":                     "AAK.ST",
    "Vitrolife":                  "VITR.ST",
    "Hoist Finance AB":           "HOFI.ST",
    "Fiserv Inc.":                "FI",
    "BioGaia AB B":               "BIOG-B.ST",
    "SKF B":                      "SKF-B.ST",
    "Dometic Group":              "DOMETIC.ST",
    "Truecaller AK B":            "TRUE-B.ST",
    # Fonder - ingen Yahoo-ticker
    "Carnegie Smabolagsfond A":   None,
    "DIS Globala Index SA":       None,
    "DI SE Smabolag SA":          None,
    "Lannebo Smabolag":           None,
    "PN Sverige Aktiv Ac":        None,
    # Kontant
    "Likvida medel SEK":          None,
}

# Lagg till svenska teckenvarianter (matchas mot DB-namn med korrekt UTF-8)
TICKER_MAP["ÅÅK AB"]                    = "AAK.ST"          # fallback
TICKER_MAP["Carnegie Småbolagsfond A"]        = None
TICKER_MAP["DI SE Småbolag SA"]              = None
TICKER_MAP["Lannebo Småbolag"]               = None
TICKER_MAP["Industivärden C"]                = "INDU-C.ST"       # fallback
TICKER_MAP["Industrivarden C"]                    = "INDU-C.ST"
TICKER_MAP["Industrivarden C"]                    = "INDU-C.ST"


def run_schema_migrations(cur):
    print("Steg 1: Schema-migrationer...")

    cur.execute("ALTER TABLE innehav ALTER COLUMN ticker DROP NOT NULL")
    print("  -> innehav.ticker: NOT NULL borttagen")

    cur.execute("""
        ALTER TABLE transaktioner
        ADD COLUMN IF NOT EXISTS fx_rate REAL DEFAULT 1.0
    """)
    print("  -> transaktioner.fx_rate: ADD COLUMN IF NOT EXISTS (fixar befintlig bugg)")

    cur.execute("""
        ALTER TABLE innehav
        ADD COLUMN IF NOT EXISTS senaste_kurs_manuell NUMERIC
    """)
    cur.execute("""
        ALTER TABLE innehav
        ADD COLUMN IF NOT EXISTS senaste_kurs_manuell_datum DATE
    """)
    print("  -> innehav.senaste_kurs_manuell*: ADD COLUMN IF NOT EXISTS")
    print()


def get_or_create_portfolio(cur, namn, niva):
    cur.execute("SELECT id FROM portfoljer WHERE namn = %s", (namn,))
    row = cur.fetchone()
    if row:
        print(f"  Portfolj '{namn}' finns redan (id={row[0]})")
        return row[0]
    cur.execute(
        "INSERT INTO portfoljer (namn, niva, skapad) VALUES (%s, %s, %s) RETURNING id",
        (namn, niva, NOW)
    )
    pid = cur.fetchone()[0]
    print(f"  Skapad portfolj '{namn}' (id={pid}, niva='{niva}')")
    return pid


def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        sys.exit("Fel: DATABASE_URL saknas i .env")

    print("Ansluter till PostgreSQL...\n")
    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cur:

            # Steg 1: Schema
            run_schema_migrations(cur)

            # Steg 2: Portfoljer
            print("Steg 2: Portfoljer...")
            pid_danske   = get_or_create_portfolio(cur, "Danske Bank",  "Depå")
            pid_carnegie = get_or_create_portfolio(cur, "Carnegie ISK", "ISK")
            depa_to_pid  = {DEPA_DANSKE: pid_danske, DEPA_CARNEGIE: pid_carnegie}
            print()

            # Steg 3: Snapshot
            print(f"Steg 3: Laser historik_holdings_snapshot ({SNAPSHOT_DATE})...")
            cur.execute("""
                SELECT
                    s.namn,
                    s.tillgangsslag,
                    s.valuta,
                    h.depa,
                    h.antal,
                    h.kurs_lokal,
                    h.marknadsvarde_sek,
                    h.gav_sek
                FROM historik_holdings_snapshot h
                JOIN historik_securities s ON s.id = h.security_id
                WHERE h.datum = %s
                ORDER BY h.marknadsvarde_sek DESC
            """, (SNAPSHOT_DATE,))
            snapshot_rows = cur.fetchall()
            print(f"  -> {len(snapshot_rows)} rader hittade\n")

            # Steg 4: Innehav + transaktioner
            print("Steg 4: Infogar innehav och transaktioner...")
            n_skapade   = 0
            n_hoppade   = 0
            n_ej_ticker = 0

            for row in snapshot_rows:
                namn, tillgangsslag, valuta, depa, antal, kurs_lokal, mv_sek, gav_sek = row
                antal      = float(antal      or 0)
                kurs_lokal = float(kurs_lokal or 0) if kurs_lokal else None
                mv_sek     = float(mv_sek     or 0)
                gav_sek    = float(gav_sek    or 0)

                portfolj_id = depa_to_pid.get(depa)
                if not portfolj_id:
                    print(f"  VARNING: okand depa '{depa}' for '{namn}' - hoppar")
                    continue

                cur.execute(
                    "SELECT id FROM innehav WHERE namn = %s AND portfolj_id = %s",
                    (namn, portfolj_id)
                )
                if cur.fetchone():
                    print(f"  HOPP: '{namn}' finns redan")
                    n_hoppade += 1
                    continue

                ticker = TICKER_MAP.get(namn)
                if ticker is None:
                    n_ej_ticker += 1

                # Kurs-logik: alltid SEK, fx_rate alltid 1.0
                kurs_tx = round(gav_sek / antal, 6) if antal > 0 else 0.0
                fx_tx   = 1.0

                # Manuell kurs for fonder och kontant
                manuell_kurs  = None
                manuell_datum = None
                if ticker is None:
                    if namn == "Likvida medel SEK":
                        manuell_kurs  = 1.0
                        manuell_datum = SNAPSHOT_DATE
                    elif kurs_lokal and kurs_lokal > 0:
                        manuell_kurs  = kurs_lokal
                        manuell_datum = SNAPSHOT_DATE

                cur.execute("""
                    INSERT INTO innehav
                        (portfolj_id, namn, ticker, tillgangsslag, valuta,
                         senaste_kurs_manuell, senaste_kurs_manuell_datum, skapad)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    portfolj_id, namn, ticker, tillgangsslag, valuta,
                    manuell_kurs, manuell_datum, NOW
                ))
                innehav_id = cur.fetchone()[0]

                cur.execute("""
                    INSERT INTO transaktioner
                        (innehav_id, typ, antal, kurs, fx_rate, datum, notering, skapad)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    innehav_id, "KOP", antal, kurs_tx, fx_tx,
                    SNAPSHOT_DATE, OPENING_NOTE, NOW
                ))

                ticker_disp = ticker if ticker else "(ingen ticker)"
                print(f"  + {namn:<35} {ticker_disp:<14} {valuta}"
                      f"  kurs_sek={kurs_tx:.2f}")
                n_skapade += 1

            # Steg 5: Verifiering
            print("\nSteg 5: Verifiering av anskaffningsvardens...")
            cur.execute("""
                SELECT
                    p.namn AS portfolj,
                    COUNT(i.id) AS antal_innehav,
                    COALESCE(SUM(t.antal * t.kurs * t.fx_rate), 0)::numeric(12,2) AS anskaffning_sek
                FROM portfoljer p
                LEFT JOIN innehav i ON i.portfolj_id = p.id
                LEFT JOIN transaktioner t ON t.innehav_id = i.id
                WHERE p.id IN (%s, %s)
                GROUP BY p.id, p.namn
                ORDER BY p.id
            """, (pid_danske, pid_carnegie))

            print(f"\n  {'Portfolj':<20} {'Innehav':>8} {'Anskaffning SEK':>18}  Forvantad")
            print(f"  {'-'*62}")
            forvantade = {
                "Danske Bank":  ("ca 7 327 117", 7_327_117),
                "Carnegie ISK": ("ca 3 295 745", 3_295_745),
            }
            for portfolj, antal_i, anskaffning in cur.fetchall():
                forv_text, forv_val = forvantade.get(portfolj, ("?", 0))
                diff = abs(float(anskaffning) - forv_val)
                flagga = "OK" if diff < 5_000 else f"DIFF {diff:,.0f} SEK"
                print(f"  {portfolj:<20} {antal_i:>8} {float(anskaffning):>18,.2f}"
                      f"  {forv_text}  {flagga}")

    # Sammanfattning
    print(f"""
============================================================
SEED KLAR
============================================================
  Portfoljer  : Danske Bank (id={pid_danske}), Carnegie ISK (id={pid_carnegie})
  Innehav in  : {n_skapade}
  Hoppade     : {n_hoppade}  (namn+portfolj_id fanns redan)
  Utan ticker : {n_ej_ticker}  (fonder + Likvida medel)
""")

    # STATUS.md
    status_path = BASE_DIR / "STATUS.md"
    entry = (
        f"\n## {datetime.now().strftime('%Y-%m-%d')} - seed_portfolios.py\n\n"
        "### Kanda begransningar (Known Limitations)\n\n"
        "**KL-1: Icke-SEK-innehav uppdaterar inte vaxelkursen live**\n"
        "iShares Physical Gold, iShares S&P 500, Fiserv (USD), Neste, "
        "iShares Euro Stoxx, Xtr Art (EUR), Novo Nordisk (DKK) saknar live-FX.\n"
        "Planerad forbattring: live-FX via yfinance USDSEK=X / EURSEK=X / DKKSEK=X.\n\n"
        "**KL-2: Fonder saknar Yahoo-ticker - visar manuell kurs fran 2026-04-28**\n"
        "Carnegie Smabolagsfond A, DIS Globala Index SA, DI SE Smabolag SA,\n"
        "Lannebo Smabolag, PN Sverige Aktiv Ac uppdateras inte automatiskt.\n\n"
        f"**KL-3: KOP-datum ar snapshot-datumet {SNAPSHOT_DATE}, inte verkligt kopdatum**\n"
        "Historiska kopdatum finns i historik_transactions men ar inte kopplade hit.\n\n"
        "**KL-4: kurs i transaktioner ar alltid i SEK (oavsett handelsvaluta)**\n"
        "For icke-SEK-innehav konverterades GAV till SEK i historik_holdings_snapshot.\n"
        "Live-kursjamforelse i lokal valuta vs kurs-faltet ar inte rattvisande\n"
        "for USD/EUR/DKK-innehav. Atgardas nar historik_transactions kopplas in.\n"
    )
    with open(status_path, "a", encoding="utf-8") as f:
        f.write(entry)
    print("STATUS.md uppdaterad med kanda begransningar.")


if __name__ == "__main__":
    main()
