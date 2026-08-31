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

**Voci ElevenLabs richieste (da importare):** Alberto = "Alberto Loco"
(`gRvYX74pTqIZPvUtIqog`), commerciale = `uV2Bhcm1HwmAqPqkbjfl`. Queste vivono nella
**Voice Library di ElevenLabs**, non nel set curato di Retell → **non importabili via API**.
Per usarle serve collegare **una volta** la API key ElevenLabs nella dashboard Retell
(Agent → Voice → *Add custom voice → ElevenLabs → Voice ID*) e poi assegnarle agli agenti.
NB: la libreria condivisa di Retell (300 voci) non ha voci con accento nativo italiano.

## 5. Velocità / fluidità

- LLM dei flow: `gpt-4.1-mini` (cascading) — più veloce di `gpt-4.1`.
- Agenti: `responsiveness = 1.0`, `voice_speed = 1.05`.
- Nodi function di verifica: `speak_during_execution = true` (niente silenzi morti mentre
  interroga il backend).
- **Keep-alive** consigliato: monitor HTTP su `/health` ogni ~5 min (UptimeRobot) per
  evitare il cold-start di Render (piano free) alla prima chiamata.

## 6. Identificatori Retell

- Agenti: Eleonora `agent_15c1087d7b8fd2873e707574bc`, Alberto
  `agent_d9b29244aa6709da9ca8267f9e`, Mariarosia `agent_193ebc8217fea06005c077b735`.
- Flow: Eleonora `conversation_flow_09f7f12b1310`, Alberto `conversation_flow_9d2dee0ebee6`
  (gli ID nodo interni restano `silvia_*`, cosmetico — non pronunciato), Mariarosia
  `conversation_flow_dfaa7bed7a10`.
- KB: comune `knowledge_base_2d7fb38d70a52ec1`, tecnica `knowledge_base_8608f2e8e5182cdc`,
  commerciale `knowledge_base_cfa5bc282d3d2755`.
- Escalation umana: +39 378 405 7222.
