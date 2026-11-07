# {title}

Esta pasta contém o workflow do Nodestro **{title}** como uma aplicação pronta para uso em **{platform}**. Ela traz
tudo de que precisa: seu próprio Python, todas as bibliotecas, o workflow (versão publicada **v{number}**, versão do workflow
v{version}, publicada em {published}, digest `{digest}`){local_models_line} e, quando exportada com ela, uma
interface web. Nada precisa ser instalado: nem Python, nem pip, nem Node.js, nem Nodestro Studio.

## Iniciar

1. Descompacte o arquivo ZIP em uma pasta na qual você tenha permissão de escrita (por exemplo, sua pasta Documentos). No Windows, o caminho não deve
   ser longo demais; `C:\Apps\` funciona bem.
2. Inicie a aplicação:
   - **Windows:** clique duas vezes em `start.cmd`.
   - **macOS:** clique duas vezes em `Start.command` (na primeira vez: clique com o botão direito, "Abrir", porque o arquivo foi baixado).
   - **Linux:** execute `./start.sh` em um terminal.
3. A primeira inicialização verifica os arquivos{models_check}. Quando estiver pronta, a janela mostra o endereço
   (`http://127.0.0.1:{port}`) e abre o navegador{ui_line}.

Pare a aplicação com **Ctrl+C** na janela dela, ou com `stop.cmd` / `./stop.sh`. Ao iniciá-la novamente, todas as
execuções, aprovações, arquivos e configurações são mantidos.

| Comando (`start.cmd …` no Windows, `./start.sh …` nos demais) | O que faz |
| --- | --- |
| `start` (ou nada) | Inicia a aplicação. |
| `stop` | Para uma instância em execução. |
| `status` | Informa se está em execução, e onde. |
| `open` | Abre o navegador com um novo link de acesso (somente neste computador). |
| `configure` | Insere as chaves dos provedores de IA (a entrada fica oculta). |
| `keys new --name "My app" --scopes run,read` | Cria uma chave de API para outro programa (`keys list`, `keys revoke <id>`). |
| `check` | Um autoteste sem iniciar (`check --full` também verifica cada arquivo de modelo por SHA-256). |

## Provedores de IA e chaves

{providers_section}

As chaves nunca fazem parte deste pacote. Insira-as no navegador (Configurações, somente para o operador deste computador)
ou com `configure` em um terminal; ambos as armazenam em `data/secrets/providers.json`, legível somente pela sua conta
de usuário. O arquivo não é criptografado (uma chave de criptografia ao lado dele não protegeria nada): proteja-o como uma chave
SSH, ou defina as chaves como variáveis de ambiente a partir de um cofre de segredos. Variáveis de ambiente sempre têm prioridade.
Uma chave alterada vale para a próxima execução; não é necessária uma nova exportação.

## Modelos locais

{models_section}

## Rede e operação offline

{network_section}

## Arquivos de resultado

{output_section}

Cada execução também mantém seus arquivos na aplicação (baixe-os no navegador ou com
`GET /runs/{run_id}/files/{file_id}`). A pasta padrão é `[data] output_dir` em `config/settings.toml` (relativa a
esta pasta ou absoluta). O operador pode escolher outra pasta na interface web em **Configurações → Pasta de
resultados**: um navegador de pastas deste computador (só nomes de pastas), uma pasta nova só é criada após
confirmação e **Restaurar padrão** volta à pasta do arquivo de configurações. A escolha fica salva em
`data/settings/output-folder.json` e vale a partir da próxima execução, sem reiniciar; arquivos já salvos ficam onde
estão. Pastas do sistema e as pastas da própria aplicação (programa, Python, dados, logs) são recusadas. Uma variável
`OUTPUT_DIR` definida no ambiente tem prioridade e não pode ser alterada nessa página. **Abrir pasta** só aparece em
um navegador deste computador quando a aplicação atende apenas este computador. Os nomes de arquivo são ajustados para
serem válidos em qualquer sistema; um nome que já existe recebe ` (2)`, ` (3)` …

## Configurações

