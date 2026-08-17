# Integrazione gestionale Palesya → Centralino (Finanza)

Come il gestionale invia **pagamenti clienti (entrate)** e **costi aziendali (uscite)**
al backend, che li scrive su HubSpot e calcola scadenze/stato/riepiloghi.

- **Base URL**: `https://palesya-centralino.onrender.com`
- **Autenticazione (endpoint admin)**: header `x-support-admin-secret: <SEGRETO>`
  (il valore è nel file locale `C:\Users\ltett\support-admin-secret.txt`, da mettere
  come segreto anche nel gestionale — non va scritto nel codice in chiaro).
- Tutti i corpi sono JSON (`Content-Type: application/json`). Importi in euro, IVA esclusa.
  Date in formato `YYYY-MM-DD`.

---

## 1) ENTRATE — pagamento/piano di un cliente
`POST /api/finance/sync`

Identifica il cliente con **uno** tra: `company_id` (HubSpot), `phone`, `company_name`.

| Campo | Obbligatorio | Valori / note |
|---|---|---|
| `company_id` / `phone` / `company_name` | uno dei tre | come riconoscere il cliente |
| `tipo_contratto` | consigliato | `licenza` · `active_12` · `active_24` · `active_36` |
| `stato_pagamento` | consigliato | `pagato` · `parziale` · `insoluto` · `rimborsato` |
| `metodo_pagamento` | consigliato | `fattura` · `bonifico` (default `bonifico` se pagato) |
| `licenza_pagata` | opz. | `true`/`false` |
| `licenza_importo` | opz. | default 1800 se contratto con licenza |
| `active_inizio` | opz. | es. `2026-01-01` (per calcolare scadenza) |
| `cadenza_pagamento` | opz. | `mensile` · `annuale` |
| `ultimo_pagamento_data` / `ultimo_pagamento_importo` | opz. | |
| `incassato_totale` | opz. | totale incassato da quel cliente (fonte gestionale) |
| `interventi_totali` | opz. | default 5 con Active |

Il backend calcola da solo: **scadenza Active** (31/12), **prossimo pagamento**
(data+importo per cadenza), **stato** (attivo/scaduto/in attesa), **interventi residui**.

Esempio:
```json
{
  "company_name": "Mooving",
  "tipo_contratto": "active_12",
  "stato_pagamento": "pagato",
  "metodo_pagamento": "bonifico",
  "active_inizio": "2026-01-01",
  "cadenza_pagamento": "mensile",
  "ultimo_pagamento_data": "2026-08-01",
  "ultimo_pagamento_importo": 70,
  "incassato_totale": 490
}
```
Finisce su HubSpot → azienda → sezione **"Finanza Palesya"**.

---

## 2) USCITE — costo aziendale
`POST /api/finance/expense`

| Campo | Obbligatorio | Valori / note |
|---|---|---|
| `importo` | **sì** | numero ≥ 0 |
| `categoria` | consigliato | `affitto` `stipendi` `fornitori` `software` `utenze` `tasse` `marketing` `hardware` `consulenze` `banca` `trasporti` `altro` |
| `stato` | opz. | `pagata` (default) · `da_pagare` |
| `metodo` | opz. | `bonifico` `fattura` `carta` `contanti` `sepa` `addebito` `altro` |
| `data` | opz. | default oggi |
| `fornitore` | opz. | testo |
| `descrizione` | opz. | note |

Esempio:
```json
{ "importo": 1200, "categoria": "affitto", "metodo": "bonifico",
  "stato": "pagata", "data": "2026-08-01", "fornitore": "Immobiliare X" }
```
Finisce su HubSpot → oggetto **Ticket** → gruppo **"Uscite Palesya"**.

---

## 3) LETTURE / REPORT
- `GET /api/finance/overview` (admin) → **entrate − uscite = utile** + insoluti + scadenze.
- `GET /api/finance/report` (admin) → solo entrate aggregate.
- `GET /api/finance/status?phone=...` (firma voice AI) → posizione di UN cliente (usata dall'agente).

## Note
- Ogni chiamata a `/sync` **aggiorna** (upsert) il cliente: rimandare gli stessi dati è sicuro.
- `/expense` crea un nuovo record per ogni costo.
- Setup proprietà (una tantum, già eseguito): `POST /api/finance/setup` e `POST /api/finance/expenses/setup`.
