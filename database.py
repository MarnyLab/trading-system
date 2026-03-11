from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Konversation(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    datum       = db.Column(db.DateTime, default=datetime.utcnow)
    fraga       = db.Column(db.Text, nullable=False)
    svar        = db.Column(db.Text, nullable=False)
    marknad     = db.Column(db.Text)        # marknadsläge vid tillfället
    kalla       = db.Column(db.String(100)) # "chatt", "pdf", "email"
    kalla_namn  = db.Column(db.String(200)) # filnamn eller avsändare

    def to_dict(self):
        return {
            "id":        self.id,
            "datum":     self.datum.strftime("%Y-%m-%d %H:%M"),
            "fraga":     self.fraga,
            "svar":      self.svar,
            "kalla":     self.kalla,
            "kalla_namn": self.kalla_namn,
        }


class Beslut(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    datum        = db.Column(db.DateTime, default=datetime.utcnow)
    index_namn   = db.Column(db.String(50))   # OMX30, S&P500 etc
    riktning     = db.Column(db.String(10))   # KOP, SALJ
    entry        = db.Column(db.Float)
    stop_loss    = db.Column(db.Float)
    target       = db.Column(db.Float)
    motivering   = db.Column(db.Text)
    status       = db.Column(db.String(20), default="Öppen")  # Öppen, Stängd, Stop
    utfall       = db.Column(db.Float)        # faktiskt utfall i %
    lardom       = db.Column(db.Text)         # vad lärde vi oss

    def to_dict(self):
        return {
            "id":          self.id,
            "datum":       self.datum.strftime("%Y-%m-%d"),
            "index_namn":  self.index_namn,
            "riktning":    self.riktning,
            "entry":       self.entry,
            "stop_loss":   self.stop_loss,
            "target":      self.target,
            "motivering":  self.motivering,
            "status":      self.status,
            "utfall":      self.utfall,
            "lardom":      self.lardom,
        }
