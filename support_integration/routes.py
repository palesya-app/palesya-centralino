"""API routes per webhook Telnyx/Voice AI e amministrazione supporto."""
import collections
import threading
import time

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

import security
from lan_config import is_loopback

from .config import settings as support_settings
from .service import SupportService
from .telnyx import parse_event, verify_signature
from .voice_ai import (
    parse_event as parse_voice_event,
    verify_retell_signature,
    verify_signature as verify_voice_signature,
)


service = SupportService()
_rate_lock = threading.Lock()
_rate_buckets = collections.defaultdict(collections.deque)


def _rate_limited(request, limit=120):
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _rate_lock:
        bucket = _rate_buckets[ip]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        return False


def _admin_allowed(request):
    user = getattr(request.state, "user", None)
    if security.allowed(user, "admin") and (not security.settings.auth_required or user):
        return True
    if is_loopback(request.client.host if request.client else "") and not security.settings.auth_required:
        return True
    provided = request.headers.get("x-support-admin-secret", "")
    return bool(support_settings.support_admin_secret and provided and provided == support_settings.support_admin_secret)


def _unsigned_allowed():
    return support_settings.allow_unsigned_webhooks and str(__import__("os").environ.get("PALESYA_ENV", __import__("os").environ.get("GYMFLOW_ENV", "local"))).lower() != "production"


