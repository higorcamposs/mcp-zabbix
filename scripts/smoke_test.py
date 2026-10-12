#!/usr/bin/env python3
"""Smoke test de ponta a ponta: sobe o servidor por stdio e fala MCP de verdade.

Usa o cliente do SDK oficial para iniciar `python -m mcp_zabbix` e chama as Tools
contra o Zabbix configurado (ZABBIX_URL, credenciais no `.env`). Nenhuma função
Python do servidor é chamada diretamente: tudo passa pelo protocolo.

O teste cria objetos com o prefixo `smoke-` (grupo, host, item, trigger, macro e
manutenção) e apaga tudo no final, mesmo se uma verificação falhar.

Uso:
    python scripts/smoke_test.py [--ack]

--ack também reconhece e desfaz o reconhecimento de um problema que já exista
(deixa um registro no histórico do evento), por isso é opcional.
Sai com código 0 se tudo passar e 1 se alguma verificação falhar.
"""

from __future__ import annotations

import sys
import time
from typing import Any

import anyio
from mcp import Client, StdioServerParameters
from mcp.types import TextContent

failures: list[str] = []
SUFFIX = str(int(time.time()))
GROUP = f"smoke-group-{SUFFIX}"
HOST = f"smoke-host-{SUFFIX}"
MAINTENANCE = f"smoke-maint-{SUFFIX}"


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [ok]    {label}")
        return
    print(f"  [FALHA] {label}" + (f" — {detail}" if detail else ""))
    failures.append(label)


def text_of(result: Any) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


