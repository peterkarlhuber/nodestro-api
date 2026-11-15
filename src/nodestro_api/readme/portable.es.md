# {title}

Esta carpeta es el workflow de Nodestro **{title}** como aplicación lista para usar en **{platform}**. Incluye
todo lo que necesita: su propio Python, todas las bibliotecas, el workflow (versión publicada **v{number}**, versión del workflow
v{version}, publicada el {published}, digest `{digest}`){local_models_line} y, si se exportó con ella, una
interfaz web. No hay que instalar nada: ni Python, ni pip, ni Node.js, ni Nodestro Studio.

## Inicio

1. Descomprime el archivo ZIP en una carpeta en la que puedas escribir (por ejemplo, tu carpeta de documentos). En Windows la ruta no debería
   ser demasiado larga; `C:\Apps\` funciona bien.
2. Inicia la aplicación:
   - **Windows:** haz doble clic en `start.cmd`.
   - **macOS:** haz doble clic en `Start.command` (la primera vez: clic derecho, "Abrir", porque el archivo se descargó).
   - **Linux:** ejecuta `./start.sh` en un terminal.
3. El primer inicio comprueba los archivos{models_check}. Cuando está lista, la ventana muestra la dirección
   (`http://127.0.0.1:{port}`) y abre el navegador{ui_line}.

Detén la aplicación con **Ctrl+C** en su ventana, o con `stop.cmd` / `./stop.sh`. Al iniciarla de nuevo se conservan
todas las ejecuciones, aprobaciones, archivos y ajustes.

| Comando (`start.cmd …` en Windows, `./start.sh …` en los demás) | Qué hace |
| --- | --- |
| `start` (o nada) | Inicia la aplicación. |
| `stop` | Detiene una instancia en ejecución. |
| `status` | Indica si se está ejecutando y dónde. |
| `open` | Abre el navegador con un nuevo enlace de inicio de sesión (solo en este equipo). |
| `configure` | Introduce las claves de los proveedores de IA (la entrada queda oculta). |
| `keys new --name "My app" --scopes run,read` | Crea una clave de API para otro programa (`keys list`, `keys revoke <id>`). |
| `check` | Una autoprueba sin iniciar (`check --full` también comprueba cada archivo de modelo mediante SHA-256). |
| `verify` | Comprueba cada archivo de `program/` con `SHA256SUMS` (cada inicio también lo hace, sin los archivos de modelo). |
| `backup <archivo.zip>` | Copia la configuración y los datos (detenida; `--include-results`, y `--include-secrets` para las claves). |
| `restore <archivo.zip>` | Restaura una copia de esta aplicación (detenida; antes se copia el estado actual). |
| `update <paquete nuevo>` | Toma el programa de un paquete más reciente de esta aplicación, su ZIP o carpeta (detenida). |
| `rollback` | Vuelve al programa anterior a la última actualización. |
| `adopt <carpeta antigua>` | Toma la configuración y los datos de un paquete anterior de esta aplicación. |
| `ui install <archivo.tgz> --sha256 <hash>` | Instala una interfaz web más reciente (`ui reset` vuelve atrás, `ui status`). |
| `pair` | Crea un código de emparejamiento para un navegador en otro ordenador (solo con `NODESTRO_PAIRING=on`, ver Configuración). |
| `devices list` | Muestra los navegadores emparejados (`devices revoke <id>` o `--all` retira el acceso). |

## Proveedores de IA y claves

{providers_section}

Las claves nunca forman parte de este paquete. Introdúcelas en el navegador (Configuración, solo para el operador de este equipo)
o con `configure` en un terminal; ambos las guardan en `data/secrets/providers.json`, legible solo por tu cuenta
de usuario. El archivo no está cifrado (una clave de cifrado junto a él no protegería nada): protégelo como una clave
SSH, o define las claves como variables de entorno desde un almacén de secretos. Las variables de entorno siempre tienen prioridad.
Una clave modificada se aplica a la siguiente ejecución; no hace falta una nueva exportación.

## Modelos locales

{models_section}

## Red y funcionamiento sin conexión

{network_section}

## Archivos de resultados

{output_section}

