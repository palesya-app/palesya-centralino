"""API routes per webhook Telnyx/Voice AI e amministrazione supporto."""
import collections
import threading
import time

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, HTMLResponse

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


ASSISTENZA_FORM_HTML = """<!doctype html>
<html lang="it"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Assistenza Palesya</title>
<link rel="icon" href="https://palesya.it/static/palesya-mark.png">
<style>
 :root{--ink:#12261a;--muted:#5b6b60;--brand:#1a8f4e;--brand-dark:#0e3d24;--lime:#a4d65e;
   --ok:#0f7a3d;--err:#b3261e;--line:#dbe6de;--bg:#eef4ef}
 *{box-sizing:border-box} body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
  background:var(--bg);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;color:var(--ink)}
 .card{background:#fff;width:100%;max-width:560px;border:1px solid var(--line);border-radius:16px;
  box-shadow:0 8px 30px rgba(0,0,0,.06);padding:32px}
 .brand{display:flex;align-items:center;gap:12px;margin-bottom:22px;padding-bottom:20px;border-bottom:1px solid var(--line)}
 .brand img{height:34px;width:auto} .brand .t{font-size:15px;font-weight:700;letter-spacing:.02em;color:var(--brand-dark)}
 .brand .t small{display:block;font-weight:500;color:var(--muted);font-size:12px;letter-spacing:0}
 h1{margin:0 0 4px;font-size:21px;font-weight:700;color:var(--brand-dark)} .sub{color:var(--muted);margin:0 0 22px;font-size:14px;line-height:1.5}
 label{display:block;font-size:13px;font-weight:600;margin:14px 0 6px}
 input,select,textarea{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:10px;font-size:15px;font-family:inherit;background:#fff;color:var(--ink);transition:border-color .15s}
 input:focus,select:focus,textarea:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(26,143,78,.12)}
 textarea{min-height:110px;resize:vertical} .row{display:flex;gap:12px} .row>div{flex:1}
 @media(max-width:460px){.row{flex-direction:column;gap:0}}
 .hp{position:absolute;left:-9999px} button{margin-top:22px;width:100%;background:var(--brand);color:#fff;border:0;
  padding:15px;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;transition:opacity .15s}
 button:hover{background:var(--brand-dark)} button:disabled{opacity:.5;cursor:default}
 .msg{margin-top:16px;padding:14px;border-radius:10px;font-size:14px;display:none}
 .msg.ok{background:#eaf6ef;color:var(--ok);display:block} .msg.err{background:#fdecec;color:var(--err);display:block}
 .foot{margin-top:20px;color:var(--muted);font-size:12px;text-align:center}
</style></head><body>
<div class="card">
 <div class="brand">
   <svg width="40" height="40" viewBox="0 0 100 100" aria-label="Palesya">
     <polygon points="50,5 89,27 89,73 50,95 11,73 11,27" fill="#0e3d24"/>
     <polygon points="50,20 76,35 76,65 50,80 24,65 24,35" fill="#1a8f4e"/>
     <path d="M11,64 L50,86 L89,64 L89,74 L50,96 L11,74 Z" fill="#a4d65e"/>
     <rect x="35" y="35" width="7" height="30" rx="2" fill="#fff"/>
     <rect x="58" y="35" width="7" height="30" rx="2" fill="#fff"/>
     <rect x="35" y="46" width="30" height="7" rx="2" fill="#fff"/>
   </svg>
   <div class="t">Palesya<small>Assistenza clienti</small></div></div>
 <h1>Apri una segnalazione</h1>
 <p class="sub">Compila il modulo: apriamo subito il ticket e il team tecnico ti ricontatta.</p>
 <form id="f">
  <div class="row"><div><label>Nome e cognome</label><input name="name" autocomplete="name" required></div>
   <div><label>Palestra / struttura</label><input name="company_name" required></div></div>
  <div class="row"><div><label>Email</label><input type="email" name="email" autocomplete="email"></div>
   <div><label>Telefono</label><input name="phone" autocomplete="tel"></div></div>
  <label>Tipo di problema</label>
  <select name="category">
   <option value="">Rilevalo automaticamente</option>
   <option value="software">Software / gestionale</option>
   <option value="access_control">Controllo accessi / tessere</option>
   <option value="turnstile">Tornelli / varchi</option>
   <option value="hardware">Hardware (stampante, lettore, PC...)</option>
   <option value="billing">Fatturazione / pagamenti</option>
   <option value="configuration">Configurazione</option>
   <option value="migration">Migrazione dati</option>
   <option value="other">Altro</option>
  </select>
  <label>Descrivi il problema</label>
  <textarea name="description" required placeholder="Cosa non funziona, da quando, cosa hai gia' provato..."></textarea>
  <input class="hp" name="website" tabindex="-1" autocomplete="off">
  <button type="submit" id="btn">Apri la segnalazione</button>
 </form>
 <div id="msg" class="msg"></div>
 <div class="foot">Palesya — assistenza clienti</div>
</div>
<script>
 const f=document.getElementById('f'),btn=document.getElementById('btn'),msg=document.getElementById('msg');
 f.addEventListener('submit',async e=>{e.preventDefault();btn.disabled=true;btn.textContent='Invio...';msg.className='msg';
  const d=Object.fromEntries(new FormData(f).entries());
  try{const r=await fetch('/api/web/ticket',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
   const j=await r.json();
   if(r.ok&&j.ok){msg.className='msg ok';msg.textContent='Segnalazione aperta! Numero ticket '+j.ticket_id+'. Ti ricontattiamo al piu\\' presto.';f.reset();}
   else{msg.className='msg err';msg.textContent='Non e\\' stato possibile inviare: '+(j.error||'riprova piu\\' tardi')+'.';}
  }catch(_){msg.className='msg err';msg.textContent='Errore di rete, riprova.';}
  btn.disabled=false;btn.textContent='Apri la segnalazione';});
</script></body></html>"""


