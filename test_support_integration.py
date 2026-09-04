import threading
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from schema import ensure_schema
from support_integration.config import SupportSettings
from support_integration.phone import normalize_phone, phones_match
from support_integration.service import SupportService
from support_integration.telnyx import extract_call_data, verify_signature
from support_integration.voice_ai import map_final_event
from support_integration import finance
from support_integration import expenses as _expenses
from support_integration import usage as _usage
import datetime as _dt


class FakeHubSpot:
    def __init__(self, total=10, used=0, company_id="company-1"):
        self.lock = threading.Lock()
        self.company_id = company_id
        self.company = {
            "id": company_id,
            "properties": {
                "name": "Example SRL",
                "support_tickets_total": str(total),
                "support_tickets_used": str(used),
                "support_tickets_remaining": str(max(0, total - used)),
                "support_plan_status": "active",
            },
        }
        self.tickets = {}
        self.ticket_counter = 0
        self.renewal_tasks = []
        self.fail_company_update_once = False
        self.has_won_deal = True

    def company_has_won_deal(self, company_id):
        return bool(company_id) and self.has_won_deal

    def get_contact(self, contact_id):
        return {"id": contact_id, "properties": {"firstname": "Mario", "lastname": "Rossi"}}

    def lookup_customer(self, phone, default_country_code):
        return {
            "customer_found": True,
            "customer_match_status": "found",
            "contact_id": "contact-1",
            "company_id": self.company_id,
            "company_name": "Example SRL",
            "contact_name": "Mario Rossi",
            "support_plan_status": self.company["properties"]["support_plan_status"],
            "support_tickets_total": int(self.company["properties"]["support_tickets_total"]),
            "support_tickets_used": int(self.company["properties"]["support_tickets_used"]),
            "support_tickets_remaining": int(self.company["properties"]["support_tickets_remaining"]),
        }

    def find_ticket_by_call_id(self, call_id):
        return next((item for item in self.tickets.values() if item["properties"].get("telnyx_call_id") == call_id), None)

    def create_ticket(self, properties, company_id=None, contact_id=None):
        self.ticket_counter += 1
        props = dict(properties)
        props.setdefault("hs_createdate", "2026-08-15T12:00:00+00:00")
        ticket = {"id": str(self.ticket_counter), "properties": props}
        self.tickets[ticket["id"]] = ticket
        return ticket

    def find_open_tickets_by_company(self, company_id, category=None):
        terminal = {"resolved", "closed", "duplicate", "non_support"}
        out = []
        for tid, item in self.tickets.items():
            props = item["properties"]
            if category and props.get("support_issue_category") != category:
                continue
            if str(props.get("support_resolution_status") or "").lower() in terminal:
                continue
            out.append({"id": tid, "properties": dict(props)})
        return out

    def create_callback_task(self, company_id, contact_id, company_name, caller_phone, caller_name, reason):
        self.callbacks = getattr(self, "callbacks", [])
        self.callbacks.append({"reason": reason, "phone": caller_phone})
        return {"id": "callback-task-1"}

    def create_call(self, properties, company_id=None, contact_id=None):
        self.calls = getattr(self, "calls", [])
        self.calls.append({"properties": dict(properties), "company_id": company_id, "contact_id": contact_id})
        return {"id": "call-{}".format(len(self.calls))}

    def search_companies_by_name(self, name):
        token = str(name or "").strip().lower()
        return [dict(self.company)] if token and token in self.company["properties"]["name"].lower() else []

    def ensure_commercial_pipeline(self):
        return {"id": "deal-pipe", "created": False, "stages": {
            "nuovo lead": "s1", "qualificato": "s2", "demo fissata": "s3", "vinto": "s4", "perso": "s5",
        }}

    def create_deal(self, properties, company_id=None, contact_id=None):
        self.deals = getattr(self, "deals", [])
        self.deals.append({"properties": dict(properties), "company_id": company_id, "contact_id": contact_id})
        return {"id": "deal-{}".format(len(self.deals))}

    def update_ticket(self, ticket_id, properties):
        self.tickets[str(ticket_id)]["properties"].update(properties)
        return self.tickets[str(ticket_id)]

    def get_company(self, company_id):
        with self.lock:
            return {"id": self.company["id"], "properties": dict(self.company["properties"])}

    def update_company(self, company_id, properties):
        with self.lock:
            if self.fail_company_update_once:
                self.fail_company_update_once = False
                raise RuntimeError("temporary HubSpot failure")
            self.company["properties"].update({key: str(value) for key, value in properties.items()})
            return self.company

    def create_renewal_task(self, company_id, company_name, alert_level):
        self.renewal_tasks.append(alert_level)
        return {"id": "task-1", "alert": alert_level}


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "support.sqlite"
    ensure_schema(path)
    fake = FakeHubSpot()
    cfg = SupportSettings(
        enabled=True, hubspot_access_token="test", hubspot_base_url="https://example.invalid",
        hubspot_timeout_seconds=1, telnyx_public_key="", telnyx_timestamp_tolerance=300,
        voice_ai_webhook_secret="secret", voice_ai_base_url="", voice_ai_api_key="",
        support_admin_secret="admin", default_country_code="39", support_pipeline_id="",
        support_pipeline_label="Assistenza Tecnica", support_pipeline_stage_id="1",
        support_consumption_statuses=("resolved", "escalated"), allow_unsigned_webhooks=True,
    )
    return SupportService(path, fake, cfg), fake


