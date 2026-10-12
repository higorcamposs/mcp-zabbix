# mcp-zabbix

MCP Server para **consultar e gerenciar um Zabbix** pela API JSON-RPC: lista hosts,
problemas, triggers e itens, e também cria hosts, grupos, macros, itens, triggers e
janelas de manutenção. Feito em Python com o SDK oficial do MCP (`mcp` 2.x).

Testado contra o Zabbix **7.4**. Usa só a API pública; não precisa de acesso ao banco.

## Início rápido (laboratório local)

O `docker-compose.yml` deste repositório sobe um Zabbix 7.4 completo
(PostgreSQL, server, web e agent) em `http://localhost:8080`:

```bash
docker compose up -d
```

Depois prepare o servidor MCP:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env        # já aponta para o Zabbix do compose
```

Confira que tudo conversa (cria objetos `smoke-*`, testa e apaga):

```bash
.venv/bin/python scripts/smoke_test.py
```

## Conectar a um cliente MCP

O transporte padrão é **stdio**: o cliente inicia o processo sozinho.

**Claude Code / app Claude (aba Code), nesta pasta:** o arquivo [`.mcp.json`](.mcp.json)
já registra o servidor `zabbix`. Basta abrir o projeto e aprovar o servidor.

**Em qualquer pasta (Claude Code):**

```bash
claude mcp add zabbix --scope user -- /caminho/para/mcp-zabbix/.venv/bin/python -m mcp_zabbix
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "zabbix": {
      "command": "/caminho/para/mcp-zabbix/.venv/bin/python",
      "args": ["-m", "mcp_zabbix"]
    }
  }
}
```

As credenciais vêm do `.env` (lido pela raiz do projeto, qualquer que seja a pasta de
onde o cliente inicia o processo) ou do bloco `env` do cliente. Variáveis já definidas
no ambiente vencem o arquivo.

## Configuração

| Variável | Padrão | O que faz |
| --- | --- | --- |
| `ZABBIX_URL` | `http://localhost:8080` | URL base. O `/api_jsonrpc.php` é acrescentado sozinho. |
| `ZABBIX_API_TOKEN` | — | Token de API (Users › API tokens). Tem prioridade sobre usuário e senha. |
| `ZABBIX_USER` / `ZABBIX_PASSWORD` | — | Login por usuário e senha. A sessão é renovada se expirar e encerrada ao sair. |
| `ZABBIX_READ_ONLY` | `false` | `true` faz toda Tool que altera o Zabbix recusar a chamada. |
| `ZABBIX_VERIFY_TLS` | `true` | `false` ignora o certificado (HTTPS com certificado próprio). |
| `ZABBIX_TIMEOUT_SECONDS` | `15` | Tempo limite de cada chamada à API. |
| `MCP_TRANSPORT` | `stdio` | `stdio` ou `streamable-http`. |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | Endereço do modo HTTP (`/mcp`, com `/health`). |
| `MCP_LOG_LEVEL` | `INFO` | Nível de log (vai para o stderr). |
| `MCP_EXTRA_ALLOWED_HOSTS` / `MCP_EXTRA_ALLOWED_ORIGINS` | — | Hosts/origens extras aceitos no modo HTTP (proteção contra DNS rebinding). |

Modo HTTP, se preferir um servidor de pé em vez de stdio:

```bash
MCP_TRANSPORT=streamable-http .venv/bin/python -m mcp_zabbix
# endpoint: http://127.0.0.1:8000/mcp
```

## Tools

**Consulta** (somente leitura)

| Tool | O que faz |
| --- | --- |
| `get_overview` | Versão, contagem de hosts/grupos/templates, hosts indisponíveis e problemas por severidade. |
| `list_hosts` | Hosts com grupos, templates, endereço e disponibilidade. Filtros: nome, prefixo de IP, grupo (inclui subgrupos), estado, só com problemas. |
| `get_host` | Detalhe de um host por nome técnico, nome visível ou hostid: interfaces e erros, tags, macros, itens/triggers e problemas ativos. |
| `list_host_groups`, `list_templates` | Grupos e templates, para usar os nomes exatos ao criar hosts. |
| `list_items`, `get_item_history` | Itens com o último valor; histórico de um item com mínimo, máximo e média. |
| `list_triggers`, `list_problems` | Alertas configurados e problemas ativos, por host, grupo e severidade. |
| `list_maintenances` | Janelas de manutenção (agendada, ativa ou expirada). |

**Alteração**

| Tool | O que faz |
| --- | --- |
| `create_host_group` | Cria um grupo (idempotente). |
| `create_host` | Cria host com interface (agent, SNMP, IPMI, JMX), grupos, templates, tags e macros. Cria os grupos que faltam e desfaz se a criação falhar. |
| `update_host` | Nome, descrição, habilitar/desabilitar, grupos, templates (com opção de limpar os dados), tags e endereço. |
| `set_host_macro` | Cria ou atualiza uma macro, inclusive secreta. |
| `create_item`, `create_trigger` | Item (agent, agent ativo, trapper, simple check, internal) e trigger com expressão do Zabbix 6+. |
| `acknowledge_problem` | Reconhece ou desfaz o reconhecimento, com comentário e mudança de severidade. |
| `create_maintenance` | Janela de manutenção única para hosts e/ou grupos. |
| `delete_host`, `delete_maintenance` | Apagam, e só com `confirm=true`. |
| `api_call` | Qualquer outro método da API. Fora do `.get`, respeita o modo somente leitura. |

Também há o Resource `zabbix://guide` e os Prompts `diagnose_host` e `triage_problems`.

Exemplos de pedidos: *"quais hosts estão indisponíveis?"*, *"crie o host app-prod-03
(10.20.1.33) no grupo Linux servers/Web com o template Linux by Zabbix agent"*,
*"coloque os hosts do grupo Network em manutenção por 2 horas"*.

## Segurança

- Credenciais ficam no `.env` (fora do git, permissão 600) ou no ambiente. Os valores
  de macros secretas nunca são devolvidos.
- Fora de um laboratório, use `ZABBIX_API_TOKEN` com um usuário de permissão mínima
  e `ZABBIX_READ_ONLY=true` quando só precisar consultar. O usuário e a senha de
  fábrica do `.env.example` servem apenas para o laboratório local.
- Os apagamentos exigem `confirm=true`; peça confirmação ao usuário antes de usar.
- O modo HTTP **não tem autenticação** e expõe as Tools de escrita. Por isso escuta só em
  `127.0.0.1` por padrão; ao expor em outra interface, ative `ZABBIX_READ_ONLY=true`
  ou coloque um proxy com autenticação na frente.

## Desenvolvimento

```bash
.venv/bin/pip install pytest anyio
.venv/bin/python -m pytest                      # testes offline (Zabbix simulado em memória)
.venv/bin/python scripts/smoke_test.py          # ponta a ponta, contra o Zabbix real
.venv/bin/python scripts/smoke_test.py --ack    # idem, e reconhece/desfaz um problema existente
```

Estrutura: `src/mcp_zabbix/` (`config.py`, `client.py` com login/sessão, `server.py`
com as Tools, `__main__.py` com o transporte), `tests/` e `scripts/smoke_test.py`.
