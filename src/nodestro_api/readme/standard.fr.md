# {title} : API du workflow

Ce projet expose le workflow Nodestro **{title}** sous forme d’API HTTP, indépendamment de Nodestro Studio.
Il a été exporté depuis la publication **v{number}** (version du workflow v{version}, publiée le {published},
empreinte `{digest}`) et contient :

- `workflow/release.json` : le workflow publié, ses sous-workflows et le code de ses outils, figés ;
- `nodestro_runtime/` et `nodestro_api/` : le moteur d’exécution de Nodestro (nodestro-runtime {nodestro}) et
  la couche HTTP des workflows publiés, copiés sans modification (versions, commits sources et sommes de contrôle dans
  `RUNTIME.json`), afin que le workflow s’exécute exactement comme dans le studio : agents en parallèle, décisions, approbations,
  sous-workflows, outils, budgets et fichiers de résultats ;
- `app/` : l’application (`main.py`), sa configuration (`config.py`), les modèles de requête (`schemas.py`)
  et un outil pour créer des clés d’API (`keys.py`) ;
- `tests/` : des tests avec un modèle simulé (sans frais) et `examples/`.{guide}

Rien n’est envoyé à Nodestro. L’API a besoin d’accéder aux fournisseurs d’IA de ses agents ({providers}) et de ses
propres clés d’API pour ses clients.

## Prérequis

- Python {python} (les versions de `requirements.lock` proviennent de Nodestro exécuté sous Python {python})
- Accès à {providers} (voir « Configuration »)

### orkexa (bibliothèque d’agents)

{agent_install}

## Installation

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
# or with uv:  uv sync   (creates uv.lock; needs network access)
cp .env.example .env
```

## Configuration

Tous les paramètres proviennent de variables d’environnement ou de `.env` (voir `app/config.py`) :

| Variable | Signification |
| --- | --- |
{provider_rows}
| `API_KEYS` | Les clés de vos clients (hachages uniquement, voir ci-dessous). Vide : toute requête est refusée. |
| `DATA_DIR` | Emplacement des exécutions, approbations, fichiers et de la mémoire (par défaut `./data`). |
| `API_DOCS` | `local` (par défaut : `/docs` uniquement depuis cet ordinateur), `on` ou `off`. |
| `RUN_RETENTION_DAYS` | Supprime au démarrage les exécutions plus anciennes que ce nombre de jours (0 conserve tout). |
| `TOOL_SANDBOX` | `subprocess` (par défaut) ou `docker`, voir ci-dessous. |
| `OUTPUT_DIR` | Un dossier qui reçoit en plus une copie de chaque fichier de résultat (vide : les fichiers de résultat restent dans `DATA_DIR`). |

`TOOL_SANDBOX=subprocess` n’exécute que les outils écrits par une personne dans l’éditeur d’outils, sous forme de processus de ce serveur.
`TOOL_SANDBOX=docker` exécute chaque outil propre dans un conteneur isolé. Il nécessite Docker et l’image sandbox d’Orkexa
`orkexa-sandbox`, que vous construisez une fois depuis le dépôt d’Orkexa au commit de l’`orkexa` installé
(`docker build -t orkexa-sandbox -f docker/sandbox.Dockerfile .` ; son image de base est épinglée par empreinte et ses
paquets par hachage) et vérifiez avec `python -m orkexa.dynamic.docker --self-test`. Le studio de Nodestro utilise à la place sa propre
image d’exécution, qu’un export ne peut pas construire. Un conteneur n’a pas de réseau, un système de fichiers en lecture seule, aucune
capability et un utilisateur non privilégié ; les outils n’y sont exécutés qu’une fois que le démon Docker a réussi la vérification préalable d’Orkexa et
l’autotest dans le conteneur (contrôlés en arrière-plan au démarrage), et toujours via l’id de l’image validée,
jamais via l’étiquette. Les outils générés par IA ne s’exécutent qu’avec `docker` ; sans lui, ou tant que ce contrôle n’a pas réussi, les exécutions qui
les utilisent sont refusées.

### Clés d’API

```bash
python -m app.keys new --name "My app" --scopes run,read
```

affiche une clé (remettez-la au client, elle n’est affichée qu’une fois) et la ligne pour `API_KEYS`. Autorisations :
`run` (démarrer, annuler, poursuivre), `read` (statut, résultat, événements, fichiers, consommation), `approve` (statuer sur
les approbations). Séparez plusieurs entrées par `;`. Les clés de Nodestro ne font jamais partie d’un export.

### Limites

Les limites de l’API publiée s’appliquent ; vous pouvez les remplacer avec :

| Variable | Valeur exportée |
| --- | --- |
{limits}

`LIMIT_DAILY_BUDGET_USD` bloque les nouvelles exécutions dès que les exécutions des dernières 24 heures ont atteint ce coût ;
`LIMIT_RUN_BUDGET_USD` arrête une exécution individuelle. Un dépassement de limite répond `429` avec un code.

## Démarrage

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

L’API ne répond alors que sur cet ordinateur. Pour servir d’autres ordinateurs, écoutez sur `0.0.0.0` **derrière HTTPS**
(par ex. un proxy inverse, ou `--ssl-certfile`/`--ssl-keyfile`) : les clés ne doivent pas circuler en clair.
Documentation interactive : http://127.0.0.1:8000/docs

Dans un conteneur : le `Dockerfile` fourni construit une image (son image de base Python est fixée par digest ; la
construction a besoin d’un accès réseau pour `requirements.lock`). Montez le dossier de données et, si vous voulez les
fichiers de résultat sur l’hôte, un dossier de résultats. Dans `.env`, définissez `DATA_DIR=/data` et `OUTPUT_DIR=/results`.

```bash
docker build -t workflow-api .
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
  -v "$PWD/data:/data" -v "$PWD/results:/results" workflow-api
