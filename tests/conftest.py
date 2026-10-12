"""Fixtures compartilhadas. Nenhum teste deste diretório acessa um Zabbix real.

`FakeZabbix` imita, em memória, o subconjunto da API JSON-RPC que as Tools usam,
incluindo a autenticação por sessão (login, expiração e `Bearer`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp import Client

from mcp_zabbix import server as server_module
from mcp_zabbix.client import ZabbixClient
from mcp_zabbix.config import Settings

SESSION = "session-token-1"


class FakeApiError(Exception):
    def __init__(self, data: str, message: str = "Invalid params.", code: int = -32602) -> None:
        self.error = {"code": code, "message": message, "data": data}


class FakeZabbix:
    """Zabbix em memória. `handle` é o handler do `httpx2.MockTransport`."""

    def __init__(self) -> None:
        self.groups: dict[str, str] = {"1": "Linux servers", "2": "Linux servers/Web"}
        self.templates: dict[str, dict[str, str]] = {
            "10": {"host": "Linux by Zabbix agent", "name": "Linux by Zabbix agent"},
            "11": {"host": "ICMP Ping", "name": "ICMP Ping"},
        }
        self.hosts: dict[str, dict[str, Any]] = {}
        self.macros: dict[str, dict[str, Any]] = {}
        self.items: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.problems: list[dict[str, Any]] = []
        self.event_hosts: dict[str, list[str]] = {}
        self.triggers: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.logins = 0
        self.logouts = 0
        self.valid_tokens: set[str] = set()
        self.api_token: str | None = None
        self._next = 100

    # --- infraestrutura ---------------------------------------------------

    def new_id(self) -> str:
        self._next += 1
        return str(self._next)

    def methods_called(self) -> list[str]:
        return [method for method, _ in self.calls]

    def expire_sessions(self) -> None:
        self.valid_tokens.clear()

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        method, params = body["method"], body.get("params")
        self.calls.append((method, params))

        if method not in ("apiinfo.version", "user.login"):
            bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
            allowed = bearer in self.valid_tokens or (self.api_token and bearer == self.api_token)
            if not allowed:
                return self._error(body, "Session terminated, re-login, please.")

        handler: Callable[[Any], Any] | None = getattr(self, "m_" + method.replace(".", "_"), None)
        if handler is None:
            return self._error(body, f'Incorrect method "{method}".', "Method not found.", -32601)
        try:
            result = handler(params if params is not None else {})
        except FakeApiError as exc:
            return httpx2.Response(200, json={"jsonrpc": "2.0", "error": exc.error, "id": body["id"]})
        return httpx2.Response(200, json={"jsonrpc": "2.0", "result": result, "id": body["id"]})

    @staticmethod
    def _error(body: dict[str, Any], data: str, message: str = "Invalid params.", code: int = -32602) -> httpx2.Response:
        error = {"code": code, "message": message, "data": data}
        return httpx2.Response(200, json={"jsonrpc": "2.0", "error": error, "id": body["id"]})

    # --- métodos da API ---------------------------------------------------

    def m_apiinfo_version(self, params: Any) -> str:
        return "7.4.0"

    def m_user_login(self, params: dict[str, Any]) -> str:
        if params.get("password") != "secret":
            raise FakeApiError("Incorrect user name or password or account is temporarily blocked.")
        self.logins += 1
        token = f"session-token-{self.logins}"
        self.valid_tokens.add(token)
        return token

    def m_user_logout(self, params: Any) -> bool:
        self.logouts += 1
        return True

    def m_hostgroup_get(self, p: dict[str, Any]) -> Any:
        rows = [{"groupid": i, "name": n} for i, n in self.groups.items()]
        if "filter" in p:
            wanted = p["filter"].get("name", [])
            rows = [r for r in rows if r["name"] in wanted]
        if "search" in p:
            text = p["search"]["name"].lower()
            rows = [r for r in rows if r["name"].lower().startswith(text)] if p.get("startSearch") else [
                r for r in rows if text in r["name"].lower()
            ]
        if p.get("countOutput"):
            return str(len(rows))
        if "selectHosts" in p:
            for r in rows:
                r["hosts"] = str(sum(1 for h in self.hosts.values() if r["groupid"] in h["groups"]))
        return rows[: p.get("limit", len(rows))]

    def m_hostgroup_create(self, p: dict[str, Any]) -> dict[str, list[str]]:
        if p["name"] in self.groups.values():
            raise FakeApiError(f'Host group "{p["name"]}" already exists.')
        group_id = self.new_id()
        self.groups[group_id] = p["name"]
        return {"groupids": [group_id]}

    def m_hostgroup_delete(self, p: list[str]) -> dict[str, list[str]]:
        for group_id in p:
            self.groups.pop(group_id, None)
        return {"groupids": p}

    def m_template_get(self, p: dict[str, Any]) -> Any:
        rows = [{"templateid": i, **t, "description": ""} for i, t in self.templates.items()]
        if "templateids" in p:
            rows = [r for r in rows if r["templateid"] in p["templateids"]]
        for key, values in p.get("filter", {}).items():
            rows = [r for r in rows if r[key] in values]
        if "search" in p:
            text = next(iter(p["search"].values())).lower()
            rows = [r for r in rows if text in r["name"].lower()]
        if p.get("countOutput"):
            return str(len(rows))
        return rows[: p.get("limit", len(rows))]

    def m_host_get(self, p: dict[str, Any]) -> Any:
        rows = list(self.hosts.values())
        if "hostids" in p:
            rows = [h for h in rows if h["hostid"] in p["hostids"]]
        for key, values in p.get("filter", {}).items():
            wanted = [str(v) for v in (values if isinstance(values, list) else [values])]
            rows = [h for h in rows if str(h[key]) in wanted]
        if "groupids" in p:
            rows = [h for h in rows if set(h["groups"]) & set(p["groupids"])]
        if "search" in p:
            texts = [t.lower() for t in p["search"].values()]
            rows = [h for h in rows if any(t in h["host"].lower() or t in h["name"].lower() for t in texts)]
        if p.get("countOutput"):
            return str(len(rows))
        rows = rows[: p.get("limit", len(rows))]
        return [self._host_view(h, p) for h in rows]

    def _host_view(self, host: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
        view = {
            "hostid": host["hostid"],
            "host": host["host"],
            "name": host["name"],
            "status": str(host["status"]),
            "maintenance_status": "0",
            "description": host.get("description", ""),
            "monitored_by": "0",
            "inventory_mode": "-1",
        }
        if "selectHostGroups" in p:
            view["hostgroups"] = [{"groupid": i, "name": self.groups[i]} for i in host["groups"]]
        if "selectParentTemplates" in p:
            view["parentTemplates"] = [{"templateid": i, "name": self.templates[i]["name"]} for i in host["templates"]]
        if "selectInterfaces" in p:
            view["interfaces"] = host["interfaces"]
        if "selectTags" in p:
            view["tags"] = host["tags"]
        if "selectMacros" in p:
            view["macros"] = [m for m in self.macros.values() if m["hostid"] == host["hostid"]]
        if "selectItems" in p:
            view["items"] = "0"
        if "selectTriggers" in p:
            view["triggers"] = "0"
        return view

    def m_host_create(self, p: dict[str, Any]) -> dict[str, list[str]]:
        if any(h["host"] == p["host"] for h in self.hosts.values()):
            raise FakeApiError(f'Host with the same name "{p["host"]}" already exists.')
        groups = [g["groupid"] for g in p.get("groups", [])]
        for group_id in groups:
            if group_id not in self.groups:
                raise FakeApiError('Invalid parameter "/1/groups/1": object does not exist.')
        host_id = self.new_id()
        interfaces = []
        for raw in p.get("interfaces", []):
            interfaces.append(
                {
                    "interfaceid": self.new_id(),
                    "type": str(raw["type"]),
                    "main": str(raw["main"]),
                    "useip": str(raw["useip"]),
                    "ip": raw["ip"],
                    "dns": raw["dns"],
                    "port": str(raw["port"]),
                    "available": "0",
                    "error": "",
                    "details": raw.get("details", []),
                }
            )
        self.hosts[host_id] = {
            "hostid": host_id,
            "host": p["host"],
            "name": p.get("name", p["host"]),
            "status": p.get("status", 0),
            "description": p.get("description", ""),
            "groups": groups,
            "templates": [t["templateid"] for t in p.get("templates", [])],
            "tags": [{"tag": t["tag"], "value": t["value"]} for t in p.get("tags", [])],
            "interfaces": interfaces,
        }
        for macro in p.get("macros", []):
            macro_id = self.new_id()
            self.macros[macro_id] = {
                "hostmacroid": macro_id, "hostid": host_id, "macro": macro["macro"],
                "value": macro["value"], "type": "0", "description": "",
            }
        return {"hostids": [host_id]}

    def m_host_update(self, p: dict[str, Any]) -> dict[str, list[str]]:
        host = self.hosts[p["hostid"]]
        for key in ("name", "description", "status", "tags"):
            if key in p:
                host[key] = p[key]
        if "groups" in p:
            host["groups"] = [g["groupid"] for g in p["groups"]]
        if "templates" in p:
            host["templates"] = [t["templateid"] for t in p["templates"]]
        return {"hostids": [p["hostid"]]}

    def m_host_delete(self, p: list[str]) -> dict[str, list[str]]:
        for host_id in p:
            self.hosts.pop(host_id, None)
        return {"hostids": p}

    def m_hostinterface_update(self, p: dict[str, Any]) -> dict[str, list[str]]:
        for host in self.hosts.values():
            for interface in host["interfaces"]:
                if interface["interfaceid"] == p["interfaceid"]:
                    interface.update({k: str(v) for k, v in p.items() if k != "interfaceid"})
        return {"interfaceids": [p["interfaceid"]]}

    def m_usermacro_get(self, p: dict[str, Any]) -> Any:
        rows = [m for m in self.macros.values() if m["hostid"] in p.get("hostids", [m["hostid"]])]
        for key, values in p.get("filter", {}).items():
            rows = [m for m in rows if m[key] in values]
        return rows

    def m_usermacro_create(self, p: dict[str, Any]) -> dict[str, list[str]]:
        macro_id = self.new_id()
        self.macros[macro_id] = {
            "hostmacroid": macro_id, "hostid": p["hostid"], "macro": p["macro"],
            "value": p["value"], "type": str(p.get("type", 0)), "description": p.get("description", ""),
        }
        return {"hostmacroids": [macro_id]}

    def m_usermacro_update(self, p: dict[str, Any]) -> dict[str, list[str]]:
        macro = self.macros[p["hostmacroid"]]
        macro.update({k: (str(v) if k == "type" else v) for k, v in p.items() if k != "hostmacroid"})
        return {"hostmacroids": [p["hostmacroid"]]}

    def m_problem_get(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(problem) for problem in self.problems]

    def m_event_get(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"eventid": event_id, "hosts": [{"host": name} for name in names]}
            for event_id, names in self.event_hosts.items()
            if event_id in p.get("eventids", [])
        ]

    def m_trigger_get(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(trigger) for trigger in self.triggers]

    def m_item_get(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.items]
        if "itemids" in p:
            rows = [i for i in rows if i["itemid"] in p["itemids"]]
        if "hostids" in p:
            rows = [i for i in rows if i["hostid"] in p["hostids"]]
        if "selectHosts" in p:
            for row in rows:
                row["hosts"] = [{"host": self.hosts[row["hostid"]]["host"]}]
        return rows[: p.get("limit", len(rows))]

    def m_history_get(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(point) for point in self.history][: p.get("limit", len(self.history))]


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Plugin anyio do pytest: rodar os testes assíncronos no asyncio."""
    return "asyncio"


@pytest.fixture
def fake() -> FakeZabbix:
    return FakeZabbix()


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"url": "http://zbx.test", "user": "Admin", "password": "secret"}
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def make_client(
    monkeypatch: pytest.MonkeyPatch, fake: FakeZabbix
) -> Callable[..., AbstractAsyncContextManager[Client]]:
    """Servidor MCP real (em memória), ligado ao Zabbix falso."""

    @asynccontextmanager
    async def _make(**settings_overrides: Any) -> AsyncIterator[Client]:
        settings = make_settings(**settings_overrides)
        monkeypatch.setattr(
            server_module,
            "create_client",
            lambda: ZabbixClient(settings, transport=httpx2.MockTransport(fake.handle)),
        )
        async with Client(server_module.mcp, raise_exceptions=True) as connected:
            yield connected

    return _make
