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