```

Sans le second montage, les fichiers copiés dans `OUTPUT_DIR` restent dans le conteneur et disparaissent avec lui.

## Utilisation

Les exécutions sont asynchrones : le démarrage répond `202` avec un `run_id` ; demandez ensuite le statut, suivez les événements
et récupérez le résultat.

```bash
export {key_variable}=ndk_...
{curl_start}
```

Le corps de la requête (`examples/request.json`) :

```json
{request}
```
{upload}
```bash
{curl_status}
{curl_result}
{curl_events}
```

Valeurs de statut : `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`. Renvoyer la
même `Idempotency-Key` renvoie l’exécution déjà démarrée : les nouvelles tentatives ne démarrent donc jamais une exécution deux fois.
Les événements sont des Server-Sent Events ; après une perte de connexion, reconnectez-vous avec `Last-Event-ID` ; le premier événement est
toujours le statut actuel complet. `examples/client.py` fait tout cela en Python.

Tout client peut d'abord demander ce que ce backend propose : `GET /api/app` indique le contrat d'application
(`nodestro-app/1`), l'identifiant du backend et les modes de connexion ; avec une clé, également ce workflow et les
capacités. `BACKEND_NAME` donne au backend un nom que les clients affichent. Le contrat est décrit dans
`docs/app-api/v1.md` de nodestro-api.

## Approbations

Ce workflow comporte {approvals}. Une exécution qui en atteint une se met en pause avec le statut
`awaiting_approval` ; le statut indique ce qu’il faut examiner. Une clé disposant de l’autorisation `approve` statue :

```bash
{curl_approve}
```

`approval_id` peut être omis lorsqu’une seule approbation est en attente. Chaque décision est enregistrée avec l’exécution (qui, quand,
commentaire) ; une seconde décision sur la même approbation répond `409`. Les exécutions en attente survivent aux redémarrages.

## Fichiers de résultats et données

Les fichiers de résultats écrits par le workflow sont listés sous `/runs/{run_id}/files`. Tout est stocké dans
`DATA_DIR/runtime.db` (exécutions avec leurs étapes, consommation et coût, approbations, fichiers, téléversements, mémoire) et
`DATA_DIR/tools`. Connaissances incluses dans l’export : {knowledge_in}. Non incluses (les agents s’exécutent sans
elles) : {knowledge_out}. Mémoire initiale : {memory}. Outils : {tools}.

## Erreurs et coûts

Les erreurs ont la forme `{"error": {"status", "code", "message", "params"}}`. Chaque exécution enregistre les tokens et
le coût estimé (statut et résultat : `usage`) ; `/api/published/{slug}/usage` en fait la somme. Une exécution
interrompue par un redémarrage est marquée `failed` (`run.interrupted`) et peut être poursuivie avec
`POST /runs/{run_id}/resume`.

## Tests

```bash
pip install pytest httpx
pytest
```

Les tests utilisent un modèle simulé : ils ne coûtent rien et vérifient les routes, la validation, les clés, les limites,
l’idempotence, une exécution complète, les approbations au travers d’un redémarrage et que le runtime n’a pas été modifié.