def build_router():
    router = APIRouter()

    @router.get("/assistenza")
    async def assistenza_form():
        return HTMLResponse(ASSISTENZA_FORM_HTML)

    @router.post("/api/web/ticket")
    async def web_ticket(request: Request):
        # Endpoint pubblico del form assistenza (nessun segreto lato client).
        if _rate_limited(request, 30):
            return JSONResponse({"ok": False, "error": "troppe richieste, riprova tra poco"}, status_code=429)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "dati non validi"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "dati non validi"}, status_code=400)
        if str(body.get("website") or "").strip():  # honeypot anti-bot
            return {"ok": True, "ticket_id": "—"}
        description = str(body.get("description") or "").strip()
        if len(description) < 5:
            return JSONResponse({"ok": False, "error": "descrivi meglio il problema"}, status_code=400)
        try:
            return service.create_web_ticket(
                name=str(body.get("name") or "")[:120], company_name=str(body.get("company_name") or "")[:200],
                email=str(body.get("email") or "")[:200], phone=str(body.get("phone") or "")[:40],
                category=str(body.get("category") or ""), description=description[:5000],
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"ok": False, "error": "servizio non disponibile"}, status_code=503)

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

    @router.get("/api/finance/report")
    async def finance_report(request: Request, giorni: int = 30):
        # Riepilogo finanziario aggregato (totale incassato, insoluti, prossime scadenze).
        if not _admin_allowed(request):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        try:
            giorni = max(1, min(365, int(giorni)))
        except (TypeError, ValueError):
            giorni = 30
        try:
            return {"ok": True, **service.finance_report(scadenze_giorni=giorni)}
        except Exception:
            return JSONResponse({"ok": False, "error": "finance_report_failed"}, status_code=503)

    @router.post("/api/finance/expenses/setup")
    async def expenses_setup(request: Request):
        if not _admin_allowed(request):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        try:
            return {"ok": True, **service.hubspot.ensure_expenses_model()}
        except Exception:
            return JSONResponse({"ok": False, "error": "expenses_setup_failed"}, status_code=503)

    @router.post("/api/finance/expense")
    async def finance_expense(request: Request):
        # Registra una uscita (costo aziendale).
        if not _admin_allowed(request):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        try:
            return service.record_expense(body)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"ok": False, "error": "expense_failed"}, status_code=503)

    @router.get("/api/finance/overview")
    async def finance_overview(request: Request, giorni: int = 30):
        # Quadro unico entrate - uscite = utile.
        if not _admin_allowed(request):
            return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
        try:
            giorni = max(1, min(365, int(giorni)))
        except (TypeError, ValueError):
            giorni = 30
        try:
            return {"ok": True, **service.finance_overview(scadenze_giorni=giorni)}
        except Exception:
            return JSONResponse({"ok": False, "error": "overview_failed"}, status_code=503)

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