Cada ejecución también conserva sus archivos en la aplicación (descárgalos en el navegador o con
`GET /runs/{run_id}/files/{file_id}`). La carpeta predeterminada es `[data] output_dir` en `config/settings.toml`
(relativa a esta carpeta o absoluta). El operador puede elegir otra carpeta en la interfaz web en **Ajustes → Carpeta
de resultados**: un explorador de carpetas de este equipo (solo nombres de carpetas), una carpeta nueva solo se crea
tras confirmarlo y **Restablecer predeterminada** vuelve a la carpeta del archivo de ajustes. La elección se guarda en
`data/settings/output-folder.json` y se aplica desde la próxima ejecución, sin reiniciar; los archivos ya guardados se
quedan donde están. Se rechazan las carpetas del sistema y las propias de la aplicación (programa, Python, datos,
registros). Una variable `OUTPUT_DIR` definida en el entorno tiene prioridad y no se puede cambiar en esa página.
**Abrir carpeta** solo aparece en un navegador de este equipo cuando la aplicación sirve solo a este equipo. Los
nombres de archivo se adaptan para que sean válidos en cualquier sistema; un nombre que ya existe recibe ` (2)`,
` (3)` …

## Configuración

`config/settings.toml` contiene todos los ajustes con explicaciones (puerto, dirección, carpetas, límites, memoria de los modelos,
modo sin conexión, idioma de la interfaz). Los cambios se aplican en el siguiente inicio. Las variables de entorno tienen prioridad sobre el archivo (por
ejemplo `PORT`, `DATA_DIR`, `OUTPUT_DIR`).

Por defecto, la aplicación responde **solo a este equipo** (`127.0.0.1`). Para dar servicio a otros equipos, define
`host = "0.0.0.0"` **y** HTTPS (`tls_cert`/`tls_key`, o `behind_proxy = true` detrás de un proxy inverso con
HTTPS); sin HTTPS se niega a iniciarse de ese modo. Las demás personas inician sesión con una clave de API; nadie puede iniciar sesión
sin una.

**Desde otro ordenador (emparejamiento, vista previa).** Sin certificado propio y sin proxy, la aplicación puede
usar un certificado autoemitido con `tls_self_signed = true`. El navegador avisa de ello: compara la huella que muestra
la consola al iniciar con la del diálogo de certificado del navegador antes de continuar. La interfaz marca esta
conexión como de confianza limitada mientras se use. Con el emparejamiento activado (variable de entorno
`NODESTRO_PAIRING=on`, desactivado por defecto hasta la revisión de seguridad independiente), `pair` crea un
código válido 10 minutos; introdúcelo en el navegador del otro ordenador, sin clave de API. `devices list`
muestra los navegadores emparejados y `devices revoke <id>` (o `--all`) les retira el acceso al instante. Un
certificado propio o un proxy siguen siendo la vía recomendada.

## Interfaz de programación (API)

Otros programas usan el mismo workflow a través de la API HTTP con su propia clave (consulta `keys new`):

```bash
export {key_variable}=ndk_...
{curl_start}
```

El cuerpo de la solicitud (`program/examples/request.json`):

```json
{request}
```
{upload}
```bash
{curl_status}
{curl_result}
{curl_events}
```

Las ejecuciones son asíncronas: al iniciar se responde `202` con un `run_id`. Valores de estado: `queued`, `running`,
`awaiting_approval`, `succeeded`, `failed`, `cancelled`. Los eventos son Server-Sent Events; tras perder la conexión,
vuelve a conectarte con `Last-Event-ID`. Este workflow tiene {approvals}; decide una clave con el permiso `approve`:

```bash
{curl_approve}
```

Los errores tienen la forma `{"error": {"status", "code", "message", "params"}}`. La descripción completa es
`program/openapi.json` en esta carpeta (OpenAPI 3); la referencia interactiva es `http://127.0.0.1:{port}/docs` (por defecto, solo desde
este equipo). `program/examples/client.py` lo muestra todo en Python.

Cualquier cliente puede preguntar primero qué ofrece este backend: `GET /api/app` indica el contrato de apps
(`nodestro-app/1`), el identificador del backend y cómo iniciar sesión; con una clave, además este workflow y las
capacidades. `BACKEND_NAME` da al backend un nombre que muestran los clientes. El contrato se describe en
`docs/app-api/v1.md` de nodestro-api.

