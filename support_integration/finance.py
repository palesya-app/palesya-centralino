"""Logiche di gestione finanziaria del cliente Palesya (pure, testabili).

Il gestionale Palesya alimenta questi dati (import); il backend normalizza,
calcola scadenze/prossimo pagamento/stato e li mappa sulle proprietà Company
HubSpot. L'AI legge e informa soltanto: nessuna transazione lato AI.

Listino di riferimento (IVA esclusa) dal pacchetto customer care:
- Licenza lifetime per sede: 1.800€ una tantum
- Active 12: 70€/mese oppure 630€/anno
- Active 24: 60€/mese oppure 600€/anno (x2 anni)
- Active 36: 45€/mese oppure 500€/anno (x3 anni)
- 5 interventi tecnici/anno inclusi con Active; ora extra 46€/h
"""
import datetime as dt

LICENZA_IMPORTO = 1800.0
INTERVENTI_INCLUSI_ACTIVE = 5
ORA_EXTRA = 46.0

# Metodi di incasso correnti: solo fattura + bonifico.
METODI_CORRENTI = ("bonifico", "fattura")
METODO_DEFAULT = "bonifico"

LISTINO_ACTIVE = {
    "active_12": {"mensile": 70.0, "annuo": 630.0, "anni": 1},
    "active_24": {"mensile": 60.0, "annuo": 600.0, "anni": 2},
    "active_36": {"mensile": 45.0, "annuo": 500.0, "anni": 3},
}
ACTIVE_PLANS = set(LISTINO_ACTIVE)


