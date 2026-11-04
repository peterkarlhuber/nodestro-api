# {title}: API del workflow

Este proyecto ofrece el workflow de Nodestro **{title}** como API HTTP, independiente de Nodestro Studio.
Se exportó desde la versión publicada **v{number}** (versión del workflow v{version}, publicada el {published},
digest `{digest}`) y contiene:

- `workflow/release.json`: el workflow publicado, sus subworkflows y el código de sus herramientas, congelados;
- `nodestro_runtime/` y `nodestro_api/`: el motor de ejecución de Nodestro (nodestro-runtime {nodestro}) y
  la capa HTTP de los workflows publicados, copiados sin cambios (versiones, commits de origen y sumas de control en
  `RUNTIME.json`), para que el workflow se ejecute exactamente igual que en el estudio: agentes en paralelo, decisiones, aprobaciones,
  subworkflows, herramientas, presupuestos y archivos de resultados;
- `app/`: la aplicación (`main.py`), su configuración (`config.py`), los modelos de solicitud (`schemas.py`)
  y una herramienta para crear claves de API (`keys.py`);
- `tests/`: pruebas con un modelo simulado (sin coste) y `examples/`.{guide}

No se envía nada a Nodestro. La API necesita acceso a los proveedores de IA de sus agentes ({providers}) y sus
propias claves de API para sus clientes.

## Requisitos

- Python {python} (las versiones de `requirements.lock` se tomaron de Nodestro ejecutándose con Python {python})
- Acceso a {providers} (consulta "Configuración")

### orkexa (biblioteca de agentes)

{agent_install}

## Instalación

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
# or with uv:  uv sync   (creates uv.lock; needs network access)
cp .env.example .env
```

## Configuración

Todos los ajustes provienen de variables de entorno o de `.env` (consulta `app/config.py`):

| Variable | Significado |
| --- | --- |
{provider_rows}
| `API_KEYS` | Las claves de tus clientes (solo hashes, ver más abajo). Vacía: se rechaza toda solicitud. |
| `DATA_DIR` | Dónde se guardan ejecuciones, aprobaciones, archivos y memoria (por defecto `./data`). |
| `API_DOCS` | `local` (por defecto: `/docs` solo desde este equipo), `on` u `off`. |
| `RUN_RETENTION_DAYS` | Al iniciar, elimina las ejecuciones con más de este número de días (0 lo conserva todo). |
| `TOOL_SANDBOX` | `subprocess` (por defecto) o `docker`, ver más abajo. |

`TOOL_SANDBOX=subprocess` ejecuta solo herramientas que una persona escribió en el editor de herramientas, como proceso de este servidor.
`TOOL_SANDBOX=docker` ejecuta cada herramienta propia en un contenedor aislado. Requiere Docker y la imagen de sandbox de Orkexa
`orkexa-sandbox`, que construyes una vez desde el repositorio de Orkexa en el commit del `orkexa` instalado
(`docker build -t orkexa-sandbox -f docker/sandbox.Dockerfile .`; su imagen base está fijada por digest y sus
paquetes por hash) y compruebas con `python -m orkexa.dynamic.docker --self-test`. El estudio de Nodestro usa en su lugar su propia
imagen de ejecución, que una exportación no puede construir. Un contenedor no tiene red, tiene un sistema de archivos de solo lectura, ninguna
capability y un usuario sin privilegios; las herramientas se ejecutan en él solo después de que el daemon de Docker haya superado la verificación previa de Orkexa y
la autoprueba dentro del contenedor (comprobadas en segundo plano al iniciar), y siempre mediante el id de la imagen que la superó,
nunca mediante la etiqueta. Las herramientas generadas por IA solo se ejecutan con `docker`; sin él, o hasta que se supere esa comprobación, se rechazan
las ejecuciones que las usan.

### Claves de API

```bash
python -m app.keys new --name "My app" --scopes run,read
```

muestra una clave (dásela al cliente; se muestra una sola vez) y la línea para `API_KEYS`. Permisos:
`run` (iniciar, cancelar, continuar), `read` (estado, resultado, eventos, archivos, uso), `approve` (decidir
aprobaciones). Separa varias entradas con `;`. Las claves de Nodestro nunca forman parte de una exportación.

### Límites

Se aplican los límites de la API publicada; puedes sobrescribirlos con:

| Variable | Valor exportado |
| --- | --- |
{limits}

`LIMIT_DAILY_BUDGET_USD` detiene las nuevas ejecuciones en cuanto las ejecuciones de las últimas 24 horas alcanzan ese coste;
`LIMIT_RUN_BUDGET_USD` detiene una ejecución individual. Los límites superados responden `429` con un código.

## Inicio

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Así, la API solo responde en este equipo. Para dar servicio a otros equipos, escucha en `0.0.0.0` **detrás de HTTPS**
(p. ej. un proxy inverso, o `--ssl-certfile`/`--ssl-keyfile`): las claves no deben viajar sin cifrar.
Documentación interactiva: http://127.0.0.1:8000/docs

## Uso

Las ejecuciones son asíncronas: al iniciar se responde `202` con un `run_id`; después consulta el estado, sigue los eventos
y obtén el resultado.

```bash
export {key_variable}=ndk_...
{curl_start}
```

El cuerpo de la solicitud (`examples/request.json`):

```json
{request}
```
{upload}
```bash
{curl_status}
{curl_result}
{curl_events}
```

Valores de estado: `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`. Si vuelves a enviar la
misma `Idempotency-Key`, se devuelve la ejecución ya iniciada, de modo que los reintentos nunca inician una ejecución dos veces.
Los eventos son Server-Sent Events; tras perder la conexión, vuelve a conectarte con `Last-Event-ID`; el primer evento es
siempre el estado actual completo. `examples/client.py` hace todo esto en Python.

## Aprobaciones

Este workflow tiene {approvals}. Una ejecución que llega a una se pausa con el estado
`awaiting_approval`; el estado indica qué hay que revisar. Decide una clave con el permiso `approve`:

```bash
{curl_approve}
```

`approval_id` puede omitirse cuando solo hay una aprobación pendiente. Cada decisión se guarda con la ejecución (quién, cuándo,
comentario); una segunda decisión sobre la misma aprobación responde `409`. Las ejecuciones en espera sobreviven a los reinicios.

## Archivos de resultados y datos

Los archivos de resultados que escribe el workflow aparecen en `/runs/{run_id}/files`. Todo se guarda en
`DATA_DIR/runtime.db` (ejecuciones con sus pasos, uso y coste, aprobaciones, archivos, subidas, memoria) y en
`DATA_DIR/tools`. Conocimiento incluido en la exportación: {knowledge_in}. No incluido (los agentes se ejecutan sin
él): {knowledge_out}. Memoria inicial: {memory}. Herramientas: {tools}.

## Errores y costes

Los errores tienen la forma `{"error": {"status", "code", "message", "params"}}`. Cada ejecución registra los tokens y
el coste estimado (estado y resultado: `usage`); `/api/published/{slug}/usage` los suma. Una ejecución
interrumpida por un reinicio se marca como `failed` (`run.interrupted`) y puede continuarse con
`POST /runs/{run_id}/resume`.

## Pruebas

```bash
pip install pytest httpx
pytest
```

Las pruebas usan un modelo simulado: no cuestan nada y comprueban las rutas, la validación, las claves, los límites,
la idempotencia, una ejecución completa, las aprobaciones a través de un reinicio y que el runtime no ha cambiado.
