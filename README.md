# Nodestro API

Die HTTP-API von [Nodestro](https://github.com/peterkarlhuber/nodestro-studio) als eigenes Paket: alles, was die
Oberfläche über `/api` nutzt, die veröffentlichten Workflow-APIs mit Schlüsseln und der Export eines Workflows als
eigenständiges FastAPI-Projekt. Die Workflows selbst führt
[nodestro-runtime](https://github.com/peterkarlhuber/nodestro-runtime) aus.

Teil der Nodestro-Plattform; den Überblick gibt
[PLATTFORM.md](https://github.com/peterkarlhuber/nodestro-studio/blob/master/docs/PLATTFORM.md).

## Inhalt

| Bereich | Module |
| --- | --- |
| Anwendung | `app` (`create_app`), `context` (`AppConfig`, `Nodestro` als Erweiterung des Runtime-`Workspace`), `cli` (`nodestro-api`) |
| Studio-REST-API `/api` | `api`, `api_data`, `api_inputs`, `api_examples`, `api_python`, `api_architect`, `api_assistant`, `api_automation`, `api_guide` |
| Veröffentlichte APIs `/api/published/{slug}` | `publishing`, `serving`, `api_publish`, `api_keys`, `network` (eigener Port fürs lokale Netz) |
| Export | `exporter` (ZIP mit Laufzeit, Lock-Dateien, Tests; Orkexa aus Git oder gebündelt), `standalone` |
| Dienste | `assistants`, `architect`, `assistant`, `assistant_context`, `docs_index`, `batches`, `benchmarks`, `schedules`, `backup`, `organize`, `search`, `diagnostics` |
| Vertrag | `openapi` und `openapi/nodestro-api.json` |

## Installation und Start

```bash
pip install "nodestro-api @ git+https://github.com/peterkarlhuber/nodestro-api.git@<commit>"
nodestro-api                       # http://localhost:8000/api, Referenz unter /api/docs
```

`nodestro-api` hat die Optionen von `nodestro` außer `--no-browser`: `--host`, `--port`, `--data-dir`, `--reload`,
dazu `NODESTRO_HOST`, `NODESTRO_PORT`, `NODESTRO_DATA_DIR` und eine `.env` im aktuellen Ordner. Standard ist
`127.0.0.1:8000` mit dem Datenordner `~/.nodestro`, den sich API und Studio teilen. Die Oberfläche gehört zu
`nodestro-studio`; wer sie mit dieser API ausliefern will, setzt `NODESTRO_STATIC_DIR` (und für den Nodestro
Assistant `NODESTRO_DOCS_DIR`).

In Python:

```python
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig

app = create_app(AppConfig.from_env())   # eine FastAPI-App, z. B. für uvicorn
```

Integrationen (z. B. die OCR aus `nodestro-integrations`) werden über ihren Einstiegspunkt gefunden, sobald sie
installiert sind; `create_app(..., integrations=[...])` gibt sie ausdrücklich vor.

## Der Vertrag

`openapi/nodestro-api.json` ist das OpenAPI-Dokument genau dieser API. Ein Test schlägt fehl, solange die Datei nicht
zum Code passt; nach einer Änderung an Routen oder Schemas schreibt `python -m nodestro_api.openapi
openapi/nodestro-api.json` sie neu. Die SDKs in [nodestro-sdk](https://github.com/peterkarlhuber/nodestro-sdk) werden
daraus generiert.

## Entwicklung

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

Die Tests laufen gegen die echte App mit simulierten Anbieter-Clients (`nodestro_runtime.testing`), nie gegen einen
bezahlten Anbieter. Sie decken die REST-API, veröffentlichte APIs, Schlüssel, Freigaben, Fortsetzen, Stapel,
Zeitpläne, Dateien, Python-Bibliotheken und den Export ab; der exportierte Workflow besteht im Test seine eigenen
Tests. `NODESTRO_NETWORK_TESTS=1` schaltet den Test frei, der ein echtes Paket von PyPI installiert.

Die CI braucht das Repository-Secret `NODESTRO_READ` (Lesezugriff auf orkexa und die nodestro-Repositories).

## Herkunft und Lizenz

Aus `nodestro` 0.1 (15e2076) herausgelöst, siehe [PROVENANCE.md](PROVENANCE.md) und [CHANGELOG.md](CHANGELOG.md).
MIT, siehe [LICENSE](LICENSE).