def _field(body, request, *keys):
    """Legge un campo dal body JSON, con fallback sulla querystring.

    I tool Retell passano spesso call_id/phone come query (`{{call_id}}`,
    `{{from_number}}`) mentre la descrizione libera resta nel body: così i
    due canali funzionano entrambi senza obbligare un formato preciso.
    """
    for key in keys:
        value = body.get(key)
        if value not in (None, ""):
            return str(value)
    for key in keys:
        value = request.query_params.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def build_router():
    router = APIRouter()

    @router.post("/api/webhooks/telnyx")
    async def telnyx_webhook(request: Request, background_tasks: BackgroundTasks):
        if _rate_limited(request, 600):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        raw = await request.body()
        verified = verify_signature(raw, request.headers, support_settings.telnyx_public_key, support_settings.telnyx_timestamp_tolerance)
        if not verified and not _unsigned_allowed():
            return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=403)
        try:
            event = parse_event(raw)
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        try:
            if not service.register_event(event):
                return {"ok": True, "duplicate": True}
            background_tasks.add_task(service.process_telnyx_event, event)
            return {"ok": True, "accepted": True}
        except Exception:
            return JSONResponse({"ok": False, "error": "persistence_error"}, status_code=503)

    @router.post("/api/webhooks/voice-ai")
    async def voice_ai_webhook(request: Request, background_tasks: BackgroundTasks):
        if _rate_limited(request, 600):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        raw = await request.body()
        verified = (
            verify_voice_signature(raw, request.headers, support_settings.voice_ai_webhook_secret)
            or verify_retell_signature(raw, request.headers, support_settings.retell_api_key,
                                       support_settings.telnyx_timestamp_tolerance)
        )
        if not verified and not _unsigned_allowed():
            return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=403)
        try:
            event = parse_voice_event(raw)
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        try:
            if not service.register_event(event):
                return {"ok": True, "duplicate": True}
            background_tasks.add_task(service.process_voice_event, event)
            return {"ok": True, "accepted": True}
        except Exception:
            return JSONResponse({"ok": False, "error": "persistence_error"}, status_code=503)

    @router.get("/api/voice-ai/context")
    async def voice_ai_context(request: Request, phone: str = ""):
        if _rate_limited(request, 60):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        if not verify_voice_signature(b"", request.headers, support_settings.voice_ai_webhook_secret) and not _unsigned_allowed():
            return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=403)
        if not phone or len(phone) > 64:
            return JSONResponse({"ok": False, "error": "phone_required"}, status_code=400)
        try:
            return {"ok": True, **service.context_for_phone(phone)}
        except Exception:
            return JSONResponse({"ok": False, "error": "crm_unavailable"}, status_code=503)

    def _voice_signed(raw, headers):
        return (
            verify_voice_signature(raw, headers, support_settings.voice_ai_webhook_secret)
            or verify_retell_signature(raw, headers, support_settings.retell_api_key,
                                       support_settings.telnyx_timestamp_tolerance)
            or _unsigned_allowed()
        )

    @router.post("/api/voice-ai/ticket")
    async def voice_ai_ticket(request: Request):
        if _rate_limited(request, 120):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        raw = await request.body()
        if not _voice_signed(raw, request.headers):
            return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=403)
        try:
            import json as _json
            body = _json.loads(bytes(raw).decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        call_id = _field(body, request, "call_id").strip()
        if not call_id or len(call_id) > 200:
            return JSONResponse({"ok": False, "error": "call_id_required"}, status_code=400)
        # Contratto minimo: l'agente può passare solo call_id + phone + description.
        # Alias accettati per la descrizione libera del problema.
        description = _field(body, request, "description", "problem", "issue")
        try:
            return service.upsert_ticket(
                call_id, phone=_field(body, request, "phone", "from_number"), description=description,
                contact_name=_field(body, request, "contact_name", "caller_name", "name"),
                company_name=_field(body, request, "company_name", "gym_name", "structure"),
                category=str(body.get("category") or ""),
                summary=str(body.get("summary") or ""), severity=str(body.get("severity") or ""),
                troubleshooting=str(body.get("troubleshooting") or ""), device=str(body.get("device") or ""),
                intent=str(body.get("intent") or "technical"),
                escalation_reason=str(body.get("human_escalation_reason") or ""),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"ok": False, "error": "crm_unavailable"}, status_code=503)

    @router.post("/api/voice-ai/verify")
    async def voice_ai_verify(request: Request):
        if _rate_limited(request, 120):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        raw = await request.body()
        if not _voice_signed(raw, request.headers):
            return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=403)
        try:
            import json as _json
            body = _json.loads(bytes(raw).decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        try:
            return {"ok": True, **service.verify_customer(
                phone=_field(body, request, "phone", "from_number"), company_name=str(body.get("company_name") or ""),
                contact_name=str(body.get("contact_name") or ""), call_id=_field(body, request, "call_id"),
            )}
        except Exception:
            return JSONResponse({"ok": False, "error": "crm_unavailable"}, status_code=503)

    @router.post("/api/voice-ai/commercial")
    async def voice_ai_commercial(request: Request):
        if _rate_limited(request, 120):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        raw = await request.body()
        if not _voice_signed(raw, request.headers):
            return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=403)
        try:
            import json as _json
            body = _json.loads(bytes(raw).decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        call_id = _field(body, request, "call_id").strip()
        if not call_id or len(call_id) > 200:
            return JSONResponse({"ok": False, "error": "call_id_required"}, status_code=400)
        try:
            return service.create_commercial_request(
                call_id, phone=_field(body, request, "phone", "from_number"), company_name=str(body.get("company_name") or ""),
                contact_name=str(body.get("contact_name") or ""), structure=str(body.get("structure") or ""),
                need=str(body.get("need") or ""), outcome=str(body.get("outcome") or "nuovo"),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"ok": False, "error": "crm_unavailable"}, status_code=503)

    @router.post("/api/voice-ai/callback")
    async def voice_ai_callback(request: Request):
        if _rate_limited(request, 120):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        raw = await request.body()
        if not _voice_signed(raw, request.headers):
            return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=403)
        try:
            import json as _json
            body = _json.loads(bytes(raw).decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        call_id = _field(body, request, "call_id").strip()
        if not call_id or len(call_id) > 200:
            return JSONResponse({"ok": False, "error": "call_id_required"}, status_code=400)
        try:
            return service.create_callback(
                call_id, phone=_field(body, request, "phone", "from_number"), reason=str(body.get("reason") or ""),
                name=_field(body, request, "name", "contact_name", "caller_name"),
                company_name=_field(body, request, "company_name", "gym_name", "structure"),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"ok": False, "error": "callback_failed"}, status_code=503)

    @router.post("/api/support/setup")
    async def support_setup(request: Request):
        if not _admin_allowed(request):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        try:
            return {"ok": True, **service.hubspot.ensure_support_model()}
        except Exception:
            return JSONResponse({"ok": False, "error": "hubspot_setup_failed"}, status_code=503)

    @router.post("/api/finance/setup")
    async def finance_setup(request: Request):
        if not _admin_allowed(request):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        try:
            return {"ok": True, **service.hubspot.ensure_finance_model()}
        except Exception:
            return JSONResponse({"ok": False, "error": "finance_setup_failed"}, status_code=503)

    @router.post("/api/finance/sync")
    async def finance_sync(request: Request):
        # Import dal gestionale Palesya: aggiorna il quadro finanziario di un cliente.
        if not _admin_allowed(request):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        try:
            return service.sync_finance(
                company_id=str(body.get("company_id") or ""), phone=str(body.get("phone") or ""),
                company_name=str(body.get("company_name") or ""), payload=body,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except Exception:
            return JSONResponse({"ok": False, "error": "finance_sync_failed"}, status_code=503)

    @router.get("/api/finance/status")
    async def finance_status(request: Request, phone: str = "", company_id: str = "", company_name: str = ""):
        # Sola lettura: l'AI legge il quadro finanziario del cliente.
        if _rate_limited(request, 60):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        if not verify_voice_signature(b"", request.headers, support_settings.voice_ai_webhook_secret) and not _unsigned_allowed():
            return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=403)
        if not (phone or company_id or company_name):
            return JSONResponse({"ok": False, "error": "identifier_required"}, status_code=400)
        try:
            return {"ok": True, **service.financial_status(phone=phone, company_id=company_id, company_name=company_name)}
        except Exception:
            return JSONResponse({"ok": False, "error": "crm_unavailable"}, status_code=503)

    @router.post("/api/support/admin/reverse-consumption")
    async def reverse_consumption(request: Request):
        if not _admin_allowed(request):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
            call_id = str(body.get("call_id") or "").strip()
            if not call_id or len(call_id) > 200:
                return JSONResponse({"ok": False, "error": "call_id_required"}, status_code=400)
            user = getattr(request.state, "user", None) or {}
            return service.reverse_consumption(call_id, actor=str(user.get("USERNAME") or "admin"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except Exception:
            return JSONResponse({"ok": False, "error": "reverse_failed"}, status_code=503)

    return router
