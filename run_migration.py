#!/usr/bin/env python3
"""
run_migration.py
Engångsscript: kör migrations/001_historical_import.sql mot Render PostgreSQL.
"""

import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()

BASE_DIR       = Path(__file__).parent
MIGRATION_FILE = BASE_DIR / "migrations" / "001_historical_import.sql"


def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        sys.exit("Fel: DATABASE_URL saknas i .env")

    if not MIGRATION_FILE.exists():
        sys.exit(f"Fel: Hittade inte {MIGRATION_FILE}")

    sql_text = MIGRATION_FILE.read_text(encoding="utf-8")
    print(f"Läser: {MIGRATION_FILE}")
    print(f"SQL-filens storlek: {len(sql_text)} tecken\n")

    print("Ansluter till Render PostgreSQL...")
    try:
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cur:
                print("Kör migration...")
                cur.execute(sql_text)
                print("Migration genomförd.\n")

                print("Verifierar skapade tabeller:")
                cur.execute(
                    "SELECT tablename FROM pg_tables "
                    "WHERE tablename LIKE 'historik_%' "
                    "ORDER BY tablename"
                )
                rows = cur.fetchall()
                if rows:
                    for (tablename,) in rows:
                        print(f"  + {tablename}")
                else:
                    print("  VARNING: Inga historik_-tabeller hittades.")

    except psycopg2.Error as e:
        print(f"\nFEL - transaktionen har rollbackats automatiskt.")
        print(f"Detaljer: {e}")
        sys.exit(1)

    print("\nKlar.")


if __name__ == "__main__":
    main()
