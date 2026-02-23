from flask import Flask, render_template_string, request, jsonify
import yfinance as yf
import ta
import pandas as pd
from datetime import datetime
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

TICKERS = {
    "OMX30":   "^OMX",
    "S&P500":  "^GSPC",
    "Europa":  "^STOXX50E",
    "Guld":    "GC=F",
}

PERIOD_MAP = {
    "Daily":   "1d",
    "Weekly":  "1wk",
    "Monthly": "1mo",
}

RANGE_MAP = {
    "1 Month":  "1mo",
    "3 Months": "3mo",
    "6 Months": "6mo",
    "1 Year":   "1y",
    "2 Years":  "2y",
    "3 Years":  "3y",
    "5 Years":  "5y",
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
    macd            = ta.trend.MACD(close)
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
    if rsi > 70:
        rsi_text, rsi_farg = "Överköpt", "#cc0000"
    elif rsi < 30:
        rsi_text, rsi_farg = "Översålt", "#007700"
    else:
        rsi_text, rsi_farg = "Neutralt", "#888888"
    if sma50 and sma200:
        if senaste > sma50 > sma200:
            trend, trend_farg = "Stark upptrend", "#007700"
        elif senaste > sma50:
            trend, trend_farg = "Upptrend", "#009900"
        elif senaste < sma50 < sma200:
            trend, trend_farg = "Stark nedtrend", "#cc0000"
        else:
            trend, trend_farg = "Sidledes", "#888888"
    else:
        trend, trend_farg = "För kort data", "#888888"
    macd_signal = "Positiv momentum" if macd_hist > 0 else "Negativ momentum"
    return {
        "namn": namn, "kurs": senaste, "forandring": forandring,
        "rsi": rsi, "rsi_text": rsi_text, "rsi_farg": rsi_farg,
        "trend": trend, "trend_farg": trend_farg, "macd_signal": macd_signal,
        "sma50": round(sma50, 2) if sma50 else "N/A",
        "sma200": round(sma200, 2) if sma200 else "N/A",
        "macd_hist": round(macd_hist, 2),
    }

def hamta_marknadsdata():
    sammanfattningar = []
    for namn, ticker in TICKERS.items():
        try:
            df = hamta_data(ticker, yf_period="3mo", yf_interval="1d")
            s = sammanfatta(namn, df)
            sammanfattningar.append(
                f"{namn}: Kurs {s['kurs']:.2f}, RSI {s['rsi']} ({s['rsi_text']}), "
                f"Trend: {s['trend']}, MACD: {s['macd_signal']}, "
                f"SMA50: {s['sma50']}, SMA200: {s['sma200']}"
            )
        except:
            pass
    return "\n".join(sammanfattningar)

def skapa_diagram(namn, df, interval_label="Daily"):
    if interval_label == "Daily":
        x_labels = [d.strftime("%d %b") for d in df.index]
    elif interval_label == "Weekly":
        x_labels = [d.strftime("%d %b '%y") for d in df.index]
    else:
        x_labels = [d.strftime("%b '%y") for d in df.index]

    close = df["Close"].squeeze()
    open_ = df["Open"].squeeze()

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.60, 0.20, 0.20],
        vertical_spacing=0.02,
        subplot_titles=("", "Volym", "MACD (12,26,9)")
    )

    fig.add_trace(go.Candlestick(
        x=x_labels, open=open_, high=df["High"].squeeze(),
        low=df["Low"].squeeze(), close=close, name="Pris",
        increasing=dict(line=dict(color="#007700", width=1.5), fillcolor="#00aa00"),
        decreasing=dict(line=dict(color="#cc0000", width=1.5), fillcolor="#cc0000"),
    ), row=1, col=1)

    if "EMA20" in df.columns:
        fig.add_trace(go.Scatter(x=x_labels, y=df["EMA20"], name="EMA(20)", line=dict(color="#008800", width=1.5)), row=1, col=1)
    if "SMA50" in df.columns:
        fig.add_trace(go.Scatter(x=x_labels, y=df["SMA50"], name="MA(50)", line=dict(color="#0044cc", width=1.5)), row=1, col=1)
    if "SMA200" in df.columns:
        fig.add_trace(go.Scatter(x=x_labels, y=df["SMA200"], name="MA(200)", line=dict(color="#cc0000", width=1.5)), row=1, col=1)

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


NAV_HTML = """
<nav style="margin-bottom:25px; display:flex; gap:12px;">
    <a href="/" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Dashboard</a>
    <a href="/analytiker" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">AI-Analytiker</a>
    <a href="/riskmotor" style="color:#0044cc; text-decoration:none; padding:7px 16px; background:#f0f0f0; border-radius:6px; border:1px solid #ccc; font-size:0.9em;">Riskmotor</a>
</nav>
"""

