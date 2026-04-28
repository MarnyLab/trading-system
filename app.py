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
        CREATE TABLE IF NOT EXISTS dagliga_analyser (
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
            portfolj_id INTEGER NOT NULL,
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
    <a href="/daglig-analys" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Daglig Analys</a>
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
    c.execute("SELECT id, namn, niva FROM portfoljer ORDER BY niva, namn")
    portfoljer = [{"id": r[0], "namn": r[1], "niva": r[2]} for r in c.fetchall()]
    for p in portfoljer:
        c.execute(q("SELECT ticker, namn FROM portfolj_innehav WHERE portfolj_id=?", db_type), (p["id"],))
        innehav = c.fetchall()
        total_forandring = []
        for ticker, _ in innehav:
            kurs, daglig = hamta_portfolj_kurs(ticker)
            if daglig is not None:
                total_forandring.append(daglig)
        p["antal"] = len(innehav)
        p["snitt_daglig"] = round(sum(total_forandring)/len(total_forandring), 2) if total_forandring else 0
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
            <a class="portfolj-kort" href="/portfolio/{{ p.id }}">
                <div class="niva-badge" style="background:{{ niva_farger.get(p.niva, '#888') }};">{{ p.niva }}</div>
                <div style="font-size:1.1em; font-weight:bold; color:#111; margin-bottom:6px;">{{ p.namn }}</div>
                <div class="rad"><span class="etikett">Innehav</span><span>{{ p.antal }} st</span></div>
                <div class="rad"><span class="etikett">Daglig förändring</span>
                    <span style="color:{{ '#007700' if p.snitt_daglig > 0 else '#cc0000' }}">
                        {{ "%+.2f"|format(p.snitt_daglig) }}%
                    </span>
                </div>
                <div class="detalj-btn" style="margin-top:12px;">Öppna portfölj</div>
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


@app.route("/cron/daglig-analys")
def cron_daglig_analys():
    import threading
    t = threading.Thread(target=kör_daglig_analys)
    t.daemon = True
    t.start()
    return jsonify({"status": "started", "meddelande": "Daglig analys körs i bakgrunden."})


def kör_daglig_analys():
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
        prompt = "Generera en daglig marknadsanalys för " + datetime.now().strftime("%Y-%m-%d") + ".\n\nMarknadsdata:\n" + marknadsdata + "\n\nNyhetsbrev och analyser:\n" + (dok_kontext if dok_kontext else "Inga nya dokument idag.") + "\n\nNya mejl idag: " + (", ".join(nya_dokument) if nya_dokument else "Inga") + "\n\nGe en strukturerad analys med:\n1. Sammanfattning av marknadsläget\n2. Vad nyhetsbreven säger\n3. Observationer per index\n4. Eventuella köp/säljsignaler"

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
        c.execute(q("INSERT INTO dagliga_analyser (datum, analys, skapad) VALUES (?, ?, ?)", db_type),
                  (datetime.now().strftime("%Y-%m-%d"), analys_text, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()

    except Exception as e:
        print(f"Cron fel: {str(e)}")


@app.route("/daglig-analys")
@inloggning_kravs
def daglig_analys_sida():
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, datum, analys, skapad FROM dagliga_analyser ORDER BY id DESC LIMIT 10")
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
            <p style="color:#888;">Ingen daglig analys ännu. Aktivera Cron Job på Render för automatisk körning.</p>
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

NIVA_FARGER = {"Konservativ": "#007700", "Balanserad": "#0044cc", "Aggressiv": "#cc5500"}

def hamta_portfolj_kurs(ticker):
    try:
        df = yf.download(ticker, period="5d", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return None, None
        kurs = float(df["Close"].iloc[-1])
        if len(df) >= 2:
            prev  = float(df["Close"].iloc[-2])
            daglig = (kurs - prev) / prev * 100
        else:
            daglig = 0.0
        return kurs, daglig
    except:
        return None, None


@app.route("/portfolio")
@inloggning_kravs
def portfolio_sida():
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, namn, niva, skapad FROM portfoljer ORDER BY niva, namn")
    rader = c.fetchall()
    conn.close()
    portfoljer = [{"id": r[0], "namn": r[1], "niva": r[2], "skapad": r[3]} for r in rader]

    html = """<!DOCTYPE html><html>
    <head><title>Portföljer</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .ny-form { background:#fff; border-radius:10px; padding:22px; border:1px solid #ddd; max-width:480px; margin-bottom:30px; }
        .fg { margin-bottom:12px; }
        .fg label { display:block; color:#888; font-size:0.82em; font-weight:bold; margin-bottom:4px; }
        .fg input, .fg select { width:100%; padding:9px 12px; border:1px solid #ccc; border-radius:6px; font-size:0.95em; }
        .p-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(240px,1fr)); gap:16px; }
        .p-kort { background:#fff; border-radius:10px; padding:20px; border:1px solid #ddd; cursor:pointer; transition: box-shadow 0.15s; }
        .p-kort:hover { box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .niva-badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:0.8em; font-weight:bold; color:#fff; margin-bottom:10px; }
    </style></head>
    <body>""" + NAV_HTML + """
        <h1>Portföljer</h1>
        <div class="ny-form">
            <h2 style="font-size:1em; color:#444; margin-bottom:14px;">+ Ny portfölj</h2>
            <form method="POST" action="/portfolio/ny">
                <div class="fg"><label>Namn</label><input type="text" name="namn" placeholder="Min portfölj" required></div>
                <div class="fg">
                    <label>Risknivå</label>
                    <select name="niva">
                        <option value="Konservativ">Konservativ – lägre risk, stabil avkastning</option>
                        <option value="Balanserad" selected>Balanserad – mix aktier/räntor</option>
                        <option value="Aggressiv">Aggressiv – hög risk, hög potential</option>
                    </select>
                </div>
                <button type="submit" style="padding:9px 24px; background:#0044cc; color:#fff; border:none; border-radius:6px; font-weight:bold; cursor:pointer;">Skapa portfölj</button>
            </form>
        </div>
        {% if portfoljer %}
        <div class="p-grid">
        {% for p in portfoljer %}
            <a href="/portfolio/{{ p.id }}" style="text-decoration:none; color:inherit;">
            <div class="p-kort">
                <div class="niva-badge" style="background:{{ niva_farger.get(p.niva, '#888') }};">{{ p.niva }}</div>
                <div style="font-size:1.15em; font-weight:bold; color:#111; margin-bottom:6px;">{{ p.namn }}</div>
                <div style="color:#888; font-size:0.82em;">Skapad {{ p.skapad[:10] }}</div>
            </div>
            </a>
        {% endfor %}
        </div>
        {% else %}
        <p style="color:#888;">Inga portföljer skapade ännu.</p>
        {% endif %}
    </body></html>"""
    return render_template_string(html, portfoljer=portfoljer, niva_farger=NIVA_FARGER)


@app.route("/portfolio/ny", methods=["POST"])
@inloggning_kravs
def portfolio_ny():
    namn = request.form.get("namn", "").strip()
    niva = request.form.get("niva", "Balanserad")
    if namn:
        conn, db_type = get_conn()
        c = conn.cursor()
        c.execute(q("INSERT INTO portfoljer (namn, niva, skapad) VALUES (?,?,?)", db_type),
                  (namn, niva, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()
    return redirect(url_for("portfolio_sida"))


@app.route("/portfolio/<int:portfolj_id>")
@inloggning_kravs
def portfolio_detalj(portfolj_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("SELECT namn, niva, skapad FROM portfoljer WHERE id=?", db_type), (portfolj_id,))
    prad = c.fetchone()
    if not prad:
        conn.close()
        return "Portfölj hittades inte", 404
    c.execute(q("SELECT id, ticker, namn, andel FROM portfolj_innehav WHERE portfolj_id=? ORDER BY andel DESC", db_type), (portfolj_id,))
    hrader = c.fetchall()
    conn.close()

    innehav = []
    total_daglig = 0.0
    total_andel  = sum(r[3] for r in hrader)
    for hid, ticker, namn_h, andel in hrader:
        kurs, daglig = hamta_portfolj_kurs(ticker)
        vikt = andel / total_andel if total_andel else 0
        if daglig is not None:
            total_daglig += daglig * vikt
        innehav.append({"id": hid, "ticker": ticker, "namn": namn_h,
                         "andel": andel, "kurs": kurs, "daglig": daglig})

    portfolj_namn, niva, skapad = prad
    farv = NIVA_FARGER.get(niva, "#888")

    # Pie chart för allokering
    pie_html = ""
    if innehav:
        fig = go.Figure(go.Pie(
            labels=[h["namn"] or h["ticker"] for h in innehav],
            values=[h["andel"] for h in innehav],
            hole=0.45,
            textinfo="label+percent",
            marker=dict(colors=["#0044cc","#007700","#cc5500","#884488","#886600",
                                  "#008888","#cc0000","#445588","#558844","#885544"])
        ))
        fig.update_layout(
            paper_bgcolor="#ffffff", height=300, margin=dict(l=10,r=10,t=20,b=10),
            showlegend=False
        )
        pie_html = fig.to_html(full_html=False, include_plotlyjs="cdn")

    html = """<!DOCTYPE html><html>
    <head><title>{{ pnamn }}</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .niva-badge { display:inline-block; padding:3px 12px; border-radius:12px; font-size:0.82em; font-weight:bold; color:#fff; }
        .top-info { display:flex; gap:16px; align-items:center; margin-bottom:24px; flex-wrap:wrap; }
        .daglig-total { font-size:1.6em; font-weight:bold; }
        .innehav-form { background:#fff; border-radius:10px; padding:20px; border:1px solid #ddd; max-width:560px; margin-bottom:24px; }
        .fg { margin-bottom:12px; }
        .fg label { display:block; color:#888; font-size:0.82em; font-weight:bold; margin-bottom:4px; }
        .fg input { width:100%; padding:9px 12px; border:1px solid #ccc; border-radius:6px; font-size:0.95em; }
        .row3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }
        .pie-wrap { background:#fff; border-radius:10px; padding:16px; border:1px solid #ddd; max-width:360px; }
        .layout { display:flex; gap:24px; flex-wrap:wrap; align-items:flex-start; }
    </style></head>
    <body>""" + NAV_HTML + """
        <a href="/portfolio" style="color:#888; font-size:0.88em;">← Alla portföljer</a>
        <div class="top-info" style="margin-top:14px;">
            <div>
                <h1 style="margin-bottom:4px;">{{ pnamn }}</h1>
                <span class="niva-badge" style="background:{{ farv }};">{{ niva }}</span>
            </div>
            {% if innehav %}
            <div style="margin-left:auto; text-align:right;">
                <div style="color:#888; font-size:0.82em;">Daglig portföljförändring (vägd)</div>
                <div class="daglig-total" style="color:{{ '#007700' if total_daglig >= 0 else '#cc0000' }}">
                    {{ "%+.2f"|format(total_daglig) }}%
                </div>
            </div>
            {% endif %}
        </div>

        <div class="layout">
            <div style="flex:1; min-width:300px;">
                <div class="innehav-form">
                    <h2 style="font-size:1em; color:#444; margin-bottom:14px;">+ Lägg till innehav</h2>
                    <form method="POST" action="/portfolio/{{ pid }}/lagg-till">
                        <div class="row3">
                            <div class="fg"><label>Ticker</label><input type="text" name="ticker" placeholder="t.ex. XACT" required></div>
                            <div class="fg"><label>Namn/Beskrivning</label><input type="text" name="namn" placeholder="t.ex. XACT OMX"></div>
                            <div class="fg"><label>Andel (%)</label><input type="number" step="0.1" name="andel" placeholder="25" required></div>
                        </div>
                        <button type="submit" style="padding:8px 20px; background:#0044cc; color:#fff; border:none; border-radius:6px; font-weight:bold; cursor:pointer;">Lägg till</button>
                    </form>
                </div>
                {% if innehav %}
                <table class="tabell">
                    <thead><tr><th>Ticker</th><th>Namn</th><th>Andel</th><th>Kurs</th><th>Daglig %</th><th></th></tr></thead>
                    <tbody>
                    {% for h in innehav %}
                    <tr>
                        <td><strong>{{ h.ticker }}</strong></td>
                        <td>{{ h.namn }}</td>
                        <td>{{ "%.1f"|format(h.andel) }}%</td>
                        <td>{% if h.kurs %}{{ "%.2f"|format(h.kurs) }}{% else %}<span style="color:#aaa;">–</span>{% endif %}</td>
                        <td>{% if h.daglig is not none %}
                            <span style="color:{{ '#007700' if h.daglig >= 0 else '#cc0000' }}">{{ "%+.2f"|format(h.daglig) }}%</span>
                            {% else %}<span style="color:#aaa;">–</span>{% endif %}
                        </td>
                        <td>
                            <form method="POST" action="/portfolio/{{ pid }}/ta-bort/{{ h.id }}" style="display:inline;">
                                <button type="submit" style="background:none; border:none; color:#cc0000; cursor:pointer; font-size:0.85em;">✕</button>
                            </form>
                        </td>
                    </tr>
                    {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <p style="color:#888;">Inga innehav ännu. Lägg till ovan.</p>
                {% endif %}
            </div>
            {% if pie_html %}
            <div class="pie-wrap">
                <div style="font-size:0.85em; color:#666; font-weight:bold; margin-bottom:8px;">ALLOKERING</div>
                {{ pie_html|safe }}
            </div>
            {% endif %}
        </div>

        <div style="margin-top:20px;">
            <form method="POST" action="/portfolio/{{ pid }}/ta-bort-portfolj"
                  onsubmit="return confirm('Ta bort hela portföljen?');">
                <button type="submit" style="padding:7px 16px; background:#f0f0f0; color:#cc0000; border:1px solid #ddd; border-radius:6px; font-size:0.85em; cursor:pointer;">Ta bort portfölj</button>
            </form>
        </div>
    </body></html>"""
    return render_template_string(html, pnamn=portfolj_namn, niva=niva, farv=farv,
                                  pid=portfolj_id, innehav=innehav,
                                  total_daglig=total_daglig, pie_html=pie_html)


@app.route("/portfolio/<int:portfolj_id>/lagg-till", methods=["POST"])
@inloggning_kravs
def portfolio_lagg_till(portfolj_id):
    ticker = request.form.get("ticker", "").strip().upper()
    namn   = request.form.get("namn", "").strip()
    andel  = float(request.form.get("andel", 0) or 0)
    if ticker and andel > 0:
        conn, db_type = get_conn()
        c = conn.cursor()
        c.execute(q("INSERT INTO portfolj_innehav (portfolj_id, ticker, namn, andel, skapad) VALUES (?,?,?,?,?)", db_type),
                  (portfolj_id, ticker, namn, andel, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()
    return redirect(url_for("portfolio_detalj", portfolj_id=portfolj_id))


@app.route("/portfolio/<int:portfolj_id>/ta-bort/<int:innehav_id>", methods=["POST"])
@inloggning_kravs
def portfolio_ta_bort_innehav(portfolj_id, innehav_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("DELETE FROM portfolj_innehav WHERE id=? AND portfolj_id=?", db_type), (innehav_id, portfolj_id))
    conn.commit()
    conn.close()
    return redirect(url_for("portfolio_detalj", portfolj_id=portfolj_id))


@app.route("/portfolio/<int:portfolj_id>/ta-bort-portfolj", methods=["POST"])
@inloggning_kravs
def portfolio_ta_bort(portfolj_id):
    conn, db_type = get_conn()
    c = conn.cursor()
    c.execute(q("DELETE FROM portfolj_innehav WHERE portfolj_id=?", db_type), (portfolj_id,))
    c.execute(q("DELETE FROM portfoljer WHERE id=?", db_type), (portfolj_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("portfolio_sida"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