def _session(service, call_id, company_id="company-1", match="found"):
    service._save_session(call_id, {
        "TELNYX_CALL_ID": "tel-" + call_id, "FROM_PHONE": "+393331234567",
        "CUSTOMER_MATCH_STATUS": match, "COMPANY_ID": company_id if match == "found" else None,
        "COMPANY_NAME": "Example SRL" if match == "found" else None,
        "STATE": "answered", "STARTED_AT": "2026-08-14T10:00:00+00:00",
    })


def _event(call_id, event_id, consumed=True, status="resolved"):
    return {
        "source": "voice_ai", "event_id": event_id, "event_type": "call.completed",
        "call_id": call_id, "data": {
            "id": event_id, "call_id": call_id, "category": "software", "summary": "Errore applicativo",
            "resolution": "Configurazione corretta", "resolved": True,
            "support_consumed": consumed, "resolution_status": status,
        },
    }


def test_phone_normalization_variants():
    assert normalize_phone("3331234567") == "+393331234567"
    assert normalize_phone("00393331234567") == "+393331234567"
    assert phones_match("3331234567", "+393331234567")


def test_valid_interventions_decrement_once(service):
    system, fake = service
    for index, remaining in enumerate((9, 8), start=1):
        call_id = "call-{}".format(index)
        _session(system, call_id)
        event = _event(call_id, "event-{}".format(index))
        assert system.register_event(event)
        result = system.process_voice_event(event)
        assert result["consumed"] is True
        assert int(fake.company["properties"]["support_tickets_remaining"]) == remaining


def test_informational_call_does_not_consume(service):
    system, fake = service
    _session(system, "info-1")
    result = system.process_voice_event(_event("info-1", "info-event", consumed=False))
    assert result["consumed"] is False
    assert fake.company["properties"]["support_tickets_remaining"] == "10"


def test_duplicate_webhook_and_same_call_are_idempotent(service):
    system, fake = service
    _session(system, "dup-1")
    event = _event("dup-1", "dup-event")
    assert system.register_event(event)
    system.process_voice_event(event)
    assert system.register_event(event) is False
    assert fake.company["properties"]["support_tickets_remaining"] == "9"


def test_unknown_or_ambiguous_customer_never_consumes(service):
    system, fake = service
    _session(system, "unknown-1", company_id=None, match="not_found")
    result = system.process_voice_event(_event("unknown-1", "unknown-event"))
    assert result["consumed"] is False
    assert fake.company["properties"]["support_tickets_remaining"] == "10"


def test_exhaustion_never_goes_negative(service):
    system, fake = service
    fake.company["properties"].update({"support_tickets_total": "1", "support_tickets_used": "0", "support_tickets_remaining": "1"})
    _session(system, "last-1")
    assert system.process_voice_event(_event("last-1", "last-event"))["after"] == 0
    assert fake.company["properties"]["support_plan_status"] == "exhausted"
    _session(system, "zero-1")
    result = system.process_voice_event(_event("zero-1", "zero-event"))
    assert result["consumed"] is False
    assert fake.company["properties"]["support_tickets_remaining"] == "0"
    assert fake.renewal_tasks == ["renewal"]


def test_concurrent_consumption_is_serialized(service):
    system, fake = service
    for call_id in ("race-1", "race-2"):
        _session(system, call_id)
    results = []

    def worker(call_id):
        results.append(system.process_voice_event(_event(call_id, "event-" + call_id)))

    first = threading.Thread(target=worker, args=("race-1",))
    second = threading.Thread(target=worker, args=("race-2",))
    first.start(); second.start(); first.join(); second.join()
    assert sum(1 for item in results if item["consumed"]) == 2
    assert fake.company["properties"]["support_tickets_remaining"] == "8"


def test_transient_hubspot_error_is_retryable(service):
    system, fake = service
    fake.fail_company_update_once = True
    _session(system, "retry-1")
    event = _event("retry-1", "retry-event")
    system.register_event(event)
    with pytest.raises(RuntimeError):
        system.process_voice_event(event)
    assert fake.company["properties"]["support_tickets_remaining"] == "10"
    assert system.register_event(event)
    result = system.process_voice_event(event)
    assert result["consumed"] is True
    assert fake.company["properties"]["support_tickets_remaining"] == "9"


