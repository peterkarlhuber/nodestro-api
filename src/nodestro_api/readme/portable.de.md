# {title}

Dieser Ordner ist der Nodestro-Workflow **{title}** als startfertige Anwendung für **{platform}**. Er bringt
alles mit, was er braucht: ein eigenes Python, alle Bibliotheken, den Workflow (Release **v{number}**,
Workflow-Version v{version}, veröffentlicht am {published}, Digest `{digest}`){local_models_line} und, wenn er so
exportiert wurde, eine Weboberfläche. Installiert werden muss nichts: kein Python, kein pip, kein Node.js und kein
Nodestro Studio.

## Starten

1. Entpacke die ZIP-Datei in einen Ordner, in den du schreiben darfst (zum Beispiel deinen Dokumente-Ordner). Unter
   Windows sollte der Pfad nicht zu lang sein; `C:\Apps\` funktioniert gut.
2. Starte die Anwendung:
   - **Windows:** Doppelklick auf `start.cmd`.
   - **macOS:** Doppelklick auf `Start.command` (beim ersten Mal: Rechtsklick, „Öffnen“, weil die Datei
     heruntergeladen wurde).
   - **Linux:** `./start.sh` in einem Terminal ausführen.
3. Der erste Start prüft die Dateien{models_check}. Sobald sie bereit ist, zeigt das Fenster die Adresse
   (`http://127.0.0.1:{port}`) und öffnet den Browser{ui_line}.

Beenden mit **Strg+C** im Fenster der Anwendung oder mit `stop.cmd` / `./stop.sh`. Beim nächsten Start sind alle
Läufe, Freigaben, Dateien und Einstellungen noch da.

| Befehl (`start.cmd …` unter Windows, sonst `./start.sh …`) | Was er tut |
| --- | --- |
| `start` (oder nichts) | Startet die Anwendung. |
| `stop` | Beendet eine laufende Instanz. |
| `status` | Sagt, ob und wo sie läuft. |
| `open` | Öffnet den Browser mit einem neuen Anmeldelink (nur auf diesem Computer). |
| `configure` | Trägt die Schlüssel der KI-Anbieter ein (die Eingabe ist verborgen). |
| `keys new --name "Meine App" --scopes run,read` | Erstellt einen API-Schlüssel für ein anderes Programm (`keys list`, `keys revoke <id>`). |
| `check` | Ein Selbsttest ohne Start (`check --full` prüft zusätzlich jede Modelldatei per SHA-256). |

## KI-Anbieter und Schlüssel

{providers_section}

Schlüssel sind nie Teil dieses Pakets. Trag sie im Browser ein (Einstellungen, nur für den Betreiber dieses
Computers) oder mit `configure` im Terminal; beides speichert sie in `data/secrets/providers.json`, lesbar nur für
dein Benutzerkonto. Die Datei ist nicht verschlüsselt (ein Schlüssel daneben würde nichts schützen): Schütze sie wie
einen SSH-Schlüssel, oder setze die Schlüssel stattdessen als Umgebungsvariablen aus einem Secret-Store.
Umgebungsvariablen haben immer Vorrang. Ein geänderter Schlüssel gilt ab dem nächsten Lauf; ein neuer Export ist
nicht nötig.

## Lokale Modelle

{models_section}

## Netzwerk und Offline-Betrieb

{network_section}

## Ergebnisdateien

{output_section}

Jeder Lauf behält seine Dateien außerdem in der Anwendung (Download im Browser oder über
`GET /runs/{run_id}/files/{file_id}`). Der Browser kann keine Ordner auf diesem Computer wählen: Den Ordner legt der
Betreiber in `config/settings.toml` fest (`[data] output_dir`, relativ zu diesem Ordner oder absolut). Dateinamen
werden für jedes Betriebssystem sicher gemacht; ein bereits vorhandener Name bekommt ` (2)`, ` (3)` …

## Einstellungen

`config/settings.toml` enthält alle Einstellungen mit Erklärungen (Port, Adresse, Ordner, Limits, Modellspeicher,
Offline-Modus, Sprache der Oberfläche). Änderungen gelten ab dem nächsten Start. Umgebungsvariablen haben Vorrang
vor der Datei (zum Beispiel `PORT`, `DATA_DIR`, `OUTPUT_DIR`).

