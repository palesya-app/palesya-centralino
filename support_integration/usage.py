"""Servizio AI a pagamento: idoneità (abbonati "in pipeline") e minuti/mese.

Regole:
- L'AI serve pienamente solo gli utenti abbonati (``ai_service_active``) quando
  l'enforcement è attivo; con enforcement OFF nessuno viene bloccato (rollout).
- Ogni utente ha un monte minuti al mese (default 20), dal primo all'ultimo del
  mese di calendario; a inizio mese si azzera automaticamente.
Logiche pure e testabili: la persistenza (HubSpot) sta in service/hubspot.
"""
import datetime as dt


def current_period(now=None):
    now = now or dt.date.today()
    if isinstance(now, dt.datetime):
        now = now.date()
    return "{:04d}-{:02d}".format(now.year, now.month)


def _num(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _flag(value):
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def usage_status(props, monthly_minutes_default=20, enforced=False, now=None):
    """Stato d'uso/idoneità di un'azienda a partire dalle sue proprietà HubSpot."""
    props = props or {}
    period = current_period(now)
    stored_period = str(props.get("ai_minutes_period") or "").strip()
    # Reset automatico se siamo in un mese nuovo rispetto all'ultimo registrato.
    used = _num(props.get("ai_minutes_used"), 0.0) if stored_period == period else 0.0
    limit_val = _num(props.get("ai_minutes_limit"), 0.0)
    limit = limit_val if limit_val > 0 else float(monthly_minutes_default)
    remaining = max(0.0, round(limit - used, 2))
    service_active = _flag(props.get("ai_service_active"))
    # Idoneità: se l'enforcement è spento, chiunque sia riconosciuto è servito.
    if not enforced:
        eligibile = True
        motivo = "enforcement_off"
    elif not service_active:
        eligibile = False
        motivo = "non_abbonato"
    elif remaining <= 0:
        eligibile = False
        motivo = "minuti_esauriti"
    else:
        eligibile = True
        motivo = "ok"
    return {
        "ai_gating_enforced": bool(enforced),
        "ai_servizio_attivo": service_active,
        "ai_eligibile": eligibile,
        "ai_motivo": motivo,
        "ai_minuti_limite": round(limit, 2),
        "ai_minuti_usati": round(used, 2),
        "ai_minuti_residui": remaining,
        "ai_periodo": period,
    }


def apply_call_minutes(props, seconds, monthly_minutes_default=20, now=None):
    """Somma i minuti di una chiamata al monte mensile, azzerando se cambia mese.

    Ritorna le proprietà HubSpot da scrivere sul Company.
    """
    props = props or {}
    period = current_period(now)
    stored_period = str(props.get("ai_minutes_period") or "").strip()
    used = _num(props.get("ai_minutes_used"), 0.0) if stored_period == period else 0.0
    minutes = max(0.0, _num(seconds, 0.0) / 60.0)
    new_used = round(used + minutes, 2)
    limit_val = _num(props.get("ai_minutes_limit"), 0.0)
    limit = limit_val if limit_val > 0 else float(monthly_minutes_default)
    return {
        "ai_minutes_used": str(new_used),
        "ai_minutes_period": period,
        "ai_minutes_limit": str(int(limit)) if float(limit).is_integer() else str(limit),
    }
