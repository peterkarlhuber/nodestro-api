# Herkunft

Quelle: `peterkarlhuber/nodestro` (heute `nodestro-legacy`) am Commit
`15e20768dea496820d4f8a286720176b47817da7` (Merge von PR #14, Version 0.1.0). Die Spalte „Stand“ vergleicht jede
Datei byte-genau mit dem Original; „angepasst“ nennt die geänderten Zeilen (meist umgeschriebene Importe).

3 Dateien unverändert, 47 angepasst, 11 neu.

| Datei | Original | Stand |
| --- | --- | --- |
| `.github/workflows/ci.yml` | — | neu |
| `.gitignore` | — | neu |
| `CHANGELOG.md` | — | neu |
| `LICENSE` | — | neu |
| `README.md` | — | neu |
| `openapi/nodestro-api.json` | — | neu |
| `pyproject.toml` | — | neu |
| `src/nodestro_api/__init__.py` | `backend/src/nodestro/__init__.py` | angepasst (+8 −3 Zeilen) |
| `src/nodestro_api/api.py` | `backend/src/nodestro/api.py` | angepasst (+35 −37 Zeilen) |
| `src/nodestro_api/api_architect.py` | `backend/src/nodestro/api_architect.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_api/api_assistant.py` | `backend/src/nodestro/api_assistant.py` | angepasst (+1 −1 Zeilen) |
| `src/nodestro_api/api_automation.py` | `backend/src/nodestro/api_automation.py` | angepasst (+1 −1 Zeilen) |
| `src/nodestro_api/api_data.py` | `backend/src/nodestro/api_data.py` | angepasst (+1 −1 Zeilen) |
| `src/nodestro_api/api_examples.py` | `backend/src/nodestro/api_examples.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_api/api_guide.py` | `backend/src/nodestro/api_guide.py` | angepasst (+4 −4 Zeilen) |
| `src/nodestro_api/api_inputs.py` | `backend/src/nodestro/api_inputs.py` | angepasst (+10 −10 Zeilen) |
| `src/nodestro_api/api_keys.py` | `backend/src/nodestro/api_keys.py` | angepasst (+3 −3 Zeilen) |
| `src/nodestro_api/api_publish.py` | `backend/src/nodestro/api_publish.py` | angepasst (+5 −5 Zeilen) |
| `src/nodestro_api/api_python.py` | `backend/src/nodestro/api_python.py` | angepasst (+7 −7 Zeilen) |
| `src/nodestro_api/app.py` | `backend/src/nodestro/app.py` | angepasst (+17 −29 Zeilen) |
| `src/nodestro_api/architect.py` | `backend/src/nodestro/architect.py` | angepasst (+26 −26 Zeilen) |
| `src/nodestro_api/assistant.py` | `backend/src/nodestro/assistant.py` | angepasst (+8 −7 Zeilen) |
| `src/nodestro_api/assistant_context.py` | `backend/src/nodestro/assistant_context.py` | angepasst (+10 −11 Zeilen) |
| `src/nodestro_api/assistants.py` | `backend/src/nodestro/assistants.py` | angepasst (+6 −7 Zeilen) |
| `src/nodestro_api/backup.py` | `backend/src/nodestro/backup.py` | angepasst (+3 −3 Zeilen) |
| `src/nodestro_api/batches.py` | `backend/src/nodestro/batches.py` | angepasst (+8 −9 Zeilen) |
| `src/nodestro_api/benchmarks.py` | `backend/src/nodestro/benchmarks.py` | angepasst (+8 −8 Zeilen) |
| `src/nodestro_api/cli.py` | — | neu |
| `src/nodestro_api/context.py` | `backend/src/nodestro/context.py` | angepasst (+56 −286 Zeilen) |
| `src/nodestro_api/diagnostics.py` | `backend/src/nodestro/diagnostics.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_api/docs_index.py` | `backend/src/nodestro/docs_index.py` | angepasst (+4 −7 Zeilen) |
| `src/nodestro_api/exporter.py` | `backend/src/nodestro/exporter.py` | angepasst (+171 −87 Zeilen) |
| `src/nodestro_api/network.py` | `backend/src/nodestro/network.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_api/openapi.py` | — | neu |
| `src/nodestro_api/organize.py` | `backend/src/nodestro/organize.py` | angepasst (+1 −2 Zeilen) |
| `src/nodestro_api/publishing.py` | `backend/src/nodestro/publishing.py` | angepasst (+20 −20 Zeilen) |
| `src/nodestro_api/py.typed` | — | neu |
| `src/nodestro_api/schedules.py` | `backend/src/nodestro/schedules.py` | angepasst (+9 −10 Zeilen) |
| `src/nodestro_api/search.py` | `backend/src/nodestro/search.py` | angepasst (+2 −2 Zeilen) |
| `src/nodestro_api/serving.py` | `backend/src/nodestro/serving.py` | angepasst (+16 −17 Zeilen) |
| `src/nodestro_api/standalone.py` | `backend/src/nodestro/standalone.py` | angepasst (+15 −15 Zeilen) |
| `tests/architect_script.py` | `backend/tests/architect_script.py` | unverändert |
| `tests/conftest.py` | `backend/tests/conftest.py` | angepasst (+19 −215 Zeilen) |
| `tests/test_api.py` | `backend/tests/test_api.py` | angepasst (+11 −17 Zeilen) |
| `tests/test_architect.py` | `backend/tests/test_architect.py` | angepasst (+3 −3 Zeilen) |
| `tests/test_assistant.py` | `backend/tests/test_assistant.py` | angepasst (+4 −86 Zeilen) |
| `tests/test_automation.py` | `backend/tests/test_automation.py` | angepasst (+4 −4 Zeilen) |
| `tests/test_data.py` | `backend/tests/test_data.py` | angepasst (+0 −41 Zeilen) |
| `tests/test_engine.py` | `backend/tests/test_engine.py` | angepasst (+2 −3 Zeilen) |
| `tests/test_export.py` | `backend/tests/test_export.py` | angepasst (+43 −22 Zeilen) |
| `tests/test_features.py` | `backend/tests/test_features.py` | unverändert |
| `tests/test_files.py` | `backend/tests/test_files.py` | angepasst (+22 −14 Zeilen) |
| `tests/test_flow.py` | `backend/tests/test_flow.py` | angepasst (+7 −7 Zeilen) |
| `tests/test_inputs.py` | `backend/tests/test_inputs.py` | angepasst (+8 −9 Zeilen) |
| `tests/test_library.py` | `backend/tests/test_library.py` | angepasst (+13 −329 Zeilen) |
| `tests/test_providers.py` | `backend/tests/test_providers.py` | angepasst (+5 −5 Zeilen) |
| `tests/test_published_api.py` | `backend/tests/test_published_api.py` | angepasst (+3 −3 Zeilen) |
| `tests/test_published_release.py` | `backend/tests/test_published_release.py` | angepasst (+1 −2 Zeilen) |
| `tests/test_python_deps.py` | `backend/tests/test_python_deps.py` | angepasst (+3 −4 Zeilen) |
| `tests/test_runs.py` | `backend/tests/test_runs.py` | unverändert |
| `tests/test_server.py` | — | neu |
