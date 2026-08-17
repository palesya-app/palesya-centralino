"""Orchestrazione end-to-end e ledger idempotente dell'assistenza."""
import datetime as dt
import hashlib
import json
import logging
import os

import db_compat
from config import settings as app_settings
from schema import ensure_schema

from . import finance
from .config import settings as support_settings
from .hubspot import HubSpotClient
from .phone import normalize_phone
from .telnyx import extract_call_data
from .voice_ai import map_final_event

logger = logging.getLogger("palesya.support")


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _int(value, default=0):
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


class SupportService:
    def __init__(self, db_path=None, hubspot=None, support_config=None, database_url=None):
        # DATABASE_URL (Postgres) vince sul percorso SQLite: in produzione la
        # memoria è persistente, in locale/test resta SQLite senza cambiare codice.
        self.database_url = str(database_url if database_url is not None else os.getenv("DATABASE_URL", "")).strip()
        self.db_path = str(db_path or app_settings.database_path)
        self._target = self.database_url or self.db_path
        self._is_pg = db_compat.is_postgres(self._target)
        self.support_config = support_config or support_settings
        self.hubspot = hubspot or HubSpotClient(self.support_config)

    def _conn(self):
        return db_compat.connect(self._target)

    def _serialize_company(self, con, company_id):
        """Serializza il consumo per azienda: su Postgres un advisory lock
        replica ciò che BEGIN IMMEDIATE fa su SQLite (write lock esclusivo)."""
        if self._is_pg and company_id:
            con.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (str(company_id),))

    def ensure_local_schema(self):
        ensure_schema(self._target)

    def register_event(self, event):
        """Registra l'evento prima dell'ack; gli errori sono ritentabili."""
        con = self._conn()
        try:
            row = con.execute(
                "SELECT STATUS FROM SUPPORT_WEBHOOK_EVENTS WHERE SOURCE=? AND EVENT_ID=?",
                (event["source"], event["event_id"]),
            ).fetchone()
            if row and row["STATUS"] == "PROCESSED":
                logger.info("duplicate_event source=%s status=processed", event["source"])
                return False
            if row:
                con.execute(
                    "UPDATE SUPPORT_WEBHOOK_EVENTS SET STATUS='RECEIVED', ERROR=NULL WHERE SOURCE=? AND EVENT_ID=?",
                    (event["source"], event["event_id"]),
                )
            else:
                con.execute(
                    """INSERT INTO SUPPORT_WEBHOOK_EVENTS
                       (SOURCE,EVENT_ID,EVENT_TYPE,CALL_ID,OCCURRED_AT,PAYLOAD_HASH)
                       VALUES(?,?,?,?,?,?)""",
                    (event["source"], event["event_id"], event.get("event_type"), event.get("call_id"),
                     event.get("occurred_at"), event.get("payload_hash")),
                )
            con.commit()
            return True
        finally:
            con.close()

    def _event_status(self, event, status, error=None):
        con = self._conn()
        try:
            con.execute(
                """UPDATE SUPPORT_WEBHOOK_EVENTS SET STATUS=?, ERROR=?, PROCESSED_AT=CASE WHEN ?='PROCESSED' THEN CAST(CURRENT_TIMESTAMP AS TEXT) ELSE PROCESSED_AT END
                   WHERE SOURCE=? AND EVENT_ID=?""",
                (status, (str(error)[:500] if error else None), status, event["source"], event["event_id"]),
            )
            con.commit()
        finally:
            con.close()

    def _session(self, call_id):
        if not call_id:
            return None
        con = self._conn()
        try:
            row = con.execute("SELECT * FROM SUPPORT_CALL_SESSIONS WHERE CALL_ID=?", (call_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def _save_session(self, call_id, values):
        if not call_id:
            return
        con = self._conn()
        try:
            row = con.execute("SELECT CALL_ID FROM SUPPORT_CALL_SESSIONS WHERE CALL_ID=?", (call_id,)).fetchone()
            if row:
                assignments = []
                params = []
                for key, value in values.items():
                    if value is not None:
                        assignments.append("{}=?".format(key))
                        params.append(value)
                if assignments:
                    assignments.append("UPDATED_AT=CURRENT_TIMESTAMP")
                    params.append(call_id)
                    con.execute("UPDATE SUPPORT_CALL_SESSIONS SET {} WHERE CALL_ID=?".format(",".join(assignments)), params)
            else:
                values = dict(values)
                values["CALL_ID"] = call_id
                columns = list(values)
                con.execute(
                    "INSERT INTO SUPPORT_CALL_SESSIONS ({}) VALUES ({})".format(
                        ",".join(columns), ",".join("?" for _ in columns)
                    ), [values[column] for column in columns],
                )
            con.commit()
        finally:
            con.close()

    def _ticket_id(self, call_id):
        con = self._conn()
        try:
            row = con.execute("SELECT HUBSPOT_TICKET_ID FROM SUPPORT_TICKET_LINKS WHERE CALL_ID=?", (call_id,)).fetchone()
            return str(row[0]) if row else None
        finally:
            con.close()

    def _link_ticket(self, call_id, ticket_id):
        con = self._conn()
        try:
            con.execute(
                """INSERT INTO SUPPORT_TICKET_LINKS(CALL_ID,HUBSPOT_TICKET_ID) VALUES(?,?)
                   ON CONFLICT(CALL_ID) DO UPDATE SET HUBSPOT_TICKET_ID=excluded.HUBSPOT_TICKET_ID,UPDATED_AT=CURRENT_TIMESTAMP""",
                (call_id, str(ticket_id)),
            )
            con.commit()
        finally:
            con.close()

    SEVERITY_TO_PRIORITY = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH", "critical": "URGENT"}

    # Inferenza categoria da descrizione libera: l'agente vocale non deve più
    # classificare a mano. Ordine = priorità (la prima categoria che matcha vince).
    CATEGORY_KEYWORDS = (
        ("turnstile", ("tornell", "varco", "cancellett", "girevol")),
        ("access_control", ("badge", "tessera", "tessere", "rfid", "controllo access", "apertura port", "impronta", "qr", "chip")),
        ("hardware", ("stampant", "scanner", "cassett", " pos", "tablet", "monitor", "computer", " pc", "wifi", "rete", "cavo", "non si accende", "guast", "rotto", "lettore")),
        ("billing", ("fattur", "pagament", "addebit", "ricevut", "rata", "incass", "sdd", "rid ", "iva", "contabil", "scontrin")),
        ("migration", ("migrazion", "importazion", "import ", "trasferiment", "esportazion", "vecchio gestional", "spostare i dati", "recupero dati")),
        ("configuration", ("configuraz", "impostazion", "impostare", "settare", "listino", "orari", "calendar", "corso", "corsi", "sala", "abbonament")),
        ("software", ("gestional", "software", "applicazion", " app", "programma", "schermat", "login", "accedere", "password", "si blocca", "crash", "errore", "bug", "pagina", "non si apre", "aggiornament")),
    )

    # Segnali forti per stimare la severity dal racconto del chiamante.
    SEVERITY_KEYWORDS = (
        ("critical", ("emergenz", "urgentissim", "bloccato tutto", "fermo tutto", "non funziona niente", "palestra ferma", "non entra nessuno", "non incass", "tutto fermo", "tutto bloccato", "down", " giù")),
        ("high", ("urgent", "non riesc", "non funzion", "bloccat", "impossibile", "da stamattina", "da ieri", "fermo", "molti client", "non apr")),
        ("low", ("informazion", "vorrei saper", "come si fa", "come faccio", "domanda", "curiosit", "piccol")),
    )

    @classmethod
    def _infer_category(cls, text):
        low = " " + (text or "").lower() + " "
        for category, needles in cls.CATEGORY_KEYWORDS:
            if any(needle in low for needle in needles):
                return category
        return "other"

    @classmethod
    def _infer_severity(cls, text):
        low = " " + (text or "").lower() + " "
        for severity, needles in cls.SEVERITY_KEYWORDS:
            if any(needle in low for needle in needles):
                return severity
        return "medium"

    def _ticket_properties(self, call, *, support_consumed=False, before=None, after=None,
                           reason=None, summary="", resolution="", category="other",
                           status="new", source="ai_phone", match_status="unknown", duration=None,
                           voice_ai_call_id="", alert_level=None, severity="", troubleshooting="",
                           device="", intent="", escalation_reason="", follow_up=None, contact_name=""):
        company = call.get("company_name") or "Cliente sconosciuto"
        contact_name = (contact_name or "").strip()
        who = "{} · {}".format(company, contact_name) if contact_name else company
        # Testata con le info del cliente sempre in evidenza sul ticket.
        header = "Segnalato da: {}{}\nTelefono: {}\n\n".format(
            contact_name or "—", " ({})".format(company) if company else "", call.get("caller_phone") or "—")
        props = {
            "subject": "[AI Support] {} — {}".format(who, (summary or "richiesta assistenza")[:120]),
            "content": header + (summary or "Chiamata assistenza telefonica"),
            "ticket_source": source,
            "support_consumed": "true" if support_consumed else "false",
            "caller_phone": call.get("caller_phone") or "",
            "telnyx_call_id": call.get("telnyx_call_id") or call.get("call_id") or "",
            "voice_ai_call_id": voice_ai_call_id or call.get("voice_ai_call_id") or "",
            "support_issue_category": category or "other",
            "support_issue_summary": summary or "",
            "support_resolution": resolution or "",
            "support_ai_summary": summary or "",
            "support_resolution_status": status or "new",
            "customer_match_status": match_status or "unknown",
            "support_consumption_reason": reason or "",
            "support_severity": severity or "",
            "support_troubleshooting": troubleshooting or "",
            "support_device": device or "",
            "support_intent": intent or "",
            "human_escalation_reason": escalation_reason or "",
            "renewal_alert_level": alert_level or ("renewal" if after == 0 and support_consumed else ("priority" if after == 1 and support_consumed else ("low" if after is not None and after <= 3 else "none"))),
        }
        if severity in self.SEVERITY_TO_PRIORITY:
            props["hs_ticket_priority"] = self.SEVERITY_TO_PRIORITY[severity]
        if follow_up is not None:
            props["support_follow_up"] = "true" if follow_up else "false"
        if duration is not None:
            props["support_call_duration"] = str(int(duration))
        if before is not None:
            props["support_tickets_before"] = str(int(before))
        if after is not None:
            props["support_tickets_after"] = str(int(after))
        return {key: value for key, value in props.items() if value not in {None, ""}}

    def _ensure_ticket(self, call):
        call_id = call.get("call_id") or call.get("telnyx_call_id")
        ticket_id = self._ticket_id(call_id)
        if ticket_id:
            return ticket_id
        if call_id:
            existing = self.hubspot.find_ticket_by_call_id(call_id)
            if existing:
                ticket_id = str(existing.get("id"))
                self._link_ticket(call_id, ticket_id)
                return ticket_id
        ticket = self.hubspot.create_ticket(
            self._ticket_properties(call, match_status=call.get("customer_match_status", "unknown"), duration=call.get("duration_seconds")),
            company_id=call.get("company_id"), contact_id=call.get("contact_id"),
        )
        ticket_id = str(ticket.get("id"))
        if call_id and ticket_id:
            self._link_ticket(call_id, ticket_id)
        logger.info("ticket_created company_present=%s", bool(call.get("company_id")))
        return ticket_id

    def _call_context(self, phone):
        normalized = normalize_phone(phone, self.support_config.default_country_code)
        context = self.hubspot.lookup_customer(normalized, self.support_config.default_country_code)
        logger.info("customer_lookup status=%s", context.get("customer_match_status"))
        return normalized, context

    def process_telnyx_event(self, event):
        try:
            event_type = event.get("event_type", "")
            data = extract_call_data(event)
            if not data["call_id"]:
                self._event_status(event, "PROCESSED")
                return {"ok": True, "ignored": "no_call_id"}
            if any(token in event_type for token in ("initiated", "incoming", "ringing", "answered", "bridged")):
                normalized, context = self._call_context(data["from_phone"])
                call = {
                    "call_id": data["call_id"], "telnyx_call_id": data["telnyx_call_id"],
                    "caller_phone": normalized, "company_id": context.get("company_id"),
                    "contact_id": context.get("contact_id"), "company_name": context.get("company_name"),
                    "customer_match_status": context.get("customer_match_status"),
                    "duration_seconds": data.get("duration_seconds"),
                }
                old = self._session(data["call_id"])
                values = {
                    "TELNYX_CALL_ID": data["telnyx_call_id"], "FROM_PHONE": normalized,
                    "TO_PHONE": data["to_phone"], "CUSTOMER_MATCH_STATUS": context.get("customer_match_status"),
                    "CONTACT_ID": context.get("contact_id"), "COMPANY_ID": context.get("company_id"),
                    "COMPANY_NAME": context.get("company_name"), "CONTEXT_JSON": _json(context),
                    "STATE": "answered" if "answered" in event_type or "bridged" in event_type else "started",
                    "STARTED_AT": event.get("occurred_at") or _now(),
                    "ANSWERED_AT": event.get("occurred_at") if "answered" in event_type or "bridged" in event_type else None,
                }
                self._save_session(data["call_id"], values)
                self._ensure_ticket(call)
                logger.info("incoming_call match=%s", context.get("customer_match_status"))
            elif "hangup" in event_type or "ended" in event_type or "completed" in event_type:
                session = self._session(data["call_id"])
                self._save_session(data["call_id"], {
                    "STATE": "ended", "ENDED_AT": event.get("occurred_at") or _now(),
                    "DURATION_SECONDS": data.get("duration_seconds"),
                })
                if session and session.get("TICKET_ID") and data.get("duration_seconds") is not None:
                    self.hubspot.update_ticket(session["TICKET_ID"], {"support_call_duration": str(data["duration_seconds"])})
            self._event_status(event, "PROCESSED")
            return {"ok": True}
        except Exception as exc:
            logger.warning("telnyx_error type=%s", type(exc).__name__)
            self._event_status(event, "ERROR", exc)
            raise

    def _server_allows_consumption(self, final, session, follow_up=False):
        if follow_up:
            return False, "follow-up su ticket già aperto: nessun nuovo intervento"
        if final.get("intent") in {"status_check", "follow_up", "callback", "commercial"}:
            return False, "intento non conteggiabile ({})".format(final.get("intent"))
        if not final.get("support_consumed_proposed"):
            return False, "AI non ha dichiarato intervento conteggiabile"
        if not session or session.get("CUSTOMER_MATCH_STATUS") != "found" or not session.get("COMPANY_ID"):
            return False, "cliente non riconosciuto univocamente"
        if final.get("category") not in {"access_control", "turnstile", "software", "configuration", "migration", "hardware", "other"}:
            return False, "categoria non tecnica"
        if final.get("resolution_status") not in set(self.support_config.support_consumption_statuses):
            return False, "esito non conteggiabile"
        if not (final.get("summary") or final.get("resolution") or final.get("resolved") or final.get("escalation_required")):
            return False, "mancano dati di assistenza"
        return True, final.get("reason_for_consumption") or "Technical assistance completed"

    def _consume_locked(self, final, session, ticket_id, event, follow_up=False):
        call_id = session["CALL_ID"]
        con = self._conn()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._serialize_company(con, session["COMPANY_ID"])
            existing = con.execute("SELECT * FROM SUPPORT_CONSUMPTION_LEDGER WHERE CALL_ID=?", (call_id,)).fetchone()
            if existing:
                return {
                    "consumed": bool(existing["CONSUMED"]), "before": existing["BEFORE_COUNT"],
                    "after": existing["AFTER_COUNT"], "reason": existing["REASON"], "duplicate": True,
                }
            company = self.hubspot.get_company(session["COMPANY_ID"])
            props = company.get("properties") or {}
            total = _int(props.get("support_tickets_total"))
            used = _int(props.get("support_tickets_used"))
            before = max(0, _int(props.get("support_tickets_remaining"), max(0, total - used)))
            plan_status = str(props.get("support_plan_status") or "active").lower()
            allowed, reason = self._server_allows_consumption(final, session, follow_up=follow_up)
            consumed = bool(allowed and before > 0 and plan_status not in {"unlimited", "suspended"})
            if plan_status == "unlimited":
                reason = "Piano illimitato: nessun credito da scalare"
            elif plan_status == "suspended":
                reason = "Piano sospeso: consumo bloccato"
            elif allowed and before <= 0:
                reason = "Pacchetto assistenza esaurito"
            after = before - 1 if consumed else before
            if consumed:
                status = "exhausted" if after == 0 else ("priority" if after == 1 else ("low" if after <= 3 else "none"))
                plan_after = "exhausted" if after == 0 else ("low" if after <= 3 else "active")
                self.hubspot.update_company(session["COMPANY_ID"], {
                    "support_tickets_used": str(used + 1),
                    "support_tickets_remaining": str(after),
                    "support_plan_status": plan_after,
                    "last_support_intervention": dt.datetime.now(dt.timezone.utc).date().isoformat(),
                })
                if status in {"low", "priority", "renewal", "exhausted"}:
                    try:
                        self.hubspot.create_renewal_task(session["COMPANY_ID"], session.get("COMPANY_NAME"), "renewal" if status in {"renewal", "exhausted"} else status)
                    except Exception:
                        logger.warning("hubspot_error renewal_task_failed")
            else:
                status = "renewal" if before == 0 and allowed else "none"
                if before == 0 and allowed and plan_status not in {"unlimited", "suspended"}:
                    # Anche un intervento valido tentato a pacchetto esaurito
                    # deve generare il rinnovo, ma una sola volta per azienda.
                    self.hubspot.update_company(session["COMPANY_ID"], {
                        "support_tickets_remaining": "0", "support_plan_status": "exhausted",
                    })
                    already_alerted = con.execute(
                        """SELECT 1 FROM SUPPORT_CONSUMPTION_LEDGER
                           WHERE COMPANY_ID=? AND AFTER_COUNT=0 AND STATUS IN ('APPLIED','SKIPPED') LIMIT 1""",
                        (session["COMPANY_ID"],),
                    ).fetchone()
                    if not already_alerted:
                        try:
                            self.hubspot.create_renewal_task(session["COMPANY_ID"], session.get("COMPANY_NAME"), "renewal")
                        except Exception:
                            logger.warning("hubspot_error renewal_task_failed")
            con.execute(
                """INSERT INTO SUPPORT_CONSUMPTION_LEDGER
                   (CALL_ID,EVENT_ID,COMPANY_ID,HUBSPOT_TICKET_ID,BEFORE_COUNT,AFTER_COUNT,CONSUMED,STATUS,REASON)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (call_id, event["event_id"], session["COMPANY_ID"], ticket_id, before, after, int(consumed),
                 "APPLIED" if consumed else "SKIPPED", reason),
            )
            con.execute(
                """INSERT INTO SUPPORT_AUDIT_LOG
                   (EVENT_KEY,CALL_ID,COMPANY_ID,HUBSPOT_TICKET_ID,BEFORE_COUNT,CONSUMED,AFTER_COUNT,REASON,SOURCE,TELNYX_CALL_ID,VOICE_AI_CALL_ID,ACTOR)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("consume:" + call_id, call_id, session["COMPANY_ID"], ticket_id, before, int(consumed), after, reason,
                 "AI Phone", session.get("TELNYX_CALL_ID"), session.get("VOICE_AI_CALL_ID"), "voice_ai"),
            )
            con.commit()
            logger.info("support_%s before=%s after=%s", "consumed" if consumed else "not_consumed", before, after)
            return {"consumed": consumed, "before": before, "after": after, "reason": reason, "alert": status}
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _parse_iso(value):
        try:
            return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def _preexisting_open_tickets(self, session, category):
        """Ticket aperti stessa categoria creati PRIMA dell'inizio di questa chiamata.

        Il vincolo temporale evita di scambiare per follow-up due chiamate
        concorrenti che aprono ticket nello stesso istante: solo un ticket già
        esistente all'arrivo della chiamata classifica un follow-up.
        """
        company_id = session.get("COMPANY_ID")
        if not company_id or not category:
            return []
        started = self._parse_iso(session.get("STARTED_AT"))
        result = []
        for ticket in self.hubspot.find_open_tickets_by_company(company_id, category):
            props = ticket.get("properties") or {}
            created = self._parse_iso(props.get("hs_createdate") or ticket.get("createdAt"))
            if started is None:
                result.append(ticket)
            elif created is not None and created < started:
                result.append(ticket)
        return result

    def _ensure_voice_session(self, call_id, final):
        """Crea la sessione dal payload Voice AI quando manca l'evento Telnyx.

        Senza numero chiamante non si inventa un cliente: la sessione non viene
        creata e l'evento resta ritentabile.
        """
        if not call_id:
            return None
        from_phone = final.get("from_phone")
        if not from_phone:
            return None
        normalized, context = self._call_context(from_phone)
        self._save_session(call_id, {
            # Il call_id Retell fa anche da riferimento univoco per il ticket,
            # così find_ticket_by_call_id e il ledger restano idempotenti.
            "TELNYX_CALL_ID": final.get("telnyx_call_id") or call_id,
            "VOICE_AI_CALL_ID": final.get("voice_ai_call_id"),
            "FROM_PHONE": normalized,
            "TO_PHONE": final.get("to_phone"),
            "CUSTOMER_MATCH_STATUS": context.get("customer_match_status"),
            "CONTACT_ID": context.get("contact_id"),
            "COMPANY_ID": context.get("company_id"),
            "COMPANY_NAME": context.get("company_name"),
            "CONTEXT_JSON": _json(context),
            "STATE": "started",
            "STARTED_AT": _now(),
        })
        logger.info("incoming_call source=voice_ai match=%s", context.get("customer_match_status"))
        return self._session(call_id)

    def process_voice_event(self, event):
        try:
            final = map_final_event(event)
            call_id = final["call_id"]
            session = self._session(call_id)
            if not session:
                # Retell/Voice AI può essere l'unica sorgente (senza webhook Telnyx):
                # ricostruiamo la sessione dal numero chiamante presente nel payload,
                # con lo stesso lookup cliente usato dal flusso Telnyx.
                session = self._ensure_voice_session(call_id, final)
            if not session:
                self._event_status(event, "ERROR", "sessione chiamata non trovata")
                return {"ok": False, "reason": "session_not_found"}
            ticket_id = session.get("TICKET_ID") or self._ticket_id(call_id)
            # Dedup: se il cliente ha già un ticket aperto della stessa categoria e
            # questa chiamata non ne ha ancora uno proprio, si riusa quello (niente
            # ticket duplicato). In ogni caso, la presenza di un altro ticket aperto
            # sullo stesso problema classifica la chiamata come follow-up.
            preexisting = self._preexisting_open_tickets(session, final.get("category"))
            if not ticket_id and preexisting:
                ticket_id = str(preexisting[0].get("id"))
                self._save_session(call_id, {"TICKET_ID": ticket_id})
            # Gate: il ticket di assistenza si crea SOLO per un cliente riconosciuto
            # (Azienda in HubSpot). Un non-cliente non "procede": nessun ticket,
            # nessuno scalo. La chiamata resta comunque tracciata come Call.
            if not ticket_id and session.get("COMPANY_ID"):
                call = {"call_id": call_id, "telnyx_call_id": session.get("TELNYX_CALL_ID"), "caller_phone": session.get("FROM_PHONE"),
                        "company_id": session.get("COMPANY_ID"), "contact_id": session.get("CONTACT_ID"), "company_name": session.get("COMPANY_NAME"),
                        "customer_match_status": session.get("CUSTOMER_MATCH_STATUS"), "duration_seconds": final.get("duration_seconds")}
                ticket_id = self._ensure_ticket(call)
            # follow_up: esiste un ticket aperto preesistente sullo stesso problema
            # (riusato per dedup, o comunque diverso da quello di questa chiamata).
            follow_up = bool([item for item in preexisting if str(item.get("id")) != str(ticket_id)]) or (
                bool(preexisting) and str(preexisting[0].get("id")) == str(ticket_id)
            )
            self._save_session(call_id, {"VOICE_AI_CALL_ID": final.get("voice_ai_call_id"), "SUMMARY_JSON": _json(final), "STATE": "completed", "DURATION_SECONDS": final.get("duration_seconds")})
            ticket_call = {"call_id": call_id, "telnyx_call_id": session.get("TELNYX_CALL_ID"), "voice_ai_call_id": final.get("voice_ai_call_id"),
                           "caller_phone": session.get("FROM_PHONE"), "company_name": session.get("COMPANY_NAME")}
            common_fields = dict(
                summary=final["summary"], resolution=final["resolution"], category=final["category"],
                status=final["resolution_status"], match_status=session.get("CUSTOMER_MATCH_STATUS"),
                duration=final.get("duration_seconds"), severity=final.get("severity"),
                troubleshooting=final.get("troubleshooting"), device=final.get("device"),
                intent=final.get("intent"), escalation_reason=final.get("human_escalation_reason"),
                follow_up=follow_up,
            )
            result = {"consumed": False, "before": None, "after": None, "reason": "non conteggiato"}
            if ticket_id:
                # Prima scrive il contenuto finale; il flag definitivo resta false
                # fino a quando il ledger ha applicato il decremento CRM.
                self.hubspot.update_ticket(ticket_id, self._ticket_properties(
                    ticket_call, support_consumed=False, reason=final.get("reason_for_consumption"), **common_fields,
                ))
                if session.get("COMPANY_ID"):
                    result = self._consume_locked(final, session, ticket_id, event, follow_up=follow_up)
                self.hubspot.update_ticket(ticket_id, self._ticket_properties(
                    ticket_call, support_consumed=result.get("consumed", False), before=result.get("before"),
                    after=result.get("after"), reason=result.get("reason"),
                    voice_ai_call_id=final.get("voice_ai_call_id"), alert_level=result.get("alert"), **common_fields,
                ))
                self._save_session(call_id, {"TICKET_ID": ticket_id})
            else:
                result["reason"] = "cliente non riconosciuto: nessun ticket di assistenza"
            try:
                self._log_call_to_hubspot(session, final, ticket_id, result, follow_up=follow_up)
            except Exception:
                logger.warning("hubspot_error call_log_failed")
            self._event_status(event, "PROCESSED")
            return {"ok": True, "ticket_id": ticket_id,
                    "eligible": bool(session.get("COMPANY_ID")), **result}
        except Exception as exc:
            logger.warning("voice_ai_error type=%s", type(exc).__name__)
            self._event_status(event, "ERROR", exc)
            raise

    def context_for_phone(self, phone):
        normalized, context = self._call_context(phone)
        open_tickets = []
        if context.get("company_id"):
            try:
                for item in self.hubspot.find_open_tickets_by_company(context["company_id"]):
                    props = item.get("properties") or {}
                    open_tickets.append({
                        "ticket_id": str(item.get("id")),
                        "category": props.get("support_issue_category"),
                        "status": props.get("support_resolution_status"),
                        "subject": props.get("subject"),
                    })
            except Exception:
                logger.warning("hubspot_error open_tickets_context_failed")
        finance_summary = {}
        if context.get("company_id"):
            try:
                company = self.hubspot.get_company(context["company_id"])
                finance_summary = finance.summary_from_props(company.get("properties") or {})
            except Exception:
                logger.warning("hubspot_error finance_context_failed")
        return {"caller_phone": normalized, "open_tickets": open_tickets,
                "open_tickets_count": len(open_tickets),
                "finance": finance_summary,
                **{key: context.get(key) for key in (
                    "customer_found", "customer_match_status", "company_id", "company_name", "contact_id", "contact_name",
                    "support_plan_status", "support_tickets_total", "support_tickets_used", "support_tickets_remaining",
                )}}

    def _resolve_company(self, company_id="", phone="", company_name=""):
        """Trova il Company per id, poi per numero, poi per nome (univoco)."""
        if company_id:
            try:
                return self.hubspot.get_company(company_id)
            except Exception:
                return None
        if phone:
            _, ctx = self._call_context(phone)
            if ctx.get("company_id"):
                try:
                    return self.hubspot.get_company(ctx["company_id"])
                except Exception:
                    pass
        if company_name:
            try:
                companies = self.hubspot.search_companies_by_name(company_name)
                unique = {str(item.get("id")): item for item in companies if item.get("id")}
                if len(unique) == 1:
                    return next(iter(unique.values()))
            except Exception:
                pass
        return None

    def sync_finance(self, company_id="", phone="", company_name="", payload=None, actor="gestionale"):
        """Import dal gestionale Palesya: aggiorna il quadro finanziario di un Company.

        Calcola scadenze/prossimo pagamento/stato dal listino e mappa tutto sulle
        proprietà HubSpot. Non tocca gli interventi USATI (li governa il consumo
        deterministico), ma ricalcola i residui su total-used.
        """
        payload = payload or {}
        company = self._resolve_company(company_id, phone, company_name)
        if not company:
            raise ValueError("azienda non trovata")
        cid = str(company.get("id"))
        props = company.get("properties") or {}
        used = _int(props.get("support_tickets_used"))
        fin_props, summary = finance.compute_finance(payload, current_used=used)
        self.hubspot.update_company(cid, fin_props)
        logger.info("finance_synced company=%s stato=%s", cid, summary.get("stato_pagamento"))
        return {"ok": True, "company_id": cid, "company_name": props.get("name"), **summary}

    def financial_status(self, phone="", company_id="", company_name=""):
        """Quadro finanziario leggibile dall'AI (solo lettura)."""
        company = self._resolve_company(company_id, phone, company_name)
        if not company:
            return {"found": False}
        props = company.get("properties") or {}
        return {"found": True, "company_id": str(company.get("id")),
                "company_name": props.get("name"), **finance.summary_from_props(props)}

    def _context_from_company(self, company, status="found"):
        props = (company or {}).get("properties") or {}
        total = _int(props.get("support_tickets_total"))
        used = _int(props.get("support_tickets_used"))
        remaining_value = props.get("support_tickets_remaining")
        try:
            remaining = max(0, int(float(remaining_value))) if remaining_value is not None else max(0, total - used)
        except (TypeError, ValueError):
            remaining = max(0, total - used)
        return {
            "customer_found": status == "found",
            "customer_match_status": status,
            "company_id": str(company.get("id")) if company else None,
            "company_name": props.get("name") or "Unknown Caller",
            "contact_id": None,
            "support_plan_status": props.get("support_plan_status") or ("active" if total else "exhausted"),
            "support_tickets_total": total, "support_tickets_used": used,
            "support_tickets_remaining": remaining,
        }

    def _eligibility(self, phone, context):
        """Determina se il chiamante è un cliente idoneo all'assistenza.

        Oggi: idoneo = Azienda riconosciuta in HubSpot (chi ha acquistato ha una
        Company). Estensibile al check Deals quando lo scope sarà attivo.
        """
        status = context.get("customer_match_status")
        eligible = bool(status == "found" and context.get("company_id"))
        open_tickets = []
        if context.get("company_id"):
            try:
                open_tickets = self.hubspot.find_open_tickets_by_company(context["company_id"])
            except Exception:
                open_tickets = []
        return {
            "caller_phone": phone,
            "eligible_for_support": eligible,
            "open_tickets_count": len(open_tickets),
            **{key: context.get(key) for key in (
                "customer_found", "customer_match_status", "company_id", "company_name", "contact_id",
                "support_plan_status", "support_tickets_total", "support_tickets_used", "support_tickets_remaining",
            )},
        }

    def _bind_session_customer(self, call_id, normalized, context):
        """Se identificato per nome, persiste il cliente sulla sessione della chiamata."""
        if not call_id or context.get("customer_match_status") != "found" or not context.get("company_id"):
            return
        self._save_session(call_id, {
            "FROM_PHONE": normalized or None,
            "CUSTOMER_MATCH_STATUS": "found",
            "COMPANY_ID": context.get("company_id"),
            "COMPANY_NAME": context.get("company_name"),
            "CONTACT_ID": context.get("contact_id"),
        })

    def verify_customer(self, phone="", company_name="", contact_name="", call_id=""):
        """Riconosce il cliente: prima dal Caller ID, poi dal nome palestra (1-2 domande)."""
        normalized = normalize_phone(phone, self.support_config.default_country_code) if phone else ""
        phone_context = None
        if phone:
            _, phone_context = self._call_context(phone)
            if phone_context.get("customer_match_status") == "found":
                self._bind_session_customer(call_id, normalized, phone_context)
                return self._eligibility(normalized, phone_context)
        if company_name:
            companies = self.hubspot.search_companies_by_name(company_name)
            unique = {str(item.get("id")): item for item in companies if item.get("id")}
            if len(unique) == 1:
                context = self._context_from_company(next(iter(unique.values())), "found")
                self._bind_session_customer(call_id, normalized, context)
                return self._eligibility(normalized, context)
            if len(unique) > 1:
                return self._eligibility(normalized, {
                    "customer_match_status": "ambiguous", "customer_found": False,
                    "company_id": None, "company_name": None,
                })
        if phone_context is not None:
            return self._eligibility(normalized, phone_context)
        return self._eligibility(normalized, {
            "customer_match_status": "not_found", "customer_found": False,
            "company_id": None, "company_name": None,
        })

    def create_callback(self, call_id, phone="", reason="", name="", actor="voice_ai", company_name=""):
        """Registra una richiesta di ricontatto / lead (idempotente per call_id).

        Come il ticket: usa la palestra riconosciuta dal CRM; se il numero non è
        riconosciuto prova a risolverla dal nome detto a voce e comunque la registra
        (i lead commerciali spesso NON sono ancora clienti).
        """
        call_id = str(call_id or "").strip()
        if not call_id:
            raise ValueError("call_id obbligatorio")
        session = self._session(call_id) or {}
        company_id = session.get("COMPANY_ID")
        contact_id = session.get("CONTACT_ID")
        stated_company = str(company_name or "").strip()
        if not company_id and stated_company:
            try:
                companies = self.hubspot.search_companies_by_name(stated_company)
                unique = {str(item.get("id")): item for item in companies if item.get("id")}
                if len(unique) == 1:
                    company_id = next(iter(unique))
            except Exception:
                logger.warning("hubspot_error resolve_company_by_name_failed")
        resolved_company = session.get("COMPANY_NAME")
        if resolved_company in (None, "", "Unknown Caller", "Cliente sconosciuto"):
            resolved_company = None
        company_name = resolved_company or stated_company or None
        caller_phone = normalize_phone(phone, self.support_config.default_country_code) if phone else session.get("FROM_PHONE")
        con = self._conn()
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT STATUS FROM SUPPORT_CALLBACKS WHERE CALL_ID=?", (call_id,)).fetchone()
            if existing:
                con.commit()
                return {"ok": True, "duplicate": True, "call_id": call_id}
            task_id = None
            try:
                task = self.hubspot.create_callback_task(company_id, contact_id, company_name, caller_phone, name, reason)
                task_id = str(task.get("id")) if task else None
            except Exception:
                logger.warning("hubspot_error callback_task_failed")
            con.execute(
                """INSERT INTO SUPPORT_CALLBACKS
                   (CALL_ID,COMPANY_ID,CONTACT_ID,CALLER_PHONE,CALLER_NAME,COMPANY_NAME,REASON,CATEGORY,HUBSPOT_TASK_ID)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (call_id, company_id, contact_id, caller_phone, (name or None), company_name,
                 (reason or None), (session.get("SUMMARY_JSON") and None), task_id),
            )
            con.execute(
                """INSERT INTO SUPPORT_AUDIT_LOG(EVENT_KEY,CALL_ID,COMPANY_ID,REASON,SOURCE,ACTOR)
                   VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                ("callback:" + call_id, call_id, company_id, (reason or "richiamo assistenza"), "Callback", actor),
            )
            con.commit()
            logger.info("callback_created company_present=%s task_present=%s", bool(company_id), bool(task_id))
            return {"ok": True, "call_id": call_id, "task_id": task_id}
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _log_call_to_hubspot(self, session, final, ticket_id, result, follow_up=False):
        """Registra la chiamata come Call nativo HubSpot con tutte le metriche, una volta sola."""
        call_id = session.get("CALL_ID")
        if not call_id:
            return None
        con = self._conn()
        try:
            if con.execute("SELECT 1 FROM SUPPORT_CALL_LOG WHERE CALL_ID=?", (call_id,)).fetchone():
                return None
        finally:
            con.close()
        started = self._parse_iso(session.get("STARTED_AT")) or dt.datetime.now(dt.timezone.utc)
        timestamp_ms = int(started.timestamp() * 1000)
        duration = final.get("duration_seconds")
        agent = "Mariarosia" if final.get("intent") == "commercial" else "Silvia"
        metrics = [
            "Cliente riconosciuto: {}".format(session.get("CUSTOMER_MATCH_STATUS") or "unknown"),
            "Palestra: {}".format(session.get("COMPANY_NAME") or "-"),
            "Agente: {}".format(agent),
            "Categoria: {}".format(final.get("category") or "-"),
            "Esito: {}".format(final.get("resolution_status") or "-"),
            "Severity: {}".format(final.get("severity") or "-"),
            "Follow-up: {}".format("sì" if follow_up else "no"),
            "Ticket: {}".format(ticket_id or "-"),
            "Intervento consumato: {}".format("sì" if result.get("consumed") else "no"),
            "Interventi residui: {}".format(result.get("after") if result.get("after") is not None else "-"),
            "Riepilogo: {}".format((final.get("summary") or "")[:2000]),
        ]
        properties = {
            "hs_timestamp": str(timestamp_ms),
            "hs_call_title": "Chiamata assistenza — {}".format(session.get("COMPANY_NAME") or session.get("FROM_PHONE") or "sconosciuto"),
            "hs_call_body": "\n".join(metrics),
            "hs_call_from_number": session.get("FROM_PHONE") or "",
            "hs_call_to_number": session.get("TO_PHONE") or "",
            "hs_call_direction": "INBOUND",
            "hs_call_status": "COMPLETED",
        }
        if duration is not None:
            properties["hs_call_duration"] = str(int(duration) * 1000)
        properties = {key: value for key, value in properties.items() if value not in {None, ""}}
        call = self.hubspot.create_call(properties, company_id=session.get("COMPANY_ID"), contact_id=session.get("CONTACT_ID"))
        hubspot_call_id = str(call.get("id")) if call else None
        con = self._conn()
        try:
            con.execute("INSERT INTO SUPPORT_CALL_LOG(CALL_ID,HUBSPOT_CALL_ID) VALUES(?,?) ON CONFLICT DO NOTHING",
                        (call_id, hubspot_call_id))
            con.commit()
        finally:
            con.close()
        logger.info("call_logged company_present=%s", bool(session.get("COMPANY_ID")))
        return hubspot_call_id

    def create_commercial_request(self, call_id, phone="", company_name="", contact_name="",
                                   structure="", need="", outcome="nuovo"):
        """Crea una richiesta commerciale (Deal) da Mariarosia, idempotente per call_id.

        Richiede lo scope Deals: senza, la creazione del Deal solleva e l'evento
        resta ritentabile. Nessun ticket di assistenza è coinvolto.
        """
        call_id = str(call_id or "").strip()
        if not call_id:
            raise ValueError("call_id obbligatorio")
        con = self._conn()
        try:
            if con.execute("SELECT HUBSPOT_DEAL_ID FROM SUPPORT_COMMERCIAL_LINKS WHERE CALL_ID=?", (call_id,)).fetchone():
                return {"ok": True, "duplicate": True, "call_id": call_id}
        finally:
            con.close()
        session = self._session(call_id) or {}
        company_id = session.get("COMPANY_ID")
        contact_id = session.get("CONTACT_ID")
        resolved_name = session.get("COMPANY_NAME") or company_name
        normalized = normalize_phone(phone, self.support_config.default_country_code) if phone else session.get("FROM_PHONE")
        if not company_id and company_name:
            try:
                companies = self.hubspot.search_companies_by_name(company_name)
                unique = {str(item.get("id")): item for item in companies if item.get("id")}
                if len(unique) == 1:
                    company_id = next(iter(unique))
            except Exception:
                pass
        pipeline = self.hubspot.ensure_commercial_pipeline()
        stages = pipeline.get("stages") or {}
        outcome_label = {
            "nuovo": "nuovo lead", "qualificato": "qualificato", "demo_fissata": "demo fissata",
            "vinto": "vinto", "perso": "perso",
        }.get(str(outcome or "nuovo").strip().lower(), "nuovo lead")
        props = {
            "dealname": "Richiesta commerciale — {}".format(resolved_name or contact_name or normalized or "prospect"),
            "pipeline": pipeline.get("id"),
            "dealstage": stages.get(outcome_label) or (next(iter(stages.values()), None)),
            "tipo_struttura": str(structure or "").strip().lower() or None,
            "esigenza_commerciale": need or None,
            "esito_qualifica": str(outcome or "nuovo").strip().lower(),
            "caller_phone": normalized or None,
        }
        props = {key: value for key, value in props.items() if value not in {None, ""}}
        deal = self.hubspot.create_deal(props, company_id=company_id, contact_id=contact_id)
        deal_id = str(deal.get("id")) if deal else None
        con = self._conn()
        try:
            con.execute("INSERT INTO SUPPORT_COMMERCIAL_LINKS(CALL_ID,HUBSPOT_DEAL_ID) VALUES(?,?) ON CONFLICT DO NOTHING",
                        (call_id, deal_id))
            con.commit()
        finally:
            con.close()
        logger.info("commercial_request_created company_present=%s", bool(company_id))
        return {"ok": True, "call_id": call_id, "deal_id": deal_id}

    def upsert_ticket(self, call_id, phone="", category="other", summary="", severity="",
                      troubleshooting="", device="", intent="technical", escalation_reason="",
                      description="", contact_name="", company_name=""):
        """Crea/aggiorna il ticket durante la chiamata SENZA scalare crediti.

        Contratto minimo per l'agente vocale: bastano ``call_id`` + ``phone`` +
        ``description`` (il problema raccontato a voce). Categoria e severity, se
        non passate esplicitamente, vengono dedotte dal testo. I campi granulari
        (category/severity/troubleshooting/device) restano accettati per retro-
        compatibilità e, se presenti, hanno la precedenza sull'inferenza.

        Serve a Silvia per lasciare al tecnico umano un ticket già compilato prima
        di un eventuale transfer. Il consumo resta deterministico su call_analyzed.
        """
        call_id = str(call_id or "").strip()
        if not call_id:
            raise ValueError("call_id obbligatorio")
        # Descrizione libera come fonte primaria: alimenta summary e inferenze.
        description = str(description or "").strip()
        summary = str(summary or "").strip() or description
        troubleshooting = str(troubleshooting or "").strip()
        classify_text = " ".join(filter(None, (description, summary, troubleshooting)))
        category = str(category or "").strip().lower()
        if category in ("", "other"):
            category = self._infer_category(classify_text)
        severity = str(severity or "").strip().lower()
        if not severity:
            severity = self._infer_severity(classify_text)
        session = self._session(call_id)
        if not session and phone:
            session = self._ensure_voice_session(call_id, {
                "from_phone": phone, "voice_ai_call_id": call_id,
                "telnyx_call_id": call_id, "to_phone": "",
            })
        if not session:
            raise ValueError("sessione non disponibile: fornire il numero chiamante")
        category = str(category or "other").strip().lower()
        contact_name = str(contact_name or "").strip()
        company_name = str(company_name or "").strip()
        # "Controlla nel gestionale": se il numero non ha riconosciuto la palestra,
        # prova a risolverla dal nome detto a voce e legala alla sessione/ticket.
        if not session.get("COMPANY_ID") and company_name:
            try:
                companies = self.hubspot.search_companies_by_name(company_name)
                unique = {str(item.get("id")): item for item in companies if item.get("id")}
                if len(unique) == 1:
                    context = self._context_from_company(next(iter(unique.values())), "found")
                    self._bind_session_customer(call_id, session.get("FROM_PHONE"), context)
                    session = self._session(call_id) or session
            except Exception:
                logger.warning("hubspot_error resolve_company_by_name_failed")
        # Nome palestra da mostrare: quello riconosciuto dal CRM, altrimenti quello
        # detto a voce. I placeholder di "non riconosciuto" non devono vincere sul nome reale.
        sess_company = session.get("COMPANY_NAME")
        if sess_company in (None, "", "Unknown Caller", "Cliente sconosciuto"):
            sess_company = None
        display_company = sess_company or company_name
        open_tickets = []
        if session.get("COMPANY_ID"):
            open_tickets = self.hubspot.find_open_tickets_by_company(session["COMPANY_ID"], category)
        ticket_id = session.get("TICKET_ID") or self._ticket_id(call_id)
        if not ticket_id and open_tickets:
            ticket_id = str(open_tickets[0].get("id"))
        if not ticket_id:
            ticket_id = self._ensure_ticket({
                "call_id": call_id, "telnyx_call_id": session.get("TELNYX_CALL_ID"),
                "caller_phone": session.get("FROM_PHONE"), "company_id": session.get("COMPANY_ID"),
                "contact_id": session.get("CONTACT_ID"), "company_name": display_company,
                "customer_match_status": session.get("CUSTOMER_MATCH_STATUS"),
            })
        follow_up = bool([item for item in open_tickets if str(item.get("id")) != str(ticket_id)])
        self.hubspot.update_ticket(ticket_id, self._ticket_properties(
            {"call_id": call_id, "telnyx_call_id": session.get("TELNYX_CALL_ID"),
             "caller_phone": session.get("FROM_PHONE"), "company_name": display_company},
            support_consumed=False, summary=summary, category=category, status="investigating",
            match_status=session.get("CUSTOMER_MATCH_STATUS"), severity=str(severity or "").strip().lower(),
            troubleshooting=troubleshooting, device=device, intent=str(intent or "technical").strip().lower(),
            escalation_reason=escalation_reason, follow_up=follow_up, contact_name=contact_name,
        ))
        self._save_session(call_id, {"TICKET_ID": ticket_id})
        return {"ok": True, "ticket_id": ticket_id, "follow_up": follow_up,
                "customer_found": bool(session.get("COMPANY_ID")),
                "company_name": display_company or None,
                "contact_name": contact_name or None,
                "category": category, "severity": severity,
                "existing_open_tickets": [str(item.get("id")) for item in open_tickets]}

    def reverse_consumption(self, call_id, actor="admin"):
        con = self._conn()
        try:
            con.execute("BEGIN IMMEDIATE")
            ledger = con.execute("SELECT * FROM SUPPORT_CONSUMPTION_LEDGER WHERE CALL_ID=?", (call_id,)).fetchone()
            if not ledger:
                raise ValueError("Consumo non trovato")
            if not ledger["CONSUMED"]:
                return {"ok": True, "already_reversed": True, "after": ledger["AFTER_COUNT"]}
            if ledger["REVERSED_AT"]:
                return {"ok": True, "already_reversed": True, "after": ledger["AFTER_COUNT"]}
            company = self.hubspot.get_company(ledger["COMPANY_ID"])
            props = company.get("properties") or {}
            used = _int(props.get("support_tickets_used"))
            total = _int(props.get("support_tickets_total"))
            after = max(0, used - 1)
            remaining = max(0, total - after)
            plan_status = "exhausted" if remaining == 0 else ("low" if remaining <= 3 else "active")
            self.hubspot.update_company(ledger["COMPANY_ID"], {
                "support_tickets_used": str(after), "support_tickets_remaining": str(remaining), "support_plan_status": plan_status,
            })
            if ledger["HUBSPOT_TICKET_ID"]:
                self.hubspot.update_ticket(ledger["HUBSPOT_TICKET_ID"], {"support_consumed": "false", "support_tickets_after": str(remaining), "support_consumption_reason": "Consumo ripristinato manualmente"})
            con.execute("UPDATE SUPPORT_CONSUMPTION_LEDGER SET REVERSED_AT=CURRENT_TIMESTAMP,STATUS='REVERSED' WHERE CALL_ID=?", (call_id,))
            con.execute(
                """INSERT INTO SUPPORT_AUDIT_LOG(EVENT_KEY,CALL_ID,COMPANY_ID,HUBSPOT_TICKET_ID,BEFORE_COUNT,CONSUMED,AFTER_COUNT,REASON,SOURCE,ACTOR)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("reverse:" + call_id, call_id, ledger["COMPANY_ID"], ledger["HUBSPOT_TICKET_ID"], used, 0, remaining, "Consumo ripristinato manualmente", "Admin", actor),
            )
            con.commit()
            return {"ok": True, "after": remaining}
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
