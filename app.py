from flask import Flask, render_template_string, request, jsonify, redirect, url_for, session
import yfinance as yf
import ta
import pandas as pd
from datetime import datetime
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import anthropic
import os
import json
import base64
import email as email_lib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None
from dotenv import load_dotenv
from functools import wraps
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request
    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False

load_dotenv()
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

app = Flask(__name__)
app.secret_key = "trading-system-secret-2026"
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── Databas ────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH = "trading.db"

def get_conn():
    """Returnerar databasanslutning - Supabase (PostgreSQL) eller SQLite."""
    if DATABASE_URL and psycopg2:
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
            return conn, "postgres"
        except Exception as e:
            print(f"Supabase fel, använder SQLite: {e}")
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(DB_PATH)
    return conn, "sqlite"

def q(sql, db_type):
    """Konverterar SQLite ? till PostgreSQL %s."""
    if db_type == "postgres":
        return sql.replace("?", "%s")
    return sql

def init_db():
    conn, db_type = get_conn()
    c = conn.cursor()
    if db_type == "postgres":
        serial = "SERIAL PRIMARY KEY"
        integer_pk = "INTEGER PRIMARY KEY"
    else:
        serial = "INTEGER PRIMARY KEY AUTOINCREMENT"
        integer_pk = "INTEGER PRIMARY KEY"

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS konversationer (
            id {serial},
            datum TEXT NOT NULL,
            titel TEXT,
            meddelanden TEXT NOT NULL,
            marknadsdata TEXT,
            skapad TEXT NOT NULL
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS dokument (
            id {serial},
            filnamn TEXT NOT NULL,
            innehall TEXT NOT NULL,
            uppladdad TEXT NOT NULL
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS gmail_token (
            id {integer_pk},
            token_json TEXT NOT NULL,
            uppdaterad TEXT NOT NULL
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS daily_changea_analyser (
            id {serial},
            datum TEXT NOT NULL,
            analys TEXT NOT NULL,
            skapad TEXT NOT NULL
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS tankar (
            id {serial},
            datum TEXT NOT NULL,
            index_namn TEXT NOT NULL,
            kurs_start REAL NOT NULL,
            riktning TEXT NOT NULL,
            mal_niva REAL,
            mal_procent REAL,
            period TEXT NOT NULL,
            kommentar TEXT,
            avslutad INTEGER DEFAULT 0,
            skapad TEXT NOT NULL
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS innehav (
            id {serial},
            portfolio_id INTEGER NOT NULL,
            namn TEXT NOT NULL,
            ticker TEXT NOT NULL,
            tillgangsslag TEXT NOT NULL,
            valuta TEXT DEFAULT 'SEK',
            skapad TEXT NOT NULL
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS transaktioner (
            id {serial},
            holding_id INTEGER NOT NULL,
            typ TEXT NOT NULL,
            antal REAL NOT NULL,
            kurs REAL NOT NULL,
            fx_rate REAL DEFAULT 1,
            datum TEXT NOT NULL,
            notering TEXT,
            skapad TEXT NOT NULL
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS portfolj_sammanslagning (
            id {serial},
            total_portfolio_id INTEGER NOT NULL,
            del_portfolio_id INTEGER NOT NULL
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS prisalarm (
            id {serial},
            ticker TEXT NOT NULL,
            namn TEXT NOT NULL,
            niva REAL NOT NULL,
            riktning TEXT NOT NULL,
            email TEXT NOT NULL,
            aktiv INTEGER DEFAULT 1,
            utlost INTEGER DEFAULT 0,
            skapad TEXT NOT NULL
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS portfoljer (
            id {serial},
            namn TEXT NOT NULL,
            niva TEXT NOT NULL,
            skapad TEXT NOT NULL
        )
    """)
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS portfolj_innehav (
            id {serial},
            portfolio_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            namn TEXT NOT NULL,
            andel REAL NOT NULL,
            skapad TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

# Gmail konfiguration
GMAIL_CLIENT_CONFIG = {
    "web": {
        "client_id": os.getenv("GMAIL_CLIENT_ID", ""),
        "project_id": "trading-system-489914",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": os.getenv("GMAIL_CLIENT_SECRET", ""),
        "redirect_uris": ["https://trading-system-r7ii.onrender.com/gmail/callback"]
    }
}
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
GMAIL_REDIRECT_URI = "https://trading-system-r7ii.onrender.com/gmail/callback"

def hamta_gmail_credentials():
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT token_json FROM gmail_token WHERE id=1")
    rad = c.fetchone()
    conn.close()
    if not rad or not GMAIL_AVAILABLE:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(rad[0]), GMAIL_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            spara_gmail_token(creds)
        return creds
    except Exception as e:
        print(f"Gmail token fel: {e}")
        # Ta bort ogiltig token ur databasen
        try:
            conn2, db_type2 = get_conn()
            c2 = conn2.cursor()
            c2.execute("DELETE FROM gmail_token WHERE id=1")
            conn2.commit()
            conn2.close()
        except:
            pass
        return None

def spara_gmail_token(creds):
    conn, db_type = get_conn()
    c = conn.cursor()
    sql = "INSERT INTO gmail_token (id, token_json, uppdaterad) VALUES (1, %s, %s) ON CONFLICT (id) DO UPDATE SET token_json=EXCLUDED.token_json, uppdaterad=EXCLUDED.uppdaterad" if db_type == "postgres" else "INSERT OR REPLACE INTO gmail_token (id, token_json, uppdaterad) VALUES (1, ?, ?)"
    c.execute(sql,
              (creds.to_json(), datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()

def skicka_alarm_mejl(till, subject, body):
    """Skickar e-post via Gmail SMTP (kräver SMTP_USER + SMTP_PASS i .env)."""
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = till
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"E-post fel: {e}")
        return False

def spara_konversation(titel, meddelanden, marknadsdata):
    conn, db_type = get_conn()
    c = conn.cursor()
    datum  = datetime.now().strftime("%Y-%m-%d")
    skapad = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute(q(
        "INSERT INTO konversationer (datum, titel, meddelanden, marknadsdata, skapad) VALUES (?, ?, ?, ?, ?)", db_type),
        (datum, titel, json.dumps(meddelanden, ensure_ascii=False), marknadsdata, skapad)
    )
    conn.commit()
    conn.close()

def hamta_tidigare_konversationer(antal=3):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("SELECT datum, titel, meddelanden, marknadsdata FROM konversationer ORDER BY id DESC LIMIT ?", db_type), (antal,))
    rader = c.fetchall()
    conn.close()
    resultat = []
    for rad in rader:
        msgs = json.loads(rad[2])
        resultat.append({"datum": rad[0], "titel": rad[1], "meddelanden": msgs, "marknadsdata": rad[3]})
    return resultat

def hamta_alla_konversationer():
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, datum, titel, skapad FROM konversationer ORDER BY id DESC")
    rader = c.fetchall()
    conn.close()
    return [{"id": r[0], "datum": r[1], "titel": r[2], "skapad": r[3]} for r in rader]

def spara_dokument(filnamn, innehall):
    conn, db_type = get_conn()
    c = conn.cursor()
    uppladdad = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute(q("INSERT INTO dokument (filnamn, innehall, uppladdad) VALUES (?, ?, ?)", db_type), (filnamn, innehall, uppladdad))
    conn.commit()
    conn.close()

def hamta_alla_dokument():
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, filnamn, uppladdad FROM dokument ORDER BY id DESC")
    rader = c.fetchall()
    conn.close()
    return [{"id": r[0], "filnamn": r[1], "uppladdad": r[2]} for r in rader]

# ── Login ──────────────────────────────────────────────────
ANVANDARE = {"admin": "trading2026"}

def inloggning_kravs(f):
    @wraps(f)
    def dekorerad(*args, **kwargs):
        if not session.get("inloggad"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return dekorerad

# ── Marknadsdata ───────────────────────────────────────────
TICKERS = {
    "OMX30":  "^OMX",
    "S&P500": "^GSPC",
    "Europa": "^STOXX50E",
    "Guld":   "GC=F",
}

PERIOD_MAP = {"Daily": "1d", "Weekly": "1wk", "Monthly": "1mo"}
RANGE_MAP  = {
    "1 Month": "1mo", "3 Months": "3mo", "6 Months": "6mo",
    "1 Year": "1y", "2 Years": "2y", "3 Years": "3y", "5 Years": "5y",
}

def hamta_data(ticker, yf_period="6mo", yf_interval="1d"):
    df = yf.download(ticker, period=yf_period, interval=yf_interval, progress=False, auto_adjust=True)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    close = df["Close"].squeeze()
    if len(df) > 20:
        df["EMA20"] = ta.trend.EMAIndicator(close, window=20).ema_indicator()
    if len(df) > 50:
        df["SMA50"] = ta.trend.SMAIndicator(close, window=50).sma_indicator()
    if len(df) > 200:
        df["SMA200"] = ta.trend.SMAIndicator(close, window=200).sma_indicator()
    macd = ta.trend.MACD(close)
    df["MACD"]      = macd.macd()
    df["MACD_sig"]  = macd.macd_signal()
    df["MACD_hist"] = macd.macd_diff()
    df["RSI"]       = ta.momentum.RSIIndicator(close, window=14).rsi()
    if "Volume" in df.columns:
        df["OBV"] = ta.volume.OnBalanceVolumeIndicator(close, df["Volume"].squeeze()).on_balance_volume()
    return df

def sammanfatta(namn, df, use_full_range=False):
    senaste = float(df["Close"].iloc[-1])
    if use_full_range:
        referens = float(df["Close"].iloc[0])
    else:
        ix = min(22, len(df) - 1)
        referens = float(df["Close"].iloc[-ix])
    forandring = (senaste - referens) / referens * 100
    rsi          = round(float(df["RSI"].iloc[-1]), 1)
    sma50        = float(df["SMA50"].iloc[-1]) if "SMA50" in df.columns else None
    sma200       = float(df["SMA200"].iloc[-1]) if "SMA200" in df.columns else None
    macd_hist    = float(df["MACD_hist"].iloc[-1])
    rsi_text, rsi_farg = ("Överköpt", "#cc0000") if rsi > 70 else ("Översålt", "#007700") if rsi < 30 else ("Neutralt", "#888888")
    if sma50 and sma200:
        if senaste > sma50 > sma200:   trend, trend_farg = "Stark upptrend", "#007700"
        elif senaste > sma50:          trend, trend_farg = "Upptrend", "#009900"
        elif senaste < sma50 < sma200: trend, trend_farg = "Stark nedtrend", "#cc0000"
        else:                          trend, trend_farg = "Sidledes", "#888888"
    else:
        trend, trend_farg = "För kort data", "#888888"
    return {
        "namn": namn, "kurs": senaste, "forandring": forandring,
        "rsi": rsi, "rsi_text": rsi_text, "rsi_farg": rsi_farg,
        "trend": trend, "trend_farg": trend_farg,
        "macd_signal": "Positiv momentum" if macd_hist > 0 else "Negativ momentum",
        "sma50": round(sma50, 2) if sma50 else "N/A",
        "sma200": round(sma200, 2) if sma200 else "N/A",
        "macd_hist": round(macd_hist, 2),
    }

def hamta_marknadsdata():
    rader = []
    for namn, ticker in TICKERS.items():
        try:
            df = hamta_data(ticker, yf_period="1y", yf_interval="1d")
            s  = sammanfatta(namn, df)
            rader.append(
                f"{namn}: Kurs {s['kurs']:.2f}, RSI {s['rsi']} ({s['rsi_text']}), "
                f"Trend: {s['trend']}, MACD: {s['macd_signal']}, "
                f"SMA50: {s['sma50']}, SMA200: {s['sma200']}"
            )
        except:
            pass
    return "\n".join(rader)

def skapa_diagram(namn, df, interval_label="Daily"):
    fmt = {"Daily": "%d %b", "Weekly": "%d %b '%y", "Monthly": "%b '%y"}
    x_labels = [d.strftime(fmt.get(interval_label, "%d %b")) for d in df.index]
    close, open_ = df["Close"].squeeze(), df["Open"].squeeze()

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        row_heights=[0.58, 0.21, 0.21], vertical_spacing=0.06,
                        subplot_titles=("", "Volym", "MACD (12,26,9)"))

    fig.add_trace(go.Candlestick(
        x=x_labels, open=open_, high=df["High"].squeeze(),
        low=df["Low"].squeeze(), close=close, name="Pris",
        increasing=dict(line=dict(color="#007700", width=1.5), fillcolor="#00aa00"),
        decreasing=dict(line=dict(color="#cc0000", width=1.5), fillcolor="#cc0000"),
    ), row=1, col=1)

    for col_name, color, label in [("EMA20","#008800","EMA(20)"),("SMA50","#0044cc","MA(50)"),("SMA200","#cc0000","MA(200)")]:
        if col_name in df.columns:
            fig.add_trace(go.Scatter(x=x_labels, y=df[col_name], name=label, line=dict(color=color, width=1.5)), row=1, col=1)

    if "Volume" in df.columns:
        vol_colors = ["#cc6688" if c < o else "#88aa88" for c, o in zip(close, open_)]
        fig.add_trace(go.Bar(x=x_labels, y=df["Volume"].squeeze(), name="Volym", marker_color=vol_colors, opacity=0.8), row=2, col=1)

    macd_colors = ["#008800" if v >= 0 else "#cc0000" for v in df["MACD_hist"]]
    fig.add_trace(go.Bar(x=x_labels, y=df["MACD_hist"], name="Histogram", marker_color=macd_colors, opacity=0.7), row=3, col=1)
    fig.add_trace(go.Scatter(x=x_labels, y=df["MACD"], name="MACD", line=dict(color="#111111", width=1.5)), row=3, col=1)
    fig.add_trace(go.Scatter(x=x_labels, y=df["MACD_sig"], name="Signal", line=dict(color="#cc0000", width=1.5)), row=3, col=1)

    n = len(x_labels)
    tickvals = x_labels[::4] if n > 60 else x_labels[::2] if n > 30 else x_labels

    fig.update_layout(
        paper_bgcolor="#ffffff", plot_bgcolor="#f8f8f8",
        font=dict(color="#222222", size=11), height=820,
        showlegend=True, legend=dict(orientation="h", y=1.02, x=0),
        margin=dict(l=10, r=70, t=40, b=60),
        xaxis_rangeslider_visible=False, bargap=0.2,
        xaxis=dict(type="category", gridcolor="#dddddd", tickangle=-45, tickfont=dict(size=10), tickvals=tickvals),
        xaxis2=dict(type="category", tickvals=tickvals, tickangle=-45),
        xaxis3=dict(type="category", tickvals=tickvals, tickangle=-45),
        yaxis=dict(gridcolor="#dddddd", side="right"),
        yaxis2=dict(gridcolor="#dddddd", side="right"),
        yaxis3=dict(gridcolor="#dddddd", side="right"),
    )
    for ann in fig.layout.annotations:
        ann.update(x=0.5, xanchor="center", xref="paper")
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


# ── HTML Komponenter ───────────────────────────────────────
NAV_HTML = """
<nav style="margin-bottom:25px; display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
    <a href="/" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Dashboard</a>
    <a href="/analytiker" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">AI-Analytiker</a>
    <a href="/analyslogg" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Analyslogg</a>
    <a href="/dokument" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Dokument</a>
    <a href="/riskmotor" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Riskmotor</a>
    <a href="/gmail" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Gmail</a>
    <a href="/daily_change-analys" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Daglig Analys</a>
    <a href="/tracker" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Tracker</a>
    <a href="/prisalarm" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Prisalarm</a>
    <a href="/portfolio" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Portfölj</a>
    <a href="/logout" style="color:#999; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em; margin-left:auto;">Logga ut</a>
</nav>
"""

BASE_STYLE = """
<style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f4; color: #222; padding: 30px; }
    h1 { font-size: 1.5em; margin-bottom: 5px; color: #111; }
    h2 { font-size: 1.1em; color: #444; margin-bottom: 15px; }
    .uppdaterad { color: #999; font-size: 0.85em; margin-bottom: 25px; }
    a { color: #0044cc; }
    .filter-bar { display: flex; gap: 20px; align-items: flex-end; margin-bottom: 18px; flex-wrap: wrap; }
    .filter-bar label { color: #666; font-size: 0.82em; font-weight: bold; display: block; margin-bottom: 3px; }
    .filter-bar select { padding: 6px 10px; border: 1px solid #ccc; border-radius: 5px; background: #fff; font-size: 0.9em; color: #222; cursor: pointer; }
    .filter-bar button { padding: 7px 18px; background: #0044cc; color: #fff; border: none; border-radius: 5px; font-size: 0.9em; cursor: pointer; }
    .kort { background: #fff; border-radius: 10px; padding: 20px; border: 1px solid #ddd; }
    .tabell { width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; }
    .tabell th { background: #f0f0f0; padding: 10px 14px; text-align: left; font-size: 0.85em; color: #666; border-bottom: 1px solid #ddd; }
    .tabell td { padding: 10px 14px; border-bottom: 1px solid #eee; font-size: 0.9em; }
    .tabell tr:last-child td { border-bottom: none; }
    .tabell tr:hover td { background: #f8f8f8; }
</style>
"""

PORTFOLIO_STYLE = """
    <style>
        .tb-header { background:#1F3864; color:#fff; padding:16px 20px; border-radius:8px 8px 0 0; font-weight:bold; font-size:1.05em; }
        .tb-table { width:100%; border-collapse:collapse; background:#fff; border-radius:0 0 8px 8px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,0.08); }
        .tb-table th { background:#D9E2F3; color:#1F3864; padding:9px 12px; text-align:left; font-size:0.82em; font-weight:bold; border-bottom:2px solid #1F3864; }
        .tb-table td { padding:8px 12px; font-size:0.87em; border-bottom:1px solid #eef0f5; }
        .tb-table tr:hover td { background:#f0f4ff; }
        .tb-table tr:last-child td { border-bottom:none; }
        .tb-section { margin-bottom:28px; }
        .pos { color:#007700; font-weight:bold; }
        .neg { color:#cc0000; font-weight:bold; }
        .badge-typ { display:inline-block; padding:2px 8px; border-radius:10px; font-size:0.75em; font-weight:bold; background:#D9E2F3; color:#1F3864; }
        .portfolj-tabs { display:flex; gap:8px; margin-bottom:20px; flex-wrap:wrap; }
        .portfolj-tab { padding:7px 18px; border-radius:20px; border:2px solid #1F3864; color:#1F3864; font-size:0.88em; font-weight:bold; text-decoration:none; background:#fff; }
        .portfolj-tab.aktiv { background:#1F3864; color:#fff; }
        .kpi-rad { display:grid; grid-template-columns:repeat(auto-fill, minmax(180px,1fr)); gap:12px; margin-bottom:24px; }
        .kpi-box { background:#fff; border-radius:8px; padding:16px 18px; border:1px solid #ddd; border-left:4px solid #1F3864; }
        .kpi-box .etikett { color:#888; font-size:0.78em; margin-bottom:4px; }
        .kpi-box .varde { font-size:1.3em; font-weight:bold; color:#111; }
        .ny-innehav-form { background:#fff; border-radius:10px; padding:22px; border:1px solid #ddd; margin-bottom:24px; }
        .form-grid-3 { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:12px; }
        .fg label { display:block; color:#666; font-size:0.8em; font-weight:bold; margin-bottom:3px; }
        .fg input, .fg select { width:100%; padding:8px 10px; border:1px solid #ccc; border-radius:5px; font-size:0.9em; }
        .search-results { background:#fff; border:1px solid #ccc; border-radius:6px; max-height:200px; overflow-y:auto; position:absolute; z-index:100; width:100%; }
        .search-row { padding:8px 12px; cursor:pointer; font-size:0.88em; border-bottom:1px solid #eee; }
        .search-row:hover { background:#f0f4ff; }
        .search-wrapper { position:relative; }
    </style>
"""

NIVA_FARGER = {"Konservativ": "#007700", "Balanserad": "#0044cc", "Aggressiv": "#cc5500", "Depå": "#1F3864", "ISK": "#2E5FA3", "Pension": "#4472C4", "KF": "#6FA8DC", "Total": "#888888"}

# ── Routes ─────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    fel = None
    if request.method == "POST":
        anv = request.form.get("anvandare")
        pwd = request.form.get("losenord")
        if anv in ANVANDARE and ANVANDARE[anv] == pwd:
            session["inloggad"] = True
            session["anvandare"] = anv
            return redirect(url_for("dashboard"))
        else:
            fel = "Fel användarnamn eller lösenord."
    html = """<!DOCTYPE html><html>
    <head><title>Logga in</title><meta charset="utf-8">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f4; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
        .login-box { background: #fff; border-radius: 12px; padding: 40px; width: 340px; border: 1px solid #ddd; box-shadow: 0 2px 12px rgba(0,0,0,0.08); }
        h1 { font-size: 1.4em; margin-bottom: 8px; color: #111; }
        p { color: #888; font-size: 0.88em; margin-bottom: 24px; }
        label { display: block; color: #666; font-size: 0.83em; font-weight: bold; margin-bottom: 5px; }
        input { width: 100%; padding: 10px 12px; border: 1px solid #ccc; border-radius: 6px; font-size: 1em; margin-bottom: 16px; }
        input:focus { outline: none; border-color: #0044cc; }
        button { width: 100%; padding: 11px; background: #0044cc; color: #fff; border: none; border-radius: 6px; font-size: 1em; font-weight: bold; cursor: pointer; }
        .fel { color: #cc0000; font-size: 0.88em; margin-bottom: 14px; }
    </style></head>
    <body>
        <div class="login-box">
            <h1>Trading Dashboard</h1>
            <p>Logga in för att fortsätta</p>
            {% if fel %}<p class="fel">{{ fel }}</p>{% endif %}
            <form method="POST">
                <label>Användarnamn</label>
                <input type="text" name="anvandare" placeholder="admin" autofocus>
                <label>Lösenord</label>
                <input type="password" name="losenord" placeholder="••••••••">
                <button type="submit">Logga in</button>
            </form>
        </div>
    </body></html>"""
    return render_template_string(html, fel=fel)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@inloggning_kravs
def dashboard():
    kort = []
    for namn, ticker in TICKERS.items():
        df = hamta_data(ticker, yf_period="1y", yf_interval="1d")
        kort.append(sammanfatta(namn, df))
    uppdaterad = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Hämta portföljer för dashboard
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, namn, niva FROM portfoljer ORDER BY id")
    portfoljer_rader = c.fetchall()
    portfoljer = []
    for pid, pnamn, pniva in portfoljer_rader:
        # Kolla om sammanslagen
        c.execute(q("SELECT del_portfolj_id FROM portfolj_sammanslagning WHERE total_portfolj_id=?", db_type), (pid,))
        sub_ids = [r[0] for r in c.fetchall()]
        search_ids = sub_ids if sub_ids else [pid]
        placeholders = ",".join(["%s" if db_type == "postgres" else "?" for _ in search_ids])
        c.execute(f"""SELECT i.ticker, i.valuta,
                   SUM(CASE WHEN t.typ='KOP' THEN t.antal WHEN t.typ='SALJ' THEN -t.antal ELSE 0 END) as antal
                   FROM innehav i LEFT JOIN transaktioner t ON t.innehav_id=i.id
                   WHERE i.portfolj_id IN ({placeholders})
                   GROUP BY i.id, i.ticker, i.valuta
                   HAVING SUM(CASE WHEN t.typ='KOP' THEN t.antal WHEN t.typ='SALJ' THEN -t.antal ELSE 0 END) > 0""", search_ids)
        innehav_rader = c.fetchall()
        total_mv_calc = 0
        tot_daily_change_vikt = 0
        total_weight = 0
        for ticker, valuta, antal in innehav_rader:
            if not antal: continue
            kurs, daily_change = hamta_portfolj_kurs(ticker)
            if kurs:
                mv = float(antal) * kurs
                total_mv_calc += mv
                if daily_change is not None:
                    tot_daily_change_vikt += daily_change * mv
                    total_weight += mv
        daily_change_pct = round(tot_daily_change_vikt / total_weight, 2) if total_weight else 0
        portfoljer.append({
            "id": pid, "namn": pnamn, "niva": pniva,
            "antal": len(innehav_rader), "mv": round(total_mv_calc, 0),
            "daily_change": daily_change_pct
        })
    conn.close()

    html = """<!DOCTYPE html><html>
    <head><title>Trading Dashboard</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
        .kurs { font-size: 1.9em; font-weight: bold; color: #111; }
        .forandring { font-size: 0.95em; margin: 3px 0 14px; }
        .rad { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #eee; font-size: 0.88em; }
        .rad:last-child { border-bottom: none; }
        .etikett { color: #999; }
        .detalj-btn { display: block; margin-top: 14px; text-align: center; padding: 7px; background: #0044cc11; color: #0044cc; border-radius: 6px; text-decoration: none; font-size: 0.88em; }
        .sektion-rubrik { font-size: 0.8em; font-weight: bold; color: #999; text-transform: uppercase; letter-spacing: 0.05em; margin: 24px 0 10px; }
        .portfolj-kort { background: #fff; border-radius: 10px; padding: 18px; border: 1px solid #ddd; text-decoration: none; color: inherit; display: block; transition: box-shadow 0.15s; }
        .portfolj-kort:hover { box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .niva-badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:0.75em; font-weight:bold; color:#fff; margin-bottom:8px; }
    </style></head>
    <body>""" + NAV_HTML + """
        <h1>Trading Dashboard</h1>
        <p class="uppdaterad">Uppdaterad: {{ uppdaterad }}</p>

        <div class="sektion-rubrik">Marknadsindex</div>
        <div class="grid">
        {% for k in kort %}
            <div class="kort">
                <h2 style="color:#666; font-size:1em; margin-bottom:6px;">{{ k.namn }}</h2>
                <div class="kurs">{{ "%.2f"|format(k.kurs) }}</div>
                <div class="forandring" style="color: {{ '#007700' if k.forandring > 0 else '#cc0000' }}">
                    {{ "%+.2f"|format(k.forandring) }}% senaste månaden
                </div>
                <div class="rad"><span class="etikett">Trend</span><span style="color:{{ k.trend_farg }}">{{ k.trend }}</span></div>
                <div class="rad"><span class="etikett">RSI (14)</span><span style="color:{{ k.rsi_farg }}">{{ k.rsi }} - {{ k.rsi_text }}</span></div>
                <div class="rad"><span class="etikett">MACD</span><span>{{ k.macd_signal }}</span></div>
                <div class="rad"><span class="etikett">SMA50</span><span>{{ k.sma50 }}</span></div>
                <div class="rad"><span class="etikett">SMA200</span><span>{{ k.sma200 }}</span></div>
                <a class="detalj-btn" href="/detalj/{{ k.namn }}" target="_blank">Öppna diagram</a>
            </div>
        {% endfor %}
        </div>

        {% if portfoljer %}
        <div class="sektion-rubrik">Mina portföljer</div>
        <div class="grid">
        {% for p in portfoljer %}
            <a class="portfolj-kort" href="/portfolio/{{ p.id }}" style="text-decoration:none;">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px;">
                    <div style="font-size:1.1em; font-weight:bold; color:#1F3864;">{{ p.namn }}</div>
                    <span style="background:#D9E2F3; color:#1F3864; padding:2px 8px; border-radius:10px; font-size:0.75em; font-weight:bold;">{{ p.niva }}</span>
                </div>
                <div style="font-size:1.6em; font-weight:bold; color:#111; margin-bottom:10px;">
                    {{ "{:,.0f}".format(p.mv).replace(",", " ") }} <span style="font-size:0.5em; color:#888;">SEK</span>
                </div>
                <div class="rad"><span class="etikett">Innehav</span><span>{{ p.antal }} st</span></div>
                <div class="rad"><span class="etikett">Idag</span>
                    <span style="color:{{ '#007700' if p.daily_change > 0 else '#cc0000' }}; font-weight:bold;">
                        {{ "%+.2f"|format(p.daily_change) }}%
                    </span>
                </div>
                <div class="detalj-btn" style="margin-top:12px; background:#1F3864; color:#fff; border-radius:6px; padding:7px; text-align:center; font-size:0.88em;">Öppna portfölj →</div>
            </a>
        {% endfor %}
        </div>
        {% endif %}

    </body></html>"""
    return render_template_string(html, kort=kort, uppdaterad=uppdaterad, portfoljer=portfoljer, niva_farger=NIVA_FARGER)


@app.route("/detalj/<namn>")
@inloggning_kravs
def detalj(namn):
    ticker = TICKERS.get(namn)
    if not ticker:
        return "Index hittades inte", 404
    aktiv_period = request.args.get("period", "Daily")
    aktiv_range  = request.args.get("range", "6 Months")
    if aktiv_period not in PERIOD_MAP: aktiv_period = "Daily"
    if aktiv_range not in RANGE_MAP:   aktiv_range  = "6 Months"
    df = hamta_data(ticker, yf_period=RANGE_MAP[aktiv_range], yf_interval=PERIOD_MAP[aktiv_period])
    s  = sammanfatta(namn, df, use_full_range=True)
    diagram_html = skapa_diagram(namn, df, interval_label=aktiv_period)
    period_opts = "".join(f'<option value="{p}" {"selected" if p == aktiv_period else ""}>{p}</option>' for p in PERIOD_MAP)
    range_opts  = "".join(f'<option value="{r}" {"selected" if r == aktiv_range else ""}>{r}</option>' for r in RANGE_MAP)
    html = """<!DOCTYPE html><html>
    <head><title>{{ namn }}</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .info-rad { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
        .info-box { background: #fff; border-radius: 8px; padding: 12px 18px; border: 1px solid #ddd; min-width: 120px; }
        .info-box .etikett { color: #999; font-size: 0.78em; margin-bottom: 3px; }
        .info-box .varde { font-size: 1.05em; font-weight: bold; }
        .kurs { font-size: 2.2em; font-weight: bold; color: #111; }
        .forandring { font-size: 1em; margin-bottom: 14px; }
    </style></head>
    <body>""" + NAV_HTML + """
        <h1>{{ namn }}</h1>
        <div class="kurs">{{ "%.2f"|format(s.kurs) }}</div>
        <div class="forandring" style="color: {{ '#007700' if s.forandring > 0 else '#cc0000' }}">
            {{ "%+.2f"|format(s.forandring) }}% ({{ aktiv_range }})
        </div>
        <div class="info-rad">
            <div class="info-box"><div class="etikett">Trend</div><div class="varde" style="color:{{ s.trend_farg }}">{{ s.trend }}</div></div>
            <div class="info-box"><div class="etikett">RSI (14)</div><div class="varde" style="color:{{ s.rsi_farg }}">{{ s.rsi }} - {{ s.rsi_text }}</div></div>
            <div class="info-box"><div class="etikett">MACD</div><div class="varde">{{ s.macd_signal }}</div></div>
            <div class="info-box"><div class="etikett">SMA50</div><div class="varde">{{ s.sma50 }}</div></div>
            <div class="info-box"><div class="etikett">SMA200</div><div class="varde">{{ s.sma200 }}</div></div>
        </div>
        <form method="GET" class="filter-bar">
            <div><label>PERIOD</label><select name="period">{{ period_opts|safe }}</select></div>
            <div><label>RANGE</label><select name="range">{{ range_opts|safe }}</select></div>
            <button type="submit">Uppdatera</button>
        </form>
        {{ diagram_html|safe }}
    </body></html>"""
    return render_template_string(html, namn=namn, s=s, diagram_html=diagram_html,
                                  period_opts=period_opts, range_opts=range_opts,
                                  aktiv_range=aktiv_range)


@app.route("/analytiker")
@inloggning_kravs
def analytiker():
    dokument = hamta_alla_dokument()
    dok_opts = "".join(f'<option value="{d["id"]}">{d["filnamn"]} ({d["uppladdad"]})</option>' for d in dokument)

    html = """<!DOCTYPE html><html>
    <head><title>AI-Analytiker</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .chatt-wrapper { display: grid; grid-template-columns: 1fr 300px; gap: 20px; max-width: 1100px; }
        .meddelanden { background: #fff; border: 1px solid #ddd; border-radius: 10px; padding: 20px; min-height: 350px; max-height: 520px; overflow-y: auto; margin-bottom: 16px; }
        .msg { margin-bottom: 16px; }
        .msg.user { text-align: right; }
        .msg.user .bubbla { background: #0044cc; color: #fff; display: inline-block; padding: 10px 16px; border-radius: 12px 12px 2px 12px; max-width: 85%; text-align: left; }
        .msg.ai .bubbla { background: #f0f0f0; color: #222; display: inline-block; padding: 10px 16px; border-radius: 12px 12px 12px 2px; max-width: 85%; }
        .msg .avsandare { font-size: 0.78em; color: #999; margin-bottom: 4px; }
        .inmatning { display: flex; gap: 10px; }
        .inmatning textarea { flex: 1; padding: 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 0.95em; font-family: inherit; resize: vertical; min-height: 70px; }
        .inmatning button { padding: 10px 20px; background: #0044cc; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 0.95em; align-self: flex-end; }
        .sidopanel { display: flex; flex-direction: column; gap: 14px; }
        .panel-kort { background: #fff; border-radius: 10px; padding: 16px; border: 1px solid #ddd; }
        .panel-kort h3 { font-size: 0.88em; color: #555; margin-bottom: 10px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.03em; }
        .panel-kort label { display: block; color: #888; font-size: 0.82em; margin-bottom: 4px; }
        .panel-kort select { width: 100%; padding: 6px 8px; border: 1px solid #ccc; border-radius: 5px; font-size: 0.88em; margin-bottom: 8px; }
        .panel-kort textarea { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 5px; font-size: 0.85em; resize: vertical; min-height: 80px; font-family: inherit; }
        .panel-kort input[type=text] { width: 100%; padding: 7px 8px; border: 1px solid #ccc; border-radius: 5px; font-size: 0.88em; margin-bottom: 8px; }
        .panel-kort input[type=file] { width: 100%; font-size: 0.85em; margin-bottom: 8px; }
        .grön-btn { width: 100%; padding: 7px; background: #007700; color: #fff; border: none; border-radius: 5px; cursor: pointer; font-size: 0.88em; }
        .blå-btn { width: 100%; padding: 7px; background: #0044cc; color: #fff; border: none; border-radius: 5px; cursor: pointer; font-size: 0.88em; }
        .status { font-size: 0.82em; color: #888; margin-top: 6px; }
        .laddning { color: #0044cc; font-style: italic; }
    </style></head>
    <body>""" + NAV_HTML + """
        <h1>AI-Analytiker</h1>
        <p style="color:#888; margin-bottom:20px; font-size:0.9em;">Analytikern minns tidigare konversationer och har tillgång till live-marknadsdata.</p>

        <div class="chatt-wrapper">
            <div>
                <div class="meddelanden" id="meddelanden">
                    <div class="msg ai">
                        <div class="avsandare">AI-Analytiker</div>
                        <div class="bubbla">Hej! Jag minns våra tidigare analyser och har tillgång till aktuell marknadsdata. Vad vill du analysera idag?</div>
                    </div>
                </div>
                <div class="inmatning">
                    <textarea id="fraga" placeholder="Skriv din fråga... (Enter = skicka, Shift+Enter = ny rad)"></textarea>
                    <button onclick="skicka()">Skicka</button>
                </div>
            </div>

            <div class="sidopanel">
                <div class="panel-kort">
                    <h3>Ladda upp dokument</h3>
                    <input type="file" id="fil" accept=".pdf,.txt">
                    <button class="blå-btn" onclick="laddaUpp()">Ladda upp</button>
                    <p id="upload-status" class="status"></p>
                </div>

                <div class="panel-kort">
                    <h3>Använd dokument</h3>
                    <select id="valt-dokument">
                        <option value="">-- Inget --</option>
                        """ + dok_opts + """
                    </select>
                </div>

                <div class="panel-kort">
                    <h3>Klistra in text</h3>
                    <textarea id="nyhetsbrev" placeholder="Nyhetsbrev, artikel..."></textarea>
                </div>

                <div class="panel-kort">
                    <h3>Spara konversation</h3>
                    <input type="text" id="konv-titel" placeholder="t.ex. OMX-analys mars 2026">
                    <button class="grön-btn" onclick="sparaKonversation()">Spara till logg</button>
                    <p id="spara-status" class="status"></p>
                </div>
            </div>
        </div>

        <script>
        let chattHistorik = [];

        async function skicka() {
            const fraga = document.getElementById('fraga').value.trim();
            const nyhetsbrev = document.getElementById('nyhetsbrev').value.trim();
            const valtDok = document.getElementById('valt-dokument').value;
            if (!fraga) return;
            chattHistorik.push({roll: 'user', text: fraga});
            laggTillMeddelande('user', fraga);
            document.getElementById('fraga').value = '';
            const laddning = laggTillLaddning();
            try {
                const svar = await fetch('/analytiker/chatt', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({fraga, nyhetsbrev, dokument_id: valtDok, historik: chattHistorik})
                });
                const data = await svar.json();
                laddning.remove();
                chattHistorik.push({roll: 'assistant', text: data.svar});
                laggTillMeddelande('ai', data.svar);
                autoSpara();
            } catch(e) {
                laddning.remove();
                laggTillMeddelande('ai', 'Något gick fel.');
            }
        }

        async function laddaUpp() {
            const fil = document.getElementById('fil').files[0];
            if (!fil) return;
            document.getElementById('upload-status').textContent = 'Laddar upp...';
            const formData = new FormData();
            formData.append('fil', fil);
            try {
                const svar = await fetch('/dokument/ladda-upp', {method: 'POST', body: formData});
                const data = await svar.json();
                document.getElementById('upload-status').textContent = data.meddelande;
                setTimeout(() => location.reload(), 1500);
            } catch(e) {
                document.getElementById('upload-status').textContent = 'Misslyckades.';
            }
        }

        async function sparaKonversation() {
            const titel = document.getElementById('konv-titel').value.trim() || 'Analys ' + new Date().toLocaleDateString('sv-SE');
            if (chattHistorik.length === 0) {
                document.getElementById('spara-status').textContent = 'Ingen konversation att spara.';
                return;
            }
            const svar = await fetch('/analytiker/spara', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({titel, historik: chattHistorik})
            });
            const data = await svar.json();
            document.getElementById('spara-status').textContent = data.meddelande;
        }

        function laggTillMeddelande(typ, text) {
            const div = document.createElement('div');
            div.className = 'msg ' + typ;
            div.innerHTML = '<div class="avsandare">' + (typ === 'user' ? 'Du' : 'AI-Analytiker') + '</div><div class="bubbla">' + text.replace(/\\n/g, '<br>') + '</div>';
            document.getElementById('meddelanden').appendChild(div);
            document.getElementById('meddelanden').scrollTop = 999999;
        }

        function laggTillLaddning() {
            const div = document.createElement('div');
            div.className = 'msg ai';
            div.innerHTML = '<div class="bubbla laddning">Analyserar...</div>';
            document.getElementById('meddelanden').appendChild(div);
            document.getElementById('meddelanden').scrollTop = 999999;
            return div;
        }

        document.getElementById('fraga').addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); skicka(); }
        });

        // Auto-spara till sessionStorage efter varje meddelande
        function autoSpara() {
            sessionStorage.setItem('chattHistorik', JSON.stringify(chattHistorik));
        }

        // Återställ konversation från sessionStorage vid sidladdning
        function aterstellKonversation() {
            const sparad = sessionStorage.getItem('chattHistorik');
            if (sparad) {
                chattHistorik = JSON.parse(sparad);
                chattHistorik.forEach(msg => {
                    laggTillMeddelande(msg.roll === 'user' ? 'user' : 'ai', msg.text);
                });
            }
        }
        aterstellKonversation();
        </script>
    </body></html>"""
    return render_template_string(html)


@app.route("/analytiker/chatt", methods=["POST"])
@inloggning_kravs
def analytiker_chatt():
    data        = request.get_json()
    fraga       = data.get("fraga", "")
    nyhetsbrev  = data.get("nyhetsbrev", "")
    dokument_id = data.get("dokument_id", "")
    historik    = data.get("historik", [])

    marknadsdata = hamta_marknadsdata()

    tidigare = hamta_tidigare_konversationer(antal=3)
    minne = ""
    if tidigare:
        minne = "\n\nTidigare analyser:\n"
        for k in tidigare:
            minne += f"\n[{k['datum']} - {k['titel']}]\n"
            for msg in k["meddelanden"][-4:]:
                roll = "Du" if msg.get("roll") == "user" else "Analytiker"
                minne += f"{roll}: {msg.get('text','')}\n"

    dok_text = ""
    if dokument_id:
        conn, db_type = get_conn()
        c = conn.cursor()
        c.execute(q("SELECT filnamn, innehall FROM dokument WHERE id=?", db_type), (dokument_id,))
        rad = c.fetchone()
        conn.close()
        if rad:
            dok_text = f"\n\nDokument ({rad[0]}):\n{rad[1][:3000]}"

    system_prompt = f"""Du är en erfaren teknisk analytiker som hjälper en privat investerare med beslut kring index-ETF:er och indexfonder.

Investeringsstrategi:
- Swingtrading på index (OMX30, S&P500, Europa, Guld)
- Teknisk analys: RSI, MACD, SMA50/200, EMA20
- Max 1-2% kapitalrisk per trade
- Tidshorisont: veckor till månader

Aktuell marknadsdata:
{marknadsdata}
{minne}
{dok_text}

Svara på svenska. Var konkret. Referera till tidigare analyser när det är relevant."""

    meddelanden = []
    for msg in historik[:-1]:
        meddelanden.append({"role": "user" if msg["roll"] == "user" else "assistant", "content": msg["text"]})

    sista = fraga
    if nyhetsbrev:
        sista = f"Nyhetsbrev/analys:\n{nyhetsbrev}\n\nFråga: {fraga}"
    meddelanden.append({"role": "user", "content": sista})

    try:
        svar = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=system_prompt,
            messages=meddelanden
        )
        return jsonify({"svar": svar.content[0].text})
    except Exception as e:
        return jsonify({"svar": f"Fel: {str(e)}"})


@app.route("/analytiker/spara", methods=["POST"])
@inloggning_kravs
def analytiker_spara():
    data     = request.get_json()
    titel    = data.get("titel", "Analys")
    historik = data.get("historik", [])
    spara_konversation(titel, historik, hamta_marknadsdata())
    return jsonify({"meddelande": f"Sparad: {titel}"})


@app.route("/dokument/ladda-upp", methods=["POST"])
@inloggning_kravs
def ladda_upp_dokument():
    if "fil" not in request.files:
        return jsonify({"meddelande": "Ingen fil vald."})
    fil = request.files["fil"]
    if not fil.filename:
        return jsonify({"meddelande": "Ingen fil vald."})

    filnamn  = fil.filename
    innehall = ""

    if filnamn.lower().endswith(".pdf"):
        try:
            import base64
            pdf_b64 = base64.standard_b64encode(fil.read()).decode("utf-8")
            svar = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                messages=[{"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                    {"type": "text", "text": "Extrahera och returnera all text från detta dokument."}
                ]}]
            )
            innehall = svar.content[0].text
        except Exception as e:
            return jsonify({"meddelande": f"PDF-fel: {str(e)}"})
    else:
        innehall = fil.read().decode("utf-8", errors="ignore")

    spara_dokument(filnamn, innehall)
    return jsonify({"meddelande": f"Uppladdad: {filnamn}"})


@app.route("/analyslogg")
@inloggning_kravs
def analyslogg():
    konversationer = hamta_alla_konversationer()
    html = """<!DOCTYPE html><html>
    <head><title>Analyslogg</title><meta charset="utf-8">""" + BASE_STYLE + """
    </head><body>""" + NAV_HTML + """
        <h1>Analyslogg</h1>
        <p style="color:#888; margin-bottom:20px; font-size:0.9em;">Sparade konversationer med AI-analytikern.</p>
        {% if konversationer %}
        <table class="tabell">
            <thead><tr><th>Datum</th><th>Titel</th><th>Sparad</th><th></th></tr></thead>
            <tbody>
            {% for k in konversationer %}
                <tr>
                    <td>{{ k.datum }}</td>
                    <td>{{ k.titel or 'Utan titel' }}</td>
                    <td>{{ k.skapad }}</td>
                    <td><a href="/analyslogg/{{ k.id }}">Visa</a></td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color:#888;">Inga sparade analyser ännu.</p>
        {% endif %}
    </body></html>"""
    return render_template_string(html, konversationer=konversationer)


@app.route("/analyslogg/<int:konv_id>")
@inloggning_kravs
def visa_analys(konv_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("SELECT datum, titel, meddelanden, marknadsdata, skapad FROM konversationer WHERE id=?", db_type), (konv_id,))
    rad = c.fetchone()
    conn.close()
    if not rad:
        return "Analys hittades inte", 404
    meddelanden = json.loads(rad[2])
    html = """<!DOCTYPE html><html>
    <head><title>{{ titel }}</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .msg { margin-bottom: 16px; max-width: 700px; }
        .msg.user { text-align: right; }
        .msg.user .bubbla { background: #0044cc; color: #fff; display: inline-block; padding: 10px 16px; border-radius: 12px 12px 2px 12px; max-width: 85%; text-align: left; }
        .msg.ai .bubbla { background: #f0f0f0; color: #222; display: inline-block; padding: 10px 16px; border-radius: 12px 12px 12px 2px; max-width: 85%; }
        .msg .avsandare { font-size: 0.78em; color: #999; margin-bottom: 4px; }
        .marknadsdata { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 14px; margin-bottom: 20px; font-size: 0.84em; color: #555; white-space: pre-line; }
    </style></head>
    <body>""" + NAV_HTML + """
        <h1>{{ titel }}</h1>
        <p style="color:#888; margin-bottom:16px; font-size:0.88em;">{{ skapad }}</p>
        {% if marknadsdata %}
        <div class="marknadsdata"><strong>Marknadsdata:</strong><br>{{ marknadsdata }}</div>
        {% endif %}
        {% for msg in meddelanden %}
            <div class="msg {{ 'user' if msg.roll == 'user' else 'ai' }}">
                <div class="avsandare">{{ 'Du' if msg.roll == 'user' else 'AI-Analytiker' }}</div>
                <div class="bubbla">{{ msg.text | replace('\n', '<br>') | safe }}</div>
            </div>
        {% endfor %}
        <br><a href="/analyslogg">← Tillbaka</a>
    </body></html>"""
    return render_template_string(html, titel=rad[1], skapad=rad[4], marknadsdata=rad[3], meddelanden=meddelanden)


@app.route("/dokument")
@inloggning_kravs
def dokument_sida():
    dokument = hamta_alla_dokument()
    html = """<!DOCTYPE html><html>
    <head><title>Dokument</title><meta charset="utf-8">""" + BASE_STYLE + """
    </head><body>""" + NAV_HTML + """
        <h1>Dokument</h1>
        <p style="color:#888; margin-bottom:20px; font-size:0.9em;">Uppladdade nyhetsbrev, analyser och rapporter.</p>
        {% if dokument %}
        <table class="tabell">
            <thead><tr><th>Filnamn</th><th>Uppladdad</th><th></th></tr></thead>
            <tbody>
            {% for d in dokument %}
                <tr>
                    <td>{{ d.filnamn }}</td>
                    <td>{{ d.uppladdad }}</td>
                    <td><a href="/dokument/{{ d.id }}">Visa</a></td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color:#888;">Inga dokument uppladdade ännu.</p>
        {% endif %}
    </body></html>"""
    return render_template_string(html, dokument=dokument)


@app.route("/dokument/<int:dok_id>")
@inloggning_kravs
def visa_dokument(dok_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("SELECT filnamn, innehall, uppladdad FROM dokument WHERE id=?", db_type), (dok_id,))
    rad = c.fetchone()
    conn.close()
    if not rad:
        return "Dokument hittades inte", 404
    html = """<!DOCTYPE html><html>
    <head><title>{{ filnamn }}</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .dok-innehall { background: #fff; border: 1px solid #ddd; border-radius: 10px; padding: 24px; white-space: pre-wrap; font-size: 0.9em; line-height: 1.6; max-width: 800px; }
    </style></head>
    <body>""" + NAV_HTML + """
        <h1>{{ filnamn }}</h1>
        <p style="color:#888; margin-bottom:16px; font-size:0.88em;">Uppladdad: {{ uppladdad }}</p>
        <div class="dok-innehall">{{ innehall }}</div>
        <br><a href="/dokument">← Tillbaka</a>
    </body></html>"""
    return render_template_string(html, filnamn=rad[0], innehall=rad[1], uppladdad=rad[2])


@app.route("/riskmotor", methods=["GET", "POST"])
@inloggning_kravs
def riskmotor():
    resultat = None
    fel = None
    if request.method == "POST":
        try:
            kapital    = float(request.form["kapital"].replace(" ", "").replace(",", "."))
            risk_pct   = float(request.form["risk_pct"].replace(",", "."))
            entry      = float(request.form["entry"].replace(",", "."))
            stop       = float(request.form["stop"].replace(",", "."))
            if entry <= stop:
                fel = "Entry-priset måste vara högre än stop-loss."
            else:
                max_risk       = kapital * (risk_pct / 100)
                risk_per_enhet = entry - stop
                position_size  = max_risk / risk_per_enhet
                resultat = {
                    "max_risk": max_risk, "risk_per_enhet": risk_per_enhet,
                    "position_size": round(position_size, 2), "stop": stop,
                    "target_1r": round(entry + risk_per_enhet, 2),
                    "target_2r": round(entry + 2 * risk_per_enhet, 2),
                    "target_3r": round(entry + 3 * risk_per_enhet, 2),
                    "vinst_1r": round(position_size * risk_per_enhet, 0),
                    "vinst_2r": round(position_size * 2 * risk_per_enhet, 0),
                    "vinst_3r": round(position_size * 3 * risk_per_enhet, 0),
                }
        except ValueError:
            fel = "Kontrollera att alla fält innehåller giltiga siffror."

    html = """<!DOCTYPE html><html>
    <head><title>Riskmotor</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; max-width: 480px; margin-bottom: 22px; }
        .form-group label { display: block; color: #888; font-size: 0.85em; margin-bottom: 4px; }
        .form-group input { width: 100%; padding: 9px 12px; background: #fff; border: 1px solid #ccc; border-radius: 6px; color: #111; font-size: 1em; }
        .submit-btn { padding: 9px 26px; background: #0044cc; color: #fff; border: none; border-radius: 6px; font-size: 1em; font-weight: bold; cursor: pointer; }
        .resultat { background: #fff; border-radius: 10px; padding: 22px; max-width: 480px; border: 1px solid #ddd; }
        .resultat-rad { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid #eee; font-size: 0.92em; }
        .resultat-rad:last-child { border-bottom: none; }
        .resultat-etikett { color: #888; }
        .resultat-varde { font-weight: bold; }
        .fel { color: #cc0000; margin-bottom: 14px; }
        .stor { font-size: 1.3em; color: #0044cc; }
    </style></head>
    <body>""" + NAV_HTML + """
        <h1>Riskmotor</h1>
        <p style="color:#888; margin-bottom:18px; font-size:0.9em;">Beräknar position size baserat på max risk per trade.</p>
        {% if fel %}<p class="fel">{{ fel }}</p>{% endif %}
        <form method="POST">
            <div class="form-grid">
                <div class="form-group"><label>Totalt kapital (SEK)</label><input type="text" name="kapital" placeholder="1000000" value="{{ request.form.get('kapital', '') }}"></div>
                <div class="form-group"><label>Max risk per trade (%)</label><input type="text" name="risk_pct" placeholder="1" value="{{ request.form.get('risk_pct', '1') }}"></div>
                <div class="form-group"><label>Entry-pris</label><input type="text" name="entry" placeholder="2200" value="{{ request.form.get('entry', '') }}"></div>
                <div class="form-group"><label>Stop-loss</label><input type="text" name="stop" placeholder="2150" value="{{ request.form.get('stop', '') }}"></div>
            </div>
            <button class="submit-btn" type="submit">Beräkna</button>
        </form>
        {% if resultat %}
        <br>
        <div class="resultat">
            <div class="resultat-rad"><span class="resultat-etikett">Max risk</span><span class="resultat-varde" style="color:#cc0000;">{{ "{:,.0f}".format(resultat.max_risk) }} SEK</span></div>
            <div class="resultat-rad"><span class="resultat-etikett">Risk per enhet</span><span class="resultat-varde">{{ "%.2f"|format(resultat.risk_per_enhet) }}</span></div>
            <div class="resultat-rad"><span class="resultat-etikett">Position size</span><span class="resultat-varde stor">{{ resultat.position_size }} enheter</span></div>
            <div class="resultat-rad"><span class="resultat-etikett">Stop-loss</span><span class="resultat-varde" style="color:#cc0000;">{{ resultat.stop }}</span></div>
            <div class="resultat-rad"><span class="resultat-etikett">Target 1R (1:1)</span><span class="resultat-varde" style="color:#007700;">{{ resultat.target_1r }} +{{ "{:,.0f}".format(resultat.vinst_1r) }} SEK</span></div>
            <div class="resultat-rad"><span class="resultat-etikett">Target 2R (1:2)</span><span class="resultat-varde" style="color:#007700;">{{ resultat.target_2r }} +{{ "{:,.0f}".format(resultat.vinst_2r) }} SEK</span></div>
            <div class="resultat-rad"><span class="resultat-etikett">Target 3R (1:3)</span><span class="resultat-varde" style="color:#007700;">{{ resultat.target_3r }} +{{ "{:,.0f}".format(resultat.vinst_3r) }} SEK</span></div>
        </div>
        {% endif %}
    </body></html>"""
    return render_template_string(html, resultat=resultat, fel=fel, request=request)


@app.route("/gmail/koppla")
@inloggning_kravs
def gmail_koppla():
    if not GMAIL_AVAILABLE:
        return "Gmail-bibliotek saknas. Kör: pip install google-auth google-auth-oauthlib google-api-python-client", 500
    import secrets, hashlib
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    session["gmail_code_verifier"] = code_verifier
    flow = Flow.from_client_config(GMAIL_CLIENT_CONFIG, scopes=GMAIL_SCOPES, redirect_uri=GMAIL_REDIRECT_URI)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method="S256"
    )
    session["gmail_state"] = state
    return redirect(auth_url)

@app.route("/gmail/callback")
def gmail_callback():
    if not GMAIL_AVAILABLE:
        return "Gmail-bibliotek saknas.", 500
    code_verifier = session.get("gmail_code_verifier", "")
    flow = Flow.from_client_config(GMAIL_CLIENT_CONFIG, scopes=GMAIL_SCOPES, redirect_uri=GMAIL_REDIRECT_URI, state=session.get("gmail_state"))
    flow.fetch_token(
        authorization_response=request.url.replace("http:", "https:"),
        code_verifier=code_verifier
    )
    spara_gmail_token(flow.credentials)
    return redirect(url_for("gmail_sida"))

@app.route("/gmail")
@inloggning_kravs
def gmail_sida():
    creds = hamta_gmail_credentials()
    kopplad = creds is not None and creds.valid
    mejl_lista = []
    if kopplad:
        try:
            service = build("gmail", "v1", credentials=creds)
            results = service.users().messages().list(userId="me", maxResults=20, q="is:unread").execute()
            messages = results.get("messages", [])
            for msg in messages:
                m = service.users().messages().get(userId="me", id=msg["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"]).execute()
                headers = {h["name"]: h["value"] for h in m["payload"]["headers"]}
                mejl_lista.append({
                    "id": msg["id"],
                    "subject": headers.get("Subject", "(ingen rubrik)"),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", "")
                })
        except Exception as e:
            kopplad = False

    html = """<!DOCTYPE html><html>
    <head><title>Gmail</title><meta charset="utf-8">""" + BASE_STYLE + """
    </head><body>""" + NAV_HTML + """
        <h1>Gmail-integration</h1>
        {% if kopplad %}
        <p style="color:#007700; margin-bottom:20px;">✅ Kopplad till gena.input@gmail.com</p>
        <h2>Olästa mejl ({{ mejl_lista|length }})</h2>
        {% if mejl_lista %}
        <table class="tabell">
            <thead><tr><th>Ämne</th><th>Från</th><th>Datum</th><th></th></tr></thead>
            <tbody>
            {% for m in mejl_lista %}
                <tr>
                    <td>{{ m.subject[:60] }}</td>
                    <td>{{ m["from"][:40] }}</td>
                    <td>{{ m.date[:16] }}</td>
                    <td><a href="/gmail/importera/{{ m.id }}">Importera</a></td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color:#888;">Inga olästa mejl.</p>
        {% endif %}
        {% else %}
        <p style="color:#888; margin-bottom:20px;">Inte kopplad ännu.</p>
        <a href="/gmail/koppla" style="padding:10px 22px; background:#0044cc; color:#fff; border-radius:6px; text-decoration:none;">Koppla Gmail</a>
        {% endif %}
    </body></html>"""
    return render_template_string(html, kopplad=kopplad, mejl_lista=mejl_lista)

@app.route("/gmail/importera/<msg_id>")
@inloggning_kravs
def gmail_importera(msg_id):
    creds = hamta_gmail_credentials()
    if not creds:
        return redirect(url_for("gmail_sida"))
    try:
        service = build("gmail", "v1", credentials=creds)
        m = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in m["payload"]["headers"]}
        subject = headers.get("Subject", "Gmail-mejl")
        body = ""
        payload = m["payload"]
        if "parts" in payload:
            for part in payload["parts"]:
                if part["mimeType"] == "text/plain" and "data" in part.get("body", {}):
                    body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                    break
        elif "body" in payload and "data" in payload["body"]:
            body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
        if not body:
            body = m.get("snippet", "")
        spara_dokument(subject[:100], body)
        return redirect(url_for("analytiker"))
    except Exception as e:
        return f"Fel vid import: {str(e)}", 500


@app.route("/cron/daily_change-analys")
def cron_daily_change_analys():
    import threading
    t = threading.Thread(target=kör_daily_change_analys)
    t.daemon = True
    t.start()
    return jsonify({"status": "started", "meddelande": "Daglig analys körs i bakgrunden."})


def kör_daily_change_analys():
    """Körs i bakgrundstråd så att cron-anropet inte timear ut."""
    try:
        # 1. Hämta nya Gmail-mejl
        creds = hamta_gmail_credentials()
        nya_dokument = []
        if creds and creds.valid and GMAIL_AVAILABLE:
            service = build("gmail", "v1", credentials=creds)
            results = service.users().messages().list(userId="me", maxResults=10, q="is:unread").execute()
            messages = results.get("messages", [])
            for msg in messages:
                m = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
                headers = {h["name"]: h["value"] for h in m["payload"]["headers"]}
                subject = headers.get("Subject", "Gmail-mejl")
                body = ""
                payload = m["payload"]
                if "parts" in payload:
                    for part in payload["parts"]:
                        if part["mimeType"] == "text/plain" and "data" in part.get("body", {}):
                            body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                            break
                elif "body" in payload and "data" in payload["body"]:
                    body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
                if body:
                    spara_dokument(subject[:100], body)
                    nya_dokument.append(subject[:60])
                service.users().messages().modify(userId="me", id=msg["id"], body={"removeLabelIds": ["UNREAD"]}).execute()

        # 2. Hämta marknadsdata
        marknadsdata = hamta_marknadsdata()

        # 3. Hämta senaste dokument
        alla_dok = hamta_alla_dokument()
        dok_kontext = ""
        if alla_dok:
            conn, db_type = get_conn()
            c = conn.cursor()
            for d in alla_dok[:5]:
                c.execute(q("SELECT innehall FROM dokument WHERE id=?", db_type), (d["id"],))
                rad = c.fetchone()
                if rad:
                    dok_kontext += "\n\n--- " + d["filnamn"] + " (" + d["uppladdad"] + ") ---\n" + rad[0][:1500]
            conn.close()

        # 4. Generera analys
        prompt = "Generera en daily_change marknadsanalys för " + datetime.now().strftime("%Y-%m-%d") + ".\n\nMarknadsdata:\n" + marknadsdata + "\n\nNyhetsbrev och analyser:\n" + (dok_kontext if dok_kontext else "Inga nya dokument idag.") + "\n\nNya mejl idag: " + (", ".join(nya_dokument) if nya_dokument else "Inga") + "\n\nGe en strukturerad analys med:\n1. Sammanfattning av marknadsläget\n2. Vad nyhetsbreven säger\n3. Observationer per index\n4. Eventuella köp/säljsignaler"

        svar = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system="Du är en erfaren teknisk analytiker. Svara på svenska. Var konkret och strukturerad.",
            messages=[{"role": "user", "content": prompt}]
        )
        analys_text = svar.content[0].text

        # 5. Spara analysen
        conn, db_type = get_conn()
        c = conn.cursor()
        c.execute(q("INSERT INTO daily_changea_analyser (datum, analys, skapad) VALUES (?, ?, ?)", db_type),
                  (datetime.now().strftime("%Y-%m-%d"), analys_text, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()

    except Exception as e:
        print(f"Cron fel: {str(e)}")


@app.route("/daily_change-analys")
@inloggning_kravs
def daily_change_analys_sida():
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, datum, analys, skapad FROM daily_changea_analyser ORDER BY id DESC LIMIT 10")
    rader = c.fetchall()
    conn.close()
    analyser = [{"id": r[0], "datum": r[1], "analys": r[2], "skapad": r[3]} for r in rader]

    html = """<!DOCTYPE html><html>
    <head><title>Daglig Analys</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .analys-kort { background: #fff; border-radius: 10px; padding: 24px; border: 1px solid #ddd; margin-bottom: 20px; max-width: 800px; }
        .analys-datum { font-size: 0.85em; color: #888; margin-bottom: 14px; }
        .analys-text { white-space: pre-wrap; line-height: 1.7; font-size: 0.92em; }
    </style>
    </head><body>""" + NAV_HTML + """
        <h1>Daglig Analys</h1>
        <p style="color:#888; margin-bottom:20px; font-size:0.9em;">Automatisk analys genererad varje morgon baserat på marknadsdata och nyhetsbrev.</p>
        {% if analyser %}
            {% for a in analyser %}
            <div class="analys-kort">
                <div class="analys-datum">{{ a.datum }} · Genererad {{ a.skapad }}</div>
                <div class="analys-text">{{ a.analys }}</div>
            </div>
            {% endfor %}
        {% else %}
            <p style="color:#888;">Ingen daily_change analys ännu. Aktivera Cron Job på Render för automatisk körning.</p>
        {% endif %}
    </body></html>"""
    return render_template_string(html, analyser=analyser)



# ── Tracker routes ─────────────────────────────────────────

@app.route("/tracker")
@inloggning_kravs
def tracker_sida():
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("""SELECT id, datum, index_namn, kurs_start, riktning, mal_niva, mal_procent, 
                 period, kommentar, avslutad FROM tankar ORDER BY id DESC""")
    rader = c.fetchall()
    conn.close()
    trackers = [{
        "id": r[0], "datum": r[1], "index_namn": r[2], "kurs_start": r[3],
        "riktning": r[4], "mal_niva": r[5], "mal_procent": r[6],
        "period": r[7], "kommentar": r[8], "avslutad": r[9]
    } for r in rader]

    html = """<!DOCTYPE html><html>
    <head><title>Tracker</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .ny-tracker { background:#fff; border-radius:10px; padding:24px; border:1px solid #ddd; max-width:680px; margin-bottom:30px; }
        .ny-tracker h2 { font-size:1em; color:#444; margin-bottom:18px; }
        .form-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:14px; }
        .form-full { grid-column: 1 / -1; }
        .form-group label { display:block; color:#888; font-size:0.82em; margin-bottom:4px; font-weight:bold; }
        .form-group select, .form-group input, .form-group textarea { width:100%; padding:9px 12px; border:1px solid #ccc; border-radius:6px; font-size:0.95em; font-family:inherit; }
        .form-group textarea { min-height:70px; resize:vertical; }
        .submit-btn { padding:10px 28px; background:#0044cc; color:#fff; border:none; border-radius:6px; font-size:1em; font-weight:bold; cursor:pointer; }
        .tracker-lista { max-width:900px; }
        .tracker-kort { background:#fff; border-radius:10px; padding:20px; border:1px solid #ddd; margin-bottom:16px; }
        .tracker-kort.avslutad { opacity:0.6; }
        .tracker-header { display:flex; gap:12px; align-items:center; margin-bottom:12px; flex-wrap:wrap; }
        .badge { padding:3px 10px; border-radius:20px; font-size:0.8em; font-weight:bold; }
        .badge-upp { background:#e6f4e6; color:#007700; }
        .badge-ned { background:#fce8e8; color:#cc0000; }
        .badge-aktiv { background:#e8f0fe; color:#0044cc; }
        .badge-avslutad { background:#f0f0f0; color:#888; }
        .tracker-info { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px,1fr)); gap:10px; margin-bottom:12px; }
        .info-cell { font-size:0.85em; }
        .info-cell .etikett { color:#999; font-size:0.8em; }
        .info-cell .varde { font-weight:bold; color:#111; }
        .tracker-kommentar { font-size:0.88em; color:#555; font-style:italic; margin-bottom:12px; border-left:3px solid #ddd; padding-left:10px; }
        .milstolpar { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
        .milstolpe { font-size:0.8em; padding:4px 10px; border-radius:4px; background:#f8f8f8; border:1px solid #ddd; color:#555; }
        .milstolpe.passerad { background:#fff8e6; border-color:#ffcc44; color:#886600; }
        .ai-kommentar { background:#f8f8ff; border-radius:6px; padding:12px; font-size:0.87em; color:#333; line-height:1.6; margin-top:10px; white-space:pre-wrap; }
        .btn-rad { display:flex; gap:8px; margin-top:12px; }
        .btn-liten { padding:5px 14px; border:none; border-radius:5px; font-size:0.85em; cursor:pointer; }
        .btn-analys { background:#0044cc; color:#fff; }
        .btn-avsluta { background:#888; color:#fff; }
    </style></head>
    <body>""" + NAV_HTML + """
        <h1>Tracker</h1>
        <p style="color:#888; margin-bottom:20px; font-size:0.9em;">Logga en känsla eller analys och följ upp om du hade rätt.</p>

        <div class="ny-tracker">
            <h2>+ Ny Tracker</h2>
            <form method="POST" action="/tracker/ny">
                <div class="form-grid">
                    <div class="form-group">
                        <label>Index</label>
                        <select name="index_namn">
                            <option>OMX30</option>
                            <option>S&P500</option>
                            <option>Europa</option>
                            <option>Guld</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Riktning</label>
                        <select name="riktning">
                            <option value="Upp">↑ Upp</option>
                            <option value="Ned">↓ Ned</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Målnivå (kurs)</label>
                        <input type="number" step="0.01" name="mal_niva" placeholder="t.ex. 2500">
                    </div>
                    <div class="form-group">
                        <label>Alt. mål i % (t.ex. 5 för +5%)</label>
                        <input type="number" step="0.1" name="mal_procent" placeholder="t.ex. 5">
                    </div>
                    <div class="form-group">
                        <label>Tidsperiod</label>
                        <select name="period">
                            <option>1 vecka</option>
                            <option>2 veckor</option>
                            <option selected>1 månad</option>
                            <option>3 månader</option>
                            <option>6 månader</option>
                        </select>
                    </div>
                    <div class="form-group form-full">
                        <label>Din analys / känsla</label>
                        <textarea name="kommentar" placeholder="Varför tror du detta? Vad ser du i datan?"></textarea>
                    </div>
                </div>
                <button class="submit-btn" type="submit">Starta tracker</button>
            </form>
        </div>

        <div class="tracker-lista">
        {% for t in trackers %}
            <div class="tracker-kort {{ 'avslutad' if t.avslutad else '' }}">
                <div class="tracker-header">
                    <strong style="font-size:1.1em;">{{ t.index_namn }}</strong>
                    <span class="badge {{ 'badge-upp' if t.riktning == 'Upp' else 'badge-ned' }}">
                        {{ '↑' if t.riktning == 'Upp' else '↓' }} {{ t.riktning }}
                    </span>
                    <span class="badge {{ 'badge-avslutad' if t.avslutad else 'badge-aktiv' }}">
                        {{ 'Avslutad' if t.avslutad else 'Aktiv' }}
                    </span>
                    <span style="color:#999; font-size:0.85em; margin-left:auto;">{{ t.datum }}</span>
                </div>
                <div class="tracker-info">
                    <div class="info-cell"><div class="etikett">Startkurs</div><div class="varde">{{ "%.2f"|format(t.kurs_start) }}</div></div>
                    {% if t.mal_niva %}<div class="info-cell"><div class="etikett">Målkurs</div><div class="varde">{{ "%.2f"|format(t.mal_niva) }}</div></div>{% endif %}
                    {% if t.mal_procent %}<div class="info-cell"><div class="etikett">Mål %</div><div class="varde">{{ "%+.1f"|format(t.mal_procent) }}%</div></div>{% endif %}
                    <div class="info-cell"><div class="etikett">Period</div><div class="varde">{{ t.period }}</div></div>
                </div>
                {% if t.kommentar %}
                <div class="tracker-kommentar">{{ t.kommentar }}</div>
                {% endif %}
                {% if not t.avslutad %}
                <div class="btn-rad">
                    <button class="btn-liten btn-analys" onclick="analyseraTracker({{ t.id }}, this)">AI-analys nu</button>
                    <button class="btn-liten" style="background:#555;color:#fff;" onclick="visaGraf({{ t.id }}, this)">Visa graf</button>
                    <button class="btn-liten btn-avsluta" onclick="avslutaTracker({{ t.id }})">Avsluta</button>
                </div>
                {% endif %}
                <div id="graf-{{ t.id }}" style="display:none; margin-top:10px;"></div>
                <div id="ai-{{ t.id }}" class="ai-kommentar" style="display:none;"></div>
            </div>
        {% endfor %}
        {% if not trackers %}
        <p style="color:#888;">Inga trackers ännu. Starta din första ovan!</p>
        {% endif %}
        </div>

        <script>
        async function analyseraTracker(id, btn) {
            btn.textContent = 'Analyserar...';
            btn.disabled = true;
            const svar = await fetch('/tracker/' + id + '/analysera', {method: 'POST'});
            const data = await svar.json();
            document.getElementById('ai-' + id).style.display = 'block';
            document.getElementById('ai-' + id).textContent = data.analys;
            btn.textContent = 'Uppdatera analys';
            btn.disabled = false;
        }
        async function avslutaTracker(id) {
            if (!confirm('Avsluta trackern?')) return;
            await fetch('/tracker/' + id + '/avsluta', {method: 'POST'});
            location.reload();
        }
        async function visaGraf(id, btn) {
            const el = document.getElementById('graf-' + id);
            if (el.style.display !== 'none') { el.style.display = 'none'; btn.textContent = 'Visa graf'; return; }
            btn.textContent = 'Laddar...';
            const svar = await fetch('/tracker/' + id + '/graf');
            const data = await svar.json();
            el.innerHTML = data.html;
            el.style.display = 'block';
            btn.textContent = 'Dölj graf';
        }
        </script>
    </body></html>"""
    return render_template_string(html, trackers=trackers)


@app.route("/tracker/ny", methods=["POST"])
@inloggning_kravs
def tracker_ny():
    index_namn  = request.form.get("index_namn", "OMX30")
    riktning    = request.form.get("riktning", "Upp")
    mal_niva    = request.form.get("mal_niva", "") or None
    mal_procent = request.form.get("mal_procent", "") or None
    period      = request.form.get("period", "1 månad")
    kommentar   = request.form.get("kommentar", "")

    ticker = TICKERS.get(index_namn, "^OMX")
    try:
        df = hamta_data(ticker, yf_period="5d", yf_interval="1d")
        kurs_start = float(df["Close"].iloc[-1])
    except:
        kurs_start = 0.0

    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("""INSERT INTO tankar
        (datum, index_namn, kurs_start, riktning, mal_niva, mal_procent, period, kommentar, avslutad, skapad)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""", db_type),
        (datetime.now().strftime("%Y-%m-%d"), index_namn, kurs_start,
         riktning, float(mal_niva) if mal_niva else None,
         float(mal_procent) if mal_procent else None,
         period, kommentar, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()
    return redirect(url_for("tracker_sida"))


@app.route("/tracker/<int:tracker_id>/analysera", methods=["POST"])
@inloggning_kravs
def tracker_analysera(tracker_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("SELECT datum, index_namn, kurs_start, riktning, mal_niva, mal_procent, period, kommentar FROM tankar WHERE id=?", db_type), (tracker_id,))
    rad = c.fetchone()
    conn.close()
    if not rad:
        return jsonify({"analys": "Tracker hittades inte."})

    datum, index_namn, kurs_start, riktning, mal_niva, mal_procent, period, kommentar = rad

    ticker = TICKERS.get(index_namn, "^OMX")
    try:
        df = hamta_data(ticker, yf_period="6mo", yf_interval="1d")
        kurs_nu = float(df["Close"].iloc[-1])
        s = sammanfatta(index_namn, df)
        forandring = (kurs_nu - kurs_start) / kurs_start * 100

        # Hämta kursdata från startdatum
        start_dt = datetime.strptime(datum, "%Y-%m-%d")
        df_period = df[df.index >= start_dt] if start_dt in df.index or len(df) > 0 else df

        marknadsinfo = f"""
Index: {index_namn}
Startkurs ({datum}): {kurs_start:.2f}
Aktuell kurs: {kurs_nu:.2f}
Förändring sedan start: {forandring:+.2f}%
RSI nu: {s['rsi']} ({s['rsi_text']})
Trend: {s['trend']}
MACD: {s['macd_signal']}
SMA50: {s['sma50']} | SMA200: {s['sma200']}
"""
    except Exception as e:
        marknadsinfo = f"Kunde inte hämta marknadsdata: {str(e)}"
        forandring = 0

    mal_text = ""
    if mal_niva:
        mal_text = f"Målkurs: {mal_niva:.2f}"
    if mal_procent:
        mal_text += f" (mål: {mal_procent:+.1f}%)"

    prompt = f"""En investerare startade en tracker den {datum} med följande antagande:

Index: {index_namn}
Riktning: {riktning}
{mal_text}
Tidsperiod: {period}
Investerarens analys: {kommentar}

Aktuell utveckling:
{marknadsinfo}

Analysera:
1. Hur har utvecklingen gått mot antagandet hittills?
2. Vad stödjer eller motarbetar antagandet tekniskt?
3. Om riktningen avvikit från antagandet - när och varför skedde troligen avvikelsen?
4. Din bedömning av om antagandet fortfarande håller."""

    try:
        svar = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system="Du är en erfaren teknisk analytiker. Svara på svenska. Var konkret och hjälp investeraren förstå om deras antagande håller.",
            messages=[{"role": "user", "content": prompt}]
        )
        analys = svar.content[0].text
    except Exception as e:
        analys = f"Fel: {str(e)}"

    return jsonify({"analys": analys})


@app.route("/tracker/<int:tracker_id>/avsluta", methods=["POST"])
@inloggning_kravs
def tracker_avsluta(tracker_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("UPDATE tankar SET avslutad=1 WHERE id=?", db_type), (tracker_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# ── Tracker graf ──────────────────────────────────────────
@app.route("/tracker/<int:tracker_id>/graf")
@inloggning_kravs
def tracker_graf(tracker_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("SELECT datum, index_namn, kurs_start, riktning, mal_niva FROM tankar WHERE id=?", db_type), (tracker_id,))
    rad = c.fetchone()
    conn.close()
    if not rad:
        return jsonify({"html": "Tracker hittades inte."})
    datum, index_namn, kurs_start, riktning, mal_niva = rad
    ticker = TICKERS.get(index_namn, "^OMX")
    try:
        df = hamta_data(ticker, yf_period="1y", yf_interval="1d")
        start_ts = pd.Timestamp(datetime.strptime(datum, "%Y-%m-%d"))
        df_f = df[df.index >= start_ts]
        if len(df_f) < 2:
            df_f = df.tail(30)
        close    = df_f["Close"].squeeze()
        x_labels = [d.strftime("%d %b") for d in df_f.index]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=x_labels, y=close, mode="lines",
                                 line=dict(color="#0044cc", width=2), name=index_namn))
        fig.add_hline(y=kurs_start, line=dict(color="#888", width=1, dash="dash"),
                      annotation_text=f"Start {kurs_start:.2f}", annotation_position="top right")
        if mal_niva:
            col = "#007700" if riktning == "Upp" else "#cc0000"
            fig.add_hline(y=float(mal_niva), line=dict(color=col, width=1, dash="dot"),
                          annotation_text=f"Mål {float(mal_niva):.2f}", annotation_position="bottom right")
        fig.update_layout(
            paper_bgcolor="#ffffff", plot_bgcolor="#f8f8f8",
            height=260, margin=dict(l=10, r=70, t=16, b=36),
            showlegend=False,
            xaxis=dict(type="category", gridcolor="#dddddd", tickangle=-45, tickfont=dict(size=9)),
            yaxis=dict(gridcolor="#dddddd", side="right"),
        )
        return jsonify({"html": fig.to_html(full_html=False, include_plotlyjs="cdn")})
    except Exception as e:
        return jsonify({"html": f"Fel: {e}"})


# ── Prisalarm ─────────────────────────────────────────────

@app.route("/prisalarm")
@inloggning_kravs
def prisalarm_sida():
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, ticker, namn, niva, riktning, email, aktiv, utlost, skapad FROM prisalarm ORDER BY id DESC")
    rader   = c.fetchall()
    conn.close()
    alarm_lista = [{
        "id": r[0], "ticker": r[1], "namn": r[2], "niva": r[3],
        "riktning": r[4], "email": r[5], "aktiv": r[6], "utlost": r[7], "skapad": r[8]
    } for r in rader]

    index_opts = "".join(
        f'<option value="{t}" data-ticker="{v}">{t}</option>' for t, v in TICKERS.items()
    )
    smtp_ok = bool(os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"))

    html = """<!DOCTYPE html><html>
    <head><title>Prisalarm</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .alarm-form { background:#fff; border-radius:10px; padding:24px; border:1px solid #ddd; max-width:600px; margin-bottom:30px; }
        .fg { margin-bottom:12px; }
        .fg label { display:block; color:#888; font-size:0.82em; font-weight:bold; margin-bottom:4px; }
        .fg input, .fg select { width:100%; padding:9px 12px; border:1px solid #ccc; border-radius:6px; font-size:0.95em; }
        .row2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
        .badge-over { background:#e6f4e6; color:#007700; padding:2px 9px; border-radius:12px; font-size:0.82em; font-weight:bold; }
        .badge-under { background:#fce8e8; color:#cc0000; padding:2px 9px; border-radius:12px; font-size:0.82em; font-weight:bold; }
        .badge-utlost { background:#fff8e6; color:#886600; padding:2px 9px; border-radius:12px; font-size:0.82em; }
        .smtp-warn { background:#fff3cd; border:1px solid #ffc107; border-radius:6px; padding:10px 14px; margin-bottom:18px; font-size:0.88em; color:#856404; }
    </style></head>
    <body>""" + NAV_HTML + """
        <h1>Prisalarm</h1>
        {% if not smtp_ok %}
        <div class="smtp-warn">⚠ Inga SMTP-uppgifter konfigurerade. Sätt <strong>SMTP_USER</strong> och <strong>SMTP_PASS</strong> i miljövariablerna för att aktivera e-postutskick.</div>
        {% endif %}
        <div class="alarm-form">
            <h2 style="font-size:1em; color:#444; margin-bottom:16px;">+ Nytt alarm</h2>
            <form method="POST" action="/prisalarm/ny">
                <div class="row2">
                    <div class="fg">
                        <label>Index</label>
                        <select name="namn" id="alarm-namn">""" + index_opts + """</select>
                    </div>
                    <div class="fg">
                        <label>Ticker (anpassa vid behov)</label>
                        <input type="text" name="ticker" id="alarm-ticker" placeholder="t.ex. ^OMX">
                    </div>
                </div>
                <div class="row2">
                    <div class="fg">
                        <label>Kursnivå</label>
                        <input type="number" step="0.01" name="niva" placeholder="t.ex. 2500" required>
                    </div>
                    <div class="fg">
                        <label>Riktning</label>
                        <select name="riktning">
                            <option value="OVER">Kurs går ÖVER nivån</option>
                            <option value="UNDER">Kurs går UNDER nivån</option>
                        </select>
                    </div>
                </div>
                <div class="fg">
                    <label>E-post att notifiera</label>
                    <input type="email" name="email" placeholder="din@epost.se" required>
                </div>
                <button type="submit" style="padding:9px 24px; background:#0044cc; color:#fff; border:none; border-radius:6px; font-weight:bold; cursor:pointer;">Spara alarm</button>
            </form>
        </div>
        {% if alarm_lista %}
        <table class="tabell" style="max-width:860px;">
            <thead><tr><th>Index</th><th>Ticker</th><th>Nivå</th><th>Riktning</th><th>E-post</th><th>Status</th><th>Skapad</th><th></th></tr></thead>
            <tbody>
            {% for a in alarm_lista %}
            <tr style="opacity: {{ '0.5' if not a.aktiv else '1' }};">
                <td><strong>{{ a.namn }}</strong></td>
                <td style="color:#888; font-size:0.88em;">{{ a.ticker }}</td>
                <td><strong>{{ "%.2f"|format(a.niva) }}</strong></td>
                <td>
                    {% if a.riktning == 'OVER' %}<span class="badge-over">↑ Över</span>
                    {% else %}<span class="badge-under">↓ Under</span>{% endif %}
                </td>
                <td style="font-size:0.88em;">{{ a.email }}</td>
                <td>
                    {% if a.utlost %}<span class="badge-utlost">Utlöst</span>
                    {% elif a.aktiv %}<span style="color:#007700; font-size:0.88em;">Aktivt</span>
                    {% else %}<span style="color:#888; font-size:0.88em;">Inaktivt</span>{% endif %}
                </td>
                <td style="font-size:0.82em; color:#888;">{{ a.skapad[:10] }}</td>
                <td>
                    <form method="POST" action="/prisalarm/{{ a.id }}/ta-bort" style="display:inline;">
                        <button type="submit" style="background:none; border:none; color:#cc0000; cursor:pointer; font-size:0.85em;">Ta bort</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color:#888;">Inga alarm inställda ännu.</p>
        {% endif %}
        <script>
        const tickerMap = """ + json.dumps({t: v for t, v in TICKERS.items()}) + """;
        document.getElementById('alarm-namn').addEventListener('change', function() {
            document.getElementById('alarm-ticker').value = tickerMap[this.value] || '';
        });
        document.getElementById('alarm-ticker').value = tickerMap[document.getElementById('alarm-namn').value] || '';
        </script>
    </body></html>"""
    return render_template_string(html, alarm_lista=alarm_lista, smtp_ok=smtp_ok)


@app.route("/prisalarm/ny", methods=["POST"])
@inloggning_kravs
def prisalarm_ny():
    namn    = request.form.get("namn", "")
    ticker  = request.form.get("ticker", "").strip()
    niva    = float(request.form.get("niva", 0))
    riktning = request.form.get("riktning", "OVER")
    email   = request.form.get("email", "").strip()
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("INSERT INTO prisalarm (ticker, namn, niva, riktning, email, aktiv, utlost, skapad) VALUES (?,?,?,?,?,1,0,?)", db_type),
              (ticker, namn, niva, riktning, email, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()
    return redirect(url_for("prisalarm_sida"))


@app.route("/prisalarm/<int:alarm_id>/ta-bort", methods=["POST"])
@inloggning_kravs
def prisalarm_ta_bort(alarm_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("DELETE FROM prisalarm WHERE id=?", db_type), (alarm_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("prisalarm_sida"))


@app.route("/cron/kolla-alarm")
def cron_kolla_alarm():
    """Kontrollerar aktiva prisalarm och skickar e-post vid träff."""
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, ticker, namn, niva, riktning, email FROM prisalarm WHERE aktiv=1 AND utlost=0")
    alarm = c.fetchall()
    conn.close()
    utlosta = []
    for aid, ticker, namn, niva, riktning, email in alarm:
        try:
            df = yf.download(ticker, period="2d", interval="1d", progress=False, auto_adjust=True)
            if df.empty:
                continue
            kurs = float(df["Close"].iloc[-1])
            utlost = (riktning == "OVER" and kurs >= niva) or (riktning == "UNDER" and kurs <= niva)
            if utlost:
                rikt_text = f"gått ÖVER {niva:.2f}" if riktning == "OVER" else f"gått UNDER {niva:.2f}"
                subject = f"Prisalarm: {namn} har {rikt_text}"
                body    = (f"Ditt prisalarm har utlösts!\n\n"
                           f"Index:   {namn} ({ticker})\n"
                           f"Nivå:    {niva:.2f}\n"
                           f"Aktuell kurs: {kurs:.2f}\n"
                           f"Tid: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                           f"Trading Dashboard")
                skicka_alarm_mejl(email, subject, body)
                conn2, db_type2 = get_conn()
                c2 = conn2.cursor()
                c2.execute(q("UPDATE prisalarm SET utlost=1, aktiv=0 WHERE id=?", db_type2), (aid,))
                conn2.commit()
                conn2.close()
                utlosta.append(f"{namn} @ {kurs:.2f}")
        except Exception as e:
            print(f"Alarm-fel {aid}: {e}")
    return jsonify({"utlosta": utlosta, "kontrollerade": len(alarm)})


# ── Portfölj ──────────────────────────────────────────────



def hamta_portfolj_kurs(ticker):
    try:
        df = yf.download(ticker, period="5d", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return None, None
        kurs = float(df["Close"].iloc[-1])
        if len(df) >= 2:
            prev  = float(df["Close"].iloc[-2])
            daily_change = (kurs - prev) / prev * 100
        else:
            daily_change = 0.0
        return kurs, daily_change
    except:
        return None, None


@app.route("/portfolio")
@inloggning_kravs
def portfolio_sida():
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, namn, niva, skapad FROM portfoljer ORDER BY id")
    portfoljer = [{"id": r[0], "namn": r[1], "niva": r[2], "skapad": r[3]} for r in c.fetchall()]
    conn.close()
    return redirect(url_for("portfolio_vy", portfolio_id=portfoljer[0]["id"])) if portfoljer else redirect(url_for("portfolio_ny_sida"))


@app.route("/portfolio/ny-sida")
@inloggning_kravs
def portfolio_ny_sida():
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, namn FROM portfoljer ORDER BY id")
    existing = [{"id": r[0], "namn": r[1]} for r in c.fetchall()]
    conn.close()

    html = """<!DOCTYPE html><html>
    <head><title>Ny portfölj</title><meta charset="utf-8">""" + BASE_STYLE + PORTFOLIO_STYLE + """
    </head><body>""" + NAV_HTML + """
        <h1>Skapa ny portfölj</h1>
        <div class="ny-innehav-form" style="max-width:520px;">
            <form method="POST" action="/portfolio/ny">
                <div class="fg" style="margin-bottom:12px;">
                    <label>Portföljnamn</label>
                    <input type="text" name="namn" placeholder="t.ex. Bred depå, ISK, Pension" required>
                </div>
                <div class="fg" style="margin-bottom:12px;">
                    <label>Typ</label>
                    <select name="niva">
                        <option value="Depå">Depå</option>
                        <option value="ISK">ISK</option>
                        <option value="Pension">Pension</option>
                        <option value="KF">Kapitalförsäkring</option>
                        <option value="Total">Sammanslagen (välj nedan)</option>
                    </select>
                </div>
                {% if existing %}
                <div class="fg" style="margin-bottom:16px;">
                    <label>Slå ihop existing portföljer (valfritt)</label>
                    {% for p in existing %}
                    <div style="margin-top:8px; display:flex; align-items:center; gap:10px;">
                        <input type="checkbox" name="merge_with" value="{{ p.id }}" id="p{{ p.id }}" style="width:18px; height:18px; accent-color:#1F3864;">
                        <label for="p{{ p.id }}" style="display:inline; font-weight:normal; color:#333; font-size:0.95em;">{{ p.namn }}</label>
                    </div>
                    {% endfor %}
                </div>
                {% endif %}
                <button type="submit" style="padding:9px 24px; background:#1F3864; color:#fff; border:none; border-radius:6px; font-weight:bold; cursor:pointer;">Skapa portfölj</button>
            </form>
        </div>
    </body></html>"""
    return render_template_string(html, existing=existing)


@app.route("/portfolio/<int:portfolio_id>")
@inloggning_kravs
def portfolio_vy(portfolio_id):
    conn, db_type = get_conn()
    c = conn.cursor()

    # Hämta alla portföljer för tabbar
    c.execute("SELECT id, namn, niva FROM portfoljer ORDER BY id")
    all_portfolios = [{"id": r[0], "namn": r[1], "niva": r[2]} for r in c.fetchall()]

    # Hämta denna portfölj
    c.execute(q("SELECT id, namn, niva FROM portfoljer WHERE id=?", db_type), (portfolio_id,))
    rad = c.fetchone()
    if not rad:
        conn.close()
        return redirect(url_for("portfolio_sida"))
    portfolj = {"id": rad[0], "namn": rad[1], "niva": rad[2]}

    # Kolla om detta är en sammanslagen portfölj
    c.execute(q("SELECT del_portfolj_id FROM portfolj_sammanslagning WHERE total_portfolj_id=?", db_type), (portfolio_id,))
    sub_portfolios = [r[0] for r in c.fetchall()]
    is_merged = len(sub_portfolios) > 0

    # Hämta portfölj-IDs att visa innehav för
    if is_merged:
        portfolio_ids = sub_portfolios
    else:
        portfolio_ids = [portfolio_id]

    # Hämta innehav
    placeholders = ",".join(["%s" if db_type == "postgres" else "?" for _ in portfolio_ids])
    c.execute(f"""SELECT i.id, i.namn, i.ticker, i.tillgangsslag, i.valuta, i.portfolj_id,
               SUM(CASE WHEN t.typ='KOP' THEN t.antal WHEN t.typ='SALJ' THEN -t.antal ELSE 0 END) as antal,
               SUM(CASE WHEN t.typ='KOP' THEN t.antal*t.kurs WHEN t.typ='SALJ' THEN -t.antal*t.kurs ELSE 0 END) as cost_basis
               FROM innehav i
               LEFT JOIN transaktioner t ON t.innehav_id=i.id
               WHERE i.portfolj_id IN ({placeholders})
               GROUP BY i.id, i.namn, i.ticker, i.tillgangsslag, i.valuta, i.portfolj_id
               HAVING SUM(CASE WHEN t.typ='KOP' THEN t.antal WHEN t.typ='SALJ' THEN -t.antal ELSE 0 END) > 0""",
               portfolio_ids)
    innehav_rader = c.fetchall()
    conn.close()

    # Live-kurser och beräkningar
    innehav = []
    total_mv = 0
    total_cost = 0
    asset_type_data = {}
    currency_data = {}

    for r in innehav_rader:
        iid, namn, ticker, tillgangsslag, valuta, pid, antal, cost_basis = r
        antal = float(antal or 0)
        cost_basis = float(cost_basis or 0)
        kurs, daily_change = hamta_portfolj_kurs(ticker)
        mv = round(antal * (kurs or 0), 0)
        unrealized = round(mv - cost_basis, 0) if mv else 0
        unrealized_pct = round(unrealized / cost_basis * 100, 1) if cost_basis else 0

        innehav.append({
            "id": iid, "namn": namn, "ticker": ticker,
            "tillgangsslag": tillgangsslag, "valuta": valuta,
            "antal": antal, "kurs": kurs or 0,
            "mv": mv, "anskaffning": round(cost_basis, 0),
            "unrealized": unrealized, "unrealized_pct": unrealized_pct,
            "daily_change": daily_change or 0, "portfolj_id": pid
        })
        total_mv += mv
        total_cost += cost_basis
        asset_type_data[tillgangsslag] = asset_type_data.get(tillgangsslag, 0) + mv
        currency_data[valuta] = currency_data.get(valuta, 0) + mv

    totalt_unrealized = total_mv - total_cost
    total_pct = round(totalt_unrealized / total_cost * 100, 1) if total_cost else 0
    innehav.sort(key=lambda x: x["mv"], reverse=True)

    import json as _json
    asset_type_json = _json.dumps(list(asset_type_data.keys()))
    asset_type_values = _json.dumps([round(v) for v in asset_type_data.values()])
    currency_json = _json.dumps(list(currency_data.keys()))
    currency_values = _json.dumps([round(v) for v in currency_data.values()])

    html = """<!DOCTYPE html><html>
    <head><title>{{ portfolj.namn }}</title><meta charset="utf-8">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>""" + BASE_STYLE + PORTFOLIO_STYLE + """
    <style>
        .modal { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000; justify-content:center; align-items:center; }
        .modal.visa { display:flex; }
        .modal-box { background:#fff; border-radius:10px; padding:28px; width:420px; max-width:95vw; }
        .modal-box h3 { font-size:1.1em; margin-bottom:16px; color:#1F3864; }
        .chart-container { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:24px; }
        .chart-box { background:#fff; border-radius:8px; padding:16px; border:1px solid #ddd; }
        .chart-box h3 { font-size:0.88em; color:#1F3864; font-weight:bold; margin-bottom:12px; text-transform:uppercase; letter-spacing:0.05em; }
    </style>
    </head><body>""" + NAV_HTML + """

        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
            <h1>Portfölj</h1>
            <a href="/portfolio/ny-sida" style="padding:7px 16px; background:#1F3864; color:#fff; border-radius:6px; text-decoration:none; font-size:0.88em;">+ Ny portfölj</a>
        </div>

        <div class="portfolj-tabs">
        {% for p in all_portfolios %}
            <a href="/portfolio/{{ p.id }}" class="portfolj-tab {{ 'aktiv' if p.id == portfolj.id else '' }}">{{ p.namn }}</a>
        {% endfor %}
        </div>

        <div class="kpi-rad">
            <div class="kpi-box"><div class="etikett">Marknadsvärde</div><div class="varde">{{ "{:,.0f}".format(total_mv).replace(",", " ") }} SEK</div></div>
            <div class="kpi-box"><div class="etikett">Anskaffningsvärde</div><div class="varde">{{ "{:,.0f}".format(total_cost).replace(",", " ") }} SEK</div></div>
            <div class="kpi-box">
                <div class="etikett">Orealiserat</div>
                <div class="varde {{ 'pos' if totalt_unrealized > 0 else 'neg' }}">{{ "{:,.0f}".format(totalt_unrealized).replace(",", " ") }} SEK</div>
            </div>
            <div class="kpi-box">
                <div class="etikett">Avkastning</div>
                <div class="varde {{ 'pos' if total_pct > 0 else 'neg' }}">{{ "%+.1f"|format(total_pct) }}%</div>
            </div>
        </div>

        <div class="chart-container">
            <div class="chart-box">
                <h3>Tillgångsslag</h3>
                <canvas id="donut1" height="200"></canvas>
            </div>
            <div class="chart-box">
                <h3>Valutaexponering</h3>
                <canvas id="donut2" height="200"></canvas>
            </div>
        </div>

        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
            <div class="tb-header" style="border-radius:8px; flex:1;">Innehav ({{ innehav|length }} st)</div>
            {% if not is_merged %}
            <button onclick="document.getElementById('modal-lagg-till').classList.add('visa')"
                style="margin-left:12px; padding:8px 18px; background:#1F3864; color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:0.88em;">
                + Lägg till
            </button>
            {% endif %}
        </div>

        <table class="tb-table" style="margin-bottom:24px;">
            <thead><tr>
                <th>Värdepapper</th><th>Typ</th><th>Antal</th>
                <th>Kurs</th><th>Marknadsvärde</th><th>Ansk.</th>
                <th>Orealiserat</th><th>%</th><th>Dag%</th><th></th>
            </tr></thead>
            <tbody>
            {% for h in innehav %}
            <tr>
                <td><a href="/portfolio/innehav/{{ h.id }}" style="color:#1F3864; font-weight:bold; text-decoration:none;">{{ h.namn }}</a>
                    <br><span style="color:#999; font-size:0.78em;">{{ h.ticker }}</span></td>
                <td><span class="badge-typ">{{ h.tillgangsslag[:10] }}</span></td>
                <td>{{ "%.2f"|format(h.antal) }}</td>
                <td>{{ "%.2f"|format(h.kurs) }}</td>
                <td>{{ "{:,.0f}".format(h.mv).replace(",", " ") }}</td>
                <td>{{ "{:,.0f}".format(h.anskaffning).replace(",", " ") }}</td>
                <td class="{{ 'pos' if h.unrealized > 0 else 'neg' }}">{{ "{:,.0f}".format(h.unrealized).replace(",", " ") }}</td>
                <td class="{{ 'pos' if h.unrealized_pct > 0 else 'neg' }}">{{ "%+.1f"|format(h.unrealized_pct) }}%</td>
                <td class="{{ 'pos' if h.daily_change > 0 else 'neg' }}">{{ "%+.1f"|format(h.daily_change) }}%</td>
                <td style="white-space:nowrap;">
                    <button onclick="openSellModal({{ h.id }}, '{{ h.namn }}', {{ h.antal }})"
                        style="padding:3px 8px; background:#888; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:0.78em; margin-right:4px;">Sälj</button>
                    <form method="POST" action="/portfolio/innehav/{{ h.id }}/ta-bort" style="display:inline;"
                        onsubmit="return confirm('Ta bort {{ h.namn }}?')">
                        <button type="submit" style="padding:3px 8px; background:#cc0000; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:0.78em;">✕</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>

        {% if not is_merged %}
        <div style="margin-bottom:24px;">
            <div class="tb-header" style="border-radius:8px 8px 0 0;">Importera från Excel</div>
            <div style="background:#fff; padding:16px; border-radius:0 0 8px 8px; border:1px solid #eee;">
                <form method="POST" action="/portfolio/{{ portfolj.id }}/importera-excel" enctype="multipart/form-data">
                    <div style="display:flex; gap:12px; align-items:center;">
                        <input type="file" name="fil" accept=".xlsx,.xls" style="font-size:0.9em;">
                        <button type="submit" style="padding:8px 18px; background:#1F3864; color:#fff; border:none; border-radius:6px; cursor:pointer; white-space:nowrap; font-size:0.88em;">Importera</button>
                    </div>
                    <p style="color:#888; font-size:0.78em; margin-top:6px;">Ladda upp din Excel-fil (Portfoljuppfoljning_Gena.xlsx). Innehav och transaktioner importeras automatiskt.</p>
                </form>
            </div>
        </div>
        {% endif %}

        <!-- Modal: Lägg till innehav -->
        <div id="modal-lagg-till" class="modal" onclick="if(event.target===this)this.classList.remove('visa')">
            <div class="modal-box">
                <h3>+ Lägg till innehav</h3>
                <div class="search-wrapper" style="margin-bottom:12px;">
                    <label style="display:block; color:#666; font-size:0.8em; font-weight:bold; margin-bottom:3px;">Sök värdepapper</label>
                    <input type="text" id="search-input" placeholder="t.ex. Investor, AAPL, SPY..." autocomplete="off"
                        style="width:100%; padding:9px 12px; border:1px solid #ccc; border-radius:6px; font-size:0.95em;">
                    <div id="search-results" class="search-results"></div>
                </div>
                <form method="POST" action="/portfolio/{{ portfolj.id }}/lagg-till">
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;">
                        <div class="fg"><label>Namn</label><input type="text" name="namn" id="f-namn" required></div>
                        <div class="fg"><label>Yahoo Ticker</label><input type="text" name="ticker" id="f-ticker" required></div>
                        <div class="fg"><label>Typ</label>
                            <select name="tillgangsslag" id="f-typ">
                                <option>Aktie</option><option>ETF</option><option>Fond</option>
                                <option>Råvara</option><option>Obligation</option><option>Kassa</option>
                            </select>
                        </div>
                        <div class="fg"><label>Valuta</label>
                            <select name="valuta">
                                <option>SEK</option><option>USD</option><option>EUR</option><option>DKK</option><option>NOK</option>
                            </select>
                        </div>
                        <div class="fg"><label>Antal</label><input type="number" step="0.001" name="antal" required placeholder="100"></div>
                        <div class="fg"><label>Köpkurs</label><input type="number" step="0.01" name="kurs" required placeholder="350.50"></div>
                        <div class="fg"><label>Köpdatum</label><input type="date" name="datum" required value="{{ today }}"></div>
                        <div class="fg"><label>Notering</label><input type="text" name="notering" placeholder="Valfritt"></div>
                    </div>
                    <div style="display:flex; gap:10px;">
                        <button type="submit" style="padding:9px 22px; background:#1F3864; color:#fff; border:none; border-radius:6px; font-weight:bold; cursor:pointer;">Lägg till</button>
                        <button type="button" onclick="document.getElementById('modal-lagg-till').classList.remove('visa')"
                            style="padding:9px 18px; background:#eee; border:none; border-radius:6px; cursor:pointer;">Avbryt</button>
                    </div>
                </form>
            </div>
        </div>

        <!-- Modal: Sälj -->
        <div id="modal-salj" class="modal" onclick="if(event.target===this)this.classList.remove('visa')">
            <div class="modal-box">
                <h3 id="salj-titel">Sälj</h3>
                <form method="POST" id="salj-form" action="">
                    <input type="hidden" name="typ" value="SALJ">
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:14px;">
                        <div class="fg"><label>Antal att sälja</label><input type="number" step="0.001" name="antal" id="salj-antal" required placeholder="0"></div>
                        <div class="fg"><label>Säljkurs</label><input type="number" step="0.01" name="kurs" required placeholder="0"></div>
                        <div class="fg"><label>Datum</label><input type="date" name="datum" required value="{{ today }}"></div>
                        <div class="fg"><label>Notering</label><input type="text" name="notering" placeholder="Valfritt"></div>
                    </div>
                    <div style="display:flex; gap:10px;">
                        <button type="submit" style="padding:9px 22px; background:#cc6600; color:#fff; border:none; border-radius:6px; font-weight:bold; cursor:pointer;">Registrera försäljning</button>
                        <button type="button" onclick="document.getElementById('modal-salj').classList.remove('visa')"
                            style="padding:9px 18px; background:#eee; border:none; border-radius:6px; cursor:pointer;">Avbryt</button>
                    </div>
                </form>
            </div>
        </div>

        <script>
        // Donut 1 - Tillgångsslag
        new Chart(document.getElementById('donut1').getContext('2d'), {
            type: 'doughnut',
            data: { labels: {{ asset_type_json|safe }}, datasets: [{ data: {{ asset_type_values|safe }},
                backgroundColor: ['#1F3864','#2E5FA3','#4472C4','#9DC3E6','#D9E2F3','#A9C4E4','#6FA8DC','#3D6FA6'] }] },
            options: { plugins: { legend: { position: 'bottom', labels: { font: { size: 10 } } } }, cutout: '60%' }
        });
        // Donut 2 - Valuta
        new Chart(document.getElementById('donut2').getContext('2d'), {
            type: 'doughnut',
            data: { labels: {{ currency_json|safe }}, datasets: [{ data: {{ currency_values|safe }},
                backgroundColor: ['#1F3864','#4472C4','#9DC3E6','#D9E2F3','#6FA8DC'] }] },
            options: { plugins: { legend: { position: 'bottom', labels: { font: { size: 10 } } } }, cutout: '60%' }
        });

        // Sök
        let searchTimer;
        document.getElementById('search-input').addEventListener('input', function() {
            clearTimeout(searchTimer);
            const searchQ = this.value.trim();
            const div = document.getElementById('search-results');
            if (searchQ.length < 2) { div.innerHTML=''; return; }
            searchTimer = setTimeout(() => {
                fetch('/portfolio/sok-ticker?q=' + encodeURIComponent(searchQ))
                    .then(r => r.json())
                    .then(data => {
                        if (!data.length) { div.innerHTML='<div class="search-row" style="color:#888;">Inga träffar</div>'; return; }
                        div.innerHTML = data.map(d => {
                            const t = d.ticker.replace(/"/g,'&quot;');
                            const n = d.namn.replace(/"/g,'&quot;');
                            return '<div class="search-row" onclick=\'selectTicker("' + t + '","' + n + '","' + d.typ + '")\'>'+
                            '<strong>' + d.ticker + '</strong> – ' + d.namn +
                            ' <span style="color:#888;font-size:0.8em;">(' + d.typ + ')</span></div>';
                        }).join('');
                        });
            }, 350);
        });
        function selectTicker(ticker, namn, typ) {
            document.getElementById('f-ticker').value = ticker;
            document.getElementById('f-namn').value = namn;
            document.getElementById('search-input').value = namn + ' (' + ticker + ')';
            document.getElementById('search-results').innerHTML = '';
            const typMap = {'EQUITY':'Aktie','ETF':'ETF','MUTUALFUND':'Fond','COMMODITY':'Råvara'};
            const sel = document.getElementById('f-typ');
            for(let o of sel.options) { if(o.value === (typMap[typ] || 'Aktie')) { o.selected=true; break; } }
        }
        document.addEventListener('click', function(e) {
            if (!e.target.closest('.search-wrapper')) document.getElementById('search-results').innerHTML='';
        });

        // Sälj modal
        function openSellModal(id, namn, maxAntal) {
            document.getElementById('salj-titel').textContent = 'Sälj – ' + namn;
            document.getElementById('salj-form').action = '/portfolio/innehav/' + id + '/transaktion';
            document.getElementById('salj-antal').max = maxAntal;
            document.getElementById('modal-salj').classList.add('visa');
        }
        </script>
    </body></html>"""

    today = datetime.now().strftime("%Y-%m-%d")
    return render_template_string(html, portfolj=portfolj, all_portfolios=all_portfolios,
                                  innehav=innehav, total_mv=total_mv, total_cost=total_cost,
                                  totalt_unrealized=totalt_unrealized, total_pct=total_pct,
                                  asset_type_json=asset_type_json, asset_type_values=asset_type_values,
                                  currency_json=currency_json, currency_values=currency_values,
                                  is_merged=is_merged, today=today)


@app.route("/portfolio/ny", methods=["POST"])
@inloggning_kravs
def portfolio_ny():
    namn = request.form.get("namn", "").strip()
    niva = request.form.get("niva", "Depå")
    merge_with = request.form.getlist("merge_with")
    if not namn:
        return redirect(url_for("portfolio_ny_sida"))
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("INSERT INTO portfoljer (namn, niva, skapad) VALUES (?,?,?)", db_type),
              (namn, niva, datetime.now().strftime("%Y-%m-%d %H:%M")))
    if db_type == "postgres":
        c.execute("SELECT lastval()")
    else:
        c.execute("SELECT last_insert_rowid()")
    new_id = c.fetchone()[0]
    # Spara sammanslagningar
    for del_id in merge_with:
        c.execute(q("INSERT INTO portfolj_sammanslagning (total_portfolj_id, del_portfolj_id) VALUES (?,?)", db_type),
                  (new_id, int(del_id)))
    conn.commit()
    conn.close()
    return redirect(url_for("portfolio_vy", portfolio_id=new_id))



@app.route("/portfolio/<int:portfolio_id>/ta-bort/<int:holding_id>", methods=["POST"])
@inloggning_kravs
def portfolio_ta_bort_innehav(portfolio_id, holding_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("DELETE FROM innehav WHERE id=? AND portfolj_id=?", db_type), (holding_id, portfolio_id))
    conn.commit()
    conn.close()
    return redirect(url_for("portfolio_vy", portfolio_id=portfolio_id))


@app.route("/portfolio/<int:portfolio_id>/ta-bort-portfolj", methods=["POST"])
@inloggning_kravs
def portfolio_ta_bort(portfolio_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("DELETE FROM portfolj_innehav WHERE portfolj_id=?", db_type), (portfolio_id,))
    c.execute(q("DELETE FROM portfoljer WHERE id=?", db_type), (portfolio_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("portfolio_sida"))



@app.route("/portfolio/sok-ticker")
@inloggning_kravs
def portfolio_sok_ticker():
    q_str = request.args.get("q", "").strip()
    if len(q_str) < 2:
        return jsonify([])
    try:
        import requests as _req
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={q_str}&lang=en-US&region=SE&quotesCount=8&newsCount=0"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = _req.get(url, headers=headers, timeout=5)
        data = r.json()
        resultat = []
        for t in data.get("quotes", []):
            if t.get("symbol"):
                resultat.append({
                    "ticker": t.get("symbol", ""),
                    "namn": t.get("longname") or t.get("shortname", t.get("symbol", "")),
                    "typ": t.get("quoteType", "")
                })
        return jsonify(resultat[:6])
    except Exception as e:
        return jsonify([])


@app.route("/portfolio/<int:portfolio_id>/lagg-till", methods=["POST"])
@inloggning_kravs
def portfolio_lagg_till(portfolio_id):
    namn        = request.form.get("namn", "").strip()
    ticker      = request.form.get("ticker", "").strip().upper()
    asset_type = request.form.get("tillgangsslag", "Aktie")
    valuta      = request.form.get("valuta", "SEK")
    antal       = float(request.form.get("antal", 0))
    kurs        = float(request.form.get("kurs", 0))
    datum       = request.form.get("datum", datetime.now().strftime("%Y-%m-%d"))
    notering    = request.form.get("notering", "")

    conn, db_type = get_conn()
    c = conn.cursor()

    # Kolla om ticker redan finns i portföljen
    c.execute(q("SELECT id FROM innehav WHERE portfolj_id=? AND ticker=?", db_type), (portfolio_id, ticker))
    existing_holding = c.fetchone()

    if existing_holding:
        holding_id = existing_holding[0]
    else:
        c.execute(q("INSERT INTO innehav (portfolj_id, namn, ticker, tillgangsslag, valuta, skapad) VALUES (?,?,?,?,?,?)", db_type),
                  (portfolio_id, namn, ticker, asset_type, valuta, datetime.now().strftime("%Y-%m-%d %H:%M")))
        if db_type == "postgres":
            c.execute("SELECT lastval()")
        else:
            c.execute("SELECT last_insert_rowid()")
        holding_id = c.fetchone()[0]

    c.execute(q("INSERT INTO transaktioner (innehav_id, typ, antal, kurs, datum, notering, skapad) VALUES (?,?,?,?,?,?,?)", db_type),
              (holding_id, "KOP", antal, kurs, datum, notering, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()
    return redirect(url_for("portfolio_vy", portfolio_id=portfolio_id))


@app.route("/portfolio/innehav/<int:holding_id>")
@inloggning_kravs
def portfolio_innehav_detalj(holding_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("SELECT i.id, i.namn, i.ticker, i.tillgangsslag, i.valuta, i.portfolj_id FROM innehav i WHERE i.id=?", db_type), (holding_id,))
    rad = c.fetchone()
    if not rad:
        conn.close()
        return redirect(url_for("portfolio_sida"))
    iid, namn, ticker, tillgangsslag, valuta, portfolio_id = rad

    c.execute(q("SELECT typ, antal, kurs, datum, notering FROM transaktioner WHERE innehav_id=? ORDER BY datum", db_type), (holding_id,))
    transaktioner = [{"typ": r[0], "antal": r[1], "kurs": r[2], "datum": r[3], "notering": r[4]} for r in c.fetchall()]
    conn.close()

    # Hämta historisk data
    try:
        df = yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=True)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        kurs_nu = float(df["Close"].iloc[-1])
        x_labels = [d.strftime("%d %b") for d in df.index]
        kurs_data = [round(float(v), 2) for v in df["Close"].squeeze()]

        # Beräkna snittköpkurs
        tot_antal = sum(t["antal"] if t["typ"]=="KOP" else -t["antal"] for t in transaktioner)
        tot_kost = sum(t["antal"]*t["kurs"] if t["typ"]=="KOP" else -t["antal"]*t["kurs"] for t in transaktioner)
        avg_price = tot_kost / tot_antal if tot_antal else 0
        mv = round(tot_antal * kurs_nu, 0)
        unrealized = round(mv - tot_kost, 0)
        unrealized_pct = round(unrealized / tot_kost * 100, 1) if tot_kost else 0
    except Exception as e:
        kurs_nu = 0; x_labels = []; kurs_data = []; avg_price = 0
        mv = 0; unrealized = 0; unrealized_pct = 0; tot_antal = 0; tot_kost = 0

    import json as _json
    html = """<!DOCTYPE html><html>
    <head><title>{{ namn }}</title><meta charset="utf-8">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>""" + BASE_STYLE + PORTFOLIO_STYLE + """
    <style>.modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:1000;justify-content:center;align-items:center;}.modal.visa{display:flex;}.modal-box{background:#fff;border-radius:10px;padding:28px;width:420px;max-width:95vw;}.modal-box h3{font-size:1.1em;margin-bottom:16px;color:#1F3864;}</style>
    </head><body>""" + NAV_HTML + """
        <div style="margin-bottom:14px;">
            <a href="/portfolio/{{ portfolio_id }}" style="color:#1F3864; font-size:0.88em;">← Tillbaka till portfölj</a>
        </div>
        <div style="display:flex; align-items:center; gap:14px; margin-bottom:4px;">
            <h1 style="margin:0;">{{ namn }} <span style="color:#888; font-size:0.7em; font-weight:normal;">{{ ticker }}</span></h1>
            <button onclick="document.getElementById('modal-ticker').classList.add('visa')"
                style="padding:4px 12px; background:#eee; border:1px solid #ccc; border-radius:5px; cursor:pointer; font-size:0.8em; white-space:nowrap;">Redigera ticker</button>
        </div>
        <div class="kpi-rad" style="margin-top:14px;">
            <div class="kpi-box"><div class="etikett">Aktuell kurs</div><div class="varde">{{ "%.2f"|format(kurs_nu) }} {{ valuta }}</div></div>
            <div class="kpi-box"><div class="etikett">Antal</div><div class="varde">{{ "%.3f"|format(tot_antal) }}</div></div>
            <div class="kpi-box"><div class="etikett">Snittköpkurs</div><div class="varde">{{ "%.2f"|format(avg_price) }}</div></div>
            <div class="kpi-box"><div class="etikett">Marknadsvärde</div><div class="varde">{{ "{:,.0f}".format(mv).replace(",", " ") }}</div></div>
            <div class="kpi-box"><div class="etikett">Orealiserat</div>
                <div class="varde {{ 'pos' if unrealized > 0 else 'neg' }}">{{ "{:,.0f}".format(unrealized).replace(",", " ") }} ({{ "%+.1f"|format(unrealized_pct) }}%)</div>
            </div>
        </div>

        <div style="background:#fff; border-radius:8px; padding:20px; border:1px solid #ddd; margin-bottom:24px;">
            <canvas id="kursgraf" height="120"></canvas>
        </div>

        <div class="tb-section">
            <div class="tb-header">Transaktioner</div>
            <table class="tb-table">
                <thead><tr><th>Typ</th><th>Datum</th><th>Antal</th><th>Kurs</th><th>Värde</th><th>Notering</th></tr></thead>
                <tbody>
                {% for t in transaktioner %}
                <tr>
                    <td><span style="color:{{ '#007700' if t.typ=='KOP' else '#cc0000' }}; font-weight:bold;">{{ t.typ }}</span></td>
                    <td>{{ t.datum }}</td>
                    <td>{{ "%.3f"|format(t.antal) }}</td>
                    <td>{{ "%.2f"|format(t.kurs) }}</td>
                    <td>{{ "{:,.0f}".format(t.antal*t.kurs).replace(",", " ") }}</td>
                    <td style="color:#888;">{{ t.notering or '' }}</td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>

        <!-- Modal: Redigera ticker -->
        <div id="modal-ticker" class="modal" onclick="if(event.target===this)this.classList.remove('visa')">
            <div class="modal-box">
                <h3>Redigera Yahoo Finance-ticker</h3>
                <p style="color:#666; font-size:0.85em; margin-bottom:14px;">Ange korrekt Yahoo Finance-ticker (t.ex. ASTRA.ST, AAPL, SWED-A.ST)</p>
                <form method="POST" action="/portfolio/innehav/{{ holding_id }}/redigera-ticker">
                    <div class="fg" style="margin-bottom:14px;">
                        <label>Ticker</label>
                        <input type="text" name="ticker" value="{{ ticker }}" required style="text-transform:uppercase;">
                    </div>
                    <div style="display:flex; gap:10px;">
                        <button type="submit" style="padding:9px 22px; background:#1F3864; color:#fff; border:none; border-radius:6px; font-weight:bold; cursor:pointer;">Spara</button>
                        <button type="button" onclick="document.getElementById('modal-ticker').classList.remove('visa')"
                            style="padding:9px 18px; background:#eee; border:none; border-radius:6px; cursor:pointer;">Avbryt</button>
                    </div>
                </form>
            </div>
        </div>

        <script>
        new Chart(document.getElementById('kursgraf').getContext('2d'), {
            type: 'line',
            data: {
                labels: {{ x_labels_json|safe }},
                datasets: [{
                    label: '{{ namn }}',
                    data: {{ kurs_data_json|safe }},
                    borderColor: '#1F3864', backgroundColor: 'rgba(31,56,100,0.07)',
                    borderWidth: 2, pointRadius: 0, fill: true, tension: 0.3
                }, {
                    label: 'Snittköpkurs {{ "%.2f"|format(avg_price) }}',
                    data: Array({{ x_labels_json|safe }}.length).fill({{ avg_price }}),
                    borderColor: '#cc6600', borderWidth: 1.5, borderDash: [5,5],
                    pointRadius: 0, fill: false
                }]
            },
            options: {
                plugins: { legend: { position: 'top' } },
                scales: { x: { ticks: { maxTicksLimit: 10 } }, y: { position: 'right' } }
            }
        });
        </script>
    </body></html>"""

    return render_template_string(html,
        namn=namn, ticker=ticker, valuta=valuta, portfolio_id=portfolio_id,
        holding_id=holding_id,
        kurs_nu=kurs_nu, tot_antal=tot_antal, avg_price=avg_price,
        mv=mv, unrealized=unrealized, unrealized_pct=unrealized_pct,
        transaktioner=transaktioner,
        x_labels_json=_json.dumps(x_labels),
        kurs_data_json=_json.dumps(kurs_data))



@app.route("/portfolio/innehav/<int:holding_id>/ta-bort", methods=["POST"])
@inloggning_kravs
def innehav_ta_bort(holding_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("SELECT portfolj_id FROM innehav WHERE id=?", db_type), (holding_id,))
    rad = c.fetchone()
    portfolio_id = rad[0] if rad else 1
    c.execute(q("DELETE FROM transaktioner WHERE innehav_id=?", db_type), (holding_id,))
    c.execute(q("DELETE FROM innehav WHERE id=?", db_type), (holding_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("portfolio_vy", portfolio_id=portfolio_id))


@app.route("/portfolio/innehav/<int:holding_id>/redigera-ticker", methods=["POST"])
@inloggning_kravs
def innehav_redigera_ticker(holding_id):
    ticker = request.form.get("ticker", "").strip().upper()
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("UPDATE innehav SET ticker=? WHERE id=?", db_type), (ticker, holding_id))
    conn.commit()
    conn.close()
    return redirect(url_for("portfolio_innehav_detalj", holding_id=holding_id))


@app.route("/portfolio/innehav/<int:holding_id>/transaktion", methods=["POST"])
@inloggning_kravs
def innehav_transaktion(holding_id):
    """Registrerar köp eller försäljning."""
    typ    = request.form.get("typ", "KOP")
    antal  = float(request.form.get("antal", 0))
    kurs   = float(request.form.get("kurs", 0))
    datum  = request.form.get("datum", datetime.now().strftime("%Y-%m-%d"))
    notering = request.form.get("notering", "")

    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("SELECT portfolj_id, valuta FROM innehav WHERE id=?", db_type), (holding_id,))
    rad = c.fetchone()
    portfolio_id = rad[0] if rad else 1
    valuta = rad[1] if rad else "SEK"

    # Hämta FX-kurs automatiskt om inte SEK
    fx_rate = 1.0
    if valuta != "SEK":
        try:
            fx_ticker = {"USD": "USDSEK=X", "EUR": "EURSEK=X", "DKK": "DKKSEK=X", "NOK": "NOKSEK=X"}.get(valuta)
            if fx_ticker:
                fx_df = yf.download(fx_ticker, start=datum, end=datum, progress=False, auto_adjust=True)
                if not fx_df.empty:
                    fx_rate = float(fx_df["Close"].iloc[0])
        except:
            pass

    c.execute(q("INSERT INTO transaktioner (innehav_id, typ, antal, kurs, fx_rate, datum, notering, skapad) VALUES (?,?,?,?,?,?,?,?)", db_type),
              (holding_id, typ, antal, kurs, fx_rate, datum, notering, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()
    return redirect(url_for("portfolio_vy", portfolio_id=portfolio_id))


@app.route("/portfolio/<int:portfolio_id>/importera-excel", methods=["POST"])
@inloggning_kravs
def portfolio_importera_excel(portfolio_id):
    """Importerar innehav från Excel-fil."""
    if "fil" not in request.files:
        return redirect(url_for("portfolio_vy", portfolio_id=portfolio_id))
    fil = request.files["fil"]
    if not fil.filename:
        return redirect(url_for("portfolio_vy", portfolio_id=portfolio_id))
    try:
        import io
        df = pd.read_excel(fil, sheet_name="Innehav", header=2)
        df.columns = ["depa","namn","antal","kurs","valuta","fx","mv","anskaffning","unrealized","unrealized_pct","andel","tillgangsslag"]
        df = df.dropna(subset=["namn","antal","kurs"])
        conn, db_type = get_conn()
        c = conn.cursor()
        imported_count = 0
        for _, row in df.iterrows():
            namn = str(row["namn"]).strip()
            if not namn or namn == "nan":
                continue
            antal = float(row["antal"]) if pd.notna(row["antal"]) else 0
            kurs = float(row["kurs"]) if pd.notna(row["kurs"]) else 0
            valuta = str(row["valuta"]).strip() if pd.notna(row["valuta"]) else "SEK"
            cost_basis = float(row["anskaffning"]) if pd.notna(row["anskaffning"]) else antal * kurs
            asset_type = str(row["tillgangsslag"]).strip() if pd.notna(row["tillgangsslag"]) else "Aktie"
            # Förenkla asset_type
            if "fond" in asset_type.lower(): asset_type = "Fond"
            elif "etf" in asset_type.lower() or "råvara" in asset_type.lower(): asset_type = "ETF"
            elif "invest" in asset_type.lower(): asset_type = "Aktie"
            else: asset_type = "Aktie"
            # Ticker = namn som placeholder, kan ändras
            ticker = namn.upper().replace(" ", "-")[:10]
            # Spara innehav
            c.execute(q("INSERT INTO innehav (portfolj_id, namn, ticker, tillgangsslag, valuta, skapad) VALUES (?,?,?,?,?,?)", db_type),
                      (portfolio_id, namn, ticker, asset_type, valuta, datetime.now().strftime("%Y-%m-%d %H:%M")))
            if db_type == "postgres":
                c.execute("SELECT lastval()")
            else:
                c.execute("SELECT last_insert_rowid()")
            iid = c.fetchone()[0]
            avg_price = cost_basis / antal if antal else kurs
            c.execute(q("INSERT INTO transaktioner (innehav_id, typ, antal, kurs, fx_rate, datum, notering, skapad) VALUES (?,?,?,?,?,?,?,?)", db_type),
                      (iid, "KOP", antal, avg_price, 1.0, datetime.now().strftime("%Y-%m-%d"), "Importerad från Excel", datetime.now().strftime("%Y-%m-%d %H:%M")))
            imported_count += 1
        conn.commit()
        conn.close()
        return redirect(url_for("portfolio_vy", portfolio_id=portfolio_id))
    except Exception as e:
        return f"Import-fel: {str(e)}", 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
