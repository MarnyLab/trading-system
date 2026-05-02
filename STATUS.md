
## 2026-05-02 - seed_portfolios.py

### Kanda begransningar (Known Limitations)

**KL-1: Icke-SEK-innehav uppdaterar inte vaxelkursen live**
iShares Physical Gold, iShares S&P 500, Fiserv (USD), Neste, iShares Euro Stoxx, Xtr Art (EUR), Novo Nordisk (DKK) saknar live-FX.
Planerad forbattring: live-FX via yfinance USDSEK=X / EURSEK=X / DKKSEK=X.

**KL-2: Fonder saknar Yahoo-ticker - visar manuell kurs fran 2026-04-28**
Carnegie Smabolagsfond A, DIS Globala Index SA, DI SE Smabolag SA,
Lannebo Smabolag, PN Sverige Aktiv Ac uppdateras inte automatiskt.

**KL-3: KOP-datum ar snapshot-datumet 2026-04-28, inte verkligt kopdatum**
Historiska kopdatum finns i historik_transactions men ar inte kopplade hit.

**KL-4: kurs i transaktioner ar alltid i SEK (oavsett handelsvaluta)**
For icke-SEK-innehav konverterades GAV till SEK i historik_holdings_snapshot.
Live-kursjamforelse i lokal valuta vs kurs-faltet ar inte rattvisande
for USD/EUR/DKK-innehav. Atgardas nar historik_transactions kopplas in.
