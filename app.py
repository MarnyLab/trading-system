from flask import Flask, render_template_string, request, jsonify, redirect, url_for, session
import yfinance as yf
import ta
import pandas as pd
from datetime import datetime
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import anthropic
import os
import sqlite3
import json
import base64
import email as email_lib
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
DB_PATH = "trading.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS konversationer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            datum TEXT NOT NULL,
            titel TEXT,
            meddelanden TEXT NOT NULL,
            marknadsdata TEXT,
            skapad TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS dokument (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filnamn TEXT NOT NULL,
            innehall TEXT NOT NULL,
            uppladdad TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS gmail_token (
            id INTEGER PRIMARY KEY,
            token_json TEXT NOT NULL,
            uppdaterad TEXT NOT NULL
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
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_REDIRECT_URI = "https://trading-system-r7ii.onrender.com/gmail/callback"

def hamta_gmail_credentials():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT token_json FROM gmail_token WHERE id=1")
    rad = c.fetchone()
    conn.close()
    if not rad or not GMAIL_AVAILABLE:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(rad[0]), GMAIL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        spara_gmail_token(creds)
    return creds

def spara_gmail_token(creds):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO gmail_token (id, token_json, uppdaterad) VALUES (1, ?, ?)",
              (creds.to_json(), datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()

def spara_konversation(titel, meddelanden, marknadsdata):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    datum  = datetime.now().strftime("%Y-%m-%d")
    skapad = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute(
        "INSERT INTO konversationer (datum, titel, meddelanden, marknadsdata, skapad) VALUES (?, ?, ?, ?, ?)",
        (datum, titel, json.dumps(meddelanden, ensure_ascii=False), marknadsdata, skapad)
    )
    conn.commit()
    conn.close()

def hamta_tidigare_konversationer(antal=3):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT datum, titel, meddelanden, marknadsdata FROM konversationer ORDER BY id DESC LIMIT ?", (antal,))
    rader = c.fetchall()
    conn.close()
    resultat = []
    for rad in rader:
        msgs = json.loads(rad[2])
        resultat.append({"datum": rad[0], "titel": rad[1], "meddelanden": msgs, "marknadsdata": rad[3]})
    return resultat

def hamta_alla_konversationer():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, datum, titel, skapad FROM konversationer ORDER BY id DESC")
    rader = c.fetchall()
    conn.close()
    return [{"id": r[0], "datum": r[1], "titel": r[2], "skapad": r[3]} for r in rader]

def spara_dokument(filnamn, innehall):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    uppladdad = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO dokument (filnamn, innehall, uppladdad) VALUES (?, ?, ?)", (filnamn, innehall, uppladdad))
    conn.commit()
    conn.close()

def hamta_alla_dokument():
    conn = sqlite3.connect(DB_PATH)
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

def sammanfatta(namn, df):
    senaste      = float(df["Close"].iloc[-1])
    ix           = min(22, len(df) - 1)
    for_en_manad = float(df["Close"].iloc[-ix])
    forandring   = (senaste - for_en_manad) / for_en_manad * 100
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
            df = hamta_data(ticker, yf_period="3mo", yf_interval="1d")
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
                        row_heights=[0.60, 0.20, 0.20], vertical_spacing=0.02,
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
        df = hamta_data(ticker, yf_period="6mo", yf_interval="1d")
        kort.append(sammanfatta(namn, df))
    uppdaterad = datetime.now().strftime("%Y-%m-%d %H:%M")
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
    </style></head>
    <body>""" + NAV_HTML + """
        <h1>Trading Dashboard</h1>
        <p class="uppdaterad">Uppdaterad: {{ uppdaterad }}</p>
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
    </body></html>"""
    return render_template_string(html, kort=kort, uppdaterad=uppdaterad)


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
    s  = sammanfatta(namn, df)
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
            {{ "%+.2f"|format(s.forandring) }}% senaste perioden
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
                                  period_opts=period_opts, range_opts=range_opts)


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
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT filnamn, innehall FROM dokument WHERE id=?", (dokument_id,))
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT datum, titel, meddelanden, marknadsdata, skapad FROM konversationer WHERE id=?", (konv_id,))
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT filnamn, innehall, uppladdad FROM dokument WHERE id=?", (dok_id,))
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
