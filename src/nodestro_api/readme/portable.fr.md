# {title}

Ce dossier contient le workflow Nodestro **{title}** sous forme d’application prête à l’emploi pour **{platform}**. Il apporte
tout ce dont il a besoin : son propre Python, toutes les bibliothèques, le workflow (publication **v{number}**, version du workflow
v{version}, publiée le {published}, empreinte `{digest}`){local_models_line} et, s’il a été exporté avec, une
interface web. Rien n’est à installer : ni Python, ni pip, ni Node.js, ni Nodestro Studio.

## Démarrage

1. Décompressez le fichier ZIP dans un dossier où vous pouvez écrire (par exemple votre dossier Documents). Sous Windows, le chemin ne doit pas
   être trop long ; `C:\Apps\` convient bien.
2. Démarrez l’application :
   - **Windows :** double-cliquez sur `start.cmd`.
   - **macOS :** double-cliquez sur `Start.command` (la première fois : clic droit, « Ouvrir », car le fichier a été téléchargé).
   - **Linux :** exécutez `./start.sh` dans un terminal.
3. Le premier démarrage vérifie les fichiers{models_check}. Une fois prête, la fenêtre affiche l’adresse
   (`http://127.0.0.1:{port}`) et ouvre le navigateur{ui_line}.

Arrêtez l’application avec **Ctrl+C** dans sa fenêtre, ou avec `stop.cmd` / `./stop.sh`. Un nouveau démarrage conserve
toutes les exécutions, approbations, fichiers et paramètres.

| Commande (`start.cmd …` sous Windows, `./start.sh …` ailleurs) | Effet |
| --- | --- |
| `start` (ou rien) | Démarre l’application. |
| `stop` | Arrête une instance en cours d’exécution. |
| `status` | Indique si elle s’exécute, et où. |
| `open` | Ouvre le navigateur avec un nouveau lien de connexion (uniquement sur cet ordinateur). |
| `configure` | Saisit les clés des fournisseurs d’IA (la saisie est masquée). |
| `keys new --name "My app" --scopes run,read` | Crée une clé d’API pour un autre programme (`keys list`, `keys revoke <id>`). |
| `check` | Un autotest sans démarrage (`check --full` vérifie aussi chaque fichier de modèle par SHA-256). |

## Fournisseurs d’IA et clés

{providers_section}

Les clés ne font jamais partie de ce paquet. Saisissez-les dans le navigateur (Paramètres, réservé à l’opérateur de cet ordinateur)
ou avec `configure` dans un terminal ; les deux les enregistrent dans `data/secrets/providers.json`, lisible uniquement par votre compte
utilisateur. Le fichier n’est pas chiffré (une clé de chiffrement placée à côté ne protégerait rien) : protégez-le comme une clé
SSH, ou définissez plutôt les clés comme variables d’environnement depuis un coffre de secrets. Les variables d’environnement l’emportent toujours.
Une clé modifiée s’applique à l’exécution suivante ; aucun nouvel export n’est nécessaire.

## Modèles locaux

{models_section}

## Réseau et fonctionnement hors ligne

{network_section}

## Fichiers de résultats

{output_section}

Chaque exécution conserve aussi ses fichiers dans l’application (téléchargez-les dans le navigateur ou avec
`GET /runs/{run_id}/files/{file_id}`). Le navigateur ne peut pas choisir de dossiers sur cet ordinateur : le dossier est défini par
l’opérateur dans `config/settings.toml` (`[data] output_dir`, relatif à ce dossier ou absolu). Les noms de fichiers sont rendus
compatibles avec tous les systèmes ; un nom qui existe déjà reçoit ` (2)`, ` (3)` …

## Paramètres

`config/settings.toml` contient tous les paramètres avec des explications (port, adresse, dossiers, limites, mémoire des modèles,
mode hors ligne, langue de l’interface). Les modifications s’appliquent au démarrage suivant. Les variables d’environnement priment sur le fichier (par
exemple `PORT`, `DATA_DIR`, `OUTPUT_DIR`).

