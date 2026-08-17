"""Configurazione isolata dell'integrazione supporto.

I secret possono usare sia i nomi espliciti descritti nel brief sia il prefisso
PALESYA_ già usato dal progetto. Non vengono mai esposti dal modulo.
"""
from dataclasses import dataclass
import os

from config import env


def _value(name, default=""):
    direct = os.getenv(name)
    if direct is not None:
        return direct
    return env(name, default)


def _bool(name, default=False):
    value = str(_value(name, "1" if default else "0") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _csv(name, default):
    raw = _value(name, default)
    return tuple(item.strip() for item in str(raw or "").split(",") if item.strip())


@dataclass(frozen=True)
class SupportSettings:
    enabled: bool
    hubspot_access_token: str
    hubspot_base_url: str
    hubspot_timeout_seconds: float
    telnyx_public_key: str
    telnyx_timestamp_tolerance: int
    voice_ai_webhook_secret: str
    voice_ai_base_url: str
    voice_ai_api_key: str
    support_admin_secret: str
    default_country_code: str
    support_pipeline_id: str
    support_pipeline_label: str
    support_pipeline_stage_id: str
    support_consumption_statuses: tuple
    allow_unsigned_webhooks: bool
    retell_api_key: str = ""


def load_support_settings():
    token = str(_value("HUBSPOT_ACCESS_TOKEN", "") or "").strip()
    return SupportSettings(
        enabled=bool(token),
        hubspot_access_token=token,
        hubspot_base_url=str(_value("HUBSPOT_BASE_URL", "https://api.hubapi.com") or "").rstrip("/"),
        hubspot_timeout_seconds=max(1.0, min(30.0, float(_value("HUBSPOT_TIMEOUT_SECONDS", "8")))),
        telnyx_public_key=str(_value("TELNYX_PUBLIC_KEY", "") or "").strip(),
        telnyx_timestamp_tolerance=max(30, int(_value("TELNYX_TIMESTAMP_TOLERANCE", "300"))),
        voice_ai_webhook_secret=str(_value("VOICE_AI_WEBHOOK_SECRET", "") or "").strip(),
        retell_api_key=str(_value("RETELL_API_KEY", "") or "").strip(),
        voice_ai_base_url=str(_value("VOICE_AI_BASE_URL", "") or "").rstrip("/"),
        voice_ai_api_key=str(_value("VOICE_AI_API_KEY", "") or "").strip(),
        support_admin_secret=str(_value("SUPPORT_ADMIN_SECRET", "") or "").strip(),
        default_country_code=str(_value("SUPPORT_DEFAULT_COUNTRY_CODE", "39") or "39").strip(),
        support_pipeline_id=str(_value("HUBSPOT_SUPPORT_PIPELINE_ID", "") or "").strip(),
        support_pipeline_label=str(_value("HUBSPOT_SUPPORT_PIPELINE_LABEL", "Assistenza Tecnica") or "Assistenza Tecnica").strip(),
        support_pipeline_stage_id=str(_value("HUBSPOT_SUPPORT_PIPELINE_STAGE_ID", "") or "").strip(),
        support_consumption_statuses=_csv("SUPPORT_CONSUMPTION_STATUSES", "resolved,escalated"),
        allow_unsigned_webhooks=_bool("SUPPORT_ALLOW_UNSIGNED_WEBHOOKS", False),
    )


settings = load_support_settings()
