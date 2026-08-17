"""App ASGI minima per il solo centralino AI (Retell/Telnyx -> HubSpot).

Espone unicamente gli endpoint dell'assistenza telefonica, senza il control
plane e senza il sito Palesya: si puo' deployare su un hosting separato (un
sottodominio nascosto) senza toccare il sito web. Il sito e il control plane
restano invariati.

Avvio:  uvicorn support_app:app --host 0.0.0.0 --port ${PORT:-8080}
"""
from fastapi import FastAPI

from config import settings
from schema import ensure_schema
from support_integration.routes import build_router


# Crea le tabelle SUPPORT_* se non esistono (idempotente).
ensure_schema(settings.database_path)

app = FastAPI(
    title="Palesya AI Support",
    docs_url=None, redoc_url=None, openapi_url=None,  # nessuna superficie pubblica extra
)
app.include_router(build_router())


@app.get("/health")
def health():
    return {"status": "ok", "product": "Palesya Support"}