Par défaut, l’application ne répond **qu’à cet ordinateur** (`127.0.0.1`). Pour servir d’autres ordinateurs, définissez
`host = "0.0.0.0"` **et** HTTPS (`tls_cert`/`tls_key`, ou `behind_proxy = true` derrière un proxy inverse avec
HTTPS) ; sans HTTPS, elle refuse de démarrer ainsi. Les autres personnes se connectent avec une clé d’API ; personne ne peut se connecter
sans clé.

## Interface de programmation (API)

D’autres programmes utilisent le même workflow via l’API HTTP avec leur propre clé (voir `keys new`) :

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

Les exécutions sont asynchrones : le démarrage répond `202` avec un `run_id`. Valeurs de statut : `queued`, `running`,
`awaiting_approval`, `succeeded`, `failed`, `cancelled`. Les événements sont des Server-Sent Events ; après une perte de connexion,
reconnectez-vous avec `Last-Event-ID`. Ce workflow comporte {approvals} ; une clé disposant de l’autorisation `approve` statue :

```bash
{curl_approve}
```

Les erreurs ont la forme `{"error": {"status", "code", "message", "params"}}`. La description complète est
`openapi.json` dans ce dossier (OpenAPI 3) ; la référence interactive est `http://127.0.0.1:{port}/docs` (par défaut uniquement depuis
cet ordinateur). `examples/client.py` montre tout cela en Python.

## Sécurité

- Ce paquet ne contient aucune clé de Nodestro ni d’un fournisseur d’IA, et le navigateur ne reçoit jamais de clé de fournisseur.
- L’interface web utilise un cookie de session (HttpOnly, SameSite=Strict) ; l’opérateur se connecte avec un lien à usage unique
  issu de la fenêtre de démarrage, toutes les autres personnes avec une clé d’API.
- Les outils écrits par une personne s’exécutent dans des processus séparés. Les outils générés par IA ne s’exécutent que dans un conteneur
  Docker isolé (`[tools] sandbox = "docker"`, Docker requis) ; sans Docker, les exécutions qui en ont besoin sont refusées et
  rien ne s’exécute sans protection.
- Le serveur de modèles local écoute uniquement sur `127.0.0.1`, sur un port qui lui est propre.

## Dépannage

| Problème | Que faire |
| --- | --- |
| Le port est déjà utilisé | Définissez un autre `port` dans `config/settings.toml`, ou arrêtez l’autre instance (`stop`). |
| « Clés manquantes » dans l’interface | Saisissez-les sous Paramètres, ou exécutez `configure`. |
| Une exécution échoue immédiatement | Ouvrez l’exécution dans l’interface ; ses étapes indiquent la cause. `logs/application.log` contient les détails. |
| Réponses très lentes | Les modèles locaux sans carte graphique sont lents ; voir « Modèles locaux » pour la mémoire nécessaire à chacun. |
| macOS : « ne peut pas être ouvert » | Clic droit sur `Start.command`, « Ouvrir » ; ou exécutez `xattr -dr com.apple.quarantine .` dans ce dossier. |
| Windows SmartScreen | « Informations complémentaires », « Exécuter quand même » (les scripts sont du texte brut que vous pouvez lire). |
| Autre chose | `check` teste le paquet ; `logs/` contient les journaux de l’application et du serveur de modèles. |

## Contenu de ce dossier

`python/` (Python {python_version}), `nodestro_runtime/`, `nodestro_api/` et `app/` (l’application),
`workflow/` (la publication figée), `config/`, `examples/`, {models_files}`licenses/` (licences de tout ce qui est
inclus), `lib.lock` (chaque bibliothèque avec son SHA-256), `SHA256SUMS` (une somme de contrôle de chaque fichier), `PORTABLE.json`
(ce qu’est ce paquet) et `openapi.json`. Vos données se trouvent dans `data/` et `logs/` ; sauvegardez-les pour conserver exécutions et
paramètres.
