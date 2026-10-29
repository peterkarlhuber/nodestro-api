# Changelog

## 0.2.0 (2026-10-29)

- Erste Version als eigenes Repository: aus `nodestro` 0.1.0 (Commit 15e2076, heute `nodestro-legacy`)
  herausgelöst, ohne neue Funktionen und ohne Verhaltensänderung. Herkunft jeder Datei: PROVENANCE.md.
- Abhängigkeiten auf exakte Commits gepinnt (orkexa `cbf6df3d6f727b96924f1ae66e60b469eeef58e6`); die getestete
  Kombination steht in `nodestro-studio/platform/versions.json`.
- CI mit dem Repository-Secret `NODESTRO_READ`.
- `Nodestro` erweitert jetzt den `Workspace` der Runtime. Die API liefert keine Oberfläche mehr aus (das macht
  `nodestro-studio`); neu sind der Befehl `nodestro-api` und der OpenAPI-Vertrag `openapi/nodestro-api.json`.
- Export: Die Laufzeit liegt im exportierten Projekt als `nodestro_runtime/` und `nodestro_api/`; `RUNTIME.json` nennt
  die Versionen und Commits.