def test_retell_only_call_creates_session_and_consumes(service):
    """Con solo il webhook Retell (nessun evento Telnyx) la sessione va creata
    dal numero chiamante del payload e l'intervento va conteggiato una volta."""
    system, fake = service
    event = {
        "source": "voice_ai", "event_id": "retell-analyzed-1",
        "event_type": "call_analyzed", "call_id": "retell-call-1",
        "data": {
            "call_id": "retell-call-1", "from_number": "+393331234567",
            "to_number": "+390811234567",
            "category": "software", "summary": "Errore applicativo",
            "resolution": "Configurazione corretta", "resolved": True,
            "support_consumed": True, "resolution_status": "resolved",
            "duration_ms": 42000,
        },
    }
    assert system.register_event(event)
    result = system.process_voice_event(event)
    assert result["ok"] is True
    assert result["consumed"] is True
    assert int(fake.company["properties"]["support_tickets_remaining"]) == 9
    session = system._session("retell-call-1")
    assert session["FROM_PHONE"] == "+393331234567"
    assert session["CUSTOMER_MATCH_STATUS"] == "found"


def test_retell_only_without_caller_number_is_retryable(service):
    """Senza numero chiamante non si inventa un cliente: nessuna sessione,
    evento ritentabile, nessun consumo."""
    system, fake = service
    event = {
        "source": "voice_ai", "event_id": "retell-analyzed-2",
        "event_type": "call_analyzed", "call_id": "retell-call-2",
        "data": {
            "call_id": "retell-call-2", "category": "software",
            "summary": "x", "resolved": True, "support_consumed": True,
            "resolution_status": "resolved",
        },
    }
    assert system.register_event(event)
    result = system.process_voice_event(event)
    assert result.get("reason") == "session_not_found"
    assert fake.company["properties"]["support_tickets_remaining"] == "10"


def test_followup_on_preexisting_open_ticket_does_not_consume(service):
    """Un ticket aperto preesistente sullo stesso problema → nessun nuovo ticket,
    nessun credito scalato: è un follow-up."""
    system, fake = service
    fake.tickets["900"] = {"id": "900", "properties": {
        "support_issue_category": "software", "support_resolution_status": "investigating",
        "hs_createdate": "2026-08-01T00:00:00+00:00", "subject": "Errore già aperto",
    }}
    fake.ticket_counter = 0
    _session(system, "followup-1")
    result = system.process_voice_event(_event("followup-1", "followup-event"))
    assert result["consumed"] is False
    assert "follow-up" in result["reason"].lower()
    assert result["ticket_id"] == "900"           # riusato, non duplicato
    assert fake.ticket_counter == 0               # nessun ticket nuovo creato
    assert fake.company["properties"]["support_tickets_remaining"] == "10"


def test_status_check_intent_does_not_consume(service):
    system, fake = service
    _session(system, "statuscheck-1")
    event = _event("statuscheck-1", "statuscheck-event")
    event["data"]["intent"] = "status_check"
    result = system.process_voice_event(event)
    assert result["consumed"] is False
    assert fake.company["properties"]["support_tickets_remaining"] == "10"


def test_severity_maps_to_priority(service):
    system, fake = service
    _session(system, "sev-1")
    event = _event("sev-1", "sev-event")
    event["data"]["severity"] = "critical"
    result = system.process_voice_event(event)
    ticket = fake.tickets[result["ticket_id"]]["properties"]
    assert ticket["support_severity"] == "critical"
    assert ticket["hs_ticket_priority"] == "URGENT"


def test_callback_is_idempotent(service):
    system, fake = service
    _session(system, "cb-1")
    first = system.create_callback("cb-1", reason="operatore non disponibile", name="Mario")
    second = system.create_callback("cb-1", reason="operatore non disponibile", name="Mario")
    assert first["ok"] is True
    assert second.get("duplicate") is True
    assert len(getattr(fake, "callbacks", [])) == 1


def test_upsert_ticket_creates_without_consuming(service):
    system, fake = service
    result = system.upsert_ticket(
        "mid-1", phone="+393331234567", category="turnstile",
        summary="Tornello bloccato", severity="high", troubleshooting="Riavvio lettore",
    )
    assert result["ok"] is True
    ticket = fake.tickets[result["ticket_id"]]["properties"]
    assert ticket["support_issue_category"] == "turnstile"
    assert ticket["support_consumed"] == "false"
    assert fake.company["properties"]["support_tickets_remaining"] == "10"


