"""Tools vistas por um cliente MCP, ligadas a um Zabbix falso em memória.

O `Client` do SDK conecta-se ao objeto do servidor em memória: o protocolo é o
mesmo, mas não há processo nem porta, e as chamadas HTTP vão para o `FakeZabbix`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent, TextResourceContents

from .conftest import FakeZabbix

pytestmark = pytest.mark.anyio

MakeClient = Callable[..., AbstractAsyncContextManager[Client]]

READ_TOOLS = {
    "get_overview", "list_host_groups", "list_templates", "list_hosts", "get_host",
    "list_items", "get_item_history", "list_triggers", "list_problems", "list_maintenances",
}
WRITE_TOOLS = {
    "create_host_group", "create_host", "update_host", "set_host_macro", "delete_host",
    "create_item", "create_trigger", "acknowledge_problem", "create_maintenance",
    "delete_maintenance", "api_call",
}


def text_of(result: Any) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


def add_host(fake: FakeZabbix, host: str, *, ip: str | None = "10.0.0.1", groups: tuple[str, ...] = ("1",), **extra: Any) -> str:
    params: dict[str, Any] = {"host": host, "groups": [{"groupid": g} for g in groups], **extra}
    if ip:
        params["interfaces"] = [{"type": 1, "main": 1, "useip": 1, "ip": ip, "dns": "", "port": "10050"}]
    return fake.m_host_create(params)["hostids"][0]


# --- descoberta -----------------------------------------------------------


async def test_lists_every_tool_with_the_right_hints(make_client: MakeClient) -> None:
    async with make_client() as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name in READ_TOOLS:
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is True, name
    for name in WRITE_TOOLS:
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is False, name
    for name in ("delete_host", "delete_maintenance", "api_call"):
        assert tools[name].annotations.destructive_hint is True, name
    assert all(t.description for t in tools.values())


async def test_guide_resource_and_prompts(make_client: MakeClient) -> None:
    async with make_client() as client:
        guide = await client.read_resource("zabbix://guide")
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        diagnose = await client.get_prompt("diagnose_host", {"host": "web-01"})

    content = guide.contents[0]
    assert isinstance(content, TextResourceContents)
    assert "create_host" in content.text
    assert prompts == {"diagnose_host", "triage_problems"}
    assert "web-01" in diagnose.messages[0].content.text


# --- modo somente leitura -------------------------------------------------

WRITE_CALLS = [
    ("create_host_group", {"name": "x"}),
    ("create_host", {"host": "h", "groups": ["Linux servers"]}),
    ("update_host", {"host": "h", "enabled": False}),
    ("set_host_macro", {"host": "h", "macro": "A", "value": "1"}),
    ("delete_host", {"host": "h", "confirm": True}),
    ("create_item", {"host": "h", "name": "n", "key": "k"}),
    ("create_trigger", {"name": "t", "expression": "last(/h/k)>1"}),
    ("acknowledge_problem", {"event_id": "1"}),
    ("create_maintenance", {"name": "m", "hosts": ["h"]}),
    ("delete_maintenance", {"maintenance": "m", "confirm": True}),
    ("api_call", {"method": "host.delete", "params": ["1"]}),
]


@pytest.mark.parametrize(("tool", "args"), WRITE_CALLS, ids=[c[0] for c in WRITE_CALLS])
async def test_read_only_mode_refuses_every_write(
    make_client: MakeClient, fake: FakeZabbix, tool: str, args: dict[str, Any]
) -> None:
    add_host(fake, "h")
    before = len(fake.calls)

    async with make_client(read_only=True) as client:
        result = await client.call_tool(tool, args)

    assert result.is_error
    assert "somente leitura" in text_of(result)
    assert len(fake.calls) == before, "nenhuma chamada deveria chegar ao Zabbix"
    assert "h" in {x["host"] for x in fake.hosts.values()}


async def test_read_only_mode_still_reads(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "web-01")

    async with make_client(read_only=True) as client:
        listed = await client.call_tool("list_hosts", {})
        raw = await client.call_tool("api_call", {"method": "hostgroup.get", "params": {}})

    assert listed.structured_content["count"] == 1
    assert len(raw.structured_content["result"]) == 2


# --- criar host -----------------------------------------------------------


async def test_create_host_resolves_names_and_creates_missing_groups(
    make_client: MakeClient, fake: FakeZabbix
) -> None:
    async with make_client() as client:
        result = await client.call_tool(
            "create_host",
            {
                "host": "web-01",
                "groups": ["Linux servers", "Grupo Novo"],
                "ip": "10.0.0.5",
                "templates": ["Linux by Zabbix agent"],
                "tags": {"env": "prod"},
                "macros": {"SITE": "sp"},
                "visible_name": "Web 01",
            },
        )

    assert not result.is_error, text_of(result)
    data = result.structured_content
    assert data["created"] is True
    assert data["created_groups"] == ["Grupo Novo"]
    assert data["templates"] == ["Linux by Zabbix agent"]
    assert data["address"] == "10.0.0.5:10050"

    host = fake.hosts[data["hostid"]]
    assert host["name"] == "Web 01"
    assert host["templates"] == ["10"]
    assert [fake.groups[g] for g in host["groups"]] == ["Linux servers", "Grupo Novo"]
    assert host["tags"] == [{"tag": "env", "value": "prod"}]
    assert host["interfaces"][0]["ip"] == "10.0.0.5"
    assert [m["macro"] for m in fake.macros.values()] == ["{$SITE}"]


async def test_create_host_snmp_uses_the_snmp_defaults(make_client: MakeClient, fake: FakeZabbix) -> None:
    async with make_client() as client:
        result = await client.call_tool(
            "create_host",
            {"host": "sw-01", "groups": ["Linux servers"], "dns": "sw-01.lab", "interface_type": "snmp"},
        )

    assert not result.is_error, text_of(result)
    interface = fake.hosts[result.structured_content["hostid"]]["interfaces"][0]
    assert (interface["type"], interface["port"], interface["useip"]) == ("2", "161", "0")
    assert interface["details"]["community"] == "{$SNMP_COMMUNITY}"


async def test_create_host_without_address_has_no_interface(make_client: MakeClient, fake: FakeZabbix) -> None:
    async with make_client() as client:
        result = await client.call_tool("create_host", {"host": "trap-01", "groups": ["Linux servers"]})

    assert result.structured_content["address"] is None
    assert fake.hosts[result.structured_content["hostid"]]["interfaces"] == []


async def test_create_host_rejects_port_without_an_address(make_client: MakeClient) -> None:
    async with make_client() as client:
        result = await client.call_tool("create_host", {"host": "h", "groups": ["Linux servers"], "port": "1234"})

    assert result.is_error
    assert "junto com ip ou dns" in text_of(result)


async def test_unknown_template_fails_before_creating_anything(make_client: MakeClient, fake: FakeZabbix) -> None:
    async with make_client() as client:
        result = await client.call_tool(
            "create_host", {"host": "h", "groups": ["Grupo Novo"], "templates": ["Linux by Zabbix"]}
        )

    assert result.is_error
    assert "Templates não encontrados" in text_of(result)
    assert "Linux by Zabbix agent" in text_of(result), "deveria sugerir o template parecido"
    assert "Grupo Novo" not in fake.groups.values()
    assert not {"hostgroup.create", "host.create"} & set(fake.methods_called())


async def test_failed_host_creation_removes_the_groups_it_made(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "existente")

    async with make_client() as client:
        result = await client.call_tool("create_host", {"host": "existente", "groups": ["Grupo Novo"]})

    assert result.is_error
    assert "already exists" in text_of(result)
    assert "Grupo Novo" not in fake.groups.values()


async def test_create_host_can_refuse_to_create_groups(make_client: MakeClient, fake: FakeZabbix) -> None:
    async with make_client() as client:
        result = await client.call_tool(
            "create_host", {"host": "h", "groups": ["Grupo Novo"], "create_missing_groups": False}
        )

    assert result.is_error
    assert "inexistentes" in text_of(result)
    assert "Linux servers" in text_of(result)
    assert "hostgroup.create" not in fake.methods_called()


async def test_create_host_group_is_idempotent(make_client: MakeClient, fake: FakeZabbix) -> None:
    async with make_client() as client:
        first = await client.call_tool("create_host_group", {"name": "Banco de Dados"})
        second = await client.call_tool("create_host_group", {"name": "Banco de Dados"})

    assert first.structured_content["created"] is True
    assert second.structured_content["created"] is False
    assert first.structured_content["groupid"] == second.structured_content["groupid"]
    assert list(fake.groups.values()).count("Banco de Dados") == 1


# --- alterar host ---------------------------------------------------------


async def test_update_host_changes_only_what_was_asked(make_client: MakeClient, fake: FakeZabbix) -> None:
    host_id = add_host(fake, "web-01", tags=[{"tag": "a", "value": "1"}, {"tag": "keep", "value": "x"}])

    async with make_client() as client:
        result = await client.call_tool(
            "update_host",
            {
                "host": "web-01",
                "enabled": False,
                "add_groups": ["Linux servers/Web"],
                "add_templates": ["ICMP Ping"],
                "set_tags": {"b": "2"},
                "remove_tags": ["a"],
                "ip": "10.9.9.9",
            },
        )

    assert not result.is_error, text_of(result)
    host = fake.hosts[host_id]
    assert host["status"] == 1
    assert [fake.groups[g] for g in host["groups"]] == ["Linux servers", "Linux servers/Web"]
    assert host["templates"] == ["11"]
    assert host["tags"] == [{"tag": "keep", "value": "x"}, {"tag": "b", "value": "2"}]
    assert host["interfaces"][0]["ip"] == "10.9.9.9"
    assert set(result.structured_content["changed"]) == {"desabilitado", "grupos", "templates", "tags", "interface"}


async def test_update_host_can_remove_a_template_and_clear_its_data(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "web-01", templates=[{"templateid": "10"}, {"templateid": "11"}])

    async with make_client() as client:
        await client.call_tool(
            "update_host",
            {"host": "web-01", "remove_templates": ["ICMP Ping"], "clear_data_on_template_removal": True},
        )

    update = next(params for method, params in fake.calls if method == "host.update")
    assert update["templates"] == [{"templateid": "10"}]
    assert update["templates_clear"] == [{"templateid": "11"}]


async def test_update_host_never_leaves_a_host_without_groups(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "web-01")

    async with make_client() as client:
        result = await client.call_tool("update_host", {"host": "web-01", "remove_groups": ["Linux servers"]})

    assert result.is_error
    assert "ao menos um grupo" in text_of(result)
    assert "host.update" not in fake.methods_called()


async def test_update_host_without_changes_is_an_error(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "web-01")

    async with make_client() as client:
        result = await client.call_tool("update_host", {"host": "web-01"})

    assert result.is_error
    assert "Nenhuma alteração" in text_of(result)


async def test_set_host_macro_creates_then_updates(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "web-01")

    async with make_client() as client:
        created = await client.call_tool("set_host_macro", {"host": "web-01", "macro": "SITE", "value": "sp"})
        updated = await client.call_tool("set_host_macro", {"host": "web-01", "macro": "{$SITE}", "value": "rj"})

    assert created.structured_content["created"] is True
    assert updated.structured_content["updated"] is True
    assert [(m["macro"], m["value"]) for m in fake.macros.values()] == [("{$SITE}", "rj")]


async def test_secret_macro_values_are_never_returned(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "web-01")

    async with make_client() as client:
        await client.call_tool(
            "set_host_macro", {"host": "web-01", "macro": "DB_PASS", "value": "s3gredo", "secret": True}
        )
        detail = await client.call_tool("get_host", {"host": "web-01"})

    macro = detail.structured_content["macros"][0]
    assert macro["secret"] is True
    assert macro["value"] is None
    assert "s3gredo" not in text_of(detail)


# --- apagar ---------------------------------------------------------------


async def test_delete_host_needs_confirmation(make_client: MakeClient, fake: FakeZabbix) -> None:
    host_id = add_host(fake, "velho")

    async with make_client() as client:
        refused = await client.call_tool("delete_host", {"host": "velho"})
        assert refused.is_error
        assert "confirm=true" in text_of(refused)
        assert host_id in fake.hosts

        done = await client.call_tool("delete_host", {"host": "velho", "confirm": True})

    assert done.structured_content == {"deleted": True, "hostid": host_id, "host": "velho"}
    assert host_id not in fake.hosts


# --- consultas ------------------------------------------------------------


async def test_list_hosts_filters(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "web-01", ip="10.10.1.11", groups=("2",))
    add_host(fake, "web-02", ip="10.10.1.12", groups=("2",), status=1)
    add_host(fake, "db-01", ip="10.10.2.21", groups=("1",))

    async with make_client() as client:
        everything = await client.call_tool("list_hosts", {})
        enabled = await client.call_tool("list_hosts", {"status": "enabled"})
        disabled = await client.call_tool("list_hosts", {"status": "disabled"})
        by_name = await client.call_tool("list_hosts", {"search": "db"})
        by_ip = await client.call_tool("list_hosts", {"search": "10.10.1."})
        by_parent = await client.call_tool("list_hosts", {"group": "Linux servers"})
        by_child = await client.call_tool("list_hosts", {"group": "linux servers/web"})
        limited = await client.call_tool("list_hosts", {"limit": 2})

    names = lambda r: sorted(h["host"] for h in r.structured_content["items"])  # noqa: E731
    assert names(everything) == ["db-01", "web-01", "web-02"]
    assert names(enabled) == ["db-01", "web-01"]
    assert names(disabled) == ["web-02"]
    assert names(by_name) == ["db-01"]
    assert names(by_ip) == ["web-01", "web-02"]
    assert names(by_parent) == ["db-01", "web-01", "web-02"], "grupo pai inclui os filhos"
    assert names(by_child) == ["web-01", "web-02"]
    assert limited.structured_content["count"] == 2
    assert limited.structured_content["truncated"] is True


async def test_list_hosts_shows_a_readable_row(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "web-01", ip="10.10.1.11", groups=("2",), templates=[{"templateid": "10"}])

    async with make_client() as client:
        result = await client.call_tool("list_hosts", {})

    row = result.structured_content["items"][0]
    assert row["address"] == "10.10.1.11:10050"
    assert row["interface_type"] == "agent"
    assert row["availability"] == "unknown"
    assert row["groups"] == ["Linux servers/Web"]
    assert row["templates"] == ["Linux by Zabbix agent"]
    assert row["enabled"] is True


async def test_get_host_unknown_suggests_similar_names(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "web-prod-01")

    async with make_client() as client:
        result = await client.call_tool("get_host", {"host": "web-prod"})

    assert result.is_error
    assert "Host não encontrado" in text_of(result)
    assert "web-prod-01" in text_of(result)


async def test_get_host_accepts_id_technical_and_visible_name(make_client: MakeClient, fake: FakeZabbix) -> None:
    host_id = add_host(fake, "web-01", name="Servidor Web")

    async with make_client() as client:
        by_id = await client.call_tool("get_host", {"host": host_id})
        by_host = await client.call_tool("get_host", {"host": "web-01"})
        by_name = await client.call_tool("get_host", {"host": "Servidor Web"})

    assert by_id.structured_content["hostid"] == by_host.structured_content["hostid"] == by_name.structured_content["hostid"]


async def test_get_host_with_duplicate_visible_name_asks_for_the_id(make_client: MakeClient, fake: FakeZabbix) -> None:
    add_host(fake, "web-01", name="Mesmo Nome")
    add_host(fake, "web-02", name="Mesmo Nome")

    async with make_client() as client:
        result = await client.call_tool("get_host", {"host": "Mesmo Nome"})

    assert result.is_error
    assert "Use o hostid" in text_of(result)


async def test_overview_summarizes_the_environment(make_client: MakeClient, fake: FakeZabbix) -> None:
    host_id = add_host(fake, "web-01")
    fake.hosts[host_id]["interfaces"][0]["available"] = "2"
    add_host(fake, "db-01", status=1)
    fake.problems = [
        {"eventid": "1", "severity": "4", "acknowledged": "0"},
        {"eventid": "2", "severity": "4", "acknowledged": "1"},
        {"eventid": "3", "severity": "2", "acknowledged": "0"},
    ]

    async with make_client() as client:
        result = await client.call_tool("get_overview", {})

    data = result.structured_content
    assert data["zabbix_version"] == "7.4.0"
    assert data["hosts"] == {"total": 2, "enabled": 1, "disabled": 1}
    assert data["templates"] == 2
    assert data["unavailable_hosts"] == {"count": 1, "names": ["web-01"]}
    assert data["active_problems"]["total"] == 3
    assert data["active_problems"]["unacknowledged"] == 2
    assert data["active_problems"]["by_severity"]["high"] == 2
    assert data["active_problems"]["by_severity"]["warning"] == 1


async def test_list_problems_joins_hosts_and_translates_values(make_client: MakeClient, fake: FakeZabbix) -> None:
    started = int(time.time()) - 3 * 3600
    fake.problems = [
        {
            "eventid": "7", "name": "CPU alta", "severity": "4", "clock": str(started),
            "r_clock": "0", "acknowledged": "1", "suppressed": "0", "opdata": "", "tags": [],
        }
    ]
    fake.event_hosts = {"7": ["web-01"]}

    async with make_client() as client:
        result = await client.call_tool("list_problems", {})

    row = result.structured_content["items"][0]
    assert row["problem"] == "CPU alta"
    assert row["severity"] == "high"
    assert row["hosts"] == ["web-01"]
    assert row["acknowledged"] is True
    assert row["resolved"] is None
    assert row["duration"].startswith("3h")


async def test_item_history_includes_stats_for_numeric_items(make_client: MakeClient, fake: FakeZabbix) -> None:
    host_id = add_host(fake, "web-01")
    fake.items = [{"itemid": "55", "hostid": host_id, "name": "CPU", "key_": "cpu", "value_type": "0", "units": "%"}]
    now = int(time.time())
    fake.history = [
        {"clock": str(now - 10), "value": "30"},
        {"clock": str(now - 20), "value": "10"},
        {"clock": str(now - 30), "value": "20"},
    ]

    async with make_client() as client:
        result = await client.call_tool("get_item_history", {"item_id": "55"})

    data = result.structured_content
    assert data["host"] == "web-01"
    assert data["count"] == 3
    assert [p["value"] for p in data["points"]] == ["20", "10", "30"], "do mais antigo ao mais novo"
    assert data["stats"] == {"min": 10.0, "max": 30.0, "avg": 20.0}


async def test_item_history_for_unknown_item(make_client: MakeClient) -> None:
    async with make_client() as client:
        result = await client.call_tool("get_item_history", {"item_id": "999"})

    assert result.is_error
    assert "Item não encontrado" in text_of(result)


# --- api_call -------------------------------------------------------------


async def test_api_call_blocks_session_methods(make_client: MakeClient, fake: FakeZabbix) -> None:
    async with make_client() as client:
        result = await client.call_tool("api_call", {"method": "user.login", "params": {}})

    assert result.is_error
    assert "user.login" in text_of(result)
    assert fake.calls == []


async def test_api_call_refuses_oversized_results(make_client: MakeClient, fake: FakeZabbix) -> None:
    for number in range(2500):
        fake.templates[str(1000 + number)] = {"host": f"t{number}", "name": "x" * 60}

    async with make_client() as client:
        result = await client.call_tool("api_call", {"method": "template.get", "params": {}})

    assert result.is_error
    assert "grande demais" in text_of(result)


async def test_zabbix_errors_become_readable_tool_errors(make_client: MakeClient) -> None:
    async with make_client() as client:
        result = await client.call_tool("api_call", {"method": "host.nope", "params": {}})

    assert result.is_error
    assert "Incorrect method" in text_of(result)


async def test_login_failure_is_reported_not_raised(make_client: MakeClient) -> None:
    async with make_client(password="errada") as client:
        result = await client.call_tool("list_hosts", {})

    assert result.is_error
    assert "Login recusado" in text_of(result)
