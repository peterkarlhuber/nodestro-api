# Provenance

Source: `peterkarlhuber/nodestro` (now `nodestro-legacy`) at commit
`15e20768dea496820d4f8a286720176b47817da7` (merge of PR #14, version 0.1.0). The column "State" compares each
file byte by byte with the original at the time of the extraction (version 0.2.0); "adapted" names the changed
lines (mostly rewritten imports). Changes after 0.2.0 are recorded in the Git history and in CHANGELOG.md.

3 files unchanged, 47 adapted, 11 new.

| File | Original | State |
| --- | --- | --- |
| `.github/workflows/ci.yml` | — | new |
| `.gitignore` | — | new |
| `CHANGELOG.md` | — | new |
| `LICENSE` | — | new |
| `README.md` | — | new |
| `openapi/nodestro-api.json` | — | new |
| `pyproject.toml` | — | new |
| `src/nodestro_api/__init__.py` | `backend/src/nodestro/__init__.py` | adapted (+8 −3 lines) |
| `src/nodestro_api/api.py` | `backend/src/nodestro/api.py` | adapted (+35 −37 lines) |
| `src/nodestro_api/api_architect.py` | `backend/src/nodestro/api_architect.py` | adapted (+2 −2 lines) |
| `src/nodestro_api/api_assistant.py` | `backend/src/nodestro/api_assistant.py` | adapted (+1 −1 lines) |
| `src/nodestro_api/api_automation.py` | `backend/src/nodestro/api_automation.py` | adapted (+1 −1 lines) |
| `src/nodestro_api/api_data.py` | `backend/src/nodestro/api_data.py` | adapted (+1 −1 lines) |
| `src/nodestro_api/api_examples.py` | `backend/src/nodestro/api_examples.py` | adapted (+2 −2 lines) |
| `src/nodestro_api/api_guide.py` | `backend/src/nodestro/api_guide.py` | adapted (+4 −4 lines) |
| `src/nodestro_api/api_inputs.py` | `backend/src/nodestro/api_inputs.py` | adapted (+10 −10 lines) |
| `src/nodestro_api/api_keys.py` | `backend/src/nodestro/api_keys.py` | adapted (+3 −3 lines) |
| `src/nodestro_api/api_publish.py` | `backend/src/nodestro/api_publish.py` | adapted (+5 −5 lines) |
| `src/nodestro_api/api_python.py` | `backend/src/nodestro/api_python.py` | adapted (+7 −7 lines) |
| `src/nodestro_api/app.py` | `backend/src/nodestro/app.py` | adapted (+17 −29 lines) |
| `src/nodestro_api/architect.py` | `backend/src/nodestro/architect.py` | adapted (+26 −26 lines) |
| `src/nodestro_api/assistant.py` | `backend/src/nodestro/assistant.py` | adapted (+8 −7 lines) |
| `src/nodestro_api/assistant_context.py` | `backend/src/nodestro/assistant_context.py` | adapted (+10 −11 lines) |
| `src/nodestro_api/assistants.py` | `backend/src/nodestro/assistants.py` | adapted (+6 −7 lines) |
| `src/nodestro_api/backup.py` | `backend/src/nodestro/backup.py` | adapted (+3 −3 lines) |
| `src/nodestro_api/batches.py` | `backend/src/nodestro/batches.py` | adapted (+8 −9 lines) |
| `src/nodestro_api/benchmarks.py` | `backend/src/nodestro/benchmarks.py` | adapted (+8 −8 lines) |
| `src/nodestro_api/cli.py` | — | new |
| `src/nodestro_api/context.py` | `backend/src/nodestro/context.py` | adapted (+56 −286 lines) |
| `src/nodestro_api/diagnostics.py` | `backend/src/nodestro/diagnostics.py` | adapted (+2 −2 lines) |
| `src/nodestro_api/docs_index.py` | `backend/src/nodestro/docs_index.py` | adapted (+4 −7 lines) |
| `src/nodestro_api/exporter.py` | `backend/src/nodestro/exporter.py` | adapted (+171 −87 lines) |
| `src/nodestro_api/network.py` | `backend/src/nodestro/network.py` | adapted (+2 −2 lines) |
| `src/nodestro_api/openapi.py` | — | new |
| `src/nodestro_api/organize.py` | `backend/src/nodestro/organize.py` | adapted (+1 −2 lines) |
| `src/nodestro_api/publishing.py` | `backend/src/nodestro/publishing.py` | adapted (+20 −20 lines) |
| `src/nodestro_api/py.typed` | — | new |
| `src/nodestro_api/schedules.py` | `backend/src/nodestro/schedules.py` | adapted (+9 −10 lines) |
| `src/nodestro_api/search.py` | `backend/src/nodestro/search.py` | adapted (+2 −2 lines) |
| `src/nodestro_api/serving.py` | `backend/src/nodestro/serving.py` | adapted (+16 −17 lines) |
| `src/nodestro_api/standalone.py` | `backend/src/nodestro/standalone.py` | adapted (+15 −15 lines) |
| `tests/architect_script.py` | `backend/tests/architect_script.py` | unchanged |
| `tests/conftest.py` | `backend/tests/conftest.py` | adapted (+19 −215 lines) |
| `tests/test_api.py` | `backend/tests/test_api.py` | adapted (+11 −17 lines) |
| `tests/test_architect.py` | `backend/tests/test_architect.py` | adapted (+3 −3 lines) |
| `tests/test_assistant.py` | `backend/tests/test_assistant.py` | adapted (+4 −86 lines) |
| `tests/test_automation.py` | `backend/tests/test_automation.py` | adapted (+4 −4 lines) |
| `tests/test_data.py` | `backend/tests/test_data.py` | adapted (+0 −41 lines) |
| `tests/test_engine.py` | `backend/tests/test_engine.py` | adapted (+2 −3 lines) |
| `tests/test_export.py` | `backend/tests/test_export.py` | adapted (+43 −22 lines) |
| `tests/test_features.py` | `backend/tests/test_features.py` | unchanged |
| `tests/test_files.py` | `backend/tests/test_files.py` | adapted (+22 −14 lines) |
| `tests/test_flow.py` | `backend/tests/test_flow.py` | adapted (+7 −7 lines) |
| `tests/test_inputs.py` | `backend/tests/test_inputs.py` | adapted (+8 −9 lines) |
| `tests/test_library.py` | `backend/tests/test_library.py` | adapted (+13 −329 lines) |
| `tests/test_providers.py` | `backend/tests/test_providers.py` | adapted (+5 −5 lines) |
| `tests/test_published_api.py` | `backend/tests/test_published_api.py` | adapted (+3 −3 lines) |
| `tests/test_published_release.py` | `backend/tests/test_published_release.py` | adapted (+1 −2 lines) |
| `tests/test_python_deps.py` | `backend/tests/test_python_deps.py` | adapted (+3 −4 lines) |
| `tests/test_runs.py` | `backend/tests/test_runs.py` | unchanged |
| `tests/test_server.py` | — | new |
