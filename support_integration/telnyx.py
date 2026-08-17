"""Verifica e normalizzazione dei webhook Telnyx Voice API."""
import base64
import hashlib
import json
import logging
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

logger = logging.getLogger("palesya.support.telnyx")


def _header(headers, name):
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return ""


def _public_key_bytes(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("-----BEGIN"):
        import re
        compact = re.sub(r"-----[^-]+-----|\s+", "", text)
        return base64.b64decode(compact)
    return base64.b64decode(text)


def verify_signature(raw_body, headers, public_key, tolerance_seconds=300, now=None):
    """Valida Telnyx-Signature-Ed25519 su ``timestamp|raw JSON``.

    Telnyx documenta una tolleranza di cinque minuti per mitigare i replay.
    """
    signature = _header(headers, "telnyx-signature-ed25519")
    timestamp = _header(headers, "telnyx-timestamp")
    if not signature or not timestamp or not public_key:
        return False
    try:
        timestamp_int = int(timestamp)
        current = int(time.time() if now is None else now)
        if abs(current - timestamp_int) > int(tolerance_seconds):
            return False
        if str(public_key).lstrip().startswith("-----BEGIN"):
            key = load_pem_public_key(str(public_key).encode("ascii"))
        else:
            key = Ed25519PublicKey.from_public_bytes(_public_key_bytes(public_key))
        key.verify(base64.b64decode(signature), (timestamp + "|").encode("utf-8") + bytes(raw_body))
        return True
    except (ValueError, TypeError, KeyError, InvalidSignature, base64.binascii.Error):
        logger.warning("telnyx_error invalid_signature")
        return False


def parse_event(raw_body):
    payload = json.loads(bytes(raw_body).decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("Payload Telnyx senza data")
    event_id = str(data.get("id") or payload.get("id") or "").strip()
    event_type = str(data.get("event_type") or data.get("eventType") or "").strip()
    event_payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    call_id = str(
        event_payload.get("call_control_id")
        or event_payload.get("call_session_id")
        or event_payload.get("call_leg_id")
        or event_payload.get("call_id")
        or ""
    ).strip()
    occurred_at = str(data.get("occurred_at") or event_payload.get("occurred_at") or "").strip()
    if not event_id or not event_type:
        raise ValueError("Payload Telnyx incompleto")
    return {
        "source": "telnyx",
        "event_id": event_id,
        "event_type": event_type,
        "call_id": call_id,
        "occurred_at": occurred_at,
        "payload": payload,
        "data": data,
        "event_payload": event_payload,
        "payload_hash": hashlib.sha256(bytes(raw_body)).hexdigest(),
    }


def extract_call_data(event):
    payload = event.get("event_payload") or {}
    from_value = payload.get("from") or payload.get("caller_id_number") or payload.get("caller") or ""
    to_value = payload.get("to") or payload.get("called_number") or ""
    if isinstance(from_value, dict):
        from_value = from_value.get("phone_number") or from_value.get("number") or ""
    if isinstance(to_value, dict):
        to_value = to_value.get("phone_number") or to_value.get("number") or ""
    duration = payload.get("call_duration") or payload.get("duration_seconds") or payload.get("duration")
    try:
        duration = int(float(duration)) if duration not in {None, ""} else None
    except (TypeError, ValueError):
        duration = None
    return {
        "call_id": event.get("call_id") or str(payload.get("call_control_id") or ""),
        "telnyx_call_id": str(payload.get("call_control_id") or event.get("call_id") or ""),
        "from_phone": str(from_value or ""),
        "to_phone": str(to_value or ""),
        "duration_seconds": duration,
        "recording_url": None,  # non viene conservato automaticamente
        "transcript": None,
    }
