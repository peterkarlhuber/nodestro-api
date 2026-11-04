# {title}: Workflow-API

Dieses Projekt stellt den Nodestro-Workflow **{title}** als HTTP-API bereit, unabhängig von Nodestro Studio.
Es wurde aus der Veröffentlichung **v{number}** exportiert (Workflow-Version v{version}, veröffentlicht am
{published}, Prüfsumme `{digest}`) und enthält:

- `workflow/release.json`: den veröffentlichten Workflow, seine Unterworkflows und den Code seiner Tools,
  unveränderlich;
- `nodestro_runtime/` und `nodestro_api/`: die Ausführungs-Engine von Nodestro (nodestro-runtime {nodestro})
  und die HTTP-Schicht veröffentlichter Workflows, unverändert kopiert (Versionen, Quell-Commits und
  Prüfsummen in `RUNTIME.json`). Der Workflow läuft daher genau wie im Studio: parallele Agenten,
  Entscheidungen, Freigaben, Unterworkflows, Tools, Budgets und Ergebnisdateien;
- `app/`: die Anwendung (`main.py`), ihre Konfiguration (`config.py`), die Anfragemodelle (`schemas.py`)
  und ein Werkzeug für API-Schlüssel (`keys.py`);
- `tests/`: Tests mit einem simulierten Modell (kostenlos) und `examples/`.{guide}

Nichts wird an Nodestro gesendet. Die API braucht Zugang zu den KI-Anbietern ihrer Agenten ({providers})
und eigene API-Schlüssel für ihre Clients.

## Voraussetzungen

- Python {python} (die Versionen in `requirements.lock` stammen aus Nodestro unter Python {python})
- Zugang zu {providers} (siehe „Konfiguration“)

### orkexa (Agenten-Library)

{agent_install}

