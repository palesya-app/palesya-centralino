# Centralino AI Palesya — Logica corrente

> Documento di riferimento sulla logica **attualmente in produzione** del centralino
> telefonico AI (Retell → backend Render → HubSpot). Aggiornato: 2026-08-31.
> Nessun segreto in questo file (token/chiavi restano solo nei file locali).

## 1. Flusso della chiamata

```
Cliente chiama
   │
   ▼
ELEONORA  (AI reception, voce femminile) — risponde SEMPRE, si presenta come AI, instrada:
   ├─ problema tecnico / assistenza ........... → ALBERTO  (AI tecnica)
   ├─ richiesta commerciale ................... → MARIAROSIA (AI commerciale)
   └─ "voglio una persona reale/un operatore" . → transfer umano (+39 378 405 7222)
```

Una sola linea: Eleonora parla per prima, poi la voce cambia via nodo `agent_swap`
(la voce in Retell è a livello di agente).

## 2. ALBERTO — assistenza tecnica (SOLO clienti vinti)

Regola centrale (richiesta dal cliente): **Alberto assiste solo i clienti "vinti"**, cioè
le aziende con una trattativa nello stage **"Chiuso Vinto"** della pipeline
"Pipeline Di Vendita" su HubSpot.

Flusso di Alberto:
1. `verifica_cliente` (POST `/api/voice-ai/verify`) riconosce l'azienda dal **numero**;
   se non basta, chiede il **nome** della struttura e riverifica.
2. Ramificazione **su `is_won_customer` / `eligible_for_support`** (non più solo su
   "cliente trovato"):
   - **cliente VINTO** → assistenza: risolve il problema al telefono con la Knowledge
     Base tecnica, un passo alla volta; **apre il ticket solo se** il problema è
     grosso/complesso o dopo ~5 minuti senza soluzione → poi rimanda all'**area privata**
     Palesya (chat di assistenza dedicata).
   - **contatto NON vinto** (prospect/lead) o **non riconosciuto** → `agent_swap` a
     **Mariarosia** (commerciale).
   - **nome ambiguo** (più strutture) → chiede un dettaglio e riprova una volta.
3. Il numero di cellulare NON viene chiesto: arriva dal caller-ID ed è già nel ticket.

Backend (già su `master`): `service._eligibility` calcola `eligible_for_support = found
AND is_won_customer`; `hubspot.company_has_won_deal` legge i deal associati all'azienda e
verifica lo stage vinto (`_won_deal_stages`). Perché funzioni, **i deal devono essere
associati alle aziende** su HubSpot (fatto per i 7 deal che mappano per nome; i 2 clienti
vinti — Atlantide/Elmas e Piscine Sassari — mappano puliti).

## 3. MARIAROSIA — commerciale

Ascolta l'esigenza, dà info prodotto dalla KB commerciale, registra il lead
(`/api/voice-ai/callback`) e passa a umano se richiesto. Non esegue transazioni.

## 4. Voci (Retell / ElevenLabs, modello `eleven_multilingual_v2`, it-IT)

| Agente     | Ruolo        | Voce attuale        | Note |
|------------|--------------|---------------------|------|
| Eleonora   | reception    | `11labs-Grace` ♀    | invariata |
| **Alberto**| tecnica      | `11labs-Lucas` ♂    | **provvisoria** (vedi sotto) |
| Mariarosia | commerciale  | `11labs-Zuri` ♀     | provvisoria |

L'AI si chiama **solo "Alberto"** (mai altri suffissi): è il nome dell'agente, del saluto e
di tutti i prompt.

**Voci ElevenLabs richieste (da importare):** per Alberto la voce con ID
`gRvYX74pTqIZPvUtIqog` (da **etichettare in Retell semplicemente "Alberto"**), per la
commerciale la voce `uV2Bhcm1HwmAqPqkbjfl`. Queste vivono nella
**Voice Library di ElevenLabs**, non nel set curato di Retell → **non importabili via API**.
Per usarle serve collegare **una volta** la API key ElevenLabs nella dashboard Retell
(Agent → Voice → *Add custom voice → ElevenLabs → Voice ID*) e poi assegnarle agli agenti.
NB: la libreria condivisa di Retell (300 voci) non ha voci con accento nativo italiano.

## 5. Velocità / fluidità

- LLM dei flow: `gpt-4.1-mini` (cascading) — più veloce di `gpt-4.1`.
- TTS: `eleven_flash_v2_5` (modello ElevenLabs a bassa latenza, it-IT) — era
  `eleven_multilingual_v2` (più lento).
- Agenti: `responsiveness = 1.0`, `voice_speed = 1.05`.
- Nodi function di verifica: `speak_during_execution = true` (niente silenzi morti mentre
  interroga il backend).
- **Keep-alive** consigliato: monitor HTTP su `/health` ogni ~5 min (UptimeRobot) per
  evitare il cold-start di Render (piano free) alla prima chiamata.

**Comprensione del parlato (turn-taking & STT):**
- `responsiveness = 0.7` — l'AI aspetta che il chiamante finisca (non taglia la parola,
  es. mentre dice il nome della palestra). NB: alzarlo troppo (1.0) fa partire l'AI troppo
  presto → "non mi fa parlare".
