# {title}: API del workflow

Questo progetto espone il workflow Nodestro **{title}** come API HTTP, indipendentemente da Nodestro Studio.
È stato esportato dalla release **v{number}** (versione del workflow v{version}, pubblicata il {published},
digest `{digest}`) e contiene:

- `workflow/release.json`: il workflow pubblicato, i suoi sotto-workflow e il codice dei suoi strumenti, congelati;
- `nodestro_runtime/` e `nodestro_api/`: il motore di esecuzione di Nodestro (nodestro-runtime {nodestro}) e
  il livello HTTP dei workflow pubblicati, copiati senza modifiche (versioni, commit di origine e checksum in
  `RUNTIME.json`), così il workflow viene eseguito esattamente come nello studio: agenti in parallelo, decisioni, approvazioni,
  sotto-workflow, strumenti, budget e file di risultato;
- `app/`: l'applicazione (`main.py`), la sua configurazione (`config.py`), i modelli delle richieste (`schemas.py`)
  e uno strumento per creare chiavi API (`keys.py`);
- `tests/`: test con un modello simulato (senza costi) ed `examples/`.{guide}

Nulla viene inviato a Nodestro. L'API ha bisogno di accedere ai provider di IA dei suoi agenti ({providers}) e di
chiavi API proprie per i suoi client.

## Requisiti

- Python {python} (le versioni in `requirements.lock` sono state prese da Nodestro in esecuzione con Python {python})
- Accesso a {providers} (vedi "Configurazione")

### orkexa (libreria di agenti)

{agent_install}

## Installazione

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
# or with uv:  uv sync   (creates uv.lock; needs network access)
cp .env.example .env
```

## Configurazione

Tutte le impostazioni provengono da variabili d'ambiente o da `.env` (vedi `app/config.py`):

| Variabile | Significato |
| --- | --- |
{provider_rows}
| `API_KEYS` | Le chiavi dei tuoi client (solo hash, vedi sotto). Vuota: ogni richiesta viene rifiutata. |
| `DATA_DIR` | Dove vengono salvati esecuzioni, approvazioni, file e memoria (predefinito `./data`). |
| `API_DOCS` | `local` (predefinito: `/docs` solo da questo computer), `on` oppure `off`. |
| `RUN_RETENTION_DAYS` | All'avvio elimina le esecuzioni più vecchie di questo numero di giorni (0 conserva tutto). |
| `TOOL_SANDBOX` | `subprocess` (predefinito) oppure `docker`, vedi sotto. |

`TOOL_SANDBOX=subprocess` esegue solo strumenti scritti da una persona nell'editor degli strumenti, come processo di questo server.
`TOOL_SANDBOX=docker` esegue ogni strumento proprio in un container isolato. Richiede Docker e l'immagine sandbox di Orkexa
`orkexa-sandbox`, che costruisci una volta dal repository di Orkexa al commit dell'`orkexa` installato
(`docker build -t orkexa-sandbox -f docker/sandbox.Dockerfile .`; la sua immagine di base è fissata tramite digest e i suoi
pacchetti tramite hash) e verifichi con `python -m orkexa.dynamic.docker --self-test`. Lo studio di Nodestro usa invece la propria
immagine di runtime, che un'esportazione non può costruire. Un container non ha rete, ha un file system di sola lettura, nessuna
capability e un utente non privilegiato; gli strumenti vi vengono eseguiti solo dopo che il daemon Docker ha superato il controllo preliminare di Orkexa e
l'autotest nel container (verificati in background all'avvio), e sempre tramite l'id dell'immagine che li ha superati,
mai tramite il tag. Gli strumenti generati dall'IA vengono eseguiti solo con `docker`; senza, o finché quel controllo non è superato, le esecuzioni che
li usano vengono rifiutate.

### Chiavi API

```bash
python -m app.keys new --name "My app" --scopes run,read
```

stampa una chiave (consegnala al client, viene mostrata una sola volta) e la riga per `API_KEYS`. Permessi:
`run` (avviare, annullare, proseguire), `read` (stato, risultato, eventi, file, consumo), `approve` (decidere le
approvazioni). Separa più voci con `;`. Le chiavi di Nodestro non fanno mai parte di un'esportazione.

### Limiti

Valgono i limiti dell'API pubblicata; puoi sovrascriverli con:

| Variabile | Valore esportato |
| --- | --- |
{limits}

`LIMIT_DAILY_BUDGET_USD` blocca le nuove esecuzioni non appena le esecuzioni delle ultime 24 ore raggiungono quel costo;
`LIMIT_RUN_BUDGET_USD` interrompe una singola esecuzione. I limiti superati rispondono `429` con un codice.

## Avvio

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

L'API risponde quindi solo su questo computer. Per servire altri computer, metti in ascolto su `0.0.0.0` **dietro HTTPS**
(ad es. un reverse proxy, oppure `--ssl-certfile`/`--ssl-keyfile`): le chiavi non devono viaggiare in chiaro.
Documentazione interattiva: http://127.0.0.1:8000/docs

## Utilizzo

Le esecuzioni sono asincrone: l'avvio risponde `202` con un `run_id`; poi chiedi lo stato, segui gli eventi
e recupera il risultato.

```bash
export {key_variable}=ndk_...
{curl_start}
```

Il corpo della richiesta (`examples/request.json`):

```json
{request}
```
{upload}
```bash
{curl_status}
{curl_result}
{curl_events}
```

Valori di stato: `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`. Inviando di nuovo la
stessa `Idempotency-Key` si ottiene l'esecuzione già avviata, così i tentativi ripetuti non avviano mai un'esecuzione due volte.
Gli eventi sono Server-Sent Events; dopo una connessione persa, riconnettiti con `Last-Event-ID`; il primo evento è
sempre lo stato attuale completo. `examples/client.py` fa tutto questo in Python.

## Approvazioni

Questo workflow ha {approvals}. Un'esecuzione che ne raggiunge una si mette in pausa con lo stato
`awaiting_approval`; lo stato elenca cosa esaminare. Decide una chiave con il permesso `approve`:

```bash
{curl_approve}
```

`approval_id` può essere omesso quando è in attesa una sola approvazione. Ogni decisione viene salvata con l'esecuzione (chi, quando,
commento); una seconda decisione sulla stessa approvazione risponde `409`. Le esecuzioni in attesa sopravvivono ai riavvii.

## File di risultato e dati

I file di risultato scritti dal workflow sono elencati in `/runs/{run_id}/files`. Tutto viene salvato in
`DATA_DIR/runtime.db` (esecuzioni con i loro passaggi, consumo e costi, approvazioni, file, caricamenti, memoria) e in
`DATA_DIR/tools`. Conoscenza inclusa nell'esportazione: {knowledge_in}. Non inclusa (gli agenti vengono eseguiti senza):
{knowledge_out}. Memoria iniziale: {memory}. Strumenti: {tools}.

## Errori e costi

Gli errori hanno la forma `{"error": {"status", "code", "message", "params"}}`. Ogni esecuzione registra i token e
il costo stimato (stato e risultato: `usage`); `/api/published/{slug}/usage` li somma. Un'esecuzione
interrotta da un riavvio viene contrassegnata come `failed` (`run.interrupted`) e può essere proseguita con
`POST /runs/{run_id}/resume`.

## Test

```bash
pip install pytest httpx
pytest
```

I test usano un modello simulato: non costano nulla e verificano le route, la validazione, le chiavi, i limiti,
l'idempotenza, un'esecuzione completa, le approvazioni attraverso un riavvio e che il runtime sia invariato.