## Installation

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
# oder mit uv:  uv sync   (erzeugt uv.lock; braucht Netzwerkzugriff)
cp .env.example .env
```

## Konfiguration

Alle Einstellungen kommen aus Umgebungsvariablen oder `.env` (siehe `app/config.py`):

| Variable | Bedeutung |
| --- | --- |
{provider_rows}
| `API_KEYS` | Die Schlüssel deiner Clients (nur Hashes, siehe unten). Leer: Jede Anfrage wird abgelehnt. |
| `DATA_DIR` | Ablage für Läufe, Freigaben, Dateien und Memory (Standard `./data`). |
| `API_DOCS` | `local` (Standard: `/docs` nur von diesem Rechner), `on` oder `off`. |
| `RUN_RETENTION_DAYS` | Läufe, die älter sind, werden beim Start gelöscht (0 behält alles). |
| `TOOL_SANDBOX` | `subprocess` (Standard) oder `docker`, siehe unten. |

`TOOL_SANDBOX=subprocess` führt nur Tools aus, die eine Person im Tool-Editor geschrieben hat, als Prozess
dieses Servers. `TOOL_SANDBOX=docker` führt jedes eigene Tool in einem isolierten Container aus. Dafür braucht es
Docker und Orkexas Sandbox-Image `orkexa-sandbox`, das du einmal aus Orkexas Repository im Commit des installierten
`orkexa` baust (`docker build -t orkexa-sandbox -f docker/sandbox.Dockerfile .`; sein Basis-Image ist per Digest,
seine Pakete per Hash festgelegt) und mit `python -m orkexa.dynamic.docker --self-test` prüfst. Nodestros Studio
nutzt stattdessen sein eigenes Runtime-Image, das ein Export nicht bauen kann. Ein Container hat kein Netzwerk, ein
schreibgeschütztes Dateisystem, keine Capabilities und einen Benutzer ohne Rechte; Tools laufen darin erst, nachdem
der Docker-Daemon Orkexas Vorabprüfung und den Selbsttest im Container bestanden hat (beim Start im Hintergrund
geprüft), und immer über die ID des geprüften Images, nie über den Tag. KI-generierte Tools laufen nur mit
`docker`; ohne, oder solange diese Prüfung nicht bestanden ist, werden Läufe, die sie nutzen, abgelehnt.

### API-Schlüssel

```bash
python -m app.keys new --name "Meine App" --scopes run,read
```

gibt einen Schlüssel aus (für den Client, er wird nur einmal angezeigt) und die Zeile für `API_KEYS`.
Rechte: `run` (starten, abbrechen, fortsetzen), `read` (Status, Ergebnis, Ereignisse, Dateien, Nutzung),
`approve` (Freigaben entscheiden). Mehrere Einträge trennst du mit `;`. Schlüssel aus Nodestro werden nie
exportiert.

### Limits

Es gelten die Limits der veröffentlichten API; du überschreibst sie mit:

| Variable | Exportierter Wert |
| --- | --- |
{limits}

`LIMIT_DAILY_BUDGET_USD` stoppt neue Läufe, sobald die Läufe der letzten 24 Stunden so viel gekostet haben;
`LIMIT_RUN_BUDGET_USD` stoppt einen einzelnen Lauf. Überschrittene Limits antworten mit `429` und einem Code.

## Starten

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Die API antwortet dann nur auf diesem Rechner. Für andere Rechner lauschst du auf `0.0.0.0` **hinter HTTPS**
(z. B. ein Reverse Proxy oder `--ssl-certfile`/`--ssl-keyfile`): Schlüssel dürfen nicht unverschlüsselt
übertragen werden. Interaktive Dokumentation: http://127.0.0.1:8000/docs

## Verwenden

Läufe sind asynchron: Der Start antwortet mit `202` und einer `run_id`; danach fragst du den Status ab,
folgst den Ereignissen und holst das Ergebnis.

```bash
export {key_variable}=ndk_...
{curl_start}
```

Der Anfragekörper (`examples/request.json`):

```json
{request}
```
{upload}
```bash
{curl_status}
{curl_result}
{curl_events}
```

Status: `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`. Sendest du denselben
`Idempotency-Key` erneut, kommt der bereits gestartete Lauf zurück; Wiederholungen starten also nie einen
zweiten Lauf. Die Ereignisse sind Server-Sent Events; nach einem Verbindungsabbruch verbindest du dich mit
`Last-Event-ID` neu, und das erste Ereignis ist immer der vollständige aktuelle Status. `examples/client.py`
zeigt alles in Python.

## Freigaben

Dieser Workflow hat {approvals}. Ein Lauf, der einen erreicht, pausiert mit dem Status
`awaiting_approval`; der Status zeigt, was zu prüfen ist. Ein Schlüssel mit dem Recht `approve` entscheidet:

```bash
{curl_approve}
```

`approval_id` kann entfallen, wenn nur eine Freigabe wartet. Jede Entscheidung wird mit dem Lauf gespeichert
(wer, wann, Kommentar); eine zweite Entscheidung zur selben Freigabe antwortet mit `409`. Wartende Läufe
überstehen Neustarts.

## Ergebnisdateien und Daten

Ergebnisdateien des Workflows stehen unter `/runs/{run_id}/files`. Alles liegt in `DATA_DIR/runtime.db`
(Läufe mit Schritten, Nutzung und Kosten, Freigaben, Dateien, Uploads, Memory) und `DATA_DIR/tools`. Im
Export enthaltenes Wissen: {knowledge_in}. Nicht enthalten (die Agenten laufen ohne): {knowledge_out}.
Start-Memory: {memory}. Tools: {tools}.

## Fehler und Kosten

Fehler haben die Form `{"error": {"status", "code", "message", "params"}}`. Jeder Lauf speichert Tokens und
geschätzte Kosten (Status und Ergebnis: `usage`); `/api/published/{slug}/usage` fasst sie zusammen. Ein
Lauf, der durch einen Neustart unterbrochen wurde, ist `failed` (`run.interrupted`) und lässt sich mit
`POST /runs/{run_id}/resume` fortsetzen.

## Tests

```bash
pip install pytest httpx
pytest
```

Die Tests verwenden ein simuliertes Modell: Sie kosten nichts und prüfen Routen, Validierung, Schlüssel,
Limits, Idempotenz, einen vollständigen Lauf, Freigaben über einen Neustart und die Unverändertheit der
Runtime.