async def call(client: Client, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Chama a Tool e devolve o conteúdo estruturado ({} + erro registrado em caso de falha)."""
    result = await client.call_tool(tool, args or {})
    if result.is_error:
        check(f"{tool} {args or ''}".strip(), False, text_of(result)[:300])
        return {"_error": text_of(result)}
    return result.structured_content or {}


async def expect_error(client: Client, label: str, tool: str, args: dict[str, Any], needle: str) -> None:
    result = await client.call_tool(tool, args)
    check(label, result.is_error and needle.lower() in text_of(result).lower(), text_of(result)[:200])


async def run(with_ack: bool) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_zabbix"])
    async with Client(params) as client:
        print("1. Conexão")
        check("protocolo negociado", bool(client.protocol_version))
        info = client.server_info
        print(f"         servidor: {info.name} {info.version}" if info else "")

        tools = {t.name: t for t in (await client.list_tools()).tools}
        check("21 tools anunciadas", len(tools) == 21, str(len(tools)))
        check(
            "tools de escrita marcadas como não somente leitura",
            all(not tools[n].annotations.read_only_hint for n in ("create_host", "delete_host", "api_call")),
        )
        resources = await client.list_resources()
        check("resource zabbix://guide", any(str(r.uri) == "zabbix://guide" for r in resources.resources))
        prompts = await client.list_prompts()
        check("prompts diagnose_host e triage_problems", {"diagnose_host", "triage_problems"} <= {p.name for p in prompts.prompts})

        print("\n2. Consultas")
        overview = await call(client, "get_overview")
        check("versão 7.x do Zabbix", str(overview.get("zabbix_version", "")).startswith("7."), str(overview.get("zabbix_version")))
        check("hosts contados", overview.get("hosts", {}).get("total", 0) >= 1)
        print(f"         {overview.get('hosts')} | problemas: {overview.get('active_problems', {}).get('total')}")

        groups = await call(client, "list_host_groups", {"search": "Linux"})
        check("list_host_groups encontra 'Linux servers'", any("Linux servers" in g["name"] for g in groups.get("items", [])))
        templates = await call(client, "list_templates", {"search": "Linux by Zabbix agent"})
        check("list_templates encontra o template Linux", any(t["host"] == "Linux by Zabbix agent" for t in templates.get("items", [])))
        hosts = await call(client, "list_hosts", {"limit": 50})
        check("list_hosts devolve hosts", hosts.get("count", 0) >= 1, str(hosts.get("count")))
        by_group = await call(client, "list_hosts", {"group": "Linux servers"})
        check("list_hosts por grupo inclui subgrupos", by_group.get("count", 0) >= 1)
        by_ip = await call(client, "list_hosts", {"search": "10.10.1."})
        check("list_hosts por prefixo de IP", by_ip.get("count", 0) >= 1, str(by_ip.get("count")))
        with_problems = await call(client, "list_hosts", {"only_with_problems": True})
        check("list_hosts only_with_problems", "items" in with_problems)

        listed = hosts.get("items", [])
        sample = next((h["host"] for h in listed if h["host"] == "web-prod-01"), listed[0]["host"] if listed else None)
        if sample:
            detail = await call(client, "get_host", {"host": sample})
            check(f"get_host {sample}", detail.get("host") == sample and "interfaces" in detail)
            items = await call(client, "list_items", {"host": sample, "limit": 5})
            check("list_items", items.get("count", 0) >= 1)
            if items.get("items"):
                history = await call(client, "get_item_history", {"item_id": items["items"][0]["itemid"], "hours": 1})
                check("get_item_history", "points" in history)
            triggers = await call(client, "list_triggers", {"host": sample, "limit": 5})
            check("list_triggers", "items" in triggers)
        problems = await call(client, "list_problems", {"limit": 10})
        check("list_problems", "items" in problems)
        check("list_maintenances", "items" in await call(client, "list_maintenances"))

        await expect_error(client, "host inexistente dá erro útil", "get_host", {"host": "nao-existe-xyz"}, "não encontrado")
        await expect_error(client, "template inexistente dá erro útil", "create_host", {"host": HOST, "groups": [GROUP], "templates": ["Template Que Nao Existe"]}, "Templates não encontrados")
        leaked = await call(client, "list_host_groups", {"search": GROUP})
        check("erro de template não deixa grupo criado", leaked.get("count") == 0, str(leaked.get("items")))
        await expect_error(client, "grupo inexistente com create_missing_groups=false", "create_host", {"host": HOST, "groups": [GROUP], "create_missing_groups": False}, "inexistentes")

        print("\n3. Escrita (cria, altera e apaga objetos de teste)")
        try:
            created_group = await call(client, "create_host_group", {"name": GROUP})
            check("create_host_group", created_group.get("created") is True)
            again = await call(client, "create_host_group", {"name": GROUP})
            check("create_host_group é idempotente", again.get("created") is False and again.get("groupid") == created_group.get("groupid"))

            created = await call(
                client,
                "create_host",
                {
                    "host": HOST,
                    "groups": [GROUP],
                    "ip": "10.99.99.9",
                    "templates": ["Linux by Zabbix agent"],
                    "tags": {"env": "smoke"},
                    "macros": {"{$SMOKE_MACRO}": "um"},
                    "description": "criado pelo smoke test",
                },
            )
            check("create_host", created.get("created") is True and created.get("hostid"))
            await expect_error(client, "create_host duplicado é recusado", "create_host", {"host": HOST, "groups": [GROUP]}, "already exists")
            await expect_error(client, "create_host duplicado com grupo novo", "create_host", {"host": HOST, "groups": [f"{GROUP}-extra"]}, "already exists")
            leaked = await call(client, "list_host_groups", {"search": f"{GROUP}-extra"})
            check("falha no host.create desfaz o grupo novo", leaked.get("count") == 0, str(leaked.get("items")))

            detail = await call(client, "get_host", {"host": HOST})
            check("host criado tem grupo, template, tag e macro", (
                detail.get("groups") == [GROUP]
                and detail.get("templates") == ["Linux by Zabbix agent"]
                and {"tag": "env", "value": "smoke"} in detail.get("tags", [])
                and any(m["macro"] == "{$SMOKE_MACRO}" for m in detail.get("macros", []))
            ), str({k: detail.get(k) for k in ("groups", "templates", "tags")}))

            updated = await call(client, "update_host", {"host": HOST, "visible_name": "Smoke Host", "set_tags": {"owner": "mcp"}, "enabled": False})
            check("update_host", updated.get("updated") is True, str(updated))
            detail = await call(client, "get_host", {"host": HOST})
            check("update_host aplicou nome, tag e estado", (
                detail.get("name") == "Smoke Host"
                and detail.get("enabled") is False
                and {"tag": "owner", "value": "mcp"} in detail.get("tags", [])
                and {"tag": "env", "value": "smoke"} in detail.get("tags", [])
            ))
            moved = await call(client, "update_host", {"host": HOST, "ip": "10.99.99.10", "remove_templates": ["Linux by Zabbix agent"], "clear_data_on_template_removal": True})
            check("update_host troca IP e desvincula template", moved.get("updated") is True, str(moved))
            detail = await call(client, "get_host", {"host": HOST})
            check("interface e templates atualizados", detail.get("address") == "10.99.99.10:10050" and detail.get("templates") == [], str(detail.get("address")))

            await expect_error(client, "update_host sem mudança é recusado", "update_host", {"host": HOST}, "Nenhuma alteração")
            await expect_error(client, "não deixa o host sem grupo", "update_host", {"host": HOST, "remove_groups": [GROUP]}, "ao menos um grupo")

            macro = await call(client, "set_host_macro", {"host": HOST, "macro": "SMOKE_SECRET", "value": "s3gredo", "secret": True})
            check("set_host_macro cria", macro.get("created") is True)
            macro = await call(client, "set_host_macro", {"host": HOST, "macro": "{$SMOKE_MACRO}", "value": "dois"})
            check("set_host_macro atualiza", macro.get("updated") is True)
            detail = await call(client, "get_host", {"host": HOST})
            secret = next((m for m in detail.get("macros", []) if m["macro"] == "{$SMOKE_SECRET}"), {})
            check("macro secreta não vaza o valor", secret.get("secret") is True and secret.get("value") is None)

            item = await call(client, "create_item", {"host": HOST, "name": "Smoke trapper", "key": "smoke.trap", "item_type": "trapper", "value_type": "unsigned"})
            check("create_item trapper", item.get("created") is True)
            item2 = await call(client, "create_item", {"host": HOST, "name": "Smoke agent", "key": "smoke.agent", "item_type": "agent", "value_type": "float", "interval": "30s", "units": "%"})
            check("create_item agent usa a interface principal", item2.get("created") is True, str(item2))
            listed_items = await call(client, "list_items", {"host": HOST, "search": "smoke"})
            check("list_items acha os itens criados", listed_items.get("count") == 2, str(listed_items.get("count")))

            trigger = await call(client, "create_trigger", {"name": "Smoke trigger", "expression": f"last(/{HOST}/smoke.trap)>10", "severity": "high"})
            check("create_trigger", trigger.get("created") is True, str(trigger))
            triggers = await call(client, "list_triggers", {"host": HOST, "include_disabled": True})
            check("list_triggers vê a trigger criada", any(t["name"] == "Smoke trigger" and t["severity"] == "high" for t in triggers.get("items", [])))

            maint = await call(client, "create_maintenance", {"name": MAINTENANCE, "hosts": [HOST], "groups": [GROUP], "duration_minutes": 30, "start_in_minutes": 5, "description": "smoke"})
            check("create_maintenance", maint.get("created") is True, str(maint))
            maintenances = await call(client, "list_maintenances", {"search": MAINTENANCE})
            row = next(iter(maintenances.get("items", [])), {})
            check("list_maintenances mostra agendada com host e grupo", row.get("state") == "scheduled" and row.get("hosts") == [HOST] and row.get("groups") == [GROUP], str(row))
            await expect_error(client, "delete_maintenance sem confirm é recusado", "delete_maintenance", {"maintenance": MAINTENANCE}, "confirm=true")

            await expect_error(client, "delete_host sem confirm é recusado", "delete_host", {"host": HOST}, "confirm=true")

            raw = await call(client, "api_call", {"method": "hostgroup.get", "params": {"filter": {"name": [GROUP]}, "output": ["groupid"]}})
            check("api_call .get", len(raw.get("result", [])) == 1)
            await expect_error(client, "api_call recusa user.login", "api_call", {"method": "user.login", "params": {}}, "não são permitidos")

            if with_ack and problems.get("items"):
                eventid = problems["items"][0]["eventid"]
                ack = await call(client, "acknowledge_problem", {"event_id": eventid, "message": "smoke test"})
                check("acknowledge_problem", ack.get("acknowledged") is True)
                unack = await call(client, "acknowledge_problem", {"event_id": eventid, "unacknowledge": True, "message": "smoke test: desfeito"})
                check("acknowledge_problem desfaz", unack.get("acknowledged") is False)
        finally:
            print("\n4. Limpeza")
            maintenances = await call(client, "list_maintenances", {"search": MAINTENANCE})
            for m in maintenances.get("items", []):
                done = await call(client, "delete_maintenance", {"maintenance": m["maintenanceid"], "confirm": True})
                check(f"delete_maintenance {m['name']}", done.get("deleted") is True)
            hosts_left = await call(client, "list_hosts", {"search": HOST})
            for h in hosts_left.get("items", []):
                done = await call(client, "delete_host", {"host": h["hostid"], "confirm": True})
                check(f"delete_host {h['host']}", done.get("deleted") is True)
            leftover = await call(client, "api_call", {"method": "hostgroup.get", "params": {"filter": {"name": [GROUP]}, "output": ["groupid"]}})
            for g in leftover.get("result", []):
                await call(client, "api_call", {"method": "hostgroup.delete", "params": [g["groupid"]]})
            final = await call(client, "api_call", {"method": "hostgroup.get", "params": {"filter": {"name": [GROUP]}, "output": ["groupid"]}})
            check("nada de teste sobrou", final.get("result") == [])
            await expect_error(client, "host apagado não existe mais", "get_host", {"host": HOST}, "não encontrado")


def main() -> int:
    anyio.run(run, "--ack" in sys.argv)
    print()
    if failures:
        print(f"{len(failures)} verificação(ões) falharam:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("Tudo certo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
