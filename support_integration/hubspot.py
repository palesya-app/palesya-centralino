"""Client HTTP minimale e tipizzato per HubSpot CRM v3/v4.

Il client non dipende dall'SDK ufficiale: evita di introdurre una dipendenza
pesante nel desktop e rende semplice il test con un transport in memoria.
"""
import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import SupportSettings
from .phone import phones_match

logger = logging.getLogger("palesya.support.hubspot")


class HubSpotError(RuntimeError):
    """Errore HubSpot senza includere token o payload sensibili nel messaggio."""


class HubSpotClient:
    CONTACT_PROPERTIES = (
        "firstname", "lastname", "email", "phone", "mobilephone", "company",
        "hs_additional_emails",
    )
    COMPANY_PROPERTIES = (
        "name", "phone", "support_tickets_total", "support_tickets_used",
        "support_tickets_remaining", "support_plan_status", "support_plan_start_date",
        "support_plan_expiration_date", "last_support_intervention",
        # Finanza Palesya (gestione contratto/pagamenti lato cliente)
        "fin_tipo_contratto", "fin_licenza_pagata", "fin_licenza_importo", "fin_licenza_data",
        "fin_active_stato", "fin_active_inizio", "fin_active_scadenza", "fin_active_importo_annuo",
        "fin_cadenza_pagamento", "fin_stato_pagamento", "fin_metodo_pagamento",
        "fin_ultimo_pagamento_data", "fin_ultimo_pagamento_importo",
        "fin_prossimo_pagamento_data", "fin_prossimo_pagamento_importo", "fin_incassato_totale",
        "ai_service_active", "ai_minutes_limit", "ai_minutes_used", "ai_minutes_period",
    )
    TICKET_PROPERTIES = (
        "subject", "content", "hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority",
        "ticket_source", "support_consumed", "caller_phone", "telnyx_call_id",
        "voice_ai_call_id", "support_issue_category", "support_issue_summary",
        "support_resolution", "support_ai_summary", "support_call_duration",
        "support_resolution_status", "customer_match_status", "support_tickets_before",
        "support_tickets_after", "support_consumption_reason", "renewal_alert_level",
        "support_severity", "support_troubleshooting", "support_device",
        "human_escalation_reason", "support_intent", "support_follow_up",
    )
    PROPERTY_GROUP = "assistenza_palesya"

    def __init__(self, support_settings: SupportSettings, request_fn=None):
        self.settings = support_settings
        self._request_fn = request_fn
        self._association_cache = {}

    @property
    def configured(self):
        return bool(self.settings.hubspot_access_token)

    def _request(self, method, path, body=None, params=None, attempts=3):
        if not self.configured:
            raise HubSpotError("HubSpot non configurato")
        url = self.settings.hubspot_base_url + path
        if params:
            url += "?" + urlencode(params)
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + self.settings.hubspot_access_token,
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        last_error = None
        for attempt in range(max(1, attempts)):
            try:
                if self._request_fn:
                    status, response_body = self._request_fn(method, url, headers, payload)
                    parsed = response_body if isinstance(response_body, dict) else json.loads(response_body or "{}")
                    return status, parsed
                request = Request(url, data=payload, headers=headers, method=method.upper())
                with urlopen(request, timeout=self.settings.hubspot_timeout_seconds) as response:
                    raw = response.read().decode("utf-8")
                    return response.status, json.loads(raw or "{}")
            except HTTPError as exc:
                last_error = exc
                status = int(getattr(exc, "code", 0) or 0)
                if status not in {408, 409, 429} and status < 500:
                    detail = "HubSpot HTTP {}".format(status)
                    logger.warning("hubspot_error status=%s", status)
                    raise HubSpotError(detail) from exc
                if attempt + 1 < attempts:
                    retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                    try:
                        delay = min(4.0, max(0.1, float(retry_after)))
                    except (TypeError, ValueError):
                        delay = 0.25 * (2 ** attempt)
                    time.sleep(delay)
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.25 * (2 ** attempt))
        logger.warning("hubspot_error retries_exhausted=%s", type(last_error).__name__)
        raise HubSpotError("HubSpot non disponibile dopo retry") from last_error

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)[1]

    def _post(self, path, body):
        return self._request("POST", path, body=body)[1]

    def _patch(self, path, body):
        return self._request("PATCH", path, body=body)[1]

    def _put(self, path, body=None):
        return self._request("PUT", path, body=body)[1]

    def list_properties(self, object_type):
        return self._get("/crm/v3/properties/{}".format(object_type)).get("results", [])

    def ensure_property(self, object_type, name, label, kind="string", field_type="text",
                        description="", options=(), group_name=None):
        existing = {str(item.get("name")): item for item in self.list_properties(object_type)}
        if name in existing:
            return existing[name], False
        body = {
            "name": name,
            "label": label,
            "description": description,
            "groupName": group_name or ("companyinformation" if object_type == "companies" else "ticketinformation"),
            "type": kind,
            "fieldType": field_type,
        }
        if options:
            body["options"] = [
                {"label": value.replace("_", " ").title(), "value": value,
                 "displayOrder": index, "hidden": False}
                for index, value in enumerate(options)
            ]
        return self._post("/crm/v3/properties/{}".format(object_type), body), True

    def ensure_property_group(self, object_type, name, label):
        """Crea (idempotente) un gruppo di proprietà = sezione dedicata nel record."""
        try:
            existing = {str(item.get("name")) for item in self._get(
                "/crm/v3/properties/{}/groups".format(object_type)
            ).get("results", [])}
            if name in existing:
                return False
            self._post("/crm/v3/properties/{}/groups".format(object_type),
                       {"name": name, "label": label, "displayOrder": -1})
            return True
        except HubSpotError:
            logger.warning("hubspot_error property_group_failed object=%s", object_type)
            return False

    def ensure_support_model(self):
        """Crea solo proprietà mancanti; non elimina o modifica quelle esistenti."""
        created = {"company": [], "ticket": [], "pipeline": None, "groups": []}
        for object_type in ("companies", "tickets"):
            if self.ensure_property_group(object_type, self.PROPERTY_GROUP, "Assistenza Palesya"):
                created["groups"].append(object_type)
        company_specs = {
            "support_tickets_total": ("Interventi assistenza acquistati", "number", "number", "Numero totale di interventi acquistati"),
            "support_tickets_used": ("Interventi assistenza utilizzati", "number", "number", "Numero di interventi già utilizzati"),
            "support_tickets_remaining": ("Interventi assistenza residui", "number", "number", "Totale meno utilizzati"),
            "support_plan_status": ("Stato piano assistenza", "enumeration", "select", "Stato del pacchetto assistenza"),
            "support_plan_start_date": ("Inizio piano assistenza", "date", "date", "Data di inizio del pacchetto"),
            "support_plan_expiration_date": ("Scadenza piano assistenza", "date", "date", "Data di scadenza del pacchetto"),
            "last_support_intervention": ("Ultimo intervento assistenza", "date", "date", "Data dell'ultimo intervento conteggiato"),
        }
        for name, (label, kind, field, description) in company_specs.items():
            options = ("active", "low", "exhausted", "suspended", "unlimited") if name == "support_plan_status" else ()
            _, was_created = self.ensure_property("companies", name, label, kind, field, description, options, self.PROPERTY_GROUP)
            if was_created:
                created["company"].append(name)
        ticket_specs = {
            "ticket_source": ("Origine ticket assistenza", "enumeration", "select", ("ai_phone", "manual", "email", "whatsapp", "web", "other")),
            "support_consumed": ("Intervento conteggiato", "enumeration", "booleancheckbox", ("true", "false")),
            "caller_phone": ("Telefono chiamante", "string", "phonenumber", ()),
            "telnyx_call_id": ("Telnyx call ID", "string", "text", ()),
            "voice_ai_call_id": ("Voice AI call ID", "string", "text", ()),
            "support_issue_category": ("Categoria problema", "enumeration", "select", ("access_control", "turnstile", "software", "billing", "configuration", "migration", "hardware", "other")),
            "support_issue_summary": ("Sintesi problema", "string", "textarea", ()),
            "support_resolution": ("Soluzione", "string", "textarea", ()),
            "support_ai_summary": ("Sintesi AI", "string", "textarea", ()),
            "support_call_duration": ("Durata chiamata", "number", "number", ()),
            "support_resolution_status": ("Esito assistenza", "enumeration", "select", ("new", "investigating", "resolved", "escalated", "waiting_customer", "duplicate", "non_support")),
            "customer_match_status": ("Stato riconoscimento cliente", "enumeration", "select", ("found", "not_found", "ambiguous", "unknown")),
            "support_tickets_before": ("Interventi residui prima", "number", "number", ()),
            "support_tickets_after": ("Interventi residui dopo", "number", "number", ()),
            "support_consumption_reason": ("Motivo conteggio", "string", "textarea", ()),
            "renewal_alert_level": ("Livello alert rinnovo", "enumeration", "select", ("none", "low", "priority", "renewal")),
            "support_severity": ("Severity incidente", "enumeration", "select", ("low", "medium", "high", "critical")),
            "support_troubleshooting": ("Troubleshooting effettuato", "string", "textarea", ()),
            "support_device": ("Dispositivo coinvolto", "string", "text", ()),
            "human_escalation_reason": ("Motivo escalation umana", "string", "textarea", ()),
            "support_intent": ("Intento chiamata", "enumeration", "select", ("technical", "commercial", "status_check", "follow_up", "callback", "other")),
            "support_follow_up": ("Follow-up ticket esistente", "enumeration", "booleancheckbox", ("true", "false")),
        }
        for name, (label, kind, field, options) in ticket_specs.items():
            _, was_created = self.ensure_property("tickets", name, label, kind, field, "Campo tecnico assistenza", options, self.PROPERTY_GROUP)
            if was_created:
                created["ticket"].append(name)
        created["pipeline"] = self.ensure_support_pipeline()
        return created

    FINANCE_GROUP = "finanza_palesya"

    def ensure_finance_model(self):
        """Crea (idempotente) le proprietà finanziarie sul Company: contratto,
        licenza, Active, pagamenti. Additivo: non tocca proprietà esistenti."""
        created = []
        self.ensure_property_group("companies", self.FINANCE_GROUP, "Finanza Palesya")
        specs = {
            "fin_tipo_contratto": ("Tipo contratto", "enumeration", "select",
                                   ("licenza", "active_12", "active_24", "active_36", "licenza_active", "nessuno")),
            "fin_licenza_pagata": ("Licenza pagata", "enumeration", "booleancheckbox", ("true", "false")),
            "fin_licenza_importo": ("Importo licenza", "number", "number", ()),
            "fin_licenza_data": ("Data acquisto licenza", "date", "date", ()),
            "fin_active_stato": ("Stato Active", "enumeration", "select",
                                 ("attivo", "sospeso", "scaduto", "in_attesa_pagamento", "nessuno")),
            "fin_active_inizio": ("Inizio Active", "date", "date", ()),
            "fin_active_scadenza": ("Scadenza/rinnovo Active", "date", "date", ()),
            "fin_active_importo_annuo": ("Importo Active annuo", "number", "number", ()),
            "fin_cadenza_pagamento": ("Cadenza pagamento", "enumeration", "select", ("mensile", "annuale")),
            "fin_stato_pagamento": ("Stato pagamento", "enumeration", "select",
                                    ("pagato", "parziale", "insoluto", "rimborsato")),
            # Al momento si incassa solo tramite fattura + bonifico; gli altri restano
            # disponibili per il futuro ma non sono i metodi correnti.
            "fin_metodo_pagamento": ("Metodo pagamento", "enumeration", "select",
                                     ("bonifico", "fattura", "carta", "contanti", "sepa", "altro")),
            "fin_ultimo_pagamento_data": ("Ultimo pagamento — data", "date", "date", ()),
            "fin_ultimo_pagamento_importo": ("Ultimo pagamento — importo", "number", "number", ()),
            "fin_prossimo_pagamento_data": ("Prossimo pagamento — data", "date", "date", ()),
            "fin_prossimo_pagamento_importo": ("Prossimo pagamento — importo", "number", "number", ()),
            "fin_incassato_totale": ("Totale incassato dal cliente", "number", "number", ()),
            # Servizio AI a pagamento: abbonamento attivo + monte minuti/mese.
            "ai_service_active": ("Servizio AI attivo (abbonamento)", "enumeration", "booleancheckbox", ("true", "false")),
            "ai_minutes_limit": ("Minuti AI/mese inclusi", "number", "number", ()),
            "ai_minutes_used": ("Minuti AI usati (mese corrente)", "number", "number", ()),
            "ai_minutes_period": ("Periodo minuti AI (YYYY-MM)", "string", "text", ()),
        }
        for name, (label, kind, field, options) in specs.items():
            _, was_created = self.ensure_property("companies", name, label, kind, field,
                                                  "Campo finanziario Palesya", options, self.FINANCE_GROUP)
            if was_created:
                created.append(name)
        return {"created": created, "group": self.FINANCE_GROUP}

    EXPENSE_GROUP = "uscite_palesya"
    EXPENSE_PIPELINE_LABEL = "Uscite aziendali"

    def ensure_expenses_model(self):
        """Modello Uscite (costi) sull'oggetto Ticket: gruppo + proprietà, e
        (se il piano lo consente) una pipeline dedicata; altrimenti si usa quella
        esistente distinguendo le uscite dalla proprietà `uscita_categoria`."""
        created = []
        self.ensure_property_group("tickets", self.EXPENSE_GROUP, "Uscite Palesya")
        from .expenses import CATEGORIE, METODI, STATI
        specs = {
            "uscita_importo": ("Uscita — importo", "number", "number", ()),
            "uscita_categoria": ("Uscita — categoria", "enumeration", "select", tuple(CATEGORIE)),
            "uscita_metodo": ("Uscita — metodo", "enumeration", "select", tuple(METODI)),
            "uscita_stato": ("Uscita — stato", "enumeration", "select", tuple(STATI)),
            "uscita_data": ("Uscita — data", "date", "date", ()),
            "uscita_fornitore": ("Uscita — fornitore", "string", "text", ()),
        }
        for name, (label, kind, field, options) in specs.items():
            _, was_created = self.ensure_property("tickets", name, label, kind, field,
                                                  "Campo uscite Palesya", options, self.EXPENSE_GROUP)
            if was_created:
                created.append(name)
        pipeline = self._resolve_expenses_pipeline()
        return {"created": created, "group": self.EXPENSE_GROUP, "pipeline": pipeline}

    def _resolve_expenses_pipeline(self):
        """Ritorna {id, stages:{da_pagare, pagata}, dedicated}. Prova a creare la
        pipeline 'Uscite aziendali'; se il piano non lo consente usa l'esistente."""
        if getattr(self, "_expenses_pipeline_cache", None):
            return self._expenses_pipeline_cache
        try:
            pipelines = self._get("/crm/v3/pipelines/tickets").get("results", [])
        except HubSpotError:
            pipelines = []
        for p in pipelines:
            if str(p.get("label", "")).casefold() == self.EXPENSE_PIPELINE_LABEL.casefold():
                stages = p.get("stages") or []
                by_label = {str(s.get("label", "")).casefold(): str(s.get("id")) for s in stages}
                result = {"id": str(p.get("id")), "dedicated": True,
                          "stages": {"da_pagare": by_label.get("da pagare") or (str(stages[0]["id"]) if stages else "1"),
                                     "pagata": by_label.get("pagata") or (str(stages[-1]["id"]) if stages else "1")},
                          "default_stage": str(stages[0]["id"]) if stages else "1"}
                self._expenses_pipeline_cache = result
                return result
        # Prova a creare la pipeline dedicata
        body = {"label": self.EXPENSE_PIPELINE_LABEL, "displayOrder": 2, "stages": [
            {"label": "Da pagare", "displayOrder": 0, "metadata": {"ticketState": "OPEN"}},
            {"label": "Pagata", "displayOrder": 1, "metadata": {"ticketState": "CLOSED"}},
        ]}
        try:
            p = self._post("/crm/pipelines/2026-03/tickets", body)
            stages = p.get("stages") or []
            by_label = {str(s.get("label", "")).casefold(): str(s.get("id")) for s in stages}
            result = {"id": str(p.get("id")), "dedicated": True,
                      "stages": {"da_pagare": by_label.get("da pagare"), "pagata": by_label.get("pagata")},
                      "default_stage": str(stages[0]["id"]) if stages else "1"}
        except HubSpotError:
            # Fallback: pipeline esistente, le uscite si distinguono dalla proprietà.
            pid = str(pipelines[0]["id"]) if pipelines else "0"
            stages = (pipelines[0].get("stages") if pipelines else []) or []
            sid = str(stages[0]["id"]) if stages else "1"
            result = {"id": pid, "dedicated": False, "stages": {"da_pagare": sid, "pagata": sid}, "default_stage": sid}
        self._expenses_pipeline_cache = result
        return result

    def create_expense(self, props, stato="pagata"):
        pipeline = self._resolve_expenses_pipeline()
        body = dict(props)
        body["hs_pipeline"] = pipeline["id"]
        body["hs_pipeline_stage"] = pipeline["stages"].get(stato) or pipeline["default_stage"]
        return self._post("/crm/v3/objects/tickets", {"properties": body})

    def list_expenses(self, max_pages=20):
        """Tutte le uscite (ticket con `uscita_categoria` impostata), paginato."""
        out = []
        after = None
        props = ["uscita_importo", "uscita_categoria", "uscita_metodo", "uscita_stato",
                 "uscita_data", "uscita_fornitore", "subject"]
        for _ in range(max_pages):
            body = {"filterGroups": [{"filters": [
                {"propertyName": "uscita_categoria", "operator": "HAS_PROPERTY"}]}],
                "properties": props, "limit": 100}
            if after:
                body["after"] = after
            try:
                response = self._post("/crm/v3/objects/tickets/search", body)
            except HubSpotError:
                logger.warning("hubspot_error expenses_search_failed")
                break
            out.extend(response.get("results", []))
            after = ((response.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        return out

    def ensure_support_pipeline(self):
        if self.settings.support_pipeline_id:
            return {"id": self.settings.support_pipeline_id, "label": self.settings.support_pipeline_label, "created": False}
        try:
            pipelines = self._get("/crm/v3/pipelines/tickets").get("results", [])
        except HubSpotError:
            logger.warning("hubspot_error pipeline_read_failed")
            return {"id": "0", "label": "default", "created": False}
        for pipeline in pipelines:
            if str(pipeline.get("label", "")).casefold() == self.settings.support_pipeline_label.casefold():
                stages = pipeline.get("stages") or []
                stage_id = self.settings.support_pipeline_stage_id or (str(stages[0].get("id")) if stages else "1")
                return {"id": str(pipeline.get("id")), "label": pipeline.get("label"), "stage_id": stage_id, "created": False}
        # La creazione è additiva e non modifica pipeline esistenti.
        body = {
            "label": self.settings.support_pipeline_label,
            "displayOrder": 1,
            "stages": [
                {
                    "label": label,
                    "displayOrder": index,
                    "metadata": {
                        "ticketState": "CLOSED" if label in {"Risolto", "Chiuso", "Non conteggiato"} else "OPEN"
                    },
                }
                for index, label in enumerate(("Nuovo", "Preso in carico", "In lavorazione", "In attesa cliente", "Escalation tecnico", "Risolto", "Chiuso", "Non conteggiato"))
            ],
        }
        try:
            pipeline = self._post("/crm/pipelines/2026-03/tickets", body)
            stages = pipeline.get("stages") or []
            return {"id": str(pipeline.get("id")), "label": pipeline.get("label"), "stage_id": str(stages[0].get("id")) if stages else "1", "created": True}
        except HubSpotError:
            logger.warning("hubspot_error pipeline_create_failed_reusing_existing")
            # Un piano senza Service Hub non permette pipeline Ticket aggiuntive.
            # Riutilizziamo la pipeline esistente senza modificarla.
            if pipelines:
                pipeline = pipelines[0]
                stages = pipeline.get("stages") or []
                stage_id = self.settings.support_pipeline_stage_id or (str(stages[0].get("id")) if stages else "1")
                return {
                    "id": str(pipeline.get("id")),
                    "label": pipeline.get("label"),
                    "stage_id": stage_id,
                    "created": False,
                    "fallback": "existing_pipeline",
                }
            return {"id": "0", "label": "default", "stage_id": "1", "created": False}

    def search_contacts_by_phone(self, phone, default_country_code="39"):
        results = {}
        query = next(iter(sorted(self._query_variants(phone, default_country_code))), "")
        for candidate in self._query_variants(phone, default_country_code):
            body = {
                "query": candidate,
                "limit": 100,
                "properties": list(self.CONTACT_PROPERTIES),
            }
            try:
                response = self._post("/crm/v3/objects/contacts/search", body)
            except HubSpotError:
                if candidate == query:
                    raise
                continue
            for item in response.get("results", []):
                props = item.get("properties") or {}
                phone_values = [props.get("phone"), props.get("mobilephone"), props.get("hs_additional_emails")]
                if any(phones_match(phone, value, default_country_code) for value in phone_values if value):
                    results[str(item.get("id"))] = item
        return list(results.values())

    @staticmethod
    def _query_variants(phone, default_country_code):
        from .phone import phone_variants
        variants = phone_variants(phone, default_country_code)
        return {item for item in variants if len(item) >= 7}

    def get_contact(self, contact_id):
        return self._get("/crm/v3/objects/contacts/{}".format(contact_id), {"properties": ",".join(self.CONTACT_PROPERTIES)})

    def get_company(self, company_id):
        return self._get("/crm/v3/objects/companies/{}".format(company_id), {"properties": ",".join(self.COMPANY_PROPERTIES)})

    def get_company_for_contact(self, contact_id):
        associations = self._get("/crm/v4/objects/contacts/{}/associations/companies".format(contact_id), {"limit": 100}).get("results", [])
        companies = []
        for item in associations:
            company_id = item.get("toObjectId") or (item.get("to") or {}).get("id")
            if company_id:
                companies.append(self.get_company(company_id))
        return companies

    def search_companies_by_phone(self, phone, default_country_code="39"):
        results = {}
        variants = list(self._query_variants(phone, default_country_code))

        def _absorb(response):
            for item in response.get("results", []):
                props = item.get("properties") or {}
                if phones_match(phone, props.get("phone"), default_country_code):
                    results[str(item.get("id"))] = item

        # 1) Ricerca deterministica: filtro sulla proprietà `phone` (CONTAINS_TOKEN)
        #    sui varianti E.164 — affidabile dove la query testuale fallisce.
        #    Ogni variante = un filterGroup (in OR); HubSpot ne accetta fino a 5.
        groups = [{"filters": [{"propertyName": "phone", "operator": "CONTAINS_TOKEN", "value": v}]}
                  for v in variants[:5]]
        if groups:
            try:
                _absorb(self._post("/crm/v3/objects/companies/search", {
                    "filterGroups": groups, "limit": 100, "properties": list(self.COMPANY_PROPERTIES),
                }))
            except HubSpotError:
                logger.warning("hubspot_error company_phone_filter_search_failed")

        # 2) Fallback: ricerca testuale storica (copre numeri fuori dal campo phone)
        for candidate in variants:
            try:
                _absorb(self._post("/crm/v3/objects/companies/search", {
                    "query": candidate, "limit": 100, "properties": list(self.COMPANY_PROPERTIES),
                }))
            except HubSpotError:
                continue
        return list(results.values())

    COMMERCIAL_PIPELINE_LABEL = "Richieste commerciali"
    COMMERCIAL_GROUP = "commerciale_palesya"
    DEAL_PROPERTIES = (
        "dealname", "amount", "pipeline", "dealstage",
        "metodo_pagamento", "stato_pagamento", "data_pagamento", "tipo_contratto",
        "tipo_struttura", "esigenza_commerciale", "esito_qualifica", "caller_phone",
    )

    def ensure_commercial_model(self):
        """Pipeline Deal 'Richieste commerciali' + proprietà commerciali/pagamento.

        Additivo e idempotente. Richiede lo scope Deals: se assente, ritorna
        l'errore senza creare nulla.
        """
        try:
            self._get("/crm/v3/properties/deals")
        except HubSpotError:
            return {"error": "scope_deals_mancante", "created": False}
        created = {"deal_properties": [], "pipeline": None, "group": False}
        if self.ensure_property_group("deals", self.COMMERCIAL_GROUP, "Commerciale Palesya"):
            created["group"] = True
        specs = {
            "metodo_pagamento": ("Metodo pagamento", "enumeration", "select", ("bonifico", "carta", "contanti", "paypal", "stripe", "altro")),
            "stato_pagamento": ("Stato pagamento", "enumeration", "select", ("pagato", "parziale", "insoluto", "rimborsato")),
            "data_pagamento": ("Data pagamento", "date", "date", ()),
            "tipo_contratto": ("Tipo contratto", "enumeration", "select", ("licenza", "active_12", "active_24", "active_36")),
            "tipo_struttura": ("Tipo struttura", "enumeration", "select", ("palestra", "piscina", "centro_polifunzionale", "altro")),
            "esigenza_commerciale": ("Esigenza commerciale", "string", "textarea", ()),
            "esito_qualifica": ("Esito qualifica", "enumeration", "select", ("nuovo", "qualificato", "demo_fissata", "vinto", "perso")),
            "caller_phone": ("Telefono chiamante", "string", "phonenumber", ()),
        }
        for name, (label, kind, field, options) in specs.items():
            _, was_created = self.ensure_property("deals", name, label, kind, field, "Campo commerciale Palesya", options, self.COMMERCIAL_GROUP)
            if was_created:
                created["deal_properties"].append(name)
        created["pipeline"] = self.ensure_commercial_pipeline()
        return created

    def ensure_commercial_pipeline(self):
        pipelines = self._get("/crm/v3/pipelines/deals").get("results", [])
        for pipeline in pipelines:
            if str(pipeline.get("label", "")).casefold() == self.COMMERCIAL_PIPELINE_LABEL.casefold():
                stages = {str(s.get("label", "")).casefold(): str(s.get("id")) for s in pipeline.get("stages") or []}
                return {"id": str(pipeline.get("id")), "label": pipeline.get("label"), "stages": stages, "created": False}
        body = {
            "label": self.COMMERCIAL_PIPELINE_LABEL, "displayOrder": 1,
            "stages": [
                {"label": "Nuovo lead", "displayOrder": 0, "metadata": {"isClosed": "false", "probability": "0.1"}},
                {"label": "Qualificato", "displayOrder": 1, "metadata": {"isClosed": "false", "probability": "0.3"}},
                {"label": "Demo fissata", "displayOrder": 2, "metadata": {"isClosed": "false", "probability": "0.6"}},
                {"label": "Vinto", "displayOrder": 3, "metadata": {"isClosed": "true", "probability": "1.0"}},
                {"label": "Perso", "displayOrder": 4, "metadata": {"isClosed": "true", "probability": "0.0"}},
            ],
        }
        pipeline = self._post("/crm/v3/pipelines/deals", body)
        stages = {str(s.get("label", "")).casefold(): str(s.get("id")) for s in pipeline.get("stages") or []}
        return {"id": str(pipeline.get("id")), "label": pipeline.get("label"), "stages": stages, "created": True}

    def create_deal(self, properties, company_id=None, contact_id=None):
        """Crea un Deal (richiesta commerciale) associato a azienda/contatto."""
        # Association type ID HUBSPOT_DEFINED: deal→contact=3, deal→company=341.
        associations = []
        for record_id, type_id in ((contact_id, 3), (company_id, 341)):
            if record_id:
                associations.append({
                    "to": {"id": str(record_id)},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}],
                })
        body = {"properties": properties}
        if associations:
            body["associations"] = associations
        return self._post("/crm/v3/objects/deals", body)

    def search_companies_by_name(self, name):
        """Cerca aziende per nome (fallback quando il Caller ID non identifica)."""
        name = str(name or "").strip()
        if len(name) < 2:
            return []
        try:
            response = self._post("/crm/v3/objects/companies/search", {
                "filterGroups": [{"filters": [
                    {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": name},
                ]}],
                "properties": list(self.COMPANY_PROPERTIES), "limit": 20,
            })
        except HubSpotError:
            logger.warning("hubspot_error company_name_search_failed")
            return []
        unique = {str(item.get("id")): item for item in response.get("results", []) if item.get("id")}
        return list(unique.values())

    def list_finance_companies(self, max_pages=20):
        """Tutte le aziende con un contratto finanziario impostato (paginato)."""
        out = []
        after = None
        for _ in range(max_pages):
            body = {
                "filterGroups": [{"filters": [
                    {"propertyName": "fin_tipo_contratto", "operator": "HAS_PROPERTY"},
                ]}],
                "properties": list(self.COMPANY_PROPERTIES), "limit": 100,
            }
            if after:
                body["after"] = after
            try:
                response = self._post("/crm/v3/objects/companies/search", body)
            except HubSpotError:
                logger.warning("hubspot_error finance_companies_search_failed")
                break
            out.extend(response.get("results", []))
            after = ((response.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        return out

    def lookup_customer(self, phone, default_country_code="39"):
        contacts = self.search_contacts_by_phone(phone, default_country_code)
        companies_by_phone = self.search_companies_by_phone(phone, default_country_code)
        candidates = []
        for contact in contacts:
            companies = self.get_company_for_contact(contact.get("id"))
            if len(companies) == 1:
                candidates.append((contact, companies[0]))
            elif len(companies) > 1:
                candidates.extend((contact, company) for company in companies)
        unique = {(str(company.get("id")), str(contact.get("id"))): (contact, company)
                  for contact, company in candidates}
        # 1) Match pulito contatto+azienda (un solo abbinamento).
        if len(unique) == 1:
            contact, company = next(iter(unique.values()))
            return self._context("found", contact, company)
        # 2) Una sola AZIENDA riconosciuta dal numero: vince anche se i contatti sono
        #    ambigui o assenti (il numero chiama la struttura). Allego il contatto se combacia.
        if len(companies_by_phone) == 1:
            company = companies_by_phone[0]
            cid = str(company.get("id"))
            contact = next((ct for ct, c in candidates if str(c.get("id")) == cid), None)
            return self._context("found", contact, company)
        # 3) Ambiguità reale: piu' aziende con quel numero, o piu' abbinamenti.
        if len(companies_by_phone) > 1 or len(unique) > 1:
            cands = companies_by_phone or [company for _, company in unique.values()]
            return self._context("ambiguous", None, None, candidates=cands)
        return self._context("not_found", None, None)

    def _context(self, status, contact, company, candidates=()):
        props = (company or {}).get("properties") or {}
        cprops = (contact or {}).get("properties") or {}
        def integer(name, default=0):
            try:
                return max(0, int(float(props.get(name) or default)))
            except (TypeError, ValueError):
                return default
        total = integer("support_tickets_total")
        used = integer("support_tickets_used")
        remaining_value = props.get("support_tickets_remaining")
        try:
            remaining = max(0, int(float(remaining_value))) if remaining_value is not None else max(0, total - used)
        except (TypeError, ValueError):
            remaining = max(0, total - used)
        return {
            "customer_found": status == "found",
            "customer_match_status": status,
            "contact_id": str((contact or {}).get("id")) if contact else None,
            "company_id": str((company or {}).get("id")) if company else None,
            "company_name": cprops.get("company") or props.get("name") or "Unknown Caller",
            "contact_name": " ".join(filter(None, [cprops.get("firstname"), cprops.get("lastname")])).strip() or None,
            "support_plan_status": props.get("support_plan_status") or ("active" if total else "exhausted"),
            "support_tickets_total": total,
            "support_tickets_used": used,
            "support_tickets_remaining": remaining,
            "candidate_company_ids": [str(item.get("id")) for item in candidates if item.get("id")],
        }

    def create_ticket(self, properties, company_id=None, contact_id=None):
        pipeline = self.ensure_support_pipeline()
        values = dict(properties)
        values.setdefault("hs_pipeline", pipeline.get("id", "0"))
        values.setdefault("hs_pipeline_stage", self.settings.support_pipeline_stage_id or pipeline.get("stage_id", "1"))
        associations = self._associations("tickets", {"companies": company_id, "contacts": contact_id})
        body = {"properties": values}
        if associations:
            body["associations"] = associations
        return self._post("/crm/v3/objects/tickets", body)

    def update_ticket(self, ticket_id, properties):
        return self._patch("/crm/v3/objects/tickets/{}".format(ticket_id), {"properties": properties})

    def find_ticket_by_call_id(self, call_id):
        response = self._post("/crm/v3/objects/tickets/search", {
            "filterGroups": [{"filters": [{"propertyName": "telnyx_call_id", "operator": "EQ", "value": str(call_id)}]}],
            "properties": list(self.TICKET_PROPERTIES), "limit": 10,
        })
        results = response.get("results", [])
        return results[0] if len(results) == 1 else None

    OPEN_TICKET_TERMINAL_STATUSES = {"resolved", "closed", "duplicate", "non_support"}

    def find_open_tickets_by_company(self, company_id, category=None):
        """Ticket aperti dell'azienda, opzionalmente della stessa categoria.

        Serve a evitare duplicati e a bloccare il consumo su un follow-up. Un
        ticket è 'aperto' se il suo esito assistenza non è terminale.
        """
        if not company_id:
            return []
        filters = [{"propertyName": "associations.company", "operator": "EQ", "value": str(company_id)}]
        if category:
            filters.append({"propertyName": "support_issue_category", "operator": "EQ", "value": str(category)})
        try:
            response = self._post("/crm/v3/objects/tickets/search", {
                "filterGroups": [{"filters": filters}],
                "properties": list(self.TICKET_PROPERTIES),
                "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
                "limit": 20,
            })
        except HubSpotError:
            logger.warning("hubspot_error open_ticket_search_failed")
            return []
        open_tickets = []
        for item in response.get("results", []):
            status = str((item.get("properties") or {}).get("support_resolution_status") or "").strip().lower()
            if status not in self.OPEN_TICKET_TERMINAL_STATUSES:
                open_tickets.append(item)
        return open_tickets

    def create_call(self, properties, company_id=None, contact_id=None):
        """Registra una chiamata come oggetto Call nativo, associata a azienda/contatto."""
        # Association type ID HUBSPOT_DEFINED: call→contact=194, call→company=182.
        associations = []
        for record_id, type_id in ((contact_id, 194), (company_id, 182)):
            if record_id:
                associations.append({
                    "to": {"id": str(record_id)},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}],
                })
        body = {"properties": properties}
        if associations:
            body["associations"] = associations
        return self._post("/crm/v3/objects/calls", body)

    def create_callback_task(self, company_id, contact_id, company_name, caller_phone, caller_name, reason):
        body = {
            "properties": {
                "hs_task_subject": "Richiamo assistenza - {}".format(company_name or caller_name or caller_phone or "cliente"),
                "hs_task_body": "Escalation umana non riuscita. Richiamare {} ({}). Motivo: {}".format(
                    caller_name or "cliente", caller_phone or "n/d", (reason or "assistenza")[:500]),
                "hs_timestamp": str(int(time.time() * 1000)),
                "hs_task_status": "NOT_STARTED",
                "hs_task_priority": "HIGH",
            },
            "associations": self._associations("tasks", {"companies": company_id, "contacts": contact_id}),
        }
        return self._post("/crm/v3/objects/tasks", body)

    def _association_labels(self, from_type, to_type):
        key = (from_type, to_type)
        if key not in self._association_cache:
            response = self._get("/crm/v4/associations/{}/{}/labels".format(from_type, to_type))
            self._association_cache[key] = response.get("results", [])
        return self._association_cache[key]

    # typeId HUBSPOT_DEFINED per oggetto sorgente → target (unlabeled/primary).
    _ASSOCIATION_FALLBACK = {
        "tickets": {"companies": "26", "contacts": "16"},
        "tasks": {"companies": "192", "contacts": "204"},
        "calls": {"companies": "182", "contacts": "194"},
        "deals": {"companies": "341", "contacts": "3"},
    }

    def _associations(self, from_type, targets):
        output = []
        fallback = self._ASSOCIATION_FALLBACK.get(from_type, {})
        for to_type, record_id in targets.items():
            if not record_id:
                continue
            association_type_id = fallback.get(to_type)
            try:
                labels = self._association_labels(from_type, to_type)
                # Preferisci l'associazione primaria/unlabeled corretta per questa coppia.
                wanted = {"{}_to_{}".format(from_type[:-1], to_type[:-1]), "primary"}
                preferred = next((item for item in labels if str(item.get("label", "")).lower() in wanted), None)
                if preferred:
                    association_type_id = preferred.get("typeId") or association_type_id
            except HubSpotError:
                pass
            if not association_type_id:
                continue
            output.append({"to": {"id": str(record_id)}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": int(association_type_id)}]})
        return output

    def _won_deal_stages(self):
        """ID delle fasi 'vinto' (chiuse con probabilità 1) del pipeline Deals."""
        if getattr(self, "_won_stages_cache", None) is not None:
            return self._won_stages_cache
        won = set()
        try:
            for p in self._get("/crm/v3/pipelines/deals").get("results", []):
                for s in p.get("stages", []):
                    m = s.get("metadata") or {}
                    label = str(s.get("label", "")).strip().lower()
                    is_closed = str(m.get("isClosed")).lower() == "true"
                    prob = str(m.get("probability"))
                    if (is_closed and prob in ("1.0", "1")) or label in ("chiuso vinto", "closed won", "vinto"):
                        won.add(str(s.get("id")))
        except HubSpotError:
            logger.warning("hubspot_error won_stages_read_failed")
        self._won_stages_cache = won
        return won

    def company_has_won_deal(self, company_id):
        """True se l'azienda ha almeno una trattativa in fase 'vinto' (cliente confermato)."""
        won = self._won_deal_stages()
        if not company_id or not won:
            return False
        try:
            assoc = self._get("/crm/v4/objects/companies/{}/associations/deals".format(company_id),
                              {"limit": 100}).get("results", [])
            deal_ids = [str(a.get("toObjectId")) for a in assoc if a.get("toObjectId")]
            if not deal_ids:
                return False
            resp = self._post("/crm/v3/objects/deals/batch/read",
                              {"properties": ["dealstage"], "inputs": [{"id": d} for d in deal_ids]})
            for r in resp.get("results", []):
                if str((r.get("properties") or {}).get("dealstage")) in won:
                    return True
            return False
        except HubSpotError:
            logger.warning("hubspot_error won_deal_check_failed")
            return False

    def update_company(self, company_id, properties):
        return self._patch("/crm/v3/objects/companies/{}".format(company_id), {"properties": properties})

    def create_renewal_task(self, company_id, company_name, alert_level):
        body = {
            "properties": {
                "hs_task_subject": "Rinnovo pacchetto assistenza - {}".format(company_name or company_id),
                "hs_task_body": "Pacchetto assistenza a {} interventi residui. Alert: {}.".format(alert_level, alert_level),
                "hs_timestamp": str(int(time.time() * 1000)),
                "hs_task_status": "NOT_STARTED",
                "hs_task_priority": "HIGH" if alert_level in {"priority", "renewal"} else "MEDIUM",
            },
            "associations": self._associations("tasks", {"companies": company_id}),
        }
        return self._post("/crm/v3/objects/tasks", body)
