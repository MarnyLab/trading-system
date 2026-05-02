#!/usr/bin/env python3
"""
import_history.py
Importerar historisk portföljdata till Render PostgreSQL.

Kör: python import_history.py
Kräver: pip install psycopg2-binary openpyxl python-dotenv

.env måste innehålla:
    DATABASE_URL=postgresql://user:pass@host/dbname
"""

import hashlib
import os
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import openpyxl

load_dotenv()

BASE_DIR    = Path(__file__).parent
EXCEL_PATH  = BASE_DIR / "Data" / "Historik" / "Portfoljuppfoljning_v2.xlsx"

SNAPSHOT_DATE       = date(2026, 4, 28)
OPENING_DATE        = date(2021, 5, 3)
CARNEGIE_DIV_DATE   = date(2025, 12, 31)   # placeholder för aggregerade Carnegie-utdelningar

DEPA_DANSKE   = "3023140659"
DEPA_CARNEGIE = "1687755"

# ── Namn-normalisering ───────────────────────────────────────────────────────

# Korta/varierade namn i Transaktioner -> kanoniskt namn (från Innehav)
SHORT_NAME = {
    "ISHARES EURO STO":  "iShares Euro Stoxx",
    "DIS Glb Index SA":  "DIS Globala Index SA",
    "DI SE SmåbolagSA":  "DI SE Småbolag SA",
    "PNSverigeAktivAc":  "PN Sverige Aktiv Ac",
    "iShs PhysicalGld":  "iShares Physical Gold",
    "iShsS&P500T USD":   "iShares S&P 500 USD",
    "Fastighets Bal B":  "Fastighets Balder B",  # kort namn i split-transaktion
}

# ISIN-konsolidering + split-alias -> kanoniskt namn
ISIN_ALIAS = {
    "Castellum BTA":      "Castellum AB",
    # Castellum AB TR = teckningsrätt (eget instrument, mappar INTE till Castellum AB)
    "Fast.AB Ba B OLD":   "Fastighets Balder B",
    "NovoNordisB OLD":    "Novo Nordisk B",
    "Investor AB B OL":   "Investor AB B",
    "Sampo Oyj A OLD":    "Sampo Oyj A",
    "Xtr ArtUSD1CACH":    "Xtr Art USD1CAcc",
    "Sinch AB OLD":       "Sinch Rg",
    "Swedish MatchOLD":   "Swedish Match",
    # Case-/namnvarianter
    "SMART EYE":          "Smart Eye",
    "Essity Aktie B":     "Essity B",
    "Camurus":            "Camurus AB",
}

# Securities i Innehav "Portfölj 1" som faktiskt tillhör Carnegie ISK (1687755)
CARNEGIE_OVERRIDE = {"Carnegie Småbolagsfond A"}

# Dessa har fullständig transaktionshistorik – ingen öppningsbalans
NO_OPENING_BALANCE = {"Carnegie Småbolagsfond A"}


def canonical(name: str) -> str:
    if not name:
        return name
    name = name.strip()
    name = SHORT_NAME.get(name, name)
    name = ISIN_ALIAS.get(name, name)
    return name


# ── Tillgångsslag-mapping ────────────────────────────────────────────────────

def sektor_to_tillgangsslag(sektor: str) -> str:
    if not sektor:
        return "aktie"
    s = sektor.lower()
    if "fond" in s:
        return "fond"
    if "etf" in s:
        return "etf"
    if "kontant" in s:
        return "kontant"
    return "aktie"


NON_SEK_VALUTA = {
    "iShares Physical Gold":                "USD",
    "iShares Euro Stoxx":                   "EUR",
    "Neste Oyj":                            "EUR",
    "Novo Nordisk B":                       "DKK",
    "Xtr Art USD1CAcc":                     "EUR",
    "iShares S&P 500 USD":                  "USD",
    "Fiserv Inc.":                          "USD",
    "Ørsted":                               "DKK",
    "NKT A/S":                              "DKK",
    "Microsoft Corp.":                      "USD",
    "Ish Helt Car USD":                     "USD",
    "SSgA 2000 SM USD":                     "USD",
    "Alphabet C":                           "USD",
    "Sampo Oyj A":                          "EUR",
    "JPMorgan EM Local Currency Bond ETF":  "USD",
}