BASE_STYLE = """
<style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f4; color: #222; padding: 30px; }
    h1 { font-size: 1.5em; margin-bottom: 5px; color: #111; }
    .uppdaterad { color: #999; font-size: 0.85em; margin-bottom: 25px; }
    a { color: #0044cc; }
    .filter-bar { display: flex; gap: 20px; align-items: flex-end; margin-bottom: 18px; flex-wrap: wrap; }
    .filter-bar label { color: #666; font-size: 0.82em; font-weight: bold; display: block; margin-bottom: 3px; }
    .filter-bar select { padding: 6px 10px; border: 1px solid #ccc; border-radius: 5px; background: #fff; font-size: 0.9em; color: #222; cursor: pointer; }
    .filter-bar button { padding: 7px 18px; background: #0044cc; color: #fff; border: none; border-radius: 5px; font-size: 0.9em; cursor: pointer; }
</style>
"""

@app.route("/")
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
        .kort { background: #fff; border-radius: 10px; padding: 20px; border: 1px solid #ddd; }
        .kort h2 { font-size: 1em; color: #666; margin-bottom: 6px; }
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
                <h2>{{ k.namn }}</h2>
                <div class="kurs">{{ "%.2f"|format(k.kurs) }}</div>
                <div class="forandring" style="color: {{ '#007700' if k.forandring > 0 else '#cc0000' }}">
                    {{ "%+.2f"|format(k.forandring) }}% senaste månaden
                </div>
                <div class="rad"><span class="etikett">Trend</span><span style="color:{{ k.trend_farg }}">{{ k.trend }}</span></div>
                <div class="rad"><span class="etikett">RSI (14)</span><span style="color:{{ k.rsi_farg }}">{{ k.rsi }} - {{ k.rsi_text }}</span></div>
                <div class="rad"><span class="etikett">MACD</span><span>{{ k.macd_signal }}</span></div>
                <div class="rad"><span class="etikett">SMA50</span><span>{{ k.sma50 }}</span></div>
                <div class="rad"><span class="etikett">SMA200</span><span>{{ k.sma200 }}</span></div>
                <a class="detalj-btn" href="/detalj/{{ k.namn }}" target="_blank">Oppna diagram</a>
            </div>
        {% endfor %}
        </div>
    </body></html>"""
    return render_template_string(html, kort=kort, uppdaterad=uppdaterad)


@app.route("/detalj/<namn>")
def detalj(namn):
    ticker = TICKERS.get(namn)
    if not ticker:
        return "Index hittades inte", 404
    aktiv_period = request.args.get("period", "Daily")
    aktiv_range  = request.args.get("range", "6 Months")
    if aktiv_period not in PERIOD_MAP: aktiv_period = "Daily"
    if aktiv_range not in RANGE_MAP: aktiv_range = "6 Months"
    yf_interval = PERIOD_MAP[aktiv_period]
    yf_period   = RANGE_MAP[aktiv_range]
    df = hamta_data(ticker, yf_period=yf_period, yf_interval=yf_interval)
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
def analytiker():
    html = """<!DOCTYPE html><html>
    <head><title>AI-Analytiker</title><meta charset="utf-8">""" + BASE_STYLE + """
    <style>
        .chatt-container { max-width: 800px; }
        .meddelanden { background: #fff; border: 1px solid #ddd; border-radius: 10px; padding: 20px; min-height: 300px; max-height: 500px; overflow-y: auto; margin-bottom: 16px; }
        .msg { margin-bottom: 16px; }
        .msg.user { text-align: right; }
        .msg.user .bubbla { background: #0044cc; color: #fff; display: inline-block; padding: 10px 16px; border-radius: 12px 12px 2px 12px; max-width: 80%; text-align: left; }
        .msg.ai .bubbla { background: #f0f0f0; color: #222; display: inline-block; padding: 10px 16px; border-radius: 12px 12px 12px 2px; max-width: 80%; }
        .msg .avsandare { font-size: 0.78em; color: #999; margin-bottom: 4px; }
        .inmatning { display: flex; gap: 10px; }
        .inmatning textarea { flex: 1; padding: 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 0.95em; font-family: inherit; resize: vertical; min-height: 80px; }
        .inmatning button { padding: 10px 20px; background: #0044cc; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 0.95em; align-self: flex-end; }
        .nyhetsbrev-box { margin-bottom: 16px; }
        .nyhetsbrev-box label { display: block; color: #666; font-size: 0.85em; margin-bottom: 5px; font-weight: bold; }
        .nyhetsbrev-box textarea { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 0.88em; font-family: inherit; resize: vertical; min-height: 100px; }
        .laddning { color: #0044cc; font-style: italic; font-size: 0.9em; }
    </style>
    </head>
    <body>""" + NAV_HTML + """
        <h1>AI-Analytiker</h1>
        <p style="color:#888; margin-bottom:20px; font-size:0.9em;">Klistra in ett nyhetsbrev eller skriv en fråga. Analytikern har tillgång till aktuell marknadsdata.</p>

        <div class="chatt-container">
            <div class="nyhetsbrev-box">
                <label>Klistra in nyhetsbrev eller analys (valfritt)</label>
                <textarea id="nyhetsbrev" placeholder="Klistra in text från nyhetsbrev, analys eller artiklar här..."></textarea>
            </div>

            <div class="meddelanden" id="meddelanden">
                <div class="msg ai">
                    <div class="avsandare">AI-Analytiker</div>
                    <div class="bubbla">Hej! Jag är din AI-analytiker. Jag har tillgång till aktuell teknisk data för OMX30, S&P500, Europa och Guld. Du kan klistra in ett nyhetsbrev ovan och fråga mig om det, eller bara ställa frågor direkt. Vad vill du veta?</div>
                </div>
            </div>

            <div class="inmatning">
                <textarea id="fraga" placeholder="Skriv din fråga här... t.ex. 'Ser du något köpläge baserat på nuläget?'"></textarea>
                <button onclick="skicka()">Skicka</button>
            </div>
        </div>

        <script>
        async function skicka() {
            const fraga = document.getElementById('fraga').value.trim();
            const nyhetsbrev = document.getElementById('nyhetsbrev').value.trim();
            if (!fraga) return;

            laggTillMeddelande('user', fraga);
            document.getElementById('fraga').value = '';

            const laddning = laggTillLaddning();

            try {
                const svar = await fetch('/analytiker/chatt', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({fraga: fraga, nyhetsbrev: nyhetsbrev})
                });
                const data = await svar.json();
                laddning.remove();
                laggTillMeddelande('ai', data.svar);
            } catch(e) {
                laddning.remove();
                laggTillMeddelande('ai', 'Något gick fel. Kontrollera att servern körs.');
            }
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
def analytiker_chatt():
    data       = request.get_json()
    fraga      = data.get("fraga", "")
    nyhetsbrev = data.get("nyhetsbrev", "")

    marknadsdata = hamta_marknadsdata()

    system_prompt = f"""Du är en erfaren teknisk analytiker som hjälper en privat investerare med beslut kring index-ETF:er och indexfonder.

Din investeringsstrategi:
- Swingtrading på index (OMX30, S&P500, Europa, Guld)
- Teknisk analys baserad på RSI, MACD, SMA50/200, EMA20
- Max 1-2% kapitalrisk per trade
- Tidshorisont: veckor till månader
- Köper vid tekniska köpsignaler, säljer vid mål eller stop-loss

Aktuell marknadsdata (uppdaterad just nu):
{marknadsdata}

Svara alltid på svenska. Var konkret och praktisk. Om du ser köp- eller säljlägen, säg det tydligt med motivering. Om läget är oklart, säg det. Håll svaren kortfattade men substansrika."""

    meddelanden = []

    if nyhetsbrev:
        meddelanden.append({
            "role": "user",
            "content": f"Jag har fått följande analys/nyhetsbrev:\n\n{nyhetsbrev}\n\nMin fråga: {fraga}"
        })
    else:
        meddelanden.append({
            "role": "user",
            "content": fraga
        })

    try:
        svar = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1000,
            system=system_prompt,
            messages=meddelanden
        )
        return jsonify({"svar": svar.content[0].text})
    except Exception as e:
        return jsonify({"svar": f"Fel: {str(e)}"})


@app.route("/riskmotor", methods=["GET", "POST"])
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
        <p style="color:#888; margin-bottom:18px; font-size:0.9em;">Beraknar position size baserat pa max risk per trade.</p>
        {% if fel %}<p class="fel">{{ fel }}</p>{% endif %}
        <form method="POST">
            <div class="form-grid">
                <div class="form-group"><label>Totalt kapital (SEK)</label><input type="text" name="kapital" placeholder="1000000" value="{{ request.form.get('kapital', '') }}"></div>
                <div class="form-group"><label>Max risk per trade (%)</label><input type="text" name="risk_pct" placeholder="1" value="{{ request.form.get('risk_pct', '1') }}"></div>
                <div class="form-group"><label>Entry-pris</label><input type="text" name="entry" placeholder="2200" value="{{ request.form.get('entry', '') }}"></div>
                <div class="form-group"><label>Stop-loss</label><input type="text" name="stop" placeholder="2150" value="{{ request.form.get('stop', '') }}"></div>
            </div>
            <button class="submit-btn" type="submit">Berakna</button>
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


if __name__ == "__main__":
    app.run(debug=True)
