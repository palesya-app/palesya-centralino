"""Adapter neutro per il webhook dell'agente telefonico già in uso."""
import base64
import hashlib
import hmac
import json
import re
import time


def verify_signature(raw_body, headers, secret):
    """Accetta Bearer condiviso o HMAC-SHA256 hex/base64."""
    if not secret:
        return False
    auth = next((str(value) for key, value in headers.items() if str(key).lower() == "authorization"), "")
    if hmac.compare_digest(auth, "Bearer " + secret):
        return True
    provided = next((str(value) for key, value in headers.items() if str(key).lower() in {"x-voice-ai-signature", "x-webhook-signature"}), "")
    if provided.startswith("sha256="):
        provided = provided[7:]
    digest = hmac.new(secret.encode("utf-8"), bytes(raw_body), hashlib.sha256).digest()
    encoded = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(provided, digest.hex()) or hmac.compare_digest(provided, encoded)


def verify_retell_signature(raw_body, headers, api_key, tolerance_seconds=300):
    """Verifica X-Retell-Signature usando la API key abilitata ai webhook."""
    if not api_key:
        return False
    provided = next((str(value) for key, value in headers.items() if str(key).lower() == "x-retell-signature"), "")
    match = re.fullmatch(r"v=(\d+),d=([0-9a-fA-F]+)", provided)
    if not match:
        return False
    timestamp = match.group(1)
    try:
        if abs(time.time() - int(timestamp) / 1000.0) > max(30, int(tolerance_seconds)):
            return False
    except (TypeError, ValueError):
        return False
    signed = bytes(raw_body).decode("utf-8") + timestamp
    expected = hmac.new(api_key.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(match.group(2).lower(), expected.lower())


def parse_event(raw_body):
    payload = json.loads(bytes(raw_body).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Payload Voice AI non valido")
    retell_call = payload.get("call") if isinstance(payload.get("call"), dict) else None
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else (retell_call or payload)
    event_id = str(payload.get("event_id") or payload.get("id") or nested.get("id") or "").strip()
    call_id = str(nested.get("call_id") or nested.get("conversation_id") or nested.get("voice_ai_call_id") or nested.get("telnyx_call_id") or "").strip()
    event_type = str(payload.get("event") or payload.get("event_type") or payload.get("type") or nested.get("event_type") or "call.completed").strip()
    if not event_id:
        event_id = hashlib.sha256(bytes(raw_body)).hexdigest()
    return {
        "source": "voice_ai", "event_id": event_id, "event_type": event_type,
        "call_id": call_id, "payload": payload, "data": nested,
        "payload_hash": hashlib.sha256(bytes(raw_body)).hexdigest(),
    }


def map_final_event(event):
    data = event.get("data") or {}
    analysis = data.get("call_analysis") if isinstance(data.get("call_analysis"), dict) else {}
    custom = analysis.get("custom_analysis_data") if isinstance(analysis.get("custom_analysis_data"), dict) else {}
    summary = data.get("summary") or data.get("support_ai_summary") or data.get("ai_summary") or custom.get("summary") or analysis.get("call_summary") or ""
    resolution = data.get("resolution") or data.get("support_resolution") or custom.get("resolution") or ""
    category = str(data.get("category") or data.get("support_issue_category") or custom.get("category") or "other").strip().lower()
    status = str(data.get("resolution_status") or data.get("support_resolution_status") or custom.get("resolution_status") or "new").strip().lower()
    duration = data.get("call_duration") or data.get("duration_seconds") or data.get("duration")
    duration_ms = data.get("call_duration_ms") or data.get("duration_ms")
    if duration in {None, ""} and duration_ms not in {None, ""}:
        try:
            duration = float(duration_ms) / 1000.0
        except (TypeError, ValueError):
            duration = None
    try:
        duration = int(float(duration)) if duration not in {None, ""} else None
    except (TypeError, ValueError):
        duration = None

    def _phone(*keys):
        for key in keys:
            value = data.get(key)
            if isinstance(value, dict):
                value = value.get("phone_number") or value.get("number")
            if value not in {None, ""}:
                return str(value)
        return ""

    return {
        "call_id": event.get("call_id") or str(data.get("telnyx_call_id") or ""),
        "voice_ai_call_id": str(data.get("voice_ai_call_id") or data.get("conversation_id") or event.get("call_id") or ""),
        # Numeri chiamante/chiamato dal payload provider (Retell: from_number/to_number).
        # Servono a creare la sessione quando la chiamata arriva solo dal webhook Voice AI.
        "from_phone": _phone("from_number", "from", "caller_phone", "caller_id_number", "caller"),
        "to_phone": _phone("to_number", "to", "called_number"),
        "telnyx_call_id": str(data.get("telnyx_call_id") or ""),
        "category": category,
        "summary": str(summary or "")[:10000],
        "resolution": str(resolution or "")[:10000],
        "resolved": bool(data.get("resolved") or custom.get("resolved") or analysis.get("call_successful")),
        "escalation_required": bool(data.get("escalation_required") or data.get("escalated") or custom.get("escalation_required") or custom.get("escalated")),
        "support_consumed_proposed": bool(data.get("support_consumed") or custom.get("support_consumed")),
        "reason_for_consumption": str(data.get("reason_for_consumption") or custom.get("reason_for_consumption") or "Technical assistance completed")[:1000],
        "resolution_status": status,
        "duration_seconds": duration,
        "severity": str(data.get("severity") or custom.get("severity") or "").strip().lower(),
        "troubleshooting": str(data.get("troubleshooting") or data.get("troubleshooting_attempted") or custom.get("troubleshooting") or "")[:4000],
        "device": str(data.get("device") or data.get("device_involved") or custom.get("device") or "")[:200],
        "intent": str(data.get("intent") or custom.get("intent") or "technical").strip().lower(),
        "human_escalation_reason": str(data.get("human_escalation_reason") or custom.get("human_escalation_reason") or "")[:1000],
    }
