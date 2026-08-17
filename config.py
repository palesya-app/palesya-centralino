"""Configurazione centralizzata di Palesya con alias GymFlow compatibili."""
from dataclasses import dataclass
import os
from pathlib import Path

from build_info import current_version


BASE_DIR = Path(__file__).resolve().parent


def env(name, default=None):
    """Legge PALESYA_* prima del corrispondente alias GYMFLOW_*.

    I nomi legacy restano supportati per non interrompere installazioni e
    automazioni gia distribuite.
    """
    current = "PALESYA_" + name
    legacy = "GYMFLOW_" + name
    if current in os.environ:
        return os.environ[current]
    return os.getenv(legacy, default)


def _bool(name, default=False):
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str
    host: str
    port: int
    data_dir: Path
    database_path: Path
    secret_key: str
    auth_required: bool
    secure_cookies: bool
    timezone: str
    backup_keep: int
    control_plane_url: str
    app_version: str
    fleet_enabled: bool
    license_required: bool
    heartbeat_seconds: int
    update_check_seconds: int
    license_lease_seconds: int
    update_rollout_seconds: int
    auto_update_on_exit: bool
    database_url: str
    public_site: bool
    control_plane: bool
    legal_name: str
    vat_number: str
    legal_address: str
    privacy_email: str
    support_email: str

    @property
    def production(self):
        return self.environment == "production"


def load_settings():
    environment = env("ENV", "local").strip().lower()
    data_dir = Path(env("DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    database_path = Path(
        os.getenv("FORFIT_DB", env("DB", str(data_dir / "gymflow.sqlite")))
    ).expanduser().resolve()
    secret = env("SECRET_KEY", "")
    if environment == "production" and len(secret) < 32:
        raise RuntimeError("In production PALESYA_SECRET_KEY deve contenere almeno 32 caratteri")
    if not secret:
        secret = "palesya-local-development-key-change-me"
    control_plane = _bool("CONTROL_PLANE", False)
    license_required = _bool("LICENSE_REQUIRED", not control_plane)
    control_plane_url = str(env("CONTROL_PLANE_URL", "") or "").strip().rstrip("/")
    if license_required and not control_plane_url:
        control_plane_url = "https://palesya.it"
    auth_required = _bool("AUTH_REQUIRED", environment == "production")
    secure_cookies = _bool("SECURE_COOKIES", environment == "production")
    if environment == "production" and control_plane and not auth_required:
        raise RuntimeError("Il control plane production richiede PALESYA_AUTH_REQUIRED=1")
    if environment == "production" and control_plane and not secure_cookies:
        raise RuntimeError("Il control plane production richiede PALESYA_SECURE_COOKIES=1")
    return Settings(
        environment=environment,
        host=env("HOST", "127.0.0.1" if environment == "local" else "0.0.0.0"),
        port=int(env("PORT", os.getenv("PORT", "8080"))),
        data_dir=data_dir,
        database_path=database_path,
        secret_key=secret,
        auth_required=auth_required,
        secure_cookies=secure_cookies,
        timezone=env("TIMEZONE", "Europe/Rome"),
        backup_keep=max(1, int(env("BACKUP_KEEP", "14"))),
        control_plane_url=control_plane_url,
        app_version=env("VERSION", current_version("0.0.0")),
        fleet_enabled=_bool("FLEET_ENABLED", not control_plane),
        license_required=license_required,
        heartbeat_seconds=max(60, int(env("HEARTBEAT_SECONDS", "300"))),
        update_check_seconds=max(1800, int(env("UPDATE_CHECK_SECONDS", "3600"))),
        license_lease_seconds=min(604800, max(21600, int(env("LICENSE_LEASE_SECONDS", "259200")))),
        update_rollout_seconds=min(86400, max(0, int(env("UPDATE_ROLLOUT_SECONDS", "7200")))),
        auto_update_on_exit=_bool("AUTO_UPDATE_ON_EXIT", True),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        public_site=_bool("PUBLIC_SITE", False),
        control_plane=control_plane,
        legal_name=env("LEGAL_NAME", "Palesya"),
        vat_number=env("VAT_NUMBER", ""),
        legal_address=env("LEGAL_ADDRESS", "Italia"),
        privacy_email=env("PRIVACY_EMAIL", "palesya@outlook.it"),
        support_email=env("SUPPORT_EMAIL", "palesya@outlook.it"),
    )


settings = load_settings()
