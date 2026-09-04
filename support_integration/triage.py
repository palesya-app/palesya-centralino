"""Smistamento intelligente delle richieste + apprendimento continuo.

Il centralino classificava categoria e gravità con liste di parole chiave a
"primo match vince": rigido, senza confidenza e soprattutto **incapace di
imparare** (uno stesso errore si ripete all'infinito).

Questo modulo aggiunge tre livelli in cascata:

1. **regole** — le stesse parole chiave di prima: comportamento storico,
   sempre disponibile, nessuna regressione se il modello non sa ancora nulla;
2. **modello appreso** — Naive Bayes multinomiale incrementale (Python puro,
   zero dipendenze nuove: il servizio gira su Render free e ogni MB pesa sul
   cold-start);
3. **fusione** — il modello sostituisce la regola **solo** se ha visto
   abbastanza esempi ed è abbastanza sicuro; altrimenti vince la regola.

Regola di disciplina fondamentale: il modello impara **soltanto da etichette
confermate da un umano**, mai dalle proprie previsioni. Addestrarsi sul proprio
output farebbe collassare il modello sui suoi stessi errori.
"""
import math
import re
import unicodedata

# --- Parametri di fiducia -------------------------------------------------
# Sotto queste soglie il motore resta sulle regole: meglio il comportamento
# noto che una predizione azzardata su pochi esempi.
MIN_EXAMPLES_TOTAL = 15
MIN_EXAMPLES_PER_LABEL = 3
CONFIDENCE_THRESHOLD = 0.65

_STOPWORDS = frozenset("""
il lo la i gli le un uno una di del della dei delle da dal dalla in nel nella
con su sul sulla per tra fra e ed o od ma se perche che chi cui non piu meno
molto poco tutto tutti questa questo quello quella mi ti si ci vi ne ho hai ha
abbiamo avete hanno sono sei siamo siete era erano essere avere fare fatto
c'e' ce cosa come quando dove quale quali buongiorno buonasera salve grazie
prego ecco allora quindi pero anche solo gia ancora sempre mai adesso oggi
ieri domani sto sta stanno vorrei volevo potrei posso puoi puo dovrei
""".split())

_WORD_RE = re.compile(r"[a-z]+")
# Le liste di parole chiave storiche usano prefissi ("tornell", "fattur"): il
# troncamento a 6 caratteri fa collassare le flessioni sullo stesso token
# ("tornello"/"tornelli" -> "tornel") ed è coerente con quella convenzione.
_STEM_LEN = 6


def _strip_accents(text):
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def tokenize(text):
    """Testo libero italiano -> lista di token normalizzati e troncati."""
    plain = _strip_accents(str(text or "").lower())
    tokens = []
    for word in _WORD_RE.findall(plain):
        if len(word) < 3 or word in _STOPWORDS:
            continue
        tokens.append(word[:_STEM_LEN])
    return tokens


class NaiveBayes:
    """Naive Bayes multinomiale incrementale con smoothing di Laplace.

    Tenuto volutamente in Python puro: i volumi del centralino sono nell'ordine
    delle migliaia di esempi, dove numpy/scikit-learn porterebbero decine di MB
    di dipendenze (e cold-start più lenti su Render free) senza alcun guadagno
    pratico.
    """

    def __init__(self):
        self.label_counts = {}          # label -> numero di documenti
        self.token_counts = {}          # label -> {token: occorrenze}
        self.label_totals = {}          # label -> occorrenze totali di token
        self.vocabulary = set()

    @property
    def total_examples(self):
        return sum(self.label_counts.values())

    def add(self, text, label):
        """Aggiunge un esempio etichettato (aggiornamento incrementale)."""
        label = str(label or "").strip()
        tokens = tokenize(text)
        if not label or not tokens:
            return False
        self.label_counts[label] = self.label_counts.get(label, 0) + 1
        bucket = self.token_counts.setdefault(label, {})
        for token in tokens:
            bucket[token] = bucket.get(token, 0) + 1
            self.vocabulary.add(token)
        self.label_totals[label] = self.label_totals.get(label, 0) + len(tokens)
        return True

    def is_ready(self):
        """Vero solo con abbastanza esempi, e abbastanza per singola etichetta."""
        if self.total_examples < MIN_EXAMPLES_TOTAL or len(self.label_counts) < 2:
            return False
        usable = [c for c in self.label_counts.values() if c >= MIN_EXAMPLES_PER_LABEL]
        return len(usable) >= 2

    def predict(self, text):
        """Ritorna (label, confidenza 0..1, punteggi per etichetta).

        La confidenza è la probabilità a posteriori dell'etichetta vincente,
        calcolata con softmax sui log-score (stabile: si sottrae il massimo).
        """
        tokens = tokenize(text)
        if not tokens or not self.label_counts:
            return None, 0.0, {}
        vocab_size = max(len(self.vocabulary), 1)
        total_docs = self.total_examples
        log_scores = {}
        for label, doc_count in self.label_counts.items():
            score = math.log(doc_count / total_docs)
            bucket = self.token_counts.get(label, {})
            denominator = self.label_totals.get(label, 0) + vocab_size
            for token in tokens:
                score += math.log((bucket.get(token, 0) + 1) / denominator)
            log_scores[label] = score
        best = max(log_scores, key=log_scores.get)
        peak = log_scores[best]
        exponentials = {k: math.exp(v - peak) for k, v in log_scores.items()}
        normalizer = sum(exponentials.values()) or 1.0
        probabilities = {k: v / normalizer for k, v in exponentials.items()}
        return best, probabilities[best], probabilities