`config/settings.toml` contém todas as configurações com explicações (porta, endereço, pastas, limites, memória dos modelos,
modo offline, idioma da interface). As alterações valem a partir da próxima inicialização. Variáveis de ambiente substituem o arquivo (por
exemplo `PORT`, `DATA_DIR`, `OUTPUT_DIR`).

Por padrão, a aplicação responde **somente a este computador** (`127.0.0.1`). Para atender outros computadores, defina
`host = "0.0.0.0"` **e** HTTPS (`tls_cert`/`tls_key`, ou `behind_proxy = true` atrás de um proxy reverso com
HTTPS); sem HTTPS, ela se recusa a iniciar dessa forma. Outras pessoas entram com uma chave de API; ninguém consegue entrar
sem uma.

## Interface de programação (API)

Outros programas usam o mesmo workflow pela API HTTP com sua própria chave (veja `keys new`):

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

As execuções são assíncronas: iniciar responde `202` com um `run_id`. Valores de status: `queued`, `running`,
`awaiting_approval`, `succeeded`, `failed`, `cancelled`. Os eventos são Server-Sent Events; após perder a conexão,
reconecte com `Last-Event-ID`. Este workflow tem {approvals}; uma chave com a permissão `approve` decide:

```bash
{curl_approve}
```

Os erros têm o formato `{"error": {"status", "code", "message", "params"}}`. A descrição completa é
`openapi.json` nesta pasta (OpenAPI 3); a referência interativa é `http://127.0.0.1:{port}/docs` (por padrão, somente a partir
deste computador). `examples/client.py` mostra tudo em Python.

## Segurança

- Nenhuma chave do Nodestro ou de um provedor de IA está neste pacote, e o navegador nunca recebe uma chave de provedor.
- A interface web usa um cookie de sessão (HttpOnly, SameSite=Strict); o operador entra com um link de uso único
  da janela de inicialização, todas as outras pessoas com uma chave de API.
- Ferramentas escritas por uma pessoa são executadas como processos separados. Ferramentas geradas por IA são executadas somente em um contêiner
  Docker isolado (`[tools] sandbox = "docker"`, requer Docker); sem Docker, as execuções que precisam delas são recusadas e
  nada é executado sem proteção.
- O modo offline não é um firewall: as ferramentas que uma pessoa escreveu rodam com o acesso à rede da sua conta de
  usuário, e nada neste pacote o bloqueia. Onde nenhum tráfego pode sair do computador, execute o pacote sem rede.
- O servidor de modelos local escuta somente em `127.0.0.1`, em uma porta própria.

## Solução de problemas

| Problema | O que fazer |
| --- | --- |
| A porta está em uso | Defina outra `port` em `config/settings.toml`, ou pare a outra instância (`stop`). |
| "Chaves ausentes" na interface | Insira-as em Configurações, ou execute `configure`. |
| Uma execução falha imediatamente | Abra a execução na interface; suas etapas mostram o motivo. `logs/application.log` tem os detalhes. |
| Respostas muito lentas | Modelos locais sem placa de vídeo são lentos; veja "Modelos locais" para a memória que cada um precisa. |
| macOS: "não pode ser aberto" | Clique com o botão direito em `Start.command`, "Abrir"; ou execute `xattr -dr com.apple.quarantine .` nesta pasta. |
| Windows SmartScreen | "Mais informações", "Executar assim mesmo" (os scripts são texto simples que você pode ler). |
| Outra coisa | `check` testa o pacote; `logs/` contém os logs da aplicação e do servidor de modelos. |

## O que há nesta pasta

`python/` (Python {python_version}), `nodestro_runtime/`, `nodestro_api/` e `app/` (a aplicação),
`workflow/` (a versão congelada), `config/`, `examples/`, {models_files}`licenses/` (licenças de tudo o que está
incluído), `lib.lock` (cada biblioteca com seu SHA-256), `SHA256SUMS` (um checksum de cada arquivo), `PORTABLE.json`
(o que é este pacote) e `openapi.json`. Seus dados ficam em `data/` e `logs/`; faça backup deles para manter execuções e
configurações.
