# {title}: API do workflow

Este projeto disponibiliza o workflow do Nodestro **{title}** como uma API HTTP, independente do Nodestro Studio.
Ele foi exportado da versão publicada **v{number}** (versão do workflow v{version}, publicada em {published},
digest `{digest}`) e contém:

- `workflow/release.json`: o workflow publicado, seus subworkflows e o código de suas ferramentas, congelados;
- `nodestro_runtime/` e `nodestro_api/`: o mecanismo de execução do Nodestro (nodestro-runtime {nodestro}) e
  a camada HTTP dos workflows publicados, copiados sem alterações (versões, commits de origem e checksums em
  `RUNTIME.json`), para que o workflow seja executado exatamente como no estúdio: agentes em paralelo, decisões, aprovações,
  subworkflows, ferramentas, orçamentos e arquivos de resultado;
- `app/`: a aplicação (`main.py`), sua configuração (`config.py`), os modelos de requisição (`schemas.py`)
  e uma ferramenta para criar chaves de API (`keys.py`);
- `tests/`: testes com um modelo simulado (sem custo) e `examples/`.{guide}

Nada é enviado ao Nodestro. A API precisa de acesso aos provedores de IA de seus agentes ({providers}) e de suas
próprias chaves de API para seus clientes.

## Requisitos

- Python {python} (as versões em `requirements.lock` foram obtidas do Nodestro rodando em Python {python})
- Acesso a {providers} (veja "Configuração")

### orkexa (biblioteca de agentes)

{agent_install}

## Instalação

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
# or with uv:  uv sync   (creates uv.lock; needs network access)
cp .env.example .env
```

## Configuração

Todas as configurações vêm de variáveis de ambiente ou de `.env` (veja `app/config.py`):

| Variável | Significado |
| --- | --- |
{provider_rows}
| `API_KEYS` | As chaves dos seus clientes (somente hashes, veja abaixo). Vazia: toda requisição é recusada. |
| `DATA_DIR` | Onde execuções, aprovações, arquivos e memória são armazenados (padrão `./data`). |
| `API_DOCS` | `local` (padrão: `/docs` somente a partir deste computador), `on` ou `off`. |
| `RUN_RETENTION_DAYS` | Na inicialização, exclui execuções com mais dias do que este valor (0 mantém tudo). |
| `TOOL_SANDBOX` | `subprocess` (padrão) ou `docker`, veja abaixo. |

`TOOL_SANDBOX=subprocess` executa somente ferramentas que uma pessoa escreveu no editor de ferramentas, como processo deste servidor.
`TOOL_SANDBOX=docker` executa cada ferramenta própria em um contêiner isolado. Isso requer Docker e a imagem de sandbox do Orkexa
`orkexa-sandbox`, que você cria uma vez a partir do repositório do Orkexa no commit do `orkexa` instalado
(`docker build -t orkexa-sandbox -f docker/sandbox.Dockerfile .`; sua imagem base é fixada por digest e seus
pacotes por hash) e verifica com `python -m orkexa.dynamic.docker --self-test`. O estúdio do Nodestro usa, em vez disso, sua própria
imagem de runtime, que uma exportação não consegue criar. Um contêiner não tem rede, tem um sistema de arquivos somente leitura, nenhuma
capability e um usuário sem privilégios; as ferramentas só são executadas nele depois que o daemon do Docker passou na verificação prévia do Orkexa e
no autoteste dentro do contêiner (verificados em segundo plano na inicialização), e sempre pelo id da imagem aprovada,
nunca pela tag. Ferramentas geradas por IA só são executadas com `docker`; sem ele, ou até que essa verificação seja aprovada, as execuções que
as usam são recusadas.

### Chaves de API

```bash
python -m app.keys new --name "My app" --scopes run,read
```

exibe uma chave (entregue-a ao cliente; ela é mostrada uma única vez) e a linha para `API_KEYS`. Permissões:
`run` (iniciar, cancelar, continuar), `read` (status, resultado, eventos, arquivos, uso), `approve` (decidir
aprovações). Separe várias entradas com `;`. Chaves do Nodestro nunca fazem parte de uma exportação.

### Limites

Valem os limites da API publicada; você pode substituí-los com:

| Variável | Valor exportado |
| --- | --- |
{limits}

`LIMIT_DAILY_BUDGET_USD` bloqueia novas execuções assim que as execuções das últimas 24 horas atingem esse custo;
`LIMIT_RUN_BUDGET_USD` interrompe uma execução individual. Limites excedidos respondem `429` com um código.

## Iniciar

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

A API passa então a responder somente neste computador. Para atender outros computadores, escute em `0.0.0.0` **atrás de HTTPS**
(por exemplo, um proxy reverso, ou `--ssl-certfile`/`--ssl-keyfile`): as chaves não podem trafegar sem criptografia.
Documentação interativa: http://127.0.0.1:8000/docs

## Uso

As execuções são assíncronas: iniciar responde `202` com um `run_id`; depois consulte o status, acompanhe os eventos
e busque o resultado.

```bash
export {key_variable}=ndk_...
{curl_start}
```

O corpo da requisição (`examples/request.json`):

```json
{request}
```
{upload}
```bash
{curl_status}
{curl_result}
{curl_events}
```

Valores de status: `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`. Enviar novamente a
mesma `Idempotency-Key` retorna a execução já iniciada, de modo que novas tentativas nunca iniciam uma execução duas vezes.
Os eventos são Server-Sent Events; após perder a conexão, reconecte com `Last-Event-ID`; o primeiro evento é
sempre o status atual completo. `examples/client.py` faz tudo isso em Python.

## Aprovações

Este workflow tem {approvals}. Uma execução que chega a uma delas é pausada com o status
`awaiting_approval`; o status lista o que revisar. Uma chave com a permissão `approve` decide:

```bash
{curl_approve}
```

`approval_id` pode ser omitido quando há apenas uma aprovação pendente. Cada decisão é armazenada com a execução (quem, quando,
comentário); uma segunda decisão sobre a mesma aprovação responde `409`. Execuções em espera sobrevivem a reinicializações.

## Arquivos de resultado e dados

Os arquivos de resultado que o workflow grava são listados em `/runs/{run_id}/files`. Tudo é armazenado em
`DATA_DIR/runtime.db` (execuções com suas etapas, uso e custo, aprovações, arquivos, uploads, memória) e em
`DATA_DIR/tools`. Conhecimento incluído na exportação: {knowledge_in}. Não incluído (os agentes são executados sem
ele): {knowledge_out}. Memória inicial: {memory}. Ferramentas: {tools}.

## Erros e custos

Os erros têm o formato `{"error": {"status", "code", "message", "params"}}`. Cada execução registra tokens e
custo estimado (status e resultado: `usage`); `/api/published/{slug}/usage` soma esses valores. Uma execução
interrompida por uma reinicialização é marcada como `failed` (`run.interrupted`) e pode ser continuada com
`POST /runs/{run_id}/resume`.

## Testes

```bash
pip install pytest httpx
pytest
```

Os testes usam um modelo simulado: não custam nada e verificam as rotas, a validação, as chaves, os limites,
a idempotência, uma execução completa, as aprovações após uma reinicialização e que o runtime não foi alterado.