# Historiska/sålda securities som inte finns i Innehav
KNOWN_HISTORICAL: dict[str, tuple[str, str]] = {
    # Danske sålda
    "Swedbank AB A":                        ("aktie",       "SEK"),
    "Ericsson B":                           ("aktie",       "SEK"),
    "Microsoft Corp.":                      ("aktie",       "USD"),
    "NKT A/S":                              ("aktie",       "DKK"),
    "Handelsbanken A":                      ("aktie",       "SEK"),
    "SE-Banken A":                          ("aktie",       "SEK"),
    "Sampo Oyj A":                          ("aktie",       "EUR"),
    "Ish Helt Car USD":                     ("etf",         "USD"),
    "Nokia Oyj (SE)":                       ("aktie",       "SEK"),
    "Ørsted":                               ("aktie",       "DKK"),
    "SSgA 2000 SM USD":                     ("etf",         "USD"),
    "Samhallsb Nord B":                     ("aktie",       "SEK"),
    "Sandvik AB":                           ("aktie",       "SEK"),
    "Telia Company AB":                     ("aktie",       "SEK"),
    "Camurus AB":                           ("aktie",       "SEK"),
    "Essity B":                             ("aktie",       "SEK"),
    "Fenix Outdoor SE":                     ("aktie",       "SEK"),
    "Resurs Holding":                       ("aktie",       "SEK"),
    "Swedish Match":                        ("aktie",       "SEK"),
    "Alleima AB":                           ("aktie",       "SEK"),
    "Neobo Fastigh AB":                     ("aktie",       "SEK"),
    "Solid Forsakring":                     ("aktie",       "SEK"),
    "Alphabet C":                           ("aktie",       "USD"),
    "Veoneer SDB":                          ("aktie",       "SEK"),
    "Orron Ener Rg":                        ("aktie",       "SEK"),
    "Castellum AB TR":                      ("aktie",       "SEK"),  # teckningsrätt, utgången
    # Carnegie sålda (realiserad rapport)
    "BICO Group":                           ("aktie",       "SEK"),
    "Calliditas Therapeutics":              ("aktie",       "SEK"),
    "Cell Impact B":                        ("aktie",       "SEK"),
    "Embracer Group B":                     ("aktie",       "SEK"),
    "Embracer Group B3":                    ("aktie",       "SEK"),
    "Enad Global 7":                        ("aktie",       "SEK"),
    "IRRAS":                                ("aktie",       "SEK"),
    "Millicom Int. Cellular SDB":           ("aktie",       "SEK"),
    "NCC B":                                ("aktie",       "SEK"),
    "Nibe Industrier B":                    ("aktie",       "SEK"),
    "Ninety One Global Env Fund":           ("fond",        "SEK"),
    "Oncopeptides":                         ("aktie",       "SEK"),
    "SCA B":                                ("aktie",       "SEK"),
    "SSAB B":                               ("aktie",       "SEK"),
    "Stora Enso R":                         ("aktie",       "SEK"),
    "Tele2 B":                              ("aktie",       "SEK"),
    "Thule Group":                          ("aktie",       "SEK"),
    "Atlas Copco A":                        ("aktie",       "SEK"),
    "Räntepapper (Autocirc)":               ("rantepapper", "SEK"),
    "Asmodee Group B":                      ("aktie",       "SEK"),
    "JPMorgan EM Local Currency Bond ETF":  ("etf",         "USD"),
    "Q-Linea":                              ("aktie",       "SEK"),
    "Q-Linea TR":                           ("aktie",       "SEK"),
    "Storskogen ränteobligation":           ("rantepapper", "SEK"),
    "Verisure PLC":                         ("aktie",       "SEK"),
    # Övrigt
    "Likvida medel SEK":                    ("kontant",     "SEK"),
    "Ränteintäkter Carnegie (aggregerat)":  ("rantepapper", "SEK"),
}

# ── Ordertyp-mapping ─────────────────────────────────────────────────────────

ORDERTYP_MAP = {
    "Utdelning":         "utdelning",
    "Aktiesplit, ny":    "split",
    "Aktiesplit, gl.":   "split",
    "Fission, ny":       "fission",
    "Fission, gl.":      "fission",
    "Isinbyt,ny":        "isinbyte",
    "Isinbyt,gl.":       "isinbyte",
    "Tilldel av rätt":   "teckningsratt",
    "Teckrätt, ut":      "teckningsratt",
    "Teck, ny ak":       "teckningsratt",
    "Överföring ingång": "overforing",
    "Överf. utg.":       "overforing",
    "Emission":          "ovrigt",
    "Innehavet låst":    "ovrigt",
    "Förfall värdelösa": "ovrigt",
}