- `boosted_keywords` = nomi reali delle palestre (da HubSpot) + termini Palesya (tornello,
  check-in, abbonamento, planning, socio, ricevuta, backup, area personale, …) → lo STT
  riconosce i nomi propri invece di storpiarli → niente loop sul riconoscimento.
- `interruption_sensitivity = 0.85` (il chiamante può interrompere ed essere ascoltato).
- `stt_mode = "accurate"` — trascrizione più precisa del parlato.
- `denoising_mode = "noise-cancellation"` — cancella il rumore di fondo (palestre rumorose)
  → STT più affidabile.
- `backchannel_frequency = 0.4` (meno "mmh/certo" mentre il cliente parla),
  `reminder_trigger_ms = 10000` (più pazienza sui silenzi).
- KB retrieval: `top_k = 6`, `filter_score = 0.35` (più preciso e veloce di 8/0.3).

## 5b. Come tutto finisce su HubSpot (reporting & gestione)

**Ticket di assistenza** (da Alberto e dal form web) — `service._ticket_properties` +
`hubspot.create_ticket`:
- Subject: `[Categoria] Azienda · Nome — sintesi`.
- Corpo strutturato: Cliente / Segnalato da / Telefono (dal caller-ID, automatico) /
  Email / Origine / Categoria / Gravità / PROBLEMA / Passi tentati.
- Proprietà dedicate (gruppo "Assistenza Palesya"): `support_issue_category`,
  `support_severity`, `support_resolution_status`, `customer_match_status`,
  `voice_ai_call_id`, `hs_ticket_priority`, ecc.
- **Categoria e gravità dedotte dal backend** (`_infer_category`/`_infer_severity`, per
  keyword) — l'AI passa solo la descrizione. Floor di gravità **ALTA** per varchi/controllo
  accessi (`turnstile`/`access_control`): varco bloccato = urgente.
- **Associazioni**: il ticket è legato ad **Azienda** + **Contatto** riconosciuti
  (`_associations`, typeId corretti per ticket/task/call/deal). Idempotente per `call_id`
  (`find_ticket_by_call_id` evita duplicati).
- Nome persona: auto da HubSpot (`get_contact`) quando il cliente è riconosciuto.

**Lead commerciali** (da Mariarosia) → `create_callback` registra un task/callback su
HubSpot, legato all'azienda se riconosciuta (i lead spesso non sono ancora clienti).

**Chiamate** → loggate come oggetto Call nativo, associate ad azienda/contatto.

**Riconoscimento cliente** → `verify_customer`: prima dal Caller-ID (filtro
`phone CONTAINS_TOKEN` sulle varianti E.164), poi dal nome struttura; una singola azienda
riconosciuta dal numero vince (resta "ambiguous" solo se più aziende condividono il numero).

**Pagamenti / finanza** (gestione interna, l'AI NON li legge): gruppo Company
"Pagamenti Clienti" (`pag_*`); **Uscite** su Ticket (`uscita_*`); overview entrate−uscite
via `/api/finance/overview` (admin, header `x-support-admin-secret`).

## 6. Robustezza (audit 2026-08-31)

- Tutti e 3 i flussi: **0 edge rotti**, ogni nodo `function` ha `else_edge` (fallback su
  timeout/errore tool → non si blocca).
- **Fix**: il nodo `human_escalation` (richiesta persona reale) era un vicolo cieco (diceva
  "ti passo un collega" ma NON trasferiva). Collegato `human_escalation → transfer_call`
  (+39 378 405 7222) in tutti e 3 i flussi.
- Backend: **42/42 test verdi**.
- **Prompt Alberto snellito** (14.4k → 5.3k caratteri, −63%): rimossi i riferimenti a 5
  strumenti INESISTENTI (identify_site, get_installation_status, request_support_session,
  send_help_steps, get_ticket_status) e le procedure per-funzione duplicate (già nella KB
  tecnica, che viene recuperata via RAG). Preservate integralmente: regole di sicurezza,
  persona, triage/priorità, regola di verifica, trasferimento, chiusura, parlato/lingua.
  Effetto: meno latenza per turno, meno costo, meno rischio di "strumenti simulati".
  (Le procedure passo-passo restano nella KB `knowledge_base_8608f2e8e5182cdc`.)

## 6. Identificatori Retell

- Agenti: Eleonora `agent_15c1087d7b8fd2873e707574bc`, Alberto
  `agent_d9b29244aa6709da9ca8267f9e`, Mariarosia `agent_193ebc8217fea06005c077b735`.
- Flow: Eleonora `conversation_flow_09f7f12b1310`, Alberto `conversation_flow_9d2dee0ebee6`
  (gli ID nodo interni restano `silvia_*`, cosmetico — non pronunciato), Mariarosia
  `conversation_flow_dfaa7bed7a10`.
- KB: comune `knowledge_base_2d7fb38d70a52ec1`, tecnica `knowledge_base_8608f2e8e5182cdc`,
  commerciale `knowledge_base_cfa5bc282d3d2755`.
- Escalation umana: +39 378 405 7222.
