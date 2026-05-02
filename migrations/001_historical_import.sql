-- =============================================================================
-- 001_historical_import.sql
-- Schema för historisk portföljdata – Gena Management AB
-- Kör via psql mot Render PostgreSQL: psql $DATABASE_URL -f migrations/001_historical_import.sql
--
-- Depåer:
--   Portfölj 1 = Danske Bank  (depa = '3023140659')
--   Portfölj 2 = Carnegie ISK (depa = '1687755')
--
-- Tabellnamn har historik_-prefix för tydlig separation från appens egna tabeller.
--
-- Idempotens-strategi:
--   historik_securities        – UNIQUE(namn),                    ON CONFLICT DO NOTHING
--   historik_transactions      – UNIQUE(source_ref),              ON CONFLICT DO NOTHING
--   historik_holdings_snapshot – UNIQUE(security_id,depa,datum),  ON CONFLICT DO UPDATE
--   historik_realized_pnl      – UNIQUE(source_ref),              ON CONFLICT DO NOTHING
--   → Säkert att köra om. Live-data (kalla='live') påverkas aldrig.
-- =============================================================================


-- =============================================================================
-- TABLE: historik_securities
-- Masterregister för värdepapper. En rad per unikt instrument.
-- Skapas en gång och refereras av alla andra historik-tabeller.
-- =============================================================================

CREATE TABLE IF NOT EXISTS historik_securities (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    namn            text        NOT NULL,           -- kanoniskt namn: 'Investor AB B'
    kortnamn        text,                           -- 'INVB'
    isin            text,
    yahoo_ticker    text,                           -- 'INVB.ST', används för daglig kursuppdatering
    valuta          text        NOT NULL DEFAULT 'SEK',   -- handelsvaluta
    tillgangsslag   text        NOT NULL            -- se CHECK nedan
                    CHECK (tillgangsslag IN (
                        'aktie',
                        'fond',
                        'etf',
                        'rantepapper',
                        'kontant'
                    )),
    sektor          text,
    land            text,
    aktiv           boolean     NOT NULL DEFAULT true,  -- false = avyttrad position
    skapad          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT historik_securities_namn_unique UNIQUE (namn)
);

COMMENT ON TABLE  historik_securities                IS 'Masterregister för värdepapper. En post per unikt instrument oavsett depå.';
COMMENT ON COLUMN historik_securities.namn           IS 'Kanoniskt namn – ISIN-konsoliderade alias pekar hit (t.ex. Castellum BTA → Castellum AB).';
COMMENT ON COLUMN historik_securities.aktiv          IS 'true = innehas idag. Sätts false för historiska positioner som avyttrats.';
COMMENT ON COLUMN historik_securities.tillgangsslag  IS 'aktie | fond | etf | rantepapper | kontant';

CREATE INDEX IF NOT EXISTS idx_historik_securities_isin
    ON historik_securities(isin) WHERE isin IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_historik_securities_aktiv
    ON historik_securities(aktiv);


-- =============================================================================
-- TABLE: historik_transactions
-- Alla transaktioner – historiska och löpande.
-- Positiv antal = ökar position (köp, ny-sida av split/fission).
-- Negativ antal  = minskar position (sälj, gl-sida av split/fission).
-- Positivt likvidbelopp_sek = pengar in. Negativt = pengar ut.
-- =============================================================================

