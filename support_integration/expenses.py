"""Uscite aziendali (costi) — logiche pure e testabili.

Le uscite non sono legate a un cliente: sul piano HubSpot attuale (no Custom
Objects) vivono come record Ticket in una pipeline dedicata "Uscite aziendali".
Qui stanno solo normalizzazione e aggregazione; la parte HubSpot è in hubspot.py.
"""
import datetime as dt

CATEGORIE = (
    "affitto", "stipendi", "fornitori", "software", "utenze", "tasse",
    "marketing", "hardware", "consulenze", "banca", "trasporti", "altro",
)
METODI = ("bonifico", "fattura", "carta", "contanti", "sepa", "addebito", "altro")
STATI = ("da_pagare", "pagata")


def _num(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _date(value):
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


def normalize_expense(payload):
    """Valida e normalizza una uscita. Ritorna le proprietà Ticket da scrivere.

    Richiede almeno l'importo. Categoria/metodo fuori lista → 'altro'.
    """
    importo = _num(payload.get("importo"))
    if importo is None or importo < 0:
        raise ValueError("importo obbligatorio e non negativo")
    categoria = str(payload.get("categoria") or "altro").strip().lower()
    if categoria not in CATEGORIE:
        categoria = "altro"
    metodo = str(payload.get("metodo") or "bonifico").strip().lower()
    if metodo not in METODI:
        metodo = "altro"
    stato = str(payload.get("stato") or "pagata").strip().lower()
    if stato not in STATI:
        stato = "pagata"
    data = _date(payload.get("data")) or dt.date.today()
    fornitore = str(payload.get("fornitore") or "").strip()
    descrizione = str(payload.get("descrizione") or payload.get("note") or "").strip()

    subject = "Uscita: {} — {:.2f}€{}".format(
        categoria, importo, " · {}".format(fornitore) if fornitore else "")
    props = {
        "subject": subject,
        "content": descrizione or subject,
        "uscita_importo": importo,
        "uscita_categoria": categoria,
        "uscita_metodo": metodo,
        "uscita_stato": stato,
        "uscita_data": _iso(data),
        "uscita_fornitore": fornitore,
    }
    props = {k: v for k, v in props.items() if v not in (None, "")}
    summary = {
        "importo": importo, "categoria": categoria, "metodo": metodo,
        "stato": stato, "data": _iso(data), "fornitore": fornitore or None,
    }
    return props, summary


def aggregate_expenses(tickets, today=None, from_date=None, to_date=None):
    """Riepilogo uscite: totale, pagate vs da pagare, per categoria, elenco da pagare."""
    today = today or dt.date.today()
    frm = _date(from_date)
    to = _date(to_date)
    totale = 0.0
    pagate = 0.0
    da_pagare_tot = 0.0
    per_categoria = {}
    da_pagare = []
    conteggio = 0
    for t in tickets or []:
        props = t.get("properties") or {}
        importo = _num(props.get("uscita_importo"))
        if importo is None:
            continue
        d = _date(props.get("uscita_data"))
        if frm and (not d or d < frm):
            continue
        if to and (not d or d > to):
            continue
        conteggio += 1
        cat = props.get("uscita_categoria") or "altro"
        stato = props.get("uscita_stato") or "pagata"
        totale += importo
        per_categoria[cat] = round(per_categoria.get(cat, 0.0) + importo, 2)
        if stato == "pagata":
            pagate += importo
        else:
            da_pagare_tot += importo
            da_pagare.append({"ticket_id": str(t.get("id")), "categoria": cat,
                              "fornitore": props.get("uscita_fornitore") or None,
                              "importo": importo, "data": props.get("uscita_data") or None})
    return {
        "uscite_count": conteggio,
        "uscite_totali": round(totale, 2),
        "uscite_pagate": round(pagate, 2),
        "uscite_da_pagare": round(da_pagare_tot, 2),
        "per_categoria": per_categoria,
        "da_pagare": da_pagare,
        "da_pagare_count": len(da_pagare),
    }