# Ordertypar som påverkar innehavsstorlek (används för nettokalkyl)
POSITION_CHANGING = {
    "kop", "salj", "split", "fission", "isinbyte",
    "teckningsratt", "overforing",
}


def map_ordertyp(raw: str, antal) -> str:
    if raw in ORDERTYP_MAP:
        return ORDERTYP_MAP[raw]
    # kop/salj bestäms av antal-tecknet
    if antal is not None and float(antal) < 0:
        return "salj"
    return "kop"


# ── Hjälpfunktioner ──────────────────────────────────────────────────────────

def source_ref(*parts) -> str:
    content = "|".join("NULL" if p is None else str(p) for p in parts)
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_date_str(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    return s if s and s != "–" else None


def bulk_insert(cur, table: str, columns: list[str], records: list[dict],
                conflict_sql: str = "ON CONFLICT DO NOTHING") -> int:
    if not records:
        return 0
    values = [tuple(r.get(c) for c in columns) for r in records]
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s {conflict_sql}"
    psycopg2.extras.execute_values(cur, sql, values, page_size=200)
    return len(values)


# ── Excel-läsning ────────────────────────────────────────────────────────────

def load_sheet(path: Path, sheet: str) -> list[tuple]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    return rows


# ── Securities ───────────────────────────────────────────────────────────────

def build_securities(innehav_rows: list) -> dict[str, dict]:
    """
    Returnerar {kanoniskt_namn: security_dict}.
    Kombinerar Innehav (aktiva) med KNOWN_HISTORICAL (sålda).
    """
    securities: dict[str, dict] = {}

    for row in innehav_rows:
        # Innehav-kolumner: depå, namn, antal, kurs, valuta, fx, mv_sek,
        #                   gav_sek, gav_local, orealj, orealj_pct, andel, sektor
        raw_namn = row[1]
        sektor   = row[12]
        if not raw_namn:
            continue

        if str(raw_namn).lower().startswith("likvida"):
            namn = "Likvida medel SEK"
            tillgangsslag = "kontant"
            valuta = "SEK"
        else:
            namn = canonical(raw_namn)
            tillgangsslag = sektor_to_tillgangsslag(sektor)
            valuta = NON_SEK_VALUTA.get(namn, "SEK")

        securities[namn] = {
            "namn":          namn,
            "tillgangsslag": tillgangsslag,
            "valuta":        valuta,
            "sektor":        sektor,
            "aktiv":         True,
        }

    for namn, (tillgangsslag, valuta) in KNOWN_HISTORICAL.items():
        if namn not in securities:
            securities[namn] = {
                "namn":          namn,
                "tillgangsslag": tillgangsslag,
                "valuta":        valuta,
                "sektor":        None,
                "aktiv":         False,
            }

    return securities


# ── Transaktioner (Danske) ───────────────────────────────────────────────────

def process_transaktioner(rows: list, sec_ids: dict) -> tuple[list, dict]:
    """
    Parsar Transaktioner-fliken (rad 4+ är data).
    Returnerar (transaction_records, canonical_net).
    canonical_net: {kanoniskt_namn -> netto antal} för positionsberäkning.
    """
    records = []
    canonical_net: dict[str, float] = {}

    # Kolumner: affarsdatum, likviddatum, namn, aktiv, ordertyp,
    #           antal, kurs, courtage, likvidbelopp_sek, valuta, depa
    for row in rows[4:]:
        if not row[0] or not row[2]:
            continue

        raw_namn   = str(row[2]).strip()
        raw_typ    = str(row[4]).strip() if row[4] else ""
        antal      = to_float(row[5])
        kurs       = to_float(row[6])
        courtage   = to_float(row[7]) or 0.0
        likvid     = to_float(row[8])
        valuta     = str(row[9]).strip() if row[9] else "SEK"
        affarsdatum = to_date_str(row[0])
        likviddatum = to_date_str(row[1])

        namn     = canonical(raw_namn)
        ordertyp = map_ordertyp(raw_typ, antal)

        sec_id = sec_ids.get(namn)
        if not sec_id:
            print(f"  VARNING: security saknas i DB: '{namn}' (raw: '{raw_namn}')", file=sys.stderr)
            continue

        # Uppdatera nettokalkyl (exkl. utdelning och ovrigt)
        if ordertyp in POSITION_CHANGING and antal is not None:
            canonical_net[namn] = canonical_net.get(namn, 0.0) + antal

        ref = source_ref("historik_danske", raw_namn, affarsdatum, raw_typ,
                         antal, likvid)

        records.append({
            "security_id":      sec_id,
            "depa":             DEPA_DANSKE,
            "affarsdatum":      affarsdatum,
            "likviddatum":      likviddatum,
            "ordertyp":         ordertyp,
            "antal":            antal,
            "kurs":             kurs,
            "courtage_sek":     courtage,
            "likvidbelopp_sek": likvid,
            "valuta":           valuta,
            "fx_rate":          None,
            "kalla":            "historik_danske",
            "notering":         None,
            "source_ref":       ref,
        })

    return records, canonical_net


# ── Carnegie Småbolagsfond A ─────────────────────────────────────────────────

CARNEGIE_SMABOLAG_TX = [
    # affarsdatum, antal, kurs_sek, likvidbelopp_sek
    (date(2023, 10, 25), 610.040000,  409.81, -250_050),
    (date(2023, 11, 24), 552.028888,  452.69, -249_950),
    (date(2024,  1, 23), 413.682408,  483.34, -200_000),
    (date(2025,  9, 25), 110.704618,  713.61,  -79_000),
    (date(2025, 10, 14), 420.209976,  713.93, -300_000),
]


def process_carnegie_smabolag(sec_ids: dict) -> list:
    sec_id = sec_ids.get("Carnegie Småbolagsfond A")
    if not sec_id:
        print("  VARNING: Carnegie Småbolagsfond A saknas i sec_ids", file=sys.stderr)
        return []

    records = []
    for tx_date, antal, kurs, likvid in CARNEGIE_SMABOLAG_TX:
        ref = source_ref("historik_carnegie", "Carnegie Småbolagsfond A",
                         tx_date.isoformat(), "kop", antal)
        records.append({
            "security_id":      sec_id,
            "depa":             DEPA_CARNEGIE,
            "affarsdatum":      tx_date.isoformat(),
            "likviddatum":      None,
            "ordertyp":         "kop",
            "antal":            antal,
            "kurs":             kurs,
            "courtage_sek":     0.0,
            "likvidbelopp_sek": float(likvid),
            "valuta":           "SEK",
            "fx_rate":          None,
            "kalla":            "historik_carnegie",
            "notering":         None,
            "source_ref":       ref,
        })
    return records


# ── Öppningsbalanser ─────────────────────────────────────────────────────────

def process_opening_balances(innehav_rows: list, sec_ids: dict,
                              canonical_net: dict) -> list:
    """
    Skapar öppningsbalans-transaktioner (kalla='oppningsbalans') för:
    - Alla Carnegie ISK-innehav (Portfölj 2) – full position
    - Danske-innehav (Portfölj 1) där nettot från transaktionerna < innehav
    """
    records = []

    for row in innehav_rows:
        portfolj  = str(row[0]).strip() if row[0] else ""
        raw_namn  = row[1]
        antal_v   = to_float(row[2])
        gav_sek_v = to_float(row[7])

        if not raw_namn or antal_v is None or antal_v <= 0:
            continue

        if str(raw_namn).lower().startswith("likvida"):
            # Likvida medel importeras som holdings_snapshot, ingen transaktionsrad
            continue

        namn = canonical(raw_namn)

        if namn in NO_OPENING_BALANCE:
            continue

        sec_id = sec_ids.get(namn)
        if not sec_id:
            print(f"  VARNING: {namn} saknas vid öppningsbalans", file=sys.stderr)
            continue

        is_carnegie = (portfolj == "Portfölj 2") or (namn in CARNEGIE_OVERRIDE)

        if is_carnegie:
            depa           = DEPA_CARNEGIE
            opening_antal  = antal_v
            opening_likvid = -(gav_sek_v) if gav_sek_v else None
        else:
            depa  = DEPA_DANSKE
            net   = canonical_net.get(namn, 0.0)
            diff  = antal_v - net
            if abs(diff) < 0.01:
                continue   # transaktionerna täcker positionen fullt ut
            opening_antal  = diff
            # Pro-rata GAV: andel av totalt anskaffningsvärde proportionellt mot diff
            opening_likvid = -(diff / antal_v * gav_sek_v) if gav_sek_v else None

        ref = source_ref("oppningsbalans", namn, depa, OPENING_DATE.isoformat())

        records.append({
            "security_id":      sec_id,
            "depa":             depa,
            "affarsdatum":      OPENING_DATE.isoformat(),
            "likviddatum":      None,
            "ordertyp":         "kop",
            "antal":            opening_antal,
            "kurs":             None,
            "courtage_sek":     0.0,
            "likvidbelopp_sek": opening_likvid,
            "valuta":           "SEK",
            "fx_rate":          None,
            "kalla":            "oppningsbalans",
            "notering":         "Öppningspost – position predaterar transaktionshistoriken",
            "source_ref":       ref,
        })

    return records


# ── Carnegie utdelningar + räntor ────────────────────────────────────────────

def process_carnegie_income(rows: list, sec_ids: dict) -> list:
    """
    Parsar 'Utdelningar & räntor'-fliken och skapar:
    - En utdelningsrad per Carnegie-värdepapper (aggregerat, datum = 2025-12-31)
    - En samlad ränterad (Ränteintäkter Carnegie aggregerat)
    """
    records = []
    carnegie_div_section = False

    for row in rows:
        # Hitta start på Carnegie-utdelningssektion
        if row[0] and "CARNEGIE ISK" in str(row[0]).upper():
            carnegie_div_section = True
            continue
        if row[0] and "CARNEGIE – RÄNTOR" in str(row[0]).upper():
            carnegie_div_section = False
            # Nästa rad med värde är total ränta
            continue
        if row[0] and "TOTAL RÄNTOR CARNEGIE" in str(row[0]).upper():
            ranta_belopp = to_float(row[2])
            if ranta_belopp:
                sec_id = sec_ids.get("Ränteintäkter Carnegie (aggregerat)")
                if sec_id:
                    ref = source_ref("historik_carnegie", "ranta", "476901")
                    records.append({
                        "security_id":      sec_id,
                        "depa":             DEPA_CARNEGIE,
                        "affarsdatum":      CARNEGIE_DIV_DATE.isoformat(),
                        "likviddatum":      None,
                        "ordertyp":         "ranta",
                        "antal":            None,
                        "kurs":             None,
                        "courtage_sek":     0.0,
                        "likvidbelopp_sek": float(ranta_belopp),
                        "valuta":           "SEK",
                        "fx_rate":          None,
                        "kalla":            "historik_carnegie",
                        "notering":         "Aggregerat 2020-2026: Autocirc + Storskogen + småräntor",
                        "source_ref":       ref,
                    })
            continue

        if not carnegie_div_section:
            continue

        # Utdelningsrad: (namn, aktiv, belopp, None, None, None)
        raw_namn = row[0]
        belopp   = to_float(row[2])
        if not raw_namn or belopp is None:
            continue
        if str(raw_namn).startswith("SUMMA"):
            carnegie_div_section = False
            continue

        namn   = canonical(str(raw_namn).strip())
        sec_id = sec_ids.get(namn)
        if not sec_id:
            print(f"  VARNING: Carnegie utdelning – security saknas: '{namn}'",
                  file=sys.stderr)
            continue

        ref = source_ref("historik_carnegie", "utdelning", namn)
        records.append({
            "security_id":      sec_id,
            "depa":             DEPA_CARNEGIE,
            "affarsdatum":      CARNEGIE_DIV_DATE.isoformat(),
            "likviddatum":      None,
            "ordertyp":         "utdelning",
            "antal":            None,
            "kurs":             None,
            "courtage_sek":     0.0,
            "likvidbelopp_sek": float(belopp),
            "valuta":           "SEK",
            "fx_rate":          None,
            "kalla":            "historik_carnegie",
            "notering":         "Aggregerat per värdepapper – detaljer saknas i Carnegie-rapport",
            "source_ref":       ref,
        })

    return records


# ── Holdings Snapshot ────────────────────────────────────────────────────────

def process_holdings_snapshot(innehav_rows: list, sec_ids: dict) -> list:
    """
    Skapar en holdings_snapshot-rad per innehav (datum = 2026-04-28).
    Inkluderar Likvida medel SEK.
    """
    records = []

    for row in innehav_rows:
        portfolj  = str(row[0]).strip() if row[0] else ""
        raw_namn  = row[1]
        antal     = to_float(row[2])
        kurs      = to_float(row[3])
        fx_rate   = to_float(row[5])
        mv_sek    = to_float(row[6])
        gav_sek   = to_float(row[7])
        orealj    = to_float(row[9])

        if not raw_namn or antal is None:
            continue

        if str(raw_namn).lower().startswith("likvida"):
            namn = "Likvida medel SEK"
            depa = DEPA_CARNEGIE
        else:
            namn = canonical(raw_namn)
            is_carnegie = (portfolj == "Portfölj 2") or (namn in CARNEGIE_OVERRIDE)
            depa = DEPA_CARNEGIE if is_carnegie else DEPA_DANSKE

        sec_id = sec_ids.get(namn)
        if not sec_id:
            print(f"  VARNING: holdings_snapshot – security saknas: '{namn}'",
                  file=sys.stderr)
            continue

        records.append({
            "security_id":       sec_id,
            "depa":              depa,
            "datum":             SNAPSHOT_DATE.isoformat(),
            "antal":             antal,
            "kurs_lokal":        kurs,
            "fx_rate":           fx_rate,
            "marknadsvarde_sek": mv_sek,
            "gav_sek":           gav_sek,
            "orealiserat_sek":   orealj,
        })

    return records


# ── Realized PnL ─────────────────────────────────────────────────────────────

def process_realized_pnl(rows: list, sec_ids: dict) -> list:
    """
    Parsar 'Realiserad historik'-fliken.
    Hanterar tre sektioner: Carnegie aktier, Carnegie övriga, Danske sålda.
    """
    records = []

    # Sektion-tracker
    # 0=söker, 1=carnegie_aktier, 2=carnegie_ovrigt, 3=danske
    section = 0

    for row in rows:
        first = str(row[0]).strip() if row[0] else ""

        # Sektionshuvuden
        if "CARNEGIE – REALISERADE VINSTER" in first.upper():
            section = 1
            continue
        if "CARNEGIE – ÖVRIGA" in first.upper():
            section = 2
            continue
        if "DANSKE" in first.upper() and "SÅLDA" in first.upper():
            section = 3
            continue

        # Hoppa över rubriker, summarader, tomrader
        if not first or first.startswith("SUMMA") or first.startswith("TOTAL") \
                or first.startswith("Värdepapper") or first.startswith("OBS"):
            continue

        if section in (1, 2):
            # Carnegie-format: namn, antal, datum, forsalj, anskaffn, vinst, kommentar
            raw_namn   = first
            antal      = to_float(row[1])
            tx_datum   = to_date_str(row[2])
            forsalj    = to_float(row[3])
            anskaffn   = to_float(row[4])
            vinst      = to_float(row[5])
            kommentar  = str(row[6]).strip() if row[6] else None

            if forsalj is None or anskaffn is None or vinst is None:
                continue

            # Hantera '–' i antal/datum (övriga-sektionen)
            if not tx_datum or tx_datum == "–":
                tx_datum = None
            if antal is not None and str(row[1]).strip() == "–":
                antal = None

            namn   = canonical(raw_namn)
            sec_id = sec_ids.get(namn)
            if not sec_id:
                print(f"  VARNING: realized_pnl Carnegie – '{namn}' saknas",
                      file=sys.stderr)
                continue

            ref = source_ref("carnegie_rapport", namn, tx_datum, antal, forsalj)
            records.append({
                "security_id":             sec_id,
                "depa":                    DEPA_CARNEGIE,
                "forsaljningsdatum":       tx_datum,
                "antal":                   antal,
                "forsaljningsbelopp_sek":  forsalj,
                "anskaffningsbelopp_sek":  anskaffn,
                "realiserad_vinst_sek":    vinst,
                "kalla":                   "carnegie_rapport",
                "kommentar":               kommentar,
                "source_ref":              ref,
            })

        elif section == 3:
            # Danske-format: namn, köpt, sålt, realiserat, utdelning, totalt, kommentar
            raw_namn  = first
            kopt      = to_float(row[1])
            salt      = to_float(row[2])
            realiserat = to_float(row[3])
            # row[4] = utdelning – ej med i realized_pnl (finns i transactions)
            kommentar = str(row[6]).strip() if row[6] else None

            if kopt is None or salt is None or realiserat is None:
                continue

            namn   = canonical(raw_namn)
            sec_id = sec_ids.get(namn)
            if not sec_id:
                print(f"  VARNING: realized_pnl Danske – '{namn}' saknas",
                      file=sys.stderr)
                continue

            ref = source_ref("danske_berakn", namn, kopt, salt)
            records.append({
                "security_id":             sec_id,
                "depa":                    DEPA_DANSKE,
                "forsaljningsdatum":       "2026-04-30",  # placeholder rapportdatum
                "antal":                   None,
                "forsaljningsbelopp_sek":  float(salt),
                "anskaffningsbelopp_sek":  float(kopt),
                "realiserad_vinst_sek":    float(realiserat),
                "kalla":                   "danske_berakn",
                "kommentar":               kommentar,
                "source_ref":              ref,
            })

    return records


# ── Main ─────────────────────────────────────────────────────────────────────

SEC_COLS = ["namn", "tillgangsslag", "valuta", "sektor", "aktiv"]

TX_COLS = [
    "security_id", "depa", "affarsdatum", "likviddatum", "ordertyp",
    "antal", "kurs", "courtage_sek", "likvidbelopp_sek", "valuta",
    "fx_rate", "kalla", "notering", "source_ref",
]

HS_COLS = [
    "security_id", "depa", "datum", "antal", "kurs_lokal",
    "fx_rate", "marknadsvarde_sek", "gav_sek", "orealiserat_sek",
]

PNL_COLS = [
    "security_id", "depa", "forsaljningsdatum", "antal",
    "forsaljningsbelopp_sek", "anskaffningsbelopp_sek",
    "realiserad_vinst_sek", "kalla", "kommentar", "source_ref",
]

HS_CONFLICT = (
    "ON CONFLICT (security_id, depa, datum) DO UPDATE SET "
    "antal = EXCLUDED.antal, "
    "kurs_lokal = EXCLUDED.kurs_lokal, "
    "fx_rate = EXCLUDED.fx_rate, "
    "marknadsvarde_sek = EXCLUDED.marknadsvarde_sek, "
    "gav_sek = EXCLUDED.gav_sek, "
    "orealiserat_sek = EXCLUDED.orealiserat_sek"
)


def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        sys.exit("Fel: DATABASE_URL måste finnas i .env")

    if not EXCEL_PATH.exists():
        sys.exit(f"Fel: Excel-filen hittades inte: {EXCEL_PATH}")

    # ── Läs Excel-flikar ───────────────────────────────────────────────────
    print(f"Läser Excel: {EXCEL_PATH}\n")
    innehav_rows       = load_sheet(EXCEL_PATH, "Innehav")
    transaktioner_rows = load_sheet(EXCEL_PATH, "Transaktioner")
    realiserad_rows    = load_sheet(EXCEL_PATH, "Realiserad historik")
    utdelning_rows     = load_sheet(EXCEL_PATH, "Utdelningar & räntor")

    # Innehav-datarader: hoppa över 4 headerrader, sista raden är TOTALT
    innehav_data = [r for r in innehav_rows[4:] if r[1] and str(r[1]) != "TOTALT"]

    # Bygg alla poster i minnet innan vi öppnar DB-anslutningen
    print("Steg 0: Bygger securities-lista...")
    sec_dict = build_securities(innehav_data)
    print(f"  -> {len(sec_dict)} unika värdepapper (aktiva + historiska)\n")

    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cur:

            # ── STEG 1: Securities ─────────────────────────────────────────
            print("Steg 1/6: Infogar securities...")
            n_sec = bulk_insert(
                cur, "historik_securities", SEC_COLS,
                list(sec_dict.values()),
                conflict_sql="ON CONFLICT (namn) DO NOTHING",
            )
            print(f"  -> {n_sec} securities behandlade (ON CONFLICT DO NOTHING)")

            # Hämta ID-mapping från DB (inkl. redan befintliga)
            cur.execute("SELECT id::text, namn FROM historik_securities")
            sec_ids: dict[str, str] = {namn: uid for uid, namn in cur.fetchall()}
            print(f"  -> {len(sec_ids)} securities i databasen\n")

            # ── STEG 2: Transaktioner (Danske) ────────────────────────────
            print("Steg 2/6: Importerar Danske-transaktioner...")
            tx_records, canonical_net = process_transaktioner(transaktioner_rows, sec_ids)
            n_tx = bulk_insert(
                cur, "historik_transactions", TX_COLS, tx_records,
                conflict_sql="ON CONFLICT (source_ref) DO NOTHING",
            )
            print(f"  -> {n_tx} Danske-transaktioner infogade\n")

            # ── STEG 3: Carnegie Småbolagsfond + öppningsbalanser ─────────
            print("Steg 3/6: Carnegie Småbolagsfond A + öppningsbalanser...")
            cs_records  = process_carnegie_smabolag(sec_ids)
            ob_records  = process_opening_balances(innehav_data, sec_ids, canonical_net)
            all_opening = cs_records + ob_records
            n_open = bulk_insert(
                cur, "historik_transactions", TX_COLS, all_opening,
                conflict_sql="ON CONFLICT (source_ref) DO NOTHING",
            )
            print(f"  -> {len(cs_records)} Carnegie Småbolagsfond-transaktioner infogade")
            print(f"  -> {len(ob_records)} öppningsbalanser infogade")
            print(f"  -> {n_open} totalt\n")

            # ── STEG 4: Carnegie utdelningar + räntor ─────────────────────
            print("Steg 4/6: Carnegie utdelningar och räntor...")
            inc_records = process_carnegie_income(utdelning_rows, sec_ids)
            n_inc = bulk_insert(
                cur, "historik_transactions", TX_COLS, inc_records,
                conflict_sql="ON CONFLICT (source_ref) DO NOTHING",
            )
            utd_sum = sum(r["likvidbelopp_sek"] for r in inc_records
                          if r["ordertyp"] == "utdelning" and r["likvidbelopp_sek"])
            rnt_sum = sum(r["likvidbelopp_sek"] for r in inc_records
                          if r["ordertyp"] == "ranta" and r["likvidbelopp_sek"])
            print(f"  -> {n_inc} rader infogade")
            print(f"     Utdelning: {utd_sum:,.0f} SEK  (rapport: 563 003 SEK)")
            if abs(utd_sum - 563_003) > 1:
                print(f"     NOTERA: differens {563_003 - utd_sum:,.0f} SEK vs Carnegie PDF-rapport")
            print(f"     Räntor:    {rnt_sum:,.0f} SEK  (rapport: 476 901 SEK)\n")

            # ── STEG 5: Holdings snapshot ──────────────────────────────────
            print("Steg 5/6: Holdings snapshot (2026-04-28)...")
            hs_records = process_holdings_snapshot(innehav_data, sec_ids)
            n_hs = bulk_insert(
                cur, "historik_holdings_snapshot", HS_COLS, hs_records,
                conflict_sql=HS_CONFLICT,
            )
            mv_total     = sum(r.get("marknadsvarde_sek") or 0 for r in hs_records)
            orealj_total = sum(r.get("orealiserat_sek") or 0 for r in hs_records)
            print(f"  -> {n_hs} snapshot-rader upsertade")
            print(f"     Marknadsvärde:  {mv_total:,.0f} SEK  (förväntat: 12 415 323 SEK)")
            print(f"     Orealiserat:    {orealj_total:,.0f} SEK  (förväntat: 1 792 461 SEK)\n")

            # ── STEG 6: Realized PnL ──────────────────────────────────────
            print("Steg 6/6: Realiserad historik...")
            pnl_records = process_realized_pnl(realiserad_rows, sec_ids)
            n_pnl = bulk_insert(
                cur, "historik_realized_pnl", PNL_COLS, pnl_records,
                conflict_sql="ON CONFLICT (source_ref) DO NOTHING",
            )
            carn_aktier = sum(r["realiserad_vinst_sek"] for r in pnl_records
                              if r["kalla"] == "carnegie_rapport"
                              and (r.get("kommentar") or "") not in
                                  {"Förfallen ränteobligation"})
            carn_total  = sum(r["realiserad_vinst_sek"] for r in pnl_records
                              if r["kalla"] == "carnegie_rapport")
            print(f"  -> {n_pnl} realized_pnl-rader infogade")
            print(f"     Carnegie aktier:  {carn_aktier:,.0f} SEK  (förväntat: 602 393 SEK)")
            print(f"     Carnegie totalt:  {carn_total:,.0f} SEK  (förväntat: 276 115 SEK)")

            # Notera om Excel-cellen C67 stämmer
            print()
            print("NOTE: Excel-cell C67 visar -326 278 (felaktig, bara övriga-summan).")
            print("      Korrekt total beraknad i DB: 602 393 + (-326 278) = 276 115 SEK")

        # conn.__exit__ anropas här – commit vid framgång, rollback vid undantag

    # ── Sammanfattning ────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("IMPORT KLAR")
    print("=" * 60)
    print(f"  Securities:          {len(sec_ids):>6}")
    print(f"  Transactions:        {n_tx + n_open + n_inc:>6}")
    print(f"    varav Danske:      {n_tx:>6}")
    print(f"    varav Carnegie tx: {len(cs_records):>6}")
    print(f"    varav öppnings.:   {len(ob_records):>6}")
    print(f"    varav income:      {n_inc:>6}")
    print(f"  Holdings snapshot:   {n_hs:>6}")
    print(f"  Realized PnL:        {n_pnl:>6}")
    print()
    print("Kör verifieringsqueryts i migrations/001_historical_import.sql")
    print("för att jämföra mot kända kontrollsummor.")


if __name__ == "__main__":
    main()