CREATE TABLE IF NOT EXISTS historik_transactions (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    security_id         uuid        NOT NULL REFERENCES historik_securities(id),
    depa                text        NOT NULL,   -- '3023140659' eller '1687755'
    affarsdatum         date        NOT NULL,
    likviddatum         date,
    ordertyp            text        NOT NULL
                        CHECK (ordertyp IN (
                            'kop',           -- köp (inkl fondorder köp, öppningsbalans)
                            'salj',          -- försäljning
                            'utdelning',     -- utdelning
                            'ranta',         -- ränteintäkt eller räntekostnad
                            'split',         -- aktiesplit (ny-rad positiv, gl-rad negativ)
                            'fission',       -- avknoppning (ny-rad + gl-rad per bolag)
                            'isinbyte',      -- ISIN-byte utan kassaflöde
                            'teckningsratt', -- tilldelning, utnyttjande eller teckning av rätter
                            'overforing',    -- överföring mellan konton (filtreras ur avkastning)
                            'ovrigt'         -- emission, förfall, låst – ej kassaflöde
                        )),
    antal               numeric,               -- NULL t.ex. för aggregerade Carnegie-utdelningar
    kurs                numeric,               -- i handelsvaluta; NULL vid split/utdelning/fission
    courtage_sek        numeric     NOT NULL DEFAULT 0,
    likvidbelopp_sek    numeric,               -- NULL för isinbyte/fission/split
    valuta              text,                  -- handelsvaluta (SEK, EUR, USD, DKK)
    fx_rate             numeric,               -- SEK per valutaenhet vid affärstillfället
    kalla               text        NOT NULL
                        CHECK (kalla IN (
                            'historik_danske',   -- Danskefilen 2021-05-04→
                            'historik_carnegie', -- Carnegie-rapport (utd/ränta) + egna köp
                            'oppningsbalans',    -- öppningspost för position som föregår historiken
                            'live'               -- löpande handel efter historikimport
                        )),
    notering            text,                  -- fritextkommentar, t.ex. 'aggregerat per år'
    source_ref          text        UNIQUE,    -- deterministisk hash för idempotens vid reimport
    skapad              timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  historik_transactions                  IS 'Alla transaktioner. Historiska (kalla≠live) och löpande (kalla=live) samexisterar.';
COMMENT ON COLUMN historik_transactions.ordertyp         IS 'kop|salj|utdelning|ranta|split|fission|isinbyte|teckningsratt|overforing|ovrigt';
COMMENT ON COLUMN historik_transactions.kalla            IS 'Datakälla. Filtreras på vid P&L-beräkning (uteslut oppningsbalans/overforing ur totalavkastning).';
COMMENT ON COLUMN historik_transactions.source_ref       IS 'MD5 av (kalla+security_id+datum+ordertyp+antal+likvidbelopp). Förhindrar dubbletter vid reimport.';
COMMENT ON COLUMN historik_transactions.likvidbelopp_sek IS 'Negativt = pengar ut (köp). Positivt = pengar in (sälj, utdelning). NULL vid bokföringstransaktioner.';

CREATE INDEX IF NOT EXISTS idx_historik_transactions_security_id
    ON historik_transactions(security_id);

CREATE INDEX IF NOT EXISTS idx_historik_transactions_depa
    ON historik_transactions(depa);

CREATE INDEX IF NOT EXISTS idx_historik_transactions_affarsdatum
    ON historik_transactions(affarsdatum DESC);

CREATE INDEX IF NOT EXISTS idx_historik_transactions_kalla
    ON historik_transactions(kalla);

CREATE INDEX IF NOT EXISTS idx_historik_transactions_ordertyp
    ON historik_transactions(ordertyp);

-- Vanligaste frågemönstret: alla transaktioner för ett värdepapper i en depå sorterat på datum
CREATE INDEX IF NOT EXISTS idx_historik_transactions_sec_depa_datum
    ON historik_transactions(security_id, depa, affarsdatum DESC);


-- =============================================================================
-- TABLE: historik_holdings_snapshot
-- Portföljbild vid ett givet datum. Genereras av import-script eller daglig cron.
-- Upsertad: en ny körning uppdaterar befintlig rad för samma (security, depå, datum).
-- gav_sek = totalt anskaffningsvärde i SEK (inte per aktie; räkna gav_sek/antal för snitt).
-- =============================================================================

CREATE TABLE IF NOT EXISTS historik_holdings_snapshot (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    security_id         uuid        NOT NULL REFERENCES historik_securities(id),
    depa                text        NOT NULL,
    datum               date        NOT NULL,
    antal               numeric     NOT NULL,
    kurs_lokal          numeric,               -- senaste kurs i handelsvaluta
    fx_rate             numeric,               -- SEK per valutaenhet på datum
    marknadsvarde_sek   numeric,               -- antal × kurs_lokal × fx_rate
    gav_sek             numeric,               -- totalt anskaffningsvärde SEK (kostnadsbas)
    orealiserat_sek     numeric,               -- marknadsvarde_sek − gav_sek
    skapad              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT historik_holdings_snapshot_unique UNIQUE (security_id, depa, datum)
);

COMMENT ON TABLE  historik_holdings_snapshot             IS 'Portföljögonblicksbild per (security, depå, datum). Upsertad vid reimport.';
COMMENT ON COLUMN historik_holdings_snapshot.gav_sek     IS 'Totalt anskaffningsvärde i SEK. GAV per aktie = gav_sek / antal.';
COMMENT ON COLUMN historik_holdings_snapshot.orealiserat_sek IS 'marknadsvarde_sek − gav_sek. Negativt = orealiserad förlust.';

CREATE INDEX IF NOT EXISTS idx_historik_holdings_snapshot_datum
    ON historik_holdings_snapshot(datum DESC);

CREATE INDEX IF NOT EXISTS idx_historik_holdings_snapshot_depa
    ON historik_holdings_snapshot(depa);

CREATE INDEX IF NOT EXISTS idx_historik_holdings_snapshot_sec_datum
    ON historik_holdings_snapshot(security_id, datum DESC);


-- =============================================================================
-- TABLE: historik_realized_pnl
-- Realiserade vinster/förluster på avyttrade positioner.
-- Carnegie-data är transaktionsbaserad (per affär, korrekt GAV-metod från rapport).
-- Danske-data är aggregerad (per värdepapper, simple köp–sälj-diff).
-- forsaljningsdatum och antal är nullable för aggregerade Danske-poster.
-- =============================================================================

CREATE TABLE IF NOT EXISTS historik_realized_pnl (
    id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    security_id             uuid        NOT NULL REFERENCES historik_securities(id),
    depa                    text        NOT NULL,
    forsaljningsdatum       date,                  -- NULL för aggregerade Danske-poster
    antal                   numeric,               -- NULL för aggregerade poster
    forsaljningsbelopp_sek  numeric     NOT NULL,
    anskaffningsbelopp_sek  numeric     NOT NULL,
    realiserad_vinst_sek    numeric     NOT NULL,  -- beräknad: försäljning − anskaffning
    kalla                   text        NOT NULL
                            CHECK (kalla IN (
                                'carnegie_rapport',  -- skattemässigt korrekt, GAV-metod
                                'danske_berakn'      -- förenklad: ej exakt skattemässig GAV
                            )),
    kommentar               text,
    source_ref              text        UNIQUE,
    skapad                  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  historik_realized_pnl                          IS 'Realiserade vinster/förluster. Carnegie = per transaktion (rapport). Danske = aggregerat per värdepapper.';
COMMENT ON COLUMN historik_realized_pnl.forsaljningsdatum        IS 'NULL för aggregerade Danske-poster. Placeholder 2026-04-30 används som rapportdatum.';
COMMENT ON COLUMN historik_realized_pnl.realiserad_vinst_sek     IS 'Positivt = vinst. Negativt = förlust.';
COMMENT ON COLUMN historik_realized_pnl.kalla                    IS 'carnegie_rapport = skattemässigt korrekt. danske_berakn = approximation (ej GAV-metod).';

CREATE INDEX IF NOT EXISTS idx_historik_realized_pnl_security_id
    ON historik_realized_pnl(security_id);

CREATE INDEX IF NOT EXISTS idx_historik_realized_pnl_depa
    ON historik_realized_pnl(depa);

CREATE INDEX IF NOT EXISTS idx_historik_realized_pnl_forsaljningsdatum
    ON historik_realized_pnl(forsaljningsdatum DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_historik_realized_pnl_kalla
    ON historik_realized_pnl(kalla);


-- =============================================================================
-- VERIFICATION QUERIES
-- Kör dessa manuellt efter import och jämför mot kända kontrollsummor.
--
-- Förväntade värden (2026-04-28):
--   Marknadsvärde total        : 12 415 323 SEK
--   Orealiserat total          : 1 792 461 SEK
--   Carnegie realiserat aktier :    602 393 SEK
--   Carnegie realiserat totalt :    276 115 SEK   (602393 + (−8094) + (−318184))
--   Carnegie utdelningar       :    563 003 SEK
--   Carnegie räntor            :    476 901 SEK
--   Danske utdelningar         :    499 216 SEK
-- =============================================================================

/*
-- 1. Marknadsvärde och orealiserat per depå (2026-04-28)
SELECT
    depa,
    SUM(marknadsvarde_sek)   AS marknadsvarde_sek,
    SUM(gav_sek)             AS anskaffningsvarde_sek,
    SUM(orealiserat_sek)     AS orealiserat_sek,
    COUNT(*)                 AS antal_innehav
FROM historik_holdings_snapshot
WHERE datum = '2026-04-28'
GROUP BY depa
UNION ALL
SELECT
    'TOTALT'                 AS depa,
    SUM(marknadsvarde_sek),
    SUM(gav_sek),
    SUM(orealiserat_sek),
    COUNT(*)
FROM historik_holdings_snapshot
WHERE datum = '2026-04-28';
-- Förväntat totalt: marknadsvärde = 12 415 323, orealiserat = 1 792 461


-- 2. Carnegie realiserat per kategori
SELECT
    kalla,
    kommentar,
    COUNT(*)                             AS antal_poster,
    SUM(forsaljningsbelopp_sek)          AS forsaljning_sek,
    SUM(anskaffningsbelopp_sek)          AS anskaffning_sek,
    SUM(realiserad_vinst_sek)            AS realiserat_sek
FROM historik_realized_pnl
WHERE kalla = 'carnegie_rapport'
GROUP BY kalla, kommentar
UNION ALL
SELECT
    'Carnegie TOTALT'        AS kalla,
    NULL                     AS kommentar,
    COUNT(*),
    SUM(forsaljningsbelopp_sek),
    SUM(anskaffningsbelopp_sek),
    SUM(realiserad_vinst_sek)
FROM historik_realized_pnl
WHERE kalla = 'carnegie_rapport';
-- Förväntat TOTALT realiserat: 276 115 SEK
-- Förväntat aktier-delsektion (ej övriga): 602 393 SEK


-- 3. Utdelningar och räntor per källa
SELECT
    kalla,
    ordertyp,
    COUNT(*)                 AS antal_transaktioner,
    SUM(likvidbelopp_sek)    AS total_sek
FROM historik_transactions
WHERE ordertyp IN ('utdelning', 'ranta')
  AND kalla IN ('historik_danske', 'historik_carnegie')
GROUP BY kalla, ordertyp
ORDER BY kalla, ordertyp;
-- Förväntat:
--   historik_carnegie | ranta     : 476 901 SEK
--   historik_carnegie | utdelning : 563 003 SEK
--   historik_danske   | utdelning : 499 216 SEK


-- 4. Antal rader per tabell och källa
SELECT 'historik_securities'        AS tabell, NULL AS kalla, COUNT(*) FROM historik_securities
UNION ALL
SELECT 'historik_transactions',     kalla,     COUNT(*) FROM historik_transactions     GROUP BY kalla
UNION ALL
SELECT 'historik_holdings_snapshot', NULL,     COUNT(*) FROM historik_holdings_snapshot WHERE datum = '2026-04-28'
UNION ALL
SELECT 'historik_realized_pnl',     kalla,     COUNT(*) FROM historik_realized_pnl     GROUP BY kalla
ORDER BY tabell, kalla;


-- 5. Öppningsbalanser – kontroll att inga negativa nettopositioner uppstått
SELECT
    s.namn,
    t.depa,
    SUM(CASE WHEN t.ordertyp IN ('kop','split','fission','isinbyte','teckningsratt','overforing')
             THEN COALESCE(t.antal, 0) ELSE 0 END)
    + SUM(CASE WHEN t.ordertyp = 'salj' THEN COALESCE(t.antal, 0) ELSE 0 END) AS netto_antal
FROM historik_transactions t
JOIN historik_securities s ON s.id = t.security_id
WHERE t.ordertyp != 'utdelning'
GROUP BY s.namn, t.depa
HAVING ABS(
    SUM(CASE WHEN t.ordertyp IN ('kop','split','fission','isinbyte','teckningsratt','overforing')
             THEN COALESCE(t.antal, 0) ELSE 0 END)
    + SUM(CASE WHEN t.ordertyp = 'salj' THEN COALESCE(t.antal, 0) ELSE 0 END)
) > 0.01
ORDER BY s.namn;
-- Aktiva positioner ska ha positiv netto. Avyttrade positioner netto ≈ 0.
-- Negativa nettotal = öppningsbalans-rad saknas.
*/
