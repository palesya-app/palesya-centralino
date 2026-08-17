# Palesya Centralino AI

Backend isolato del centralino telefonico AI di Palesya: fa da ponte tra
**Retell AI / Telnyx** e **HubSpot** (riconoscimento cliente, ticket assistenza,
scalo interventi, richieste commerciali, log chiamate, callback).

È un servizio **separato** dal sito e dal control plane Palesya: si deploya da
solo (es. Render, blueprint `render.yaml`) su un URL nascosto.

## Avvio
```
uvicorn support_app:app --host 0.0.0.0 --port ${PORT:-8080}
```

## Variabili d'ambiente (secret nel runtime, mai nel repo)
`PALESYA_SECRET_KEY`, `HUBSPOT_ACCESS_TOKEN`, `HUBSPOT_SUPPORT_PIPELINE_ID=0`,
`RETELL_API_KEY`, `TELNYX_PUBLIC_KEY`, `SUPPORT_ADMIN_SECRET`,
`SUPPORT_ALLOW_UNSIGNED_WEBHOOKS=0`.

## Test
```
pip install -r requirements-dev.txt
pytest -q
```
