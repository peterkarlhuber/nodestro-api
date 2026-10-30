# Changelog

## Unveröffentlicht

- Sicherheit: Mit `--host 0.0.0.0` (oder `::`) beantwortete die Studio-API jeden Hostnamen; eine Webseite konnte sie
  dann per DNS-Rebinding lesen und bedienen, die Origin-Prüfung half dagegen nicht (gleicher Origin). Jetzt werden
  dort IP-Adressen, Loopback, der Name dieses Rechners (`name`, `name.local`) und `NODESTRO_ALLOWED_HOSTS`
  beantwortet, fremde Hostnamen erhalten 403 `request.host_not_allowed`. `NODESTRO_ALLOWED_HOSTS=*` nimmt weiter
  ausdrücklich jeden Hostnamen an.
- Sicherheit: Die Studio-API (ohne eigene Schlüssel) beantwortet nur Anfragen an `localhost`, Loopback-Adressen und
  die mit `--host` gewählte Adresse und nimmt Änderungen nur von ihren eigenen Seiten an. Eine Webseite im Browser
  kann sie so weder über DNS-Rebinding lesen und bedienen noch per Cross-Site-Anfrage Workflows anlegen, Läufe
  starten oder Dateien hochladen (Antwort 403 mit `request.host_not_allowed` bzw. `request.origin_not_allowed`).
  `NODESTRO_ALLOWED_HOSTS` nennt weitere Hostnamen (Reverse Proxy).
- Datenschutz: Das Löschen eines Laufs löscht auch die Läufe seiner Unter-Workflows (mit ihren Ergebnisdateien) und
  gibt deren Eingabedateien frei. Bisher blieben diese Kopien unsichtbar in der Datenbank und hielten die
  hochgeladenen Dateien fest.
- Der Nodestro Assistant gibt die Live-Ereignisse einer beendeten Antwort frei (bisher blieben sie für jede Antwort
  im Speicher).
- Pins von `nodestro-runtime` und `nodestro-examples` auf den aktuellen Stand.
- CI: aktuelle Actions (Node 24), Checkout ohne gespeicherte Zugangsdaten.

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