def test_upsert_ticket_infers_category_and_severity_from_description(service):
    system, fake = service
    result = system.upsert_ticket(
        "mid-min", phone="+393331234567",
        description="Il tornello all'ingresso non legge più i badge da stamattina",
    )
    assert result["ok"] is True
    ticket = fake.tickets[result["ticket_id"]]["properties"]
    assert ticket["support_issue_category"] == "turnstile"
    assert ticket["support_severity"] == "high"
    assert ticket["support_issue_summary"].startswith("Il tornello")
    assert ticket["support_consumed"] == "false"


def test_upsert_ticket_includes_client_info(service):
    system, fake = service
    result = system.upsert_ticket(
        "mid-info", phone="+393331234567",
        description="Il tornello non apre", contact_name="Mario Rossi",
    )
    assert result["ok"] is True
    assert result["contact_name"] == "Mario Rossi"
    ticket = fake.tickets[result["ticket_id"]]["properties"]
    assert "Mario Rossi" in ticket["subject"]
    assert "Segnalato da: Mario Rossi" in ticket["content"]


def test_eligibility_requires_won_deal(service):
    system, fake = service
    fake.has_won_deal = True
    r = system.verify_customer(phone="+393331234567")
    assert r["is_won_customer"] is True and r["eligible_for_support"] is True
    fake.has_won_deal = False
    r2 = system.verify_customer(phone="+393331234567")
    assert r2["is_won_customer"] is False and r2["eligible_for_support"] is False


def test_severity_floor_for_access_categories(service):
    system, _ = service
    # tornello/accessi: pavimento minimo "high" anche se il testo sembra blando
    assert system._severity_floor("turnstile", "medium") == "high"
    assert system._severity_floor("access_control", "low") == "high"
    # critico resta critico; altre categorie invariate
    assert system._severity_floor("turnstile", "critical") == "critical"
    assert system._severity_floor("software", "medium") == "medium"


def test_upsert_ticket_turnstile_is_high(service):
    system, fake = service
    r = system.upsert_ticket("floor-1", phone="+393331234567",
                             description="Il tornello ogni tanto non apre")
    t = fake.tickets[r["ticket_id"]]["properties"]
    assert t["support_issue_category"] == "turnstile"
    assert t["support_severity"] == "high"


def test_create_web_ticket_structured(service):
    system, fake = service
    r = system.create_web_ticket(name="Mario Rossi", company_name="Example SRL",
                                 email="mario@example.it", phone="+393331234567",
                                 description="Il tornello non apre da stamattina")
    assert r["ok"] is True and r["ticket_id"]
    t = fake.tickets[r["ticket_id"]]["properties"]
    assert "SEGNALAZIONE ASSISTENZA PALESYA" in t["content"]
    assert "Origine:      Web (form assistenza)" in t["content"]
    assert "Mario Rossi" in t["content"]
    assert t["ticket_source"] == "web"
    assert t["support_issue_category"] == "turnstile"


def test_upsert_ticket_explicit_fields_win_over_inference(service):
    system, fake = service
    result = system.upsert_ticket(
        "mid-expl", phone="+393331234567",
        description="Il tornello non legge i badge", category="hardware", severity="low",
    )
    ticket = fake.tickets[result["ticket_id"]]["properties"]
    assert ticket["support_issue_category"] == "hardware"
    assert ticket["support_severity"] == "low"


def test_call_is_logged_to_hubspot_with_metrics(service):
    system, fake = service
    _session(system, "log-1")
    result = system.process_voice_event(_event("log-1", "log-event"))
    calls = getattr(fake, "calls", [])
    assert len(calls) == 1
    body = calls[0]["properties"]["hs_call_body"]
    assert "Intervento consumato: sì" in body
    assert "Cliente riconosciuto: found" in body
    assert calls[0]["properties"]["hs_call_direction"] == "INBOUND"
    # idempotenza: un secondo log per la stessa chiamata non crea un altro Call
    system._log_call_to_hubspot(system._session("log-1"),
                                map_final_event(_event("log-1", "x")), result["ticket_id"], result)
    assert len(getattr(fake, "calls", [])) == 1


def test_context_includes_open_tickets(service):
    system, fake = service
    fake.tickets["800"] = {"id": "800", "properties": {
        "support_issue_category": "software", "support_resolution_status": "investigating",
        "subject": "Aperto",
    }}
    context = system.context_for_phone("3331234567")
    assert context["open_tickets_count"] == 1
    assert context["open_tickets"][0]["ticket_id"] == "800"


def test_verify_by_name_is_eligible(service):
    system, fake = service
    result = system.verify_customer(company_name="Example")
    assert result["customer_match_status"] == "found"
    assert result["eligible_for_support"] is True


def test_verify_unknown_is_not_eligible(service):
    system, fake = service
    result = system.verify_customer(company_name="Palestra Sconosciuta")
    assert result["eligible_for_support"] is False
    assert result["customer_match_status"] == "not_found"


