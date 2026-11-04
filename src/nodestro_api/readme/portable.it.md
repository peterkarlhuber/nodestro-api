# {title}

Questa cartella contiene il workflow Nodestro **{title}** come applicazione pronta all'uso per **{platform}**. Include
tutto ciò che serve: il proprio Python, tutte le librerie, il workflow (release **v{number}**, versione del workflow
v{version}, pubblicata il {published}, digest `{digest}`){local_models_line} e, se è stata esportata con essa, un'interfaccia
web. Non serve installare nulla: né Python, né pip, né Node.js, né Nodestro Studio.

## Avvio

1. Estrai il file ZIP in una cartella in cui puoi scrivere (ad esempio la cartella Documenti). Su Windows il percorso non dovrebbe
   essere troppo lungo; `C:\Apps\` va bene.
2. Avvia l'applicazione:
   - **Windows:** fai doppio clic su `start.cmd`.
   - **macOS:** fai doppio clic su `Start.command` (la prima volta: clic destro, "Apri", perché il file è stato scaricato).
   - **Linux:** esegui `./start.sh` in un terminale.
3. Il primo avvio verifica i file{models_check}. Quando è pronta, la finestra mostra l'indirizzo
   (`http://127.0.0.1:{port}`) e apre il browser{ui_line}.

Arresta l'applicazione con **Ctrl+C** nella sua finestra, oppure con `stop.cmd` / `./stop.sh`. Riavviandola si conservano
tutte le esecuzioni, approvazioni, file e impostazioni.

| Comando (`start.cmd …` su Windows, `./start.sh …` altrove) | Cosa fa |
| --- | --- |
| `start` (o niente) | Avvia l'applicazione. |
| `stop` | Arresta un'istanza in esecuzione. |
| `status` | Indica se è in esecuzione, e dove. |
| `open` | Apre il browser con un nuovo link di accesso (solo su questo computer). |
| `configure` | Inserisce le chiavi dei provider di IA (l'input è nascosto). |
| `keys new --name "My app" --scopes run,read` | Crea una chiave API per un altro programma (`keys list`, `keys revoke <id>`). |
| `check` | Un autotest senza avvio (`check --full` verifica anche ogni file di modello tramite SHA-256). |

## Provider di IA e chiavi

{providers_section}

Le chiavi non fanno mai parte di questo pacchetto. Inseriscile nel browser (Impostazioni, solo per l'operatore di questo computer)
oppure con `configure` in un terminale; entrambi le salvano in `data/secrets/providers.json`, leggibile solo dal tuo account
utente. Il file non è cifrato (una chiave di cifratura accanto a esso non proteggerebbe nulla): proteggilo come una chiave
SSH, oppure imposta le chiavi come variabili d'ambiente da un archivio di segreti. Le variabili d'ambiente hanno sempre la precedenza.
Una chiave modificata vale dall'esecuzione successiva; non serve una nuova esportazione.

## Modelli locali

{models_section}

## Rete e funzionamento offline

{network_section}

## File di risultato

{output_section}

Ogni esecuzione conserva anche i propri file nell'applicazione (scaricali nel browser o con
`GET /runs/{run_id}/files/{file_id}`). Il browser non può scegliere cartelle su questo computer: la cartella viene impostata
dall'operatore in `config/settings.toml` (`[data] output_dir`, relativa a questa cartella o assoluta). I nomi dei file vengono resi
validi per ogni sistema; un nome già esistente riceve ` (2)`, ` (3)` …

## Impostazioni

`config/settings.toml` contiene tutte le impostazioni con spiegazioni (porta, indirizzo, cartelle, limiti, memoria dei modelli,
modalità offline, lingua dell'interfaccia). Le modifiche valgono dall'avvio successivo. Le variabili d'ambiente prevalgono sul file (ad
esempio `PORT`, `DATA_DIR`, `OUTPUT_DIR`).

Per impostazione predefinita l'applicazione risponde **solo a questo computer** (`127.0.0.1`). Per servire altri computer imposta
`host = "0.0.0.0"` **e** HTTPS (`tls_cert`/`tls_key`, oppure `behind_proxy = true` dietro un reverse proxy con
HTTPS); senza HTTPS si rifiuta di avviarsi in quel modo. Le altre persone accedono con una chiave API; nessuno può accedere
senza.

## Interfaccia di programmazione (API)

Altri programmi usano lo stesso workflow tramite l'API HTTP con la propria chiave (vedi `keys new`):

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

Le esecuzioni sono asincrone: l'avvio risponde `202` con un `run_id`. Valori di stato: `queued`, `running`,
`awaiting_approval`, `succeeded`, `failed`, `cancelled`. Gli eventi sono Server-Sent Events; dopo una connessione persa,
riconnettiti con `Last-Event-ID`. Questo workflow ha {approvals}; decide una chiave con il permesso `approve`:

```bash
{curl_approve}
```

Gli errori hanno la forma `{"error": {"status", "code", "message", "params"}}`. La descrizione completa è
`openapi.json` in questa cartella (OpenAPI 3); il riferimento interattivo è `http://127.0.0.1:{port}/docs` (per impostazione predefinita solo da
questo computer). `examples/client.py` mostra tutto in Python.

## Sicurezza

- Questo pacchetto non contiene alcuna chiave di Nodestro o di un provider di IA, e il browser non riceve mai una chiave di provider.
- L'interfaccia web usa un cookie di sessione (HttpOnly, SameSite=Strict); l'operatore accede con un link monouso
  dalla finestra di avvio, tutti gli altri con una chiave API.
- Gli strumenti scritti da una persona vengono eseguiti come processi separati. Gli strumenti generati dall'IA vengono eseguiti solo in un container
  Docker isolato (`[tools] sandbox = "docker"`, Docker necessario); senza Docker le esecuzioni che li richiedono vengono rifiutate e
  nulla viene eseguito senza protezione.
- Il server dei modelli locale è in ascolto solo su `127.0.0.1`, su una porta dedicata.

## Risoluzione dei problemi

| Problema | Cosa fare |
| --- | --- |
| La porta è in uso | Imposta un'altra `port` in `config/settings.toml`, oppure arresta l'altra istanza (`stop`). |
| "Chiavi mancanti" nell'interfaccia | Inseriscile in Impostazioni, oppure esegui `configure`. |
| Un'esecuzione fallisce subito | Apri l'esecuzione nell'interfaccia; i suoi passaggi mostrano il motivo. `logs/application.log` contiene i dettagli. |
| Risposte molto lente | I modelli locali senza scheda grafica sono lenti; vedi "Modelli locali" per la memoria che ciascuno richiede. |
| macOS: "non può essere aperto" | Clic destro su `Start.command`, "Apri"; oppure esegui `xattr -dr com.apple.quarantine .` in questa cartella. |
| Windows SmartScreen | "Ulteriori informazioni", "Esegui comunque" (gli script sono testo semplice che puoi leggere). |
| Altro | `check` verifica il pacchetto; `logs/` contiene i log dell'applicazione e del server dei modelli. |

## Contenuto di questa cartella

`python/` (Python {python_version}), `nodestro_runtime/`, `nodestro_api/` e `app/` (l'applicazione),
`workflow/` (la release congelata), `config/`, `examples/`, {models_files}`licenses/` (licenze di tutto ciò che è
incluso), `lib.lock` (ogni libreria con il suo SHA-256), `SHA256SUMS` (un checksum di ogni file), `PORTABLE.json`
(cos'è questo pacchetto) e `openapi.json`. I tuoi dati si trovano in `data/` e `logs/`; fanne un backup per conservare esecuzioni e
impostazioni.