## Seguridad

- Este paquete no contiene ninguna clave de Nodestro ni de un proveedor de IA, y el navegador nunca recibe una clave de proveedor.
- La interfaz web usa una cookie de sesión (HttpOnly, SameSite=Strict); el operador inicia sesión con un enlace de un solo uso
  de la ventana de inicio, todos los demás con una clave de API.
- Las herramientas que escribió una persona se ejecutan como procesos separados. Las herramientas generadas por IA solo se ejecutan en un contenedor
  Docker aislado (`[tools] sandbox = "docker"`, requiere Docker); sin Docker, se rechazan las ejecuciones que las necesitan y
  nada se ejecuta sin protección.
- El modo sin conexión no es un cortafuegos: las herramientas que escribió una persona se ejecutan con el acceso a la red
  de tu cuenta de usuario, y nada en este paquete lo bloquea. Donde ningún tráfico pueda salir del equipo, ejecuta el
  paquete sin red.
- El servidor de modelos local escucha solo en `127.0.0.1`, en un puerto propio.

## Solución de problemas

| Problema | Qué hacer |
| --- | --- |
| El puerto está en uso | Define otro `port` en `config/settings.toml`, o detén la otra instancia (`stop`). |
| "Faltan claves" en la interfaz | Introdúcelas en Configuración, o ejecuta `configure`. |
| Una ejecución falla de inmediato | Abre la ejecución en la interfaz; sus pasos muestran el motivo. `logs/application.log` tiene los detalles. |
| Respuestas muy lentas | Los modelos locales sin tarjeta gráfica son lentos; consulta "Modelos locales" para ver la memoria que necesita cada uno. |
| macOS: "no se puede abrir" | Clic derecho en `Start.command`, "Abrir"; o ejecuta `xattr -dr com.apple.quarantine .` en esta carpeta. |
| Windows SmartScreen | "Más información", "Ejecutar de todas formas" (los scripts son texto plano que puedes leer). |
| Otra cosa | `check` prueba el paquete; `logs/` contiene los registros de la aplicación y del servidor de modelos. |

## Actualizar, hacer copias, comprobar

- **Actualizar:** detén la aplicación y ejecuta aquí `update <paquete nuevo.zip>` (un paquete de la misma aplicación
  para la misma plataforma). Solo se reemplaza `program/`; la configuración, los datos, las claves y los archivos de
  resultados se quedan. Antes se copian los datos en `backups/`, y el programa anterior queda como
  `program.previous/`: `rollback` vuelve a él.
- **Desde un paquete anterior** (uno sin carpeta `program/`): descomprime el paquete nuevo a su lado y ejecuta
  `adopt <carpeta antigua>` en el nuevo. La carpeta antigua no se modifica.
- **Copia de seguridad:** `backup <archivo.zip>` con la aplicación detenida. Las claves solo se incluyen con
  `--include-secrets`; guarda esa copia tan segura como las claves. `restore <archivo.zip>` la restaura.
- **Comprobar:** cada inicio comprueba que ningún archivo de `program/` se haya modificado, eliminado o añadido, y si
  no, no arranca. Así se detectan copias dañadas y cambios por error; no protege frente a alguien que pueda escribir en
  esta carpeta (también podría cambiar `SHA256SUMS`).

## Qué hay en esta carpeta

`program/` es lo exportado y no se modifica después: `python/` (Python {python_version}), `nodestro_runtime/`,
`nodestro_api/` y `app/` (la aplicación), `workflow/` (la versión congelada), `examples/`, {models_files}`licenses/`
(licencias de todo lo incluido), `lib.lock` (cada biblioteca con su SHA-256), `SHA256SUMS` (una suma de control de
cada archivo), `PORTABLE.json` (qué es este paquete) y `openapi.json`. Al lado: los scripts de inicio, `config/` (tu
configuración) y, tras el primer uso, `data/` (ejecuciones, claves), `results/`, `logs/` y `backups/`. Haz copia de
`config/` y `data/` para conservar ejecuciones y ajustes.