Standardmäßig antwortet die Anwendung **nur diesem Computer** (`127.0.0.1`). Um andere Computer zu bedienen, setze
`host = "0.0.0.0"` **und** HTTPS (`tls_cert`/`tls_key`, oder `behind_proxy = true` hinter einem Reverse Proxy mit
HTTPS); ohne HTTPS startet sie so nicht. Andere melden sich mit einem API-Schlüssel an; ohne Schlüssel kommt niemand
hinein.

## Programmierschnittstelle (API)

Andere Programme nutzen denselben Workflow über die HTTP-API mit eigenem Schlüssel (siehe `keys new`):

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

Läufe sind asynchron: Der Start antwortet mit `202` und einer `run_id`. Status: `queued`, `running`,
`awaiting_approval`, `succeeded`, `failed`, `cancelled`. Die Ereignisse sind Server-Sent Events; nach einem
Verbindungsabbruch verbindest du dich mit `Last-Event-ID` neu. Dieser Workflow hat {approvals}; ein Schlüssel mit
dem Recht `approve` entscheidet:

```bash
{curl_approve}
```

Fehler haben die Form `{"error": {"status", "code", "message", "params"}}`. Die vollständige Beschreibung ist
`openapi.json` in diesem Ordner (OpenAPI 3); die interaktive Referenz ist `http://127.0.0.1:{port}/docs`
(standardmäßig nur von diesem Computer aus). `examples/client.py` zeigt alles in Python.

## Sicherheit

- Kein Schlüssel von Nodestro oder eines KI-Anbieters ist in diesem Paket, und der Browser erhält nie einen
  Anbieterschlüssel.
- Die Weboberfläche nutzt ein Sitzungs-Cookie (HttpOnly, SameSite=Strict); der Betreiber meldet sich mit einem
  einmaligen Link aus dem Startfenster an, alle anderen mit einem API-Schlüssel.
- Tools, die ein Mensch geschrieben hat, laufen als eigene Prozesse. KI-generierte Tools laufen nur in einem
  isolierten Docker-Container (`[tools] sandbox = "docker"`, Docker nötig); ohne Docker werden Läufe, die sie
  brauchen, abgelehnt, und nichts läuft ungeschützt.
- Der lokale Modellserver lauscht nur auf `127.0.0.1`, auf einem eigenen Port.

## Fehlerbehebung

| Problem | Was tun |
| --- | --- |
| Der Port ist belegt | Einen anderen `port` in `config/settings.toml` setzen oder die andere Instanz beenden (`stop`). |
| „Es fehlen Schlüssel“ in der Oberfläche | Unter Einstellungen eintragen oder `configure` ausführen. |
| Ein Lauf scheitert sofort | Den Lauf in der Oberfläche öffnen; seine Schritte zeigen den Grund. `logs/application.log` hat Details. |
| Sehr langsame Antworten | Lokale Modelle ohne Grafikkarte sind langsam; unter „Lokale Modelle“ steht, wie viel Speicher jedes braucht. |
| macOS: „kann nicht geöffnet werden“ | Rechtsklick auf `Start.command`, „Öffnen“; oder in diesem Ordner `xattr -dr com.apple.quarantine .` ausführen. |
| Windows SmartScreen | „Weitere Informationen“, „Trotzdem ausführen“ (die Skripte sind lesbarer Text). |
| Etwas anderes | `check` prüft das Paket; `logs/` enthält die Protokolle der Anwendung und des Modellservers. |

## Was in diesem Ordner liegt

`python/` (Python {python_version}), `nodestro_runtime/`, `nodestro_api/` und `app/` (die Anwendung), `workflow/`
(das eingefrorene Release), `config/`, `examples/`, {models_files}`licenses/` (die Lizenzen von allem Enthaltenen),
`lib.lock` (jede Bibliothek mit ihrem SHA-256), `SHA256SUMS` (eine Prüfsumme jeder Datei), `PORTABLE.json` (was
dieses Paket ist) und `openapi.json`. Deine Daten liegen in `data/` und `logs/`; sichere sie, um Läufe und
Einstellungen zu behalten.