class Decision:
    """Esito di una classificazione, con la motivazione."""

    __slots__ = ("label", "confidence", "source", "scores")

    def __init__(self, label, confidence=0.0, source="rules", scores=None):
        self.label = label
        self.confidence = float(confidence or 0.0)
        self.source = source
        self.scores = scores or {}

    def as_dict(self):
        return {
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "source": self.source,
        }

    def __repr__(self):
        return "Decision(%r, %.3f, %s)" % (self.label, self.confidence, self.source)


class TriageEngine:
    """Classifica categoria e gravità fondendo regole e modello appreso."""

    def __init__(self, valid_categories, valid_severities,
                 category_rule=None, severity_rule=None,
                 confidence_threshold=CONFIDENCE_THRESHOLD):
        self.valid_categories = tuple(valid_categories)
        self.valid_severities = tuple(valid_severities)
        self._category_rule = category_rule
        self._severity_rule = severity_rule
        self.confidence_threshold = confidence_threshold
        self.category_model = NaiveBayes()
        self.severity_model = NaiveBayes()

    # --- apprendimento ---------------------------------------------------
    def learn(self, text, category=None, severity=None):
        """Assimila un esempio **confermato da un umano**.

        Le etichette fuori tassonomia vengono ignorate: meglio non imparare che
        imparare rumore.
        """
        learned = False
        if category in self.valid_categories:
            learned |= self.category_model.add(text, category)
        if severity in self.valid_severities:
            learned |= self.severity_model.add(text, severity)
        return learned

    def learn_many(self, rows):
        """Ricostruisce i modelli da una lista di esempi confermati."""
        self.category_model = NaiveBayes()
        self.severity_model = NaiveBayes()
        count = 0
        for row in rows or ():
            if self.learn(row.get("text"), row.get("category"), row.get("severity")):
                count += 1
        return count

    # --- classificazione -------------------------------------------------
    def _fuse(self, model, rule_label, text, valid):
        """Il modello prevale sulla regola solo se pronto e sicuro."""
        if not model.is_ready():
            return Decision(rule_label, 0.0, "rules")
        label, confidence, scores = model.predict(text)
        if label in valid and confidence >= self.confidence_threshold and label != rule_label:
            return Decision(label, confidence, "model", scores)
        if label == rule_label:
            # Regola e modello concordano: stessa etichetta, ma con confidenza.
            return Decision(rule_label, confidence, "rules+model", scores)
        return Decision(rule_label, confidence, "rules", scores)

    def classify_category(self, text):
        rule_label = self._category_rule(text) if self._category_rule else "other"
        return self._fuse(self.category_model, rule_label, text, self.valid_categories)

    def classify_severity(self, text):
        rule_label = self._severity_rule(text) if self._severity_rule else "medium"
        return self._fuse(self.severity_model, rule_label, text, self.valid_severities)

    # --- diagnostica ------------------------------------------------------
    def stats(self):
        return {
            "category": {
                "examples": self.category_model.total_examples,
                "labels": dict(self.category_model.label_counts),
                "ready": self.category_model.is_ready(),
            },
            "severity": {
                "examples": self.severity_model.total_examples,
                "labels": dict(self.severity_model.label_counts),
                "ready": self.severity_model.is_ready(),
            },
            "confidence_threshold": self.confidence_threshold,
        }


# --- Smistamento verso l'agente giusto ------------------------------------

# Segnali espliciti di richiesta umana: hanno sempre la precedenza.
_HUMAN_NEEDLES = (
    "persona reale", "operatore", "persona vera", "un umano", "essere umano",
    "parlare con qualcun", "un tecnico vero", "responsabile", "titolare",
)
# Segnali commerciali: preventivi, prezzi, nuove attivazioni.
_COMMERCIAL_NEEDLES = (
    "preventiv", "quanto costa", "prezzi", "listino prezzi", "acquist",
    "abbonarmi", "attivare il servizio", "informazioni commercial", "offerta",
    "vorrei provare", "demo", "nuovo cliente", "diventare cliente",
)


def route_request(text, *, eligible_for_support=False, match_status="unknown",
                  failed_attempts=0, confidence=1.0, confidence_floor=0.4):
    """Decide a chi passare la chiamata, con motivazione tracciabile.

    Ritorna un dict con ``destination`` (``umano``/``commerciale``/``tecnica``)
    e ``reason``. Non sostituisce la logica di eleggibilità già in produzione:
    la formalizza e vi aggiunge due valvole di sicurezza — richiesta esplicita
    di un operatore e ripetuti fallimenti/incertezza -> umano.
    """
    low = " " + _strip_accents(str(text or "").lower()) + " "

    if any(needle in low for needle in _HUMAN_NEEDLES):
        return {"destination": "umano", "reason": "richiesta_esplicita_operatore"}
    if failed_attempts >= 2:
        return {"destination": "umano", "reason": "tentativi_falliti"}
    if any(needle in low for needle in _COMMERCIAL_NEEDLES):
        return {"destination": "commerciale", "reason": "intento_commerciale"}
    if not eligible_for_support:
        # Non cliente vinto: la regola di business manda al commerciale.
        return {"destination": "commerciale",
                "reason": "non_idoneo_assistenza" if match_status == "found" else "cliente_non_riconosciuto"}
    if confidence < confidence_floor:
        return {"destination": "umano", "reason": "richiesta_non_compresa"}
    return {"destination": "tecnica", "reason": "cliente_idoneo"}
