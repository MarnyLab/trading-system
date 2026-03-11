import anthropic
import os
from database import db, Konversation

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """Du är en erfaren teknisk analytiker som hjälper en privat investerare med beslut kring index-ETF:er och indexfonder.

Din investeringsstrategi:
- Swingtrading på index (OMX30, S&P500, Europa, Guld)
- Teknisk analys baserad på RSI, MACD, SMA50/200, EMA20
- Max 1-2% kapitalrisk per trade
- Tidshorisont: veckor till månader
- Köper vid tekniska köpsignaler, säljer vid mål eller stop-loss

Svara alltid på svenska. Var konkret och praktisk. Om du ser köp- eller säljlägen, säg det tydligt med motivering. Om läget är oklart, säg det. Håll svaren kortfattade men substansrika."""


def hamta_tidigare_konversationer(antal=5):
    """Hämtar de senaste konversationerna som kontext."""
    tidigare = Konversation.query.order_by(Konversation.datum.desc()).limit(antal).all()
    if not tidigare:
        return ""
    text = "\n\nTidigare analyser och konversationer (för kontext):\n"
    for k in reversed(tidigare):
        text += f"\n[{k.datum.strftime('%Y-%m-%d')}] "
        if k.kalla_namn:
            text += f"(Källa: {k.kalla_namn}) "
        text += f"\nFråga: {k.fraga[:200]}\nSvar: {k.svar[:400]}\n---"
    return text


def fraga_analytiker(fraga, marknadsdata, kalltext="", kalla="chatt", kalla_namn=None):
    """Skickar fråga till Claude och sparar konversationen."""
    tidigare = hamta_tidigare_konversationer()

    system = SYSTEM_PROMPT
    system += f"\n\nAktuell marknadsdata:\n{marknadsdata}"
    system += tidigare

    innehall = fraga
    if kalltext:
        innehall = f"Källmaterial:\n\n{kalltext}\n\nMin fråga: {fraga}"

    try:
        svar = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": innehall}]
        )
        svar_text = svar.content[0].text

        # Spara i databasen
        k = Konversation(
            fraga=fraga[:2000],
            svar=svar_text[:4000],
            marknad=marknadsdata[:1000],
            kalla=kalla,
            kalla_namn=kalla_namn
        )
        db.session.add(k)
        db.session.commit()

        return svar_text

    except Exception as e:
        return f"Fel: {str(e)}"