def test_verify_by_name_binds_session(service):
    system, fake = service
    system._save_session("bind-1", {"CUSTOMER_MATCH_STATUS": "not_found", "STATE": "answered",
                                     "STARTED_AT": "2026-08-14T10:00:00+00:00"})
    system.verify_customer(company_name="Example", call_id="bind-1")
    session = system._session("bind-1")
    assert session["CUSTOMER_MATCH_STATUS"] == "found"
    assert session["COMPANY_ID"] == "company-1"


def test_non_customer_call_creates_no_ticket_and_no_consume(service):
    """Se il chiamante non è un cliente riconosciuto, l'assistenza non procede:
    nessun ticket, nessuno scalo. La chiamata resta tracciata."""
    system, fake = service
    system._save_session("nc-1", {"CUSTOMER_MATCH_STATUS": "not_found", "STATE": "answered",
                                   "STARTED_AT": "2026-08-14T10:00:00+00:00", "FROM_PHONE": "+393330000000"})
    result = system.process_voice_event(_event("nc-1", "nc-event"))
    assert result["eligible"] is False
    assert result["ticket_id"] is None
    assert result["consumed"] is False
    assert fake.ticket_counter == 0
    assert fake.company["properties"]["support_tickets_remaining"] == "10"
    assert len(getattr(fake, "calls", [])) == 1   # chiamata comunque loggata


def test_commercial_request_creates_deal_idempotent(service):
    system, fake = service
    system._save_session("comm-1", {"CUSTOMER_MATCH_STATUS": "not_found", "STATE": "answered",
                                     "FROM_PHONE": "+393339990000"})
    first = system.create_commercial_request(
        "comm-1", company_name="Nuova Palestra", structure="palestra",
        need="Vuole cambiare gestionale", outcome="qualificato",
    )
    assert first["ok"] is True and first.get("deal_id")
    deal = fake.deals[0]["properties"]
    assert deal["pipeline"] == "deal-pipe"
    assert deal["dealstage"] == "s2"           # qualificato
    assert deal["tipo_struttura"] == "palestra"
    assert deal["esito_qualifica"] == "qualificato"
    second = system.create_commercial_request("comm-1", company_name="Nuova Palestra")
    assert second.get("duplicate") is True
    assert len(getattr(fake, "deals", [])) == 1


def test_finance_active12_monthly_computes_next_payment():
    props, summary = finance.compute_finance({
        "tipo_contratto": "active_12", "cadenza_pagamento": "mensile", "stato_pagamento": "pagato",
        "active_inizio": "2026-01-01", "ultimo_pagamento_data": "2026-08-01", "licenza_pagata": True,
    }, current_used=1, today=_dt.date(2026, 8, 17))
    assert props["fin_active_stato"] == "attivo"
    assert props["fin_active_importo_annuo"] == 630.0
    assert props["fin_prossimo_pagamento_data"] == "2026-09-01"
    assert props["fin_prossimo_pagamento_importo"] == 70.0
    assert props["fin_active_scadenza"] == "2026-12-31"
    assert props["support_tickets_total"] == 5 and props["support_tickets_remaining"] == 4
    assert summary["in_regola_pagamenti"] is True and summary["interventi_residui"] == 4


def test_finance_default_method_is_bonifico_when_paid():
    props, summary = finance.compute_finance({
        "tipo_contratto": "active_12", "stato_pagamento": "pagato", "active_inizio": "2026-01-01",
    }, today=_dt.date(2026, 3, 1))
    assert props["fin_metodo_pagamento"] == "bonifico"
    # metodo esplicito rispettato
    props2, _ = finance.compute_finance({
        "tipo_contratto": "active_12", "stato_pagamento": "pagato", "metodo_pagamento": "fattura",
    }, today=_dt.date(2026, 3, 1))
    assert props2["fin_metodo_pagamento"] == "fattura"


def test_finance_insoluto_suspends_and_bills_now():
    props, summary = finance.compute_finance({
        "tipo_contratto": "active_24", "stato_pagamento": "insoluto", "active_inizio": "2026-01-01",
    }, current_used=0, today=_dt.date(2026, 6, 10))
    assert props["fin_active_stato"] == "in_attesa_pagamento"
    assert props["support_plan_status"] == "suspended"
    assert props["fin_prossimo_pagamento_data"] == "2026-06-10"
    assert summary["in_regola_pagamenti"] is False


def test_finance_expired_plan_marked_scaduto():
    props, _ = finance.compute_finance({
        "tipo_contratto": "active_12", "stato_pagamento": "pagato", "active_scadenza": "2025-12-31",
    }, today=_dt.date(2026, 8, 17))
    assert props["fin_active_stato"] == "scaduto"