def _num(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _date(value):
    """Accetta 'YYYY-MM-DD' (o datetime/date); ritorna date o None."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _iso(d):
    return d.isoformat() if isinstance(d, dt.date) else ""


def _add_month(d):
    """d + 1 mese, con clamp di fine mese."""
    year = d.year + (1 if d.month == 12 else 0)
    month = 1 if d.month == 12 else d.month + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return dt.date(year, month, day)


def summary_from_props(props):
    """Riepilogo finanziario leggibile dall'AI a partire dalle proprietà Company."""
    props = props or {}
    remaining = props.get("support_tickets_remaining")
    try:
        residui = int(float(remaining)) if remaining not in (None, "") else None
    except (TypeError, ValueError):
        residui = None
    try:
        prossimo_importo = float(props.get("fin_prossimo_pagamento_importo")) if props.get("fin_prossimo_pagamento_importo") not in (None, "") else None
    except (TypeError, ValueError):
        prossimo_importo = None
    active_stato = props.get("fin_active_stato") or None
    stato_pagamento = props.get("fin_stato_pagamento") or None
    return {
        "tipo_contratto": props.get("fin_tipo_contratto") or None,
        "active_stato": active_stato,
        "piano_attivo": active_stato == "attivo",
        "stato_pagamento": stato_pagamento,
        "in_regola_pagamenti": stato_pagamento == "pagato" and active_stato in ("attivo", "nessuno", None),
        "licenza_pagata": str(props.get("fin_licenza_pagata") or "").lower() == "true",
        "interventi_residui": residui,
        "active_scadenza": props.get("fin_active_scadenza") or None,
        "prossimo_pagamento_data": props.get("fin_prossimo_pagamento_data") or None,
        "prossimo_pagamento_importo": prossimo_importo,
        "metodo_pagamento": props.get("fin_metodo_pagamento") or None,
    }


def compute_finance(payload, current_used=0, today=None):
    """Normalizza il payload del gestionale e calcola i campi derivati.

    Ritorna (props, summary):
    - props: proprietà da scrivere sul Company HubSpot
    - summary: quadro sintetico che l'AI può leggere/pronunciare
    """
    today = today or dt.date.today()
    tipo = str(payload.get("tipo_contratto") or "nessuno").strip().lower()
    is_active = tipo in ACTIVE_PLANS
    listino = LISTINO_ACTIVE.get(tipo, {})

    cadenza = str(payload.get("cadenza_pagamento") or ("annuale" if is_active else "")).strip().lower()
    if cadenza not in ("mensile", "annuale"):
        cadenza = "annuale" if is_active else ""

    stato_pagamento = str(payload.get("stato_pagamento") or "").strip().lower()
    if stato_pagamento not in ("pagato", "parziale", "insoluto", "rimborsato", ""):
        stato_pagamento = ""

    # Metodo di incasso: oggi solo fattura/bonifico. Se pagato senza metodo, default bonifico.
    metodo = str(payload.get("metodo_pagamento") or "").strip().lower()
    if not metodo and stato_pagamento in ("pagato", "parziale"):
        metodo = METODO_DEFAULT

    # Licenza
    licenza_in_contratto = tipo in ("licenza", "licenza_active") or bool(payload.get("licenza_pagata"))
    licenza_pagata = bool(payload.get("licenza_pagata"))
    licenza_importo = _num(payload.get("licenza_importo"), LICENZA_IMPORTO if licenza_in_contratto else None)
    licenza_data = _date(payload.get("licenza_data"))

    # Active: importi e scadenza
    active_inizio = _date(payload.get("active_inizio"))
    active_importo_annuo = _num(payload.get("active_importo_annuo"), listino.get("annuo") if is_active else None)
    active_scadenza = _date(payload.get("active_scadenza"))
    if is_active and not active_scadenza and active_inizio:
        # Active segue l'anno solare: scadenza al 31/12 dell'anno di inizio.
        active_scadenza = dt.date(active_inizio.year, 12, 31)

    ultimo_data = _date(payload.get("ultimo_pagamento_data"))
    ultimo_importo = _num(payload.get("ultimo_pagamento_importo"))

    # Prossimo pagamento
    prossimo_data = _date(payload.get("prossimo_pagamento_data"))
    prossimo_importo = _num(payload.get("prossimo_pagamento_importo"))
    if is_active and not prossimo_data:
        if stato_pagamento in ("insoluto", "parziale"):
            prossimo_data = today
            prossimo_importo = prossimo_importo or (listino.get("mensile") if cadenza == "mensile" else active_importo_annuo)
        elif cadenza == "mensile":
            base = ultimo_data or active_inizio
            prossimo_data = _add_month(base) if base else None
            prossimo_importo = prossimo_importo or listino.get("mensile")
        elif cadenza == "annuale":
            prossimo_data = active_scadenza
            prossimo_importo = prossimo_importo or active_importo_annuo

    # Stato Active
    if not is_active:
        active_stato = "nessuno"
    elif stato_pagamento in ("insoluto", "parziale"):
        active_stato = "in_attesa_pagamento"
    elif active_scadenza and active_scadenza < today:
        active_stato = "scaduto"
    else:
        active_stato = "attivo"

    # Interventi: total dal gestionale o 5/anno con Active; remaining = total - usati.
    total = _num(payload.get("interventi_totali"), INTERVENTI_INCLUSI_ACTIVE if is_active else 0)
    total = int(total or 0)
    used = int(current_used or 0)
    remaining = max(0, total - used)
    if active_stato == "in_attesa_pagamento" or active_stato == "scaduto":
        plan_status = "suspended"
    elif remaining == 0:
        plan_status = "exhausted"
    elif remaining <= 3:
        plan_status = "low"
    else:
        plan_status = "active"

    props = {
        "fin_tipo_contratto": tipo,
        "fin_licenza_pagata": "true" if licenza_pagata else "false",
        "fin_licenza_importo": licenza_importo,
        "fin_licenza_data": _iso(licenza_data),
        "fin_active_stato": active_stato,
        "fin_active_inizio": _iso(active_inizio),
        "fin_active_scadenza": _iso(active_scadenza),
        "fin_active_importo_annuo": active_importo_annuo,
        "fin_cadenza_pagamento": cadenza,
        "fin_stato_pagamento": stato_pagamento,
        "fin_metodo_pagamento": metodo,
        "fin_ultimo_pagamento_data": _iso(ultimo_data),
        "fin_ultimo_pagamento_importo": ultimo_importo,
        "fin_prossimo_pagamento_data": _iso(prossimo_data),
        "fin_prossimo_pagamento_importo": prossimo_importo,
        "support_tickets_total": total,
        "support_tickets_remaining": remaining,
        "support_plan_status": plan_status,
    }
    if active_scadenza:
        props["support_plan_expiration_date"] = _iso(active_scadenza)
    if active_inizio:
        props["support_plan_start_date"] = _iso(active_inizio)
    # Solo valori valorizzati (numeri/stringhe non vuote); i numeri 0 restano.
    props = {k: v for k, v in props.items() if v not in (None, "")}

    in_regola = stato_pagamento == "pagato" and active_stato in ("attivo", "nessuno")
    summary = {
        "tipo_contratto": tipo,
        "piano_attivo": active_stato == "attivo",
        "in_regola_pagamenti": bool(in_regola),
        "stato_pagamento": stato_pagamento or None,
        "licenza_pagata": licenza_pagata,
        "interventi_totali": total,
        "interventi_residui": remaining,
        "active_scadenza": _iso(active_scadenza) or None,
        "prossimo_pagamento_data": _iso(prossimo_data) or None,
        "prossimo_pagamento_importo": prossimo_importo,
        "metodo_pagamento": props.get("fin_metodo_pagamento") or None,
    }
    return props, summary