def test_finance_aggregate_report():
    companies = [
        {"id": "1", "properties": {"name": "A", "fin_tipo_contratto": "active_12",
         "fin_stato_pagamento": "pagato", "fin_active_stato": "attivo",
         "fin_incassato_totale": "630", "fin_prossimo_pagamento_data": "2026-09-01",
         "fin_prossimo_pagamento_importo": "70"}},
        {"id": "2", "properties": {"name": "B", "fin_tipo_contratto": "active_24",
         "fin_stato_pagamento": "insoluto", "fin_active_stato": "in_attesa_pagamento",
         "fin_incassato_totale": "600", "fin_prossimo_pagamento_data": "2026-08-20",
         "fin_prossimo_pagamento_importo": "600"}},
        {"id": "3", "properties": {"name": "NoContract"}},  # ignorata
    ]
    rep = finance.aggregate_report(companies, today=_dt.date(2026, 8, 18), scadenze_giorni=30)
    assert rep["clienti_con_contratto"] == 2
    assert rep["incassato_totale"] == 1230.0
    assert rep["pagamenti_per_stato"]["pagato"] == 1 and rep["pagamenti_per_stato"]["insoluto"] == 1
    assert rep["insoluti_count"] == 1 and rep["insoluti"][0]["azienda"] == "B"
    assert rep["prossime_scadenze_count"] == 2


def test_finance_summary_from_props_reads_back():
    s = finance.summary_from_props({
        "fin_tipo_contratto": "active_12", "fin_active_stato": "attivo", "fin_stato_pagamento": "pagato",
        "fin_licenza_pagata": "true", "support_tickets_remaining": "3",
        "fin_prossimo_pagamento_data": "2026-09-01", "fin_prossimo_pagamento_importo": "70",
    })
    assert s["piano_attivo"] is True and s["in_regola_pagamenti"] is True
    assert s["interventi_residui"] == 3 and s["prossimo_pagamento_importo"] == 70.0


def test_ai_usage_enforcement_off_serves_everyone():
    s = _usage.usage_status({}, monthly_minutes_default=20, enforced=False)
    assert s["ai_eligibile"] is True and s["ai_motivo"] == "enforcement_off"
    assert s["ai_minuti_limite"] == 20 and s["ai_minuti_residui"] == 20


def test_ai_usage_enforced_gating():
    now = _dt.date(2026, 8, 18)
    # non abbonato -> non idoneo
    s = _usage.usage_status({"ai_service_active": "false"}, enforced=True, now=now)
    assert s["ai_eligibile"] is False and s["ai_motivo"] == "non_abbonato"
    # abbonato con minuti -> idoneo
    s = _usage.usage_status({"ai_service_active": "true", "ai_minutes_used": "5", "ai_minutes_period": "2026-08"}, enforced=True, now=now)
    assert s["ai_eligibile"] is True and s["ai_minuti_residui"] == 15
    # abbonato ma minuti esauriti -> non idoneo
    s = _usage.usage_status({"ai_service_active": "true", "ai_minutes_used": "20", "ai_minutes_period": "2026-08"}, enforced=True, now=now)
    assert s["ai_eligibile"] is False and s["ai_motivo"] == "minuti_esauriti"


def test_ai_usage_month_reset_and_accumulate():
    now = _dt.date(2026, 8, 18)
    # periodo vecchio => usati azzerati nel calcolo
    s = _usage.usage_status({"ai_minutes_used": "18", "ai_minutes_period": "2026-07"}, enforced=True, now=now)
    assert s["ai_minuti_usati"] == 0 and s["ai_minuti_residui"] == 20
    # accumulo: 90s = 1.5 min, su mese nuovo riparte da 0
    up = _usage.apply_call_minutes({"ai_minutes_used": "18", "ai_minutes_period": "2026-07"}, 90, now=now)
    assert up["ai_minutes_period"] == "2026-08" and float(up["ai_minutes_used"]) == 1.5
    # accumulo nello stesso mese: somma
    up2 = _usage.apply_call_minutes({"ai_minutes_used": "1.5", "ai_minutes_period": "2026-08"}, 120, now=now)
    assert float(up2["ai_minutes_used"]) == 3.5


def test_expense_normalize_and_validation():
    props, summary = _expenses.normalize_expense({
        "importo": "1200", "categoria": "affitto", "metodo": "bonifico", "stato": "pagata",
        "data": "2026-08-01", "fornitore": "Immobiliare X"})
    assert props["uscita_importo"] == 1200.0 and props["uscita_categoria"] == "affitto"
    assert "1200" in props["subject"] and summary["stato"] == "pagata"
    # categoria fuori lista -> altro
    p2, _ = _expenses.normalize_expense({"importo": 50, "categoria": "boh"})
    assert p2["uscita_categoria"] == "altro"
    # importo mancante -> errore
    import pytest as _pytest
    with _pytest.raises(ValueError):
        _expenses.normalize_expense({"categoria": "software"})


def test_expenses_aggregate():
    tickets = [
        {"id": "1", "properties": {"uscita_importo": "1000", "uscita_categoria": "affitto", "uscita_stato": "pagata", "uscita_data": "2026-08-01"}},
        {"id": "2", "properties": {"uscita_importo": "300", "uscita_categoria": "software", "uscita_stato": "da_pagare", "uscita_data": "2026-08-10", "uscita_fornitore": "SaaS"}},
    ]
    rep = _expenses.aggregate_expenses(tickets, today=_dt.date(2026, 8, 18))
    assert rep["uscite_totali"] == 1300.0 and rep["uscite_pagate"] == 1000.0
    assert rep["uscite_da_pagare"] == 300.0 and rep["da_pagare_count"] == 1
    assert rep["per_categoria"]["affitto"] == 1000.0


def test_telnyx_call_data_mapping():
    event = {"call_id": "cc-1", "event_payload": {"call_control_id": "cc-1", "from": "+393331234567", "to": "+390811234567", "call_duration": 42}}
    assert extract_call_data(event)["duration_seconds"] == 42


def test_telnyx_signature_and_replay_protection():
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw()
    raw = json.dumps({"data": {"id": "event-1", "event_type": "call.initiated", "payload": {}}}, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = base64.b64encode(private_key.sign((timestamp + "|").encode() + raw)).decode()
    headers = {"telnyx-timestamp": timestamp, "telnyx-signature-ed25519": signature}
    assert verify_signature(raw, headers, base64.b64encode(public_key).decode(), now=int(timestamp))
    assert not verify_signature(raw, headers, base64.b64encode(public_key).decode(), now=int(timestamp) + 301)
    assert not verify_signature(raw, {**headers, "telnyx-signature-ed25519": "invalid"}, base64.b64encode(public_key).decode(), now=int(timestamp))


# --- Smistamento intelligente e apprendimento -----------------------------

def test_tokenizer_normalizza_accenti_e_flessioni():
    from support_integration import triage as _triage
    # "tornello"/"tornelli" collassano sullo stesso stem, gli accenti spariscono,
    # le stopword non inquinano il modello.
    assert _triage.tokenize("Il tornello") == _triage.tokenize("i tornelli")
    assert "perche" not in _triage.tokenize("perché non funziona")
    assert _triage.tokenize("") == []


def test_naive_bayes_impara_e_da_confidenza():
    from support_integration.triage import NaiveBayes
    model = NaiveBayes()
    assert not model.is_ready()
    for _ in range(8):   # 16 esempi: sopra MIN_EXAMPLES_TOTAL
        model.add("il tornello non gira e resta bloccato", "turnstile")
        model.add("errore nella fattura e nel pagamento della rata", "billing")
    assert model.is_ready()
    label, confidence, _ = model.predict("tornello bloccato")
    assert label == "turnstile" and confidence > 0.5


def test_triage_usa_regole_finche_non_ha_imparato(service):
    svc, _ = service
    out = svc.classify_request("il tornello non gira")
    assert out["category"] == "turnstile"
    assert out["source"] == "rules"          # nessun esempio confermato ancora
    assert out["severity"] == "high"         # floor sulle categorie di accesso


def test_correzione_umana_insegna_al_motore(service):
    svc, _ = service
    # Frase senza parole chiave: le regole la mandano su "other".
    frase = "la colonnina si inceppa e la gente si accumula"
    assert svc.classify_request(frase)["category"] == "other"
    # Un umano corregge 15 richieste simili -> il motore impara.
    for i in range(15):
        svc.record_triage(frase, {"category": "other", "severity": "medium",
                                  "confidence": 0.0, "source": "rules"},
                          call_id="call-%d" % i)
        svc.confirm_triage(call_id="call-%d" % i, category="turnstile", severity="high")
    for i in range(15):
        svc.record_triage("problema con la fattura mensile", {"category": "billing", "severity": "medium",
                                                              "confidence": 0.0, "source": "rules"},
                          call_id="bill-%d" % i)
        svc.confirm_triage(call_id="bill-%d" % i, category="billing", severity="medium")
    out = svc.classify_request(frase)
    assert out["category"] == "turnstile"    # ha imparato dalla correzione
    assert out["source"] in ("model", "rules+model")


def test_il_motore_non_impara_dalle_proprie_previsioni(service):
    svc, _ = service
    # 30 previsioni registrate ma MAI confermate: il modello resta a zero.
    for i in range(30):
        svc.record_triage("qualcosa di strano succede", {"category": "software", "severity": "medium",
                                                          "confidence": 0.9, "source": "model"},
                          call_id="p-%d" % i)
    stats = svc.triage_stats()
    assert stats["confirmed"] == 0
    assert stats["category"]["examples"] == 0
    assert stats["category"]["ready"] is False


def test_confirm_triage_rifiuta_etichette_non_valide(service):
    svc, _ = service
    svc.record_triage("test", {"category": "other", "severity": "medium",
                               "confidence": 0.0, "source": "rules"}, call_id="x-1")
    with pytest.raises(ValueError):
        svc.confirm_triage(call_id="x-1", category="categoria_inventata")
    with pytest.raises(ValueError):
        svc.confirm_triage(call_id="x-1", severity="gravissima")
    with pytest.raises(ValueError):
        svc.confirm_triage(category="software")          # senza call_id/ticket_id
    assert svc.confirm_triage(call_id="ignoto", category="software")["ok"] is False


def test_triage_stats_misura_accuratezza(service):
    svc, _ = service
    svc.record_triage("tornello bloccato", {"category": "turnstile", "severity": "high",
                                            "confidence": 0.0, "source": "rules"}, call_id="a-1")
    svc.confirm_triage(call_id="a-1", category="turnstile")      # previsione giusta
    svc.record_triage("problema di rete", {"category": "hardware", "severity": "medium",
                                           "confidence": 0.0, "source": "rules"}, call_id="a-2")
    svc.confirm_triage(call_id="a-2", category="software")       # previsione sbagliata
    stats = svc.triage_stats()
    assert stats["confirmed"] == 2
    assert stats["accuracy_on_confirmed"] == 0.5


def test_richieste_ricorrenti_vengono_contate(service):
    svc, _ = service
    for i in range(3):
        svc.record_triage("tornello bloccato", {"category": "turnstile", "severity": "high",
                                                "confidence": 0.0, "source": "rules"},
                          call_id="r-%d" % i, company_id="company-1")
    assert svc.recurring_issue_count("company-1", "turnstile") == 3
    assert svc.recurring_issue_count("company-1", "billing") == 0
    assert svc.recurring_issue_count("", "turnstile") == 0


def test_smistamento_operatore_e_commerciale():
    from support_integration.triage import route_request
    # La richiesta esplicita di un umano vince su tutto.
    assert route_request("voglio parlare con un operatore",
                         eligible_for_support=True)["destination"] == "umano"
    # Due tentativi falliti -> non insistere, passa a un umano.
    assert route_request("non ci siamo capiti", eligible_for_support=True,
                         failed_attempts=2)["destination"] == "umano"
    # Intento commerciale.
    assert route_request("vorrei un preventivo",
                         eligible_for_support=True)["destination"] == "commerciale"
    # Non cliente vinto -> commerciale (regola di business esistente).
    assert route_request("il gestionale si blocca",
                         eligible_for_support=False)["destination"] == "commerciale"
    # Cliente idoneo con richiesta chiara -> assistenza tecnica.
    assert route_request("il gestionale si blocca", eligible_for_support=True,
                         confidence=0.9)["destination"] == "tecnica"


def test_route_call_end_to_end(service):
    svc, fake = service
    out = svc.route_call("il tornello non gira", phone="+393331234567")
    assert out["destination"] in ("tecnica", "commerciale")
    assert out["category"] == "turnstile"
    assert "reason" in out


def test_senza_modello_addestrato_non_si_finisce_tutti_da_un_umano(service):
    """Regressione: le regole non esprimono incertezza.

    Con il modello ancora vuoto la confidenza vale 0; se venisse interpretata
    come "richiesta non compresa", ogni chiamata verrebbe dirottata su un
    operatore fin dal primo giorno.
    """
    svc, _ = service
    assert svc.classify_request("il gestionale si blocca")["source"] == "rules"
    out = svc.route_call("il gestionale si blocca", phone="+393331234567")
    assert out["destination"] != "umano"


def test_split_statements_ignora_commenti_con_punto_e_virgola():
    """Regressione: un ';' dentro un commento SQL spezzava lo statement.

    Su SQLite non si vedeva (executescript esegue lo script intero); su Postgres
    il frammento orfano diventava una query invalida e il deploy falliva.
    """
    import db_compat as _db
    script = """
    -- commento con punto e virgola; e testo dopo
    CREATE TABLE T(A TEXT);
    CREATE INDEX I ON T(A);
    """
    statements = _db.split_statements(script)
    assert len(statements) == 2
    assert all("commento" not in s for s in statements)
    assert "CREATE TABLE" in statements[0] and "CREATE INDEX" in statements[1]


def test_schema_reale_non_ha_statement_orfani():
    """Ogni statement dello schema deve iniziare con una parola chiave DDL."""
    import db_compat as _db
    from schema import SUPPORT_SCHEMA
    for statement in _db.split_statements(SUPPORT_SCHEMA):
        head = statement.strip().split()[0].upper()
        assert head in ("CREATE", "ALTER", "DROP", "INSERT"), statement.strip()[:80]
