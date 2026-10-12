"""MCP Server que consulta e gerencia um Zabbix pela API JSON-RPC.

O servidor não contém nenhum LLM: ele traduz chamadas MCP em chamadas à API do
Zabbix e devolve o resultado resumido. Quem decide o que fazer é a aplicação de
IA do outro lado da conexão.

Convenções das Tools:

* Consultas (`list_*`, `get_*`) são somente leitura.
* Tools que criam ou alteram recusam a chamada com `ZABBIX_READ_ONLY=true`.
* Tools que apagam exigem `confirm=true` e só aceitam um alvo exato.
* A API do Zabbix devolve números como texto ("1"); aqui eles viram valores
  legíveis (`enabled`, `high`, `available`) e os horários viram ISO 8601 em UTC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts.base import Message, UserMessage
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .client import ZabbixClient, ZabbixError
from .config import Settings

logger = logging.getLogger("mcp_zabbix.server")
# O httpx2 registra uma linha INFO por requisição; com dezenas de chamadas por
# Tool isso só polui o stderr do cliente MCP.
logging.getLogger("httpx2").setLevel(logging.WARNING)

VERSION: Final = "0.1.0"

# --- vocabulário da API -----------------------------------------------------

SEVERITIES: Final = {
    0: "not_classified",
    1: "information",
    2: "warning",
    3: "average",
    4: "high",
    5: "disaster",
}
SEVERITY_IDS: Final = {name: number for number, name in SEVERITIES.items()}
Severity = Literal["not_classified", "information", "warning", "average", "high", "disaster"]

INTERFACE_TYPES: Final = {"1": "agent", "2": "snmp", "3": "ipmi", "4": "jmx"}
INTERFACE_IDS: Final = {name: int(number) for number, name in INTERFACE_TYPES.items()}
DEFAULT_PORTS: Final = {"agent": "10050", "snmp": "161", "ipmi": "623", "jmx": "12345"}
AVAILABILITY: Final = {"0": "unknown", "1": "available", "2": "unavailable"}
INVENTORY_MODES: Final = {"-1": "disabled", "0": "manual", "1": "automatic"}
MONITORED_BY: Final = {"0": "server", "1": "proxy", "2": "proxy_group"}

ITEM_TYPES: Final = {
    "agent": 0,
    "trapper": 2,
    "simple_check": 3,
    "internal": 5,
    "agent_active": 7,
}
ItemType = Literal["agent", "agent_active", "trapper", "simple_check", "internal"]
VALUE_TYPES: Final = {"float": 0, "char": 1, "log": 2, "unsigned": 3, "text": 4}
ValueType = Literal["float", "unsigned", "char", "text", "log"]
VALUE_TYPE_NAMES: Final = {str(number): name for name, number in VALUE_TYPES.items()}

# Bits do parâmetro `action` de event.acknowledge.
ACK_ACTIONS: Final = {"acknowledge": 2, "message": 4, "severity": 8, "unacknowledge": 16}

MAX_RESULT_CHARS: Final = 100_000

# --- tipos de argumento -----------------------------------------------------

HostRef = Annotated[
    str,
    Field(
        description="Host: nome técnico (campo 'host'), nome visível ou hostid numérico.",
        min_length=1,
        max_length=255,
    ),
]
Limit = Annotated[int, Field(description="Máximo de itens devolvidos.", ge=1, le=500)]
Search = Annotated[
    str | None,
    Field(description="Texto a procurar (sem diferenciar maiúsculas de minúsculas)."),
]

READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
WRITE: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
IDEMPOTENT_WRITE: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DESTRUCTIVE: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)


# --- ciclo de vida ----------------------------------------------------------


@dataclass
class AppContext:
    """Objeto compartilhado por todos os handlers durante a vida do servidor."""

    zabbix: ZabbixClient


def create_client() -> ZabbixClient:
    """Monta o cliente do Zabbix a partir das variáveis de ambiente.

    Os testes substituem esta função para injetar um transporte simulado, o que
    mantém a suíte automatizada sem nenhuma conexão real.
    """
    return ZabbixClient(Settings.from_env())


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    """Cria o cliente HTTP na inicialização e o fecha no desligamento."""
    client = create_client()
    logger.info(
        "cliente do Zabbix pronto (endpoint=%s, somente_leitura=%s)",
        client.endpoint,
        client.settings.read_only,
    )
    try:
        yield AppContext(zabbix=client)
    finally:
        await client.aclose()
        logger.info("cliente do Zabbix encerrado")


INSTRUCTIONS: Final = (
    "Servidor MCP para consultar e gerenciar um Zabbix. Para investigar, comece por "
    "get_overview; depois use list_hosts, get_host, list_problems, list_triggers, "
    "list_items e get_item_history. Para mudar algo use create_host, update_host, "
    "set_host_macro, create_item, create_trigger, acknowledge_problem e "
    "create_maintenance. delete_host e delete_maintenance exigem confirm=true: peça "
    "confirmação ao usuário antes. api_call é a saída de emergência para qualquer "
    "outro método da API. Antes de criar um host, confira os grupos (list_host_groups) "
    "e os templates (list_templates) com os nomes exatos."
)

mcp = MCPServer(
    "Zabbix MCP Server",
    version=VERSION,
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
    log_level=os.getenv("MCP_LOG_LEVEL", "INFO"),  # type: ignore[arg-type]
)

Ctx = Context[AppContext]


# --- acesso à API -----------------------------------------------------------


def _zabbix(ctx: Ctx) -> ZabbixClient:
    return ctx.request_context.lifespan_context.zabbix


async def _api(ctx: Ctx, method: str, params: Any = None) -> Any:
    """Chama a API e traduz qualquer falha para um erro legível pelo modelo."""
    try:
        return await _zabbix(ctx).call(method, params)
    except ZabbixError as exc:
        raise ToolError(str(exc)) from exc


def _ensure_writable(ctx: Ctx) -> None:
    if _zabbix(ctx).settings.read_only:
        raise ToolError(
            "O servidor está em modo somente leitura (ZABBIX_READ_ONLY=true); "
            "esta operação altera o Zabbix e foi recusada."
        )


def _require_confirm(confirm: bool, what: str) -> None:
    if not confirm:
        raise ToolError(
            f"Operação destrutiva: {what}. Confirme com o usuário e chame de novo com confirm=true."
        )


# --- formatação -------------------------------------------------------------


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(timestamp: Any) -> str | None:
    seconds = _int(timestamp)
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat().replace("+00:00", "Z")


def _human_duration(seconds: int) -> str:
    seconds = max(seconds, 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


def _unique(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in (values or []) if v and v.strip()))


def _tag_list(tags: Any) -> list[dict[str, str]]:
    return [{"tag": t.get("tag", ""), "value": t.get("value", "")} for t in tags or []]


def _interface_view(interface: dict[str, Any]) -> dict[str, Any]:
    return {
        "interfaceid": interface.get("interfaceid"),
        "type": INTERFACE_TYPES.get(str(interface.get("type")), str(interface.get("type"))),
        "main": interface.get("main") == "1",
        "ip": interface.get("ip") or None,
        "dns": interface.get("dns") or None,
        "use_ip": interface.get("useip") == "1",
        "port": interface.get("port"),
        "availability": AVAILABILITY.get(str(interface.get("available")), "unknown"),
        "error": interface.get("error") or None,
    }


def _main_interface(host: dict[str, Any]) -> dict[str, Any] | None:
    interfaces = host.get("interfaces") or []
    return next((i for i in interfaces if i.get("main") == "1"), interfaces[0] if interfaces else None)


def _host_row(host: dict[str, Any]) -> dict[str, Any]:
    main = _main_interface(host)
    view = _interface_view(main) if main else None
    address = None
    if view:
        target = view["ip"] if view["use_ip"] else view["dns"]
        address = f"{target}:{view['port']}" if target else None
    return {
        "hostid": host["hostid"],
        "host": host["host"],
        "name": host.get("name", host["host"]),
        "enabled": host.get("status") == "0",
        "in_maintenance": host.get("maintenance_status") == "1",
        "groups": [g["name"] for g in host.get("hostgroups", [])],
        "templates": [t["name"] for t in host.get("parentTemplates", [])],
        "address": address,
        "interface_type": view["type"] if view else None,
        "availability": view["availability"] if view else None,
        "tags": _tag_list(host.get("tags")),
    }


HOST_LIST_PARAMS: Final = {
    "output": ["hostid", "host", "name", "status", "maintenance_status"],
    "selectHostGroups": ["groupid", "name"],
    "selectParentTemplates": ["templateid", "name"],
    "selectInterfaces": ["interfaceid", "type", "main", "useip", "ip", "dns", "port", "available", "error"],
    "selectTags": ["tag", "value"],
    "sortfield": "name",
}


def _problem_row(problem: dict[str, Any], hosts: dict[str, list[str]], now: int) -> dict[str, Any]:
    started = _int(problem.get("clock"))
    resolved_at = _int(problem.get("r_clock"))
    end = resolved_at or now
    return {
        "eventid": problem["eventid"],
        "problem": problem.get("name"),
        "severity": SEVERITIES.get(_int(problem.get("severity")), "unknown"),
        "hosts": hosts.get(problem["eventid"], []),
        "started": _iso(started),
        "resolved": _iso(resolved_at),
        "duration": _human_duration(end - started),
        "acknowledged": problem.get("acknowledged") == "1",
        "suppressed": problem.get("suppressed") == "1",
        "operational_data": problem.get("opdata") or None,
        "tags": _tag_list(problem.get("tags")),
    }


# --- resolução de nomes em ids -----------------------------------------------


async def _find_host(ctx: Ctx, ref: str, **extra: Any) -> dict[str, Any]:
    """Resolve um host por hostid, nome técnico ou nome visível."""
    ref = ref.strip()
    if not ref:
        raise ToolError("Informe o host (nome técnico, nome visível ou hostid).")

    base: dict[str, Any] = {"output": ["hostid", "host", "name", "status"]}
    base.update(extra)

    found: list[dict[str, Any]] = []
    if ref.isdigit():
        found = await _api(ctx, "host.get", {**base, "hostids": [ref]})
    if not found:
        found = await _api(ctx, "host.get", {**base, "filter": {"host": [ref]}})
    if not found:
        found = await _api(ctx, "host.get", {**base, "filter": {"name": [ref]}})

    if not found:
        similar = await _api(
            ctx,
            "host.get",
            {"output": ["host"], "search": {"host": ref, "name": ref}, "searchByAny": True, "limit": 5},
        )
        hint = f" Parecidos: {', '.join(h['host'] for h in similar)}." if similar else ""
        raise ToolError(f"Host não encontrado: {ref!r}.{hint}")
    if len(found) > 1:
        ids = ", ".join(f"{h['host']} (hostid {h['hostid']})" for h in found)
        raise ToolError(f"Mais de um host com o nome {ref!r}: {ids}. Use o hostid.")
    return found[0]


async def _group_ids(ctx: Ctx, group: str) -> list[str]:
    """Ids do grupo e de seus subgrupos (nomes no formato 'Pai/Filho')."""
    wanted = group.strip().lower()
    groups = await _api(
        ctx,
        "hostgroup.get",
        {"output": ["groupid", "name"], "search": {"name": group.strip()}, "startSearch": True},
    )
    ids = [
        g["groupid"]
        for g in groups
        if g["name"].lower() == wanted or g["name"].lower().startswith(wanted + "/")
    ]
    if not ids:
        sample = await _api(ctx, "hostgroup.get", {"output": ["name"], "sortfield": "name", "limit": 30})
        names = ", ".join(g["name"] for g in sample)
        raise ToolError(f"Grupo de hosts não encontrado: {group!r}. Existentes: {names}.")
    return ids


async def _lookup_groups(ctx: Ctx, names: list[str]) -> tuple[dict[str, str], list[str]]:
    """Procura grupos pelo nome exato. Devolve ({nome: groupid}, [nomes que faltam]). Não cria nada."""
    wanted = _unique(names)
    if not wanted:
        raise ToolError("Informe ao menos um grupo de hosts.")
    found = await _api(ctx, "hostgroup.get", {"output": ["groupid", "name"], "filter": {"name": wanted}})
    by_name = {g["name"]: g["groupid"] for g in found}
    return by_name, [n for n in wanted if n not in by_name]


async def _resolve_groups(ctx: Ctx, names: list[str]) -> list[dict[str, str]]:
    """Resolve nomes de grupos em ids; falha, sem criar nada, se algum não existir."""
    by_name, missing = await _lookup_groups(ctx, names)
    if missing:
        sample = await _api(ctx, "hostgroup.get", {"output": ["name"], "sortfield": "name", "limit": 30})
        existing = ", ".join(g["name"] for g in sample)
        raise ToolError(f"Grupos de hosts inexistentes: {missing}. Existentes: {existing}.")
    return [{"groupid": by_name[n]} for n in _unique(names)]


async def _resolve_templates(ctx: Ctx, refs: list[str]) -> list[dict[str, str]]:
    """Resolve templates por templateid, nome técnico ou nome visível."""
    wanted = _unique(refs)
    resolved: dict[str, dict[str, str]] = {}

    ids = [r for r in wanted if r.isdigit()]
    names = [r for r in wanted if not r.isdigit()]
    fields = ["templateid", "host", "name"]
    if ids:
        for t in await _api(ctx, "template.get", {"output": fields, "templateids": ids}):
            resolved[t["templateid"]] = t
    if names:
        for key in ("host", "name"):
            pending = [n for n in names if not any(t[key] == n for t in resolved.values())]
            if not pending:
                break
            for t in await _api(ctx, "template.get", {"output": fields, "filter": {key: pending}}):
                resolved[t["templateid"]] = t

    matched = {t["templateid"] for t in resolved.values()} | {t["host"] for t in resolved.values()}
    matched |= {t["name"] for t in resolved.values()}
    missing = [r for r in wanted if r not in matched]
    if missing:
        hints: list[str] = []
        for ref in missing:
            similar = await _api(
                ctx,
                "template.get",
                {"output": ["name"], "search": {"name": ref}, "limit": 5},
            )
            if similar:
                hints.append(f"{ref!r} -> {', '.join(t['name'] for t in similar)}")
        extra = f" Sugestões: {'; '.join(hints)}." if hints else " Use list_templates para buscar."
        raise ToolError(f"Templates não encontrados: {missing}.{extra}")
    return [{"templateid": t["templateid"], "name": t["name"]} for t in resolved.values()]


def _macro_name(macro: str) -> str:
    """Aceita 'NOME' ou '{$NOME}' e devolve '{$NOME}'."""
    macro = macro.strip()
    if not macro.startswith("{$"):
        macro = "{$" + macro.removeprefix("$")
    if not macro.endswith("}"):
        macro += "}"
    return macro


# --- consultas --------------------------------------------------------------


@mcp.tool(title="Visão geral do Zabbix", annotations=READ_ONLY)
async def get_overview(ctx: Ctx) -> dict[str, Any]:
    """Resume o ambiente: versão, quantidade de hosts, grupos e templates, hosts
    indisponíveis e problemas ativos por severidade. Bom ponto de partida."""
    version, total, enabled, groups, templates, problems, hosts = await asyncio.gather(
        _zabbix(ctx).version(),
        _api(ctx, "host.get", {"countOutput": True}),
        _api(ctx, "host.get", {"countOutput": True, "filter": {"status": 0}}),
        _api(ctx, "hostgroup.get", {"countOutput": True}),
        _api(ctx, "template.get", {"countOutput": True}),
        _api(ctx, "problem.get", {"output": ["severity", "acknowledged"], "limit": 10000}),
        _api(
            ctx,
            "host.get",
            {
                "output": ["host"],
                "filter": {"status": 0},
                "selectInterfaces": ["main", "available"],
                "limit": 5000,
            },
        ),
    )
    unavailable = sorted(
        h["host"]
        for h in hosts
        if any(i.get("main") == "1" and i.get("available") == "2" for i in h.get("interfaces", []))
    )
    by_severity = Counter(SEVERITIES.get(_int(p["severity"]), "unknown") for p in problems)
    return {
        "zabbix_version": version,
        "endpoint": _zabbix(ctx).endpoint,
        "read_only": _zabbix(ctx).settings.read_only,
        "hosts": {"total": _int(total), "enabled": _int(enabled), "disabled": _int(total) - _int(enabled)},
        "host_groups": _int(groups),
        "templates": _int(templates),
        "unavailable_hosts": {"count": len(unavailable), "names": unavailable[:50]},
        "active_problems": {
            "total": len(problems),
            "unacknowledged": sum(1 for p in problems if p.get("acknowledged") == "0"),
            "by_severity": {name: by_severity.get(name, 0) for name in reversed(list(SEVERITIES.values()))},
        },
    }


@mcp.tool(title="Listar grupos de hosts", annotations=READ_ONLY)
async def list_host_groups(ctx: Ctx, search: Search = None, limit: Limit = 100) -> dict[str, Any]:
    """Lista os grupos de hosts, com a quantidade de hosts em cada um. Os nomes
    podem ter hierarquia no formato 'Pai/Filho'."""
    params: dict[str, Any] = {
        "output": ["groupid", "name"],
        "selectHosts": "count",
        "sortfield": "name",
        "limit": limit,
    }
    if search:
        params["search"] = {"name": search}
    groups = await _api(ctx, "hostgroup.get", params)
    items = [{"groupid": g["groupid"], "name": g["name"], "hosts": _int(g.get("hosts"))} for g in groups]
    return {"count": len(items), "items": items}


@mcp.tool(title="Listar templates", annotations=READ_ONLY)
async def list_templates(ctx: Ctx, search: Search = None, limit: Limit = 50) -> dict[str, Any]:
    """Lista templates por nome. Use o campo 'host' (nome técnico) ou 'name' ao
    vincular templates em create_host e update_host."""
    params: dict[str, Any] = {
        "output": ["templateid", "host", "name", "description"],
        "sortfield": "name",
        "limit": limit,
    }
    if search:
        params["search"] = {"name": search, "host": search}
        params["searchByAny"] = True
    templates = await _api(ctx, "template.get", params)
    items = [
        {
            "templateid": t["templateid"],
            "host": t["host"],
            "name": t["name"],
            "description": (t.get("description") or "")[:200] or None,
        }
        for t in templates
    ]
    return {"count": len(items), "truncated": len(items) >= limit, "items": items}


@mcp.tool(title="Listar hosts", annotations=READ_ONLY)
async def list_hosts(
    ctx: Ctx,
    search: Annotated[
        str | None,
        Field(description="Parte do nome técnico/visível do host, ou prefixo de IP (ex.: '10.10.1.')."),
    ] = None,
    group: Annotated[
        str | None,
        Field(description="Nome do grupo de hosts; inclui os subgrupos ('Pai' traz 'Pai/Filho')."),
    ] = None,
    status: Annotated[
        Literal["enabled", "disabled", "any"], Field(description="Filtra por estado de monitoramento.")
    ] = "any",
    only_with_problems: Annotated[
        bool, Field(description="Só hosts com algum problema ativo.")
    ] = False,
    limit: Limit = 100,
) -> dict[str, Any]:
    """Lista hosts com grupos, templates, endereço, tipo e disponibilidade da
    interface principal e tags."""
    params: dict[str, Any] = {**HOST_LIST_PARAMS, "limit": limit}
    if status != "any":
        params["filter"] = {"status": 0 if status == "enabled" else 1}
    if group:
        params["groupids"] = await _group_ids(ctx, group)
    if only_with_problems:
        triggers = await _api(
            ctx,
            "trigger.get",
            {
                "output": ["triggerid"],
                "only_true": True,
                "monitored": True,
                "skipDependent": True,
                "selectHosts": ["hostid"],
                "limit": 5000,
            },
        )
        problem_hosts = sorted({h["hostid"] for t in triggers for h in t.get("hosts", [])})
        if not problem_hosts:
            return {"count": 0, "truncated": False, "items": []}
        params["hostids"] = problem_hosts

    by_address = bool(search) and re.fullmatch(r"[\d.:]+", search.strip()) is not None
    if search and by_address:
        # A API não busca por IP: traz a lista e filtra aqui.
        params["limit"] = 5000
    elif search:
        params["search"] = {"host": search, "name": search}
        params["searchByAny"] = True

    hosts = await _api(ctx, "host.get", params)
    rows = [_host_row(h) for h in hosts]
    if search and by_address:
        rows = [r for r in rows if r["address"] and search.strip() in r["address"]]
    truncated = len(rows) > limit or (not by_address and len(rows) >= limit)
    return {"count": len(rows[:limit]), "truncated": truncated, "items": rows[:limit]}


@mcp.tool(title="Detalhar host", annotations=READ_ONLY)
async def get_host(host: HostRef, ctx: Ctx) -> dict[str, Any]:
    """Devolve tudo sobre um host: grupos, templates, interfaces com estado e
    erro, tags, macros (valores secretos nunca aparecem), quantidade de itens e
    triggers e os problemas ativos."""
    found = await _find_host(
        ctx,
        host,
        output=[
            "hostid", "host", "name", "status", "description", "maintenance_status",
            "monitored_by", "proxyid", "inventory_mode", "tls_connect", "tls_accept",
        ],
        selectHostGroups=["groupid", "name"],
        selectParentTemplates=["templateid", "name"],
        selectInterfaces="extend",
        selectTags=["tag", "value"],
        selectMacros=["macro", "value", "type", "description"],
        selectItems="count",
        selectTriggers="count",
    )
    problems = await _api(
        ctx,
        "problem.get",
        {"output": "extend", "hostids": [found["hostid"]], "sortfield": ["eventid"], "sortorder": "DESC"},
    )
    now = int(time.time())
    macros = [
        {
            "macro": m["macro"],
            "value": None if m.get("type") == "1" else m.get("value"),
            "secret": m.get("type") == "1",
            "description": m.get("description") or None,
        }
        for m in found.get("macros", [])
    ]
    row = _host_row(found)
    return {
        **row,
        "description": found.get("description") or None,
        "monitored_by": MONITORED_BY.get(str(found.get("monitored_by")), "server"),
        "inventory_mode": INVENTORY_MODES.get(str(found.get("inventory_mode")), "disabled"),
        "interfaces": [_interface_view(i) for i in found.get("interfaces", [])],
        "macros": macros,
        "item_count": _int(found.get("items")),
        "trigger_count": _int(found.get("triggers")),
        "active_problems": [
            _problem_row(p, {p["eventid"]: [found["host"]]}, now) for p in problems
        ],
    }


@mcp.tool(title="Listar itens de um host", annotations=READ_ONLY)
async def list_items(
    host: HostRef,
    ctx: Ctx,
    search: Annotated[
        str | None, Field(description="Parte do nome ou da chave do item (ex.: 'cpu', 'vfs.fs').")
    ] = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """Lista os itens de um host com o último valor coletado, unidade e estado."""
    found = await _find_host(ctx, host)
    params: dict[str, Any] = {
        "output": [
            "itemid", "name", "key_", "type", "value_type", "units", "delay", "status",
            "state", "error", "lastvalue", "lastclock",
        ],
        "hostids": [found["hostid"]],
        "sortfield": "name",
        "limit": limit,
    }
    if search:
        params["search"] = {"name": search, "key_": search}
        params["searchByAny"] = True
    items = await _api(ctx, "item.get", params)
    rows = [
        {
            "itemid": i["itemid"],
            "name": i["name"],
            "key": i["key_"],
            "value_type": VALUE_TYPE_NAMES.get(str(i["value_type"]), str(i["value_type"])),
            "last_value": i.get("lastvalue") if i.get("lastclock", "0") != "0" else None,
            "units": i.get("units") or None,
            "last_check": _iso(i.get("lastclock")),
            "interval": i.get("delay"),
            "enabled": i.get("status") == "0",
            "supported": i.get("state") == "0",
            "error": i.get("error") or None,
        }
        for i in items
    ]
    return {"host": found["host"], "count": len(rows), "truncated": len(rows) >= limit, "items": rows}


@mcp.tool(title="Histórico de um item", annotations=READ_ONLY)
async def get_item_history(
    item_id: Annotated[str, Field(description="itemid numérico (veja list_items).", min_length=1)],
    ctx: Ctx,
    hours: Annotated[float, Field(description="Janela de tempo, em horas, até agora.", ge=0.01, le=720)] = 1.0,
    limit: Annotated[int, Field(description="Máximo de pontos (os mais recentes).", ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    """Devolve os valores coletados de um item na janela pedida, do mais antigo
    ao mais novo. Para itens numéricos inclui mínimo, máximo e média."""
    items = await _api(
        ctx,
        "item.get",
        {
            "output": ["itemid", "name", "key_", "value_type", "units"],
            "itemids": [item_id],
            "selectHosts": ["host"],
        },
    )
    if not items:
        raise ToolError(f"Item não encontrado: {item_id!r}. Use list_items para ver os itemids.")
    item = items[0]
    value_type = _int(item["value_type"])
    since = int(time.time() - hours * 3600)
    history = await _api(
        ctx,
        "history.get",
        {
            "output": "extend",
            "itemids": [item_id],
            "history": value_type,
            "time_from": since,
            "sortfield": "clock",
            "sortorder": "DESC",
            "limit": limit,
        },
    )
    points = [{"time": _iso(p["clock"]), "value": p["value"]} for p in reversed(history)]
    result: dict[str, Any] = {
        "itemid": item["itemid"],
        "name": item["name"],
        "key": item["key_"],
        "host": item["hosts"][0]["host"] if item.get("hosts") else None,
        "units": item.get("units") or None,
        "since": _iso(since),
        "count": len(points),
        "truncated": len(points) >= limit,
        "points": points,
    }
    if value_type in (0, 3) and history:
        numbers = [float(p["value"]) for p in history]
        result["stats"] = {
            "min": min(numbers),
            "max": max(numbers),
            "avg": round(sum(numbers) / len(numbers), 4),
        }
    elif not history:
        result["note"] = "Sem valores na janela. O host está coletando? Veja a interface em get_host."
    return result


@mcp.tool(title="Listar triggers", annotations=READ_ONLY)
async def list_triggers(
    ctx: Ctx,
    host: Annotated[str | None, Field(description="Restringe a um host (nome ou hostid).")] = None,
    group: Annotated[str | None, Field(description="Restringe a um grupo de hosts.")] = None,
    only_in_problem: Annotated[bool, Field(description="Só triggers em estado de problema.")] = False,
    min_severity: Annotated[Severity | None, Field(description="Severidade mínima.")] = None,
    include_disabled: Annotated[bool, Field(description="Inclui triggers/hosts desabilitados.")] = False,
    limit: Limit = 50,
) -> dict[str, Any]:
    """Lista triggers com expressão, severidade e estado, da mais grave para a
    menos grave."""
    params: dict[str, Any] = {
        "output": ["triggerid", "description", "expression", "priority", "status", "value", "lastchange", "error"],
        "expandDescription": True,
        "expandExpression": True,
        "selectHosts": ["hostid", "host"],
        "sortfield": ["priority", "lastchange"],
        "sortorder": "DESC",
        "limit": limit,
    }
    if not include_disabled:
        params["monitored"] = True
    if only_in_problem:
        params["only_true"] = True
        params["skipDependent"] = True
    if min_severity:
        params["min_severity"] = SEVERITY_IDS[min_severity]
    if host:
        params["hostids"] = [(await _find_host(ctx, host))["hostid"]]
    if group:
        params["groupids"] = await _group_ids(ctx, group)
    triggers = await _api(ctx, "trigger.get", params)
    rows = [
        {
            "triggerid": t["triggerid"],
            "name": t["description"],
            "expression": t["expression"],
            "severity": SEVERITIES.get(_int(t["priority"]), "unknown"),
            "state": "problem" if t["value"] == "1" else "ok",
            "enabled": t["status"] == "0",
            "hosts": [h["host"] for h in t.get("hosts", [])],
            "last_change": _iso(t.get("lastchange")),
            "error": t.get("error") or None,
        }
        for t in triggers
    ]
    return {"count": len(rows), "truncated": len(rows) >= limit, "items": rows}


@mcp.tool(title="Listar problemas", annotations=READ_ONLY)
async def list_problems(
    ctx: Ctx,
    host: Annotated[str | None, Field(description="Restringe a um host (nome ou hostid).")] = None,
    group: Annotated[str | None, Field(description="Restringe a um grupo de hosts.")] = None,
    min_severity: Annotated[Severity | None, Field(description="Severidade mínima.")] = None,
    unacknowledged_only: Annotated[bool, Field(description="Só problemas ainda não reconhecidos.")] = False,
    include_resolved: Annotated[
        bool, Field(description="Inclui problemas resolvidos recentemente.")
    ] = False,
    limit: Limit = 50,
) -> dict[str, Any]:
    """Lista problemas (alertas) ativos, do mais novo para o mais antigo, com
    host, severidade, duração e se foram reconhecidos."""
    params: dict[str, Any] = {
        "output": "extend",
        "selectTags": ["tag", "value"],
        "sortfield": ["eventid"],
        "sortorder": "DESC",
        "limit": limit,
    }
    if host:
        params["hostids"] = [(await _find_host(ctx, host))["hostid"]]
    if group:
        params["groupids"] = await _group_ids(ctx, group)
    if min_severity:
        params["severities"] = list(range(SEVERITY_IDS[min_severity], 6))
    if unacknowledged_only:
        params["acknowledged"] = False
    if include_resolved:
        params["recent"] = True

    problems = await _api(ctx, "problem.get", params)
    hosts_by_event: dict[str, list[str]] = {}
    if problems:
        events = await _api(
            ctx,
            "event.get",
            {
                "output": ["eventid"],
                "eventids": [p["eventid"] for p in problems],
                "selectHosts": ["host"],
            },
        )
        hosts_by_event = {e["eventid"]: [h["host"] for h in e.get("hosts", [])] for e in events}
    now = int(time.time())
    rows = [_problem_row(p, hosts_by_event, now) for p in problems]
    return {"count": len(rows), "truncated": len(rows) >= limit, "items": rows}


@mcp.tool(title="Listar manutenções", annotations=READ_ONLY)
async def list_maintenances(ctx: Ctx, search: Search = None, limit: Limit = 50) -> dict[str, Any]:
    """Lista as janelas de manutenção, com hosts, grupos, período e se estão
    ativas agora."""
    params: dict[str, Any] = {
        "output": ["maintenanceid", "name", "active_since", "active_till", "maintenance_type", "description"],
        "selectHosts": ["hostid", "host"],
        "selectHostGroups": ["groupid", "name"],
        "selectTimeperiods": "extend",
        "sortfield": "name",
        "limit": limit,
    }
    if search:
        params["search"] = {"name": search}
    maintenances = await _api(ctx, "maintenance.get", params)
    now = int(time.time())
    rows = []
    for m in maintenances:
        since, till = _int(m["active_since"]), _int(m["active_till"])
        rows.append(
            {
                "maintenanceid": m["maintenanceid"],
                "name": m["name"],
                "active_now": since <= now < till,
                "state": "expired" if now >= till else ("active" if since <= now else "scheduled"),
                "from": _iso(since),
                "until": _iso(till),
                "collects_data": m.get("maintenance_type") == "0",
                "hosts": [h["host"] for h in m.get("hosts", [])],
                "groups": [g["name"] for g in m.get("hostgroups", [])],
                "description": m.get("description") or None,
            }
        )
    return {"count": len(rows), "truncated": len(rows) >= limit, "items": rows}


# --- escrita ----------------------------------------------------------------


@mcp.tool(title="Criar grupo de hosts", annotations=IDEMPOTENT_WRITE)
async def create_host_group(
    name: Annotated[str, Field(description="Nome do grupo; use 'Pai/Filho' para hierarquia.", min_length=1, max_length=255)],
    ctx: Ctx,
) -> dict[str, Any]:
    """Cria um grupo de hosts. Se já existir, devolve o existente sem erro."""
    _ensure_writable(ctx)
    name = name.strip()
    existing = await _api(ctx, "hostgroup.get", {"output": ["groupid", "name"], "filter": {"name": [name]}})
    if existing:
        return {"created": False, "groupid": existing[0]["groupid"], "name": name}
    created = await _api(ctx, "hostgroup.create", {"name": name})
    return {"created": True, "groupid": created["groupids"][0], "name": name}


@mcp.tool(title="Criar host", annotations=WRITE)
async def create_host(
    host: Annotated[
        str,
        Field(description="Nome técnico, único no Zabbix (letras, números, espaço, '.', '-', '_').", min_length=1, max_length=128),
    ],
    groups: Annotated[
        list[str], Field(description="Nomes dos grupos de hosts (ao menos um).", min_length=1)
    ],
    ctx: Ctx,
    ip: Annotated[str | None, Field(description="IP da interface (agente, SNMP, IPMI ou JMX).")] = None,
    dns: Annotated[str | None, Field(description="DNS da interface. Com ip e dns, o ip é usado.")] = None,
    port: Annotated[
        str | None, Field(description="Porta da interface. Padrão: agent 10050, snmp 161, ipmi 623, jmx 12345.")
    ] = None,
    interface_type: Annotated[
        Literal["agent", "snmp", "ipmi", "jmx"], Field(description="Tipo da interface.")
    ] = "agent",
    templates: Annotated[
        list[str] | None, Field(description="Templates a vincular (nome técnico, nome visível ou templateid).")
    ] = None,
    visible_name: Annotated[str | None, Field(description="Nome visível (padrão: o nome técnico).")] = None,
    description: Annotated[str | None, Field(description="Descrição do host.")] = None,
    tags: Annotated[dict[str, str] | None, Field(description="Tags no formato {tag: valor}.")] = None,
    macros: Annotated[
        dict[str, str] | None, Field(description="Macros no formato {'{$NOME}': valor}.")
    ] = None,
    enabled: Annotated[bool, Field(description="false cria o host desabilitado.")] = True,
    create_missing_groups: Annotated[
        bool, Field(description="Cria os grupos que ainda não existem.")
    ] = True,
    snmp_community: Annotated[
        str, Field(description="Comunidade SNMP v2c (só para interface snmp).")
    ] = "{$SNMP_COMMUNITY}",
) -> dict[str, Any]:
    """Cria um host monitorado. Sem ip nem dns, o host nasce sem interface (serve
    para itens trapper ou agente ativo). A coleta dos dados começa em até um
    minuto depois da criação."""
    _ensure_writable(ctx)
    # Valida tudo o que é só leitura antes de criar qualquer coisa: um erro de
    # template não pode deixar grupos novos para trás.
    template_refs = await _resolve_templates(ctx, templates) if templates else []
    group_ids, missing_groups = await _lookup_groups(ctx, groups)
    if missing_groups and not create_missing_groups:
        await _resolve_groups(ctx, groups)  # levanta o erro com a lista de grupos existentes

    params: dict[str, Any] = {
        "host": host.strip(),
        "status": 0 if enabled else 1,
    }
    if visible_name:
        params["name"] = visible_name
    if description:
        params["description"] = description
    if template_refs:
        params["templates"] = [{"templateid": t["templateid"]} for t in template_refs]
    if tags:
        params["tags"] = [{"tag": k, "value": v} for k, v in tags.items()]
    if macros:
        params["macros"] = [{"macro": _macro_name(k), "value": v} for k, v in macros.items()]

    if ip or dns:
        interface: dict[str, Any] = {
            "type": INTERFACE_IDS[interface_type],
            "main": 1,
            "useip": 1 if ip else 0,
            "ip": (ip or "").strip(),
            "dns": (dns or "").strip(),
            "port": port or DEFAULT_PORTS[interface_type],
        }
        if interface_type == "snmp":
            interface["details"] = {"version": 2, "bulk": 1, "community": snmp_community}
        params["interfaces"] = [interface]
    elif port or interface_type != "agent":
        raise ToolError("port e interface_type só fazem sentido junto com ip ou dns.")

    new_group_ids: list[str] = []
    try:
        for name in missing_groups:
            made = await _api(ctx, "hostgroup.create", {"name": name})
            group_ids[name] = made["groupids"][0]
            new_group_ids.append(group_ids[name])
        params["groups"] = [{"groupid": group_ids[n]} for n in _unique(groups)]
        created = await _api(ctx, "host.create", params)
    except ToolError:
        # Não deixa grupos órfãos quando a criação do host falha (nome repetido, por exemplo).
        if new_group_ids:
            try:
                await _api(ctx, "hostgroup.delete", new_group_ids)
            except ToolError:
                logger.warning("não consegui desfazer os grupos criados: %s", new_group_ids)
        raise

    hostid = created["hostids"][0]
    return {
        "created": True,
        "hostid": hostid,
        "host": params["host"],
        "enabled": enabled,
        "groups": _unique(groups),
        "created_groups": missing_groups,
        "templates": [t["name"] for t in template_refs],
        "address": f"{ip or dns}:{params['interfaces'][0]['port']}" if "interfaces" in params else None,
        "note": "A primeira coleta leva até um minuto; confira com get_host.",
    }


@mcp.tool(title="Alterar host", annotations=IDEMPOTENT_WRITE)
async def update_host(
    host: HostRef,
    ctx: Ctx,
    visible_name: Annotated[str | None, Field(description="Novo nome visível.")] = None,
    description: Annotated[str | None, Field(description="Nova descrição.")] = None,
    enabled: Annotated[
        bool | None, Field(description="true habilita e false desabilita o monitoramento.")
    ] = None,
    add_groups: Annotated[list[str] | None, Field(description="Grupos a acrescentar (devem existir).")] = None,
    remove_groups: Annotated[list[str] | None, Field(description="Grupos a retirar.")] = None,
    add_templates: Annotated[list[str] | None, Field(description="Templates a vincular.")] = None,
    remove_templates: Annotated[list[str] | None, Field(description="Templates a desvincular.")] = None,
    clear_data_on_template_removal: Annotated[
        bool,
        Field(description="Ao desvincular, apaga também os itens/triggers que vieram do template."),
    ] = False,
    set_tags: Annotated[
        dict[str, str] | None, Field(description="Tags a criar ou sobrescrever, {tag: valor}.")
    ] = None,
    remove_tags: Annotated[list[str] | None, Field(description="Nomes de tags a remover.")] = None,
    ip: Annotated[str | None, Field(description="Novo IP da interface principal.")] = None,
    dns: Annotated[str | None, Field(description="Novo DNS da interface principal.")] = None,
    port: Annotated[str | None, Field(description="Nova porta da interface principal.")] = None,
) -> dict[str, Any]:
    """Altera um host: nome, descrição, estado, grupos, templates, tags e o
    endereço da interface principal. Só os campos informados mudam."""
    _ensure_writable(ctx)
    current = await _find_host(
        ctx,
        host,
        output=["hostid", "host", "name", "status", "description"],
        selectHostGroups=["groupid", "name"],
        selectParentTemplates=["templateid", "name"],
        selectTags=["tag", "value"],
        selectInterfaces=["interfaceid", "type", "main", "useip", "ip", "dns", "port"],
    )
    update: dict[str, Any] = {"hostid": current["hostid"]}
    changes: list[str] = []

    if visible_name is not None:
        update["name"] = visible_name
        changes.append("nome visível")
    if description is not None:
        update["description"] = description
        changes.append("descrição")
    if enabled is not None:
        update["status"] = 0 if enabled else 1
        changes.append("habilitado" if enabled else "desabilitado")

    if add_groups or remove_groups:
        group_ids = {g["groupid"]: g["name"] for g in current["hostgroups"]}
        for ref in await _resolve_groups(ctx, add_groups) if add_groups else []:
            group_ids.setdefault(ref["groupid"], ref["groupid"])
        if remove_groups:
            to_remove = {n.strip().lower() for n in remove_groups}
            group_ids = {i: n for i, n in group_ids.items() if n.lower() not in to_remove and i not in to_remove}
        if not group_ids:
            raise ToolError("Um host precisa pertencer a ao menos um grupo; a alteração deixaria o host sem grupo.")
        update["groups"] = [{"groupid": i} for i in group_ids]
        changes.append("grupos")

    if add_templates or remove_templates:
        linked = {t["templateid"]: t["name"] for t in current["parentTemplates"]}
        for ref in await _resolve_templates(ctx, add_templates) if add_templates else []:
            linked[ref["templateid"]] = ref["name"]
        removed: list[str] = []
        if remove_templates:
            for ref in await _resolve_templates(ctx, remove_templates):
                if linked.pop(ref["templateid"], None) is not None:
                    removed.append(ref["templateid"])
        update["templates"] = [{"templateid": i} for i in linked]
        if removed and clear_data_on_template_removal:
            update["templates_clear"] = [{"templateid": i} for i in removed]
        changes.append("templates")

    if set_tags or remove_tags:
        merged = {t["tag"]: t["value"] for t in current["tags"]}
        merged.update(set_tags or {})
        for name in remove_tags or []:
            merged.pop(name, None)
        update["tags"] = [{"tag": k, "value": v} for k, v in merged.items()]
        changes.append("tags")

    if len(update) > 1:
        await _api(ctx, "host.update", update)

    if ip is not None or dns is not None or port is not None:
        interfaces = current["interfaces"]
        main = next((i for i in interfaces if i["main"] == "1"), None)
        if main is None:
            raise ToolError("O host não tem interface principal; crie uma com api_call (hostinterface.create).")
        change: dict[str, Any] = {"interfaceid": main["interfaceid"]}
        if ip is not None:
            change["ip"] = ip
            change["useip"] = 1
        if dns is not None:
            change["dns"] = dns
            if ip is None and not main["ip"]:
                change["useip"] = 0
        if port is not None:
            change["port"] = port
        await _api(ctx, "hostinterface.update", change)
        changes.append("interface")

    if not changes:
        raise ToolError("Nenhuma alteração informada.")
    return {"updated": True, "hostid": current["hostid"], "host": current["host"], "changed": changes}


@mcp.tool(title="Definir macro de host", annotations=IDEMPOTENT_WRITE)
async def set_host_macro(
    host: HostRef,
    macro: Annotated[str, Field(description="Nome da macro: 'NOME' ou '{$NOME}'.", min_length=1)],
    value: Annotated[str, Field(description="Valor da macro.")],
    ctx: Ctx,
    description: Annotated[str | None, Field(description="Descrição da macro.")] = None,
    secret: Annotated[
        bool | None,
        Field(description="true guarda como segredo (não pode mais ser lido). Omitido: mantém o tipo atual."),
    ] = None,
) -> dict[str, Any]:
    """Cria a macro no host ou atualiza o valor se ela já existir."""
    _ensure_writable(ctx)
    found = await _find_host(ctx, host)
    name = _macro_name(macro)
    existing = await _api(
        ctx,
        "usermacro.get",
        {"output": ["hostmacroid", "macro", "type"], "hostids": [found["hostid"]], "filter": {"macro": [name]}},
    )
    if existing:
        change: dict[str, Any] = {"hostmacroid": existing[0]["hostmacroid"], "value": value}
        if description is not None:
            change["description"] = description
        if secret is not None:
            change["type"] = 1 if secret else 0
        await _api(ctx, "usermacro.update", change)
        action = "updated"
    else:
        create: dict[str, Any] = {"hostid": found["hostid"], "macro": name, "value": value}
        if description is not None:
            create["description"] = description
        if secret:
            create["type"] = 1
        await _api(ctx, "usermacro.create", create)
        action = "created"
    return {action: True, "host": found["host"], "macro": name, "secret": bool(secret)}


@mcp.tool(title="Apagar host", annotations=DESTRUCTIVE)
async def delete_host(
    host: HostRef,
    ctx: Ctx,
    confirm: Annotated[
        bool, Field(description="Precisa ser true. Só use depois de o usuário confirmar.")
    ] = False,
) -> dict[str, Any]:
    """Apaga um host e todos os seus itens, triggers e histórico. Não tem volta."""
    _ensure_writable(ctx)
    found = await _find_host(ctx, host)
    _require_confirm(confirm, f"apagar o host {found['host']!r} (hostid {found['hostid']}) com todo o histórico")
    await _api(ctx, "host.delete", [found["hostid"]])
    return {"deleted": True, "hostid": found["hostid"], "host": found["host"]}


@mcp.tool(title="Criar item", annotations=WRITE)
async def create_item(
    host: HostRef,
    name: Annotated[str, Field(description="Nome do item.", min_length=1, max_length=255)],
    key: Annotated[
        str, Field(description="Chave do item (ex.: 'system.cpu.load[all,avg1]' ou 'meu.item').", min_length=1, max_length=2048)
    ],
    ctx: Ctx,
    item_type: Annotated[ItemType, Field(description="Tipo de coleta.")] = "agent",
    value_type: Annotated[ValueType, Field(description="Tipo do valor coletado.")] = "float",
    interval: Annotated[
        str, Field(description="Intervalo de coleta (ex.: '30s', '1m', '1h'). Ignorado em trapper.")
    ] = "1m",
    units: Annotated[str | None, Field(description="Unidade exibida (ex.: 'B', '%').")] = None,
    description: Annotated[str | None, Field(description="Descrição do item.")] = None,
) -> dict[str, Any]:
    """Cria um item de coleta em um host. Para 'agent' e 'simple_check' usa a
    interface principal do host."""
    _ensure_writable(ctx)
    found = await _find_host(
        ctx, host, selectInterfaces=["interfaceid", "type", "main"]
    )
    params: dict[str, Any] = {
        "hostid": found["hostid"],
        "name": name,
        "key_": key,
        "type": ITEM_TYPES[item_type],
        "value_type": VALUE_TYPES[value_type],
        "delay": "0" if item_type == "trapper" else interval,
    }
    if units:
        params["units"] = units
    if description:
        params["description"] = description
    if item_type in ("agent", "simple_check"):
        interfaces = found.get("interfaces", [])
        wanted = "1" if item_type == "agent" else None
        chosen = next(
            (i for i in interfaces if i["main"] == "1" and (wanted is None or i["type"] == wanted)), None
        )
        if chosen is None:
            raise ToolError(
                f"O host {found['host']!r} não tem interface principal compatível com o tipo "
                f"{item_type!r}. Crie a interface ou use item_type 'trapper' ou 'agent_active'."
            )
        params["interfaceid"] = chosen["interfaceid"]
    created = await _api(ctx, "item.create", params)
    return {"created": True, "itemid": created["itemids"][0], "host": found["host"], "key": key}


@mcp.tool(title="Criar trigger", annotations=WRITE)
async def create_trigger(
    name: Annotated[str, Field(description="Nome do alerta (aceita macros como {HOST.NAME}).", min_length=1, max_length=255)],
    expression: Annotated[
        str,
        Field(description="Expressão do Zabbix 6+, ex.: 'last(/web-prod-01/system.cpu.load)>5'.", min_length=1),
    ],
    ctx: Ctx,
    severity: Annotated[Severity, Field(description="Severidade do alerta.")] = "warning",
    description: Annotated[str | None, Field(description="Descrição/comentários da trigger.")] = None,
    enabled: Annotated[bool, Field(description="false cria a trigger desabilitada.")] = True,
) -> dict[str, Any]:
    """Cria uma trigger. O host e o item fazem parte da expressão, no formato
    'função(/host/chave)operador valor'."""
    _ensure_writable(ctx)
    params: dict[str, Any] = {
        "description": name,
        "expression": expression,
        "priority": SEVERITY_IDS[severity],
        "status": 0 if enabled else 1,
    }
    if description:
        params["comments"] = description
    created = await _api(ctx, "trigger.create", params)
    return {"created": True, "triggerid": created["triggerids"][0], "name": name, "severity": severity}


@mcp.tool(title="Reconhecer problema", annotations=WRITE)
async def acknowledge_problem(
    event_id: Annotated[str, Field(description="eventid do problema (veja list_problems).", min_length=1)],
    ctx: Ctx,
    message: Annotated[str | None, Field(description="Comentário a registrar no evento.")] = None,
    severity: Annotated[Severity | None, Field(description="Muda a severidade do problema.")] = None,
    unacknowledge: Annotated[bool, Field(description="true remove o reconhecimento.")] = False,
) -> dict[str, Any]:
    """Reconhece (ou desfaz o reconhecimento de) um problema, com comentário e
    mudança de severidade opcionais."""
    _ensure_writable(ctx)
    action = ACK_ACTIONS["unacknowledge" if unacknowledge else "acknowledge"]
    params: dict[str, Any] = {"eventids": [event_id]}
    if message:
        action |= ACK_ACTIONS["message"]
        params["message"] = message
    if severity:
        action |= ACK_ACTIONS["severity"]
        params["severity"] = SEVERITY_IDS[severity]
    params["action"] = action
    await _api(ctx, "event.acknowledge", params)
    return {
        "eventid": event_id,
        "acknowledged": not unacknowledge,
        "message": message,
        "severity": severity,
    }


@mcp.tool(title="Criar manutenção", annotations=WRITE)
async def create_maintenance(
    name: Annotated[str, Field(description="Nome da manutenção (único).", min_length=1, max_length=128)],
    ctx: Ctx,
    hosts: Annotated[list[str] | None, Field(description="Hosts incluídos (nome ou hostid).")] = None,
    groups: Annotated[list[str] | None, Field(description="Grupos de hosts incluídos.")] = None,
    duration_minutes: Annotated[
        int, Field(description="Duração da janela, em minutos.", ge=1, le=60 * 24 * 90)
    ] = 60,
    start_in_minutes: Annotated[
        int, Field(description="Começa daqui a N minutos (0 = agora).", ge=0, le=60 * 24 * 365)
    ] = 0,
    collect_data: Annotated[
        bool, Field(description="true mantém a coleta durante a janela; false suspende a coleta.")
    ] = True,
    description: Annotated[str | None, Field(description="Motivo da manutenção.")] = None,
) -> dict[str, Any]:
    """Cria uma janela de manutenção única para hosts e/ou grupos."""
    _ensure_writable(ctx)
    if not hosts and not groups:
        raise ToolError("Informe ao menos um host ou um grupo para a manutenção.")
    host_ids = [{"hostid": (await _find_host(ctx, h))["hostid"]} for h in _unique(hosts)]
    group_ids = await _resolve_groups(ctx, groups) if groups else []

    start = int(time.time()) + start_in_minutes * 60
    start -= start % 60  # a API trabalha em minutos cheios
    period = duration_minutes * 60
    params: dict[str, Any] = {
        "name": name,
        "active_since": start,
        "active_till": start + period,
        "maintenance_type": 0 if collect_data else 1,
        "timeperiods": [{"timeperiod_type": 0, "start_date": start, "period": period}],
    }
    if host_ids:
        params["hosts"] = host_ids
    if group_ids:
        params["groups"] = group_ids
    if description:
        params["description"] = description
    created = await _api(ctx, "maintenance.create", params)
    return {
        "created": True,
        "maintenanceid": created["maintenanceids"][0],
        "name": name,
        "from": _iso(start),
        "until": _iso(start + period),
    }


@mcp.tool(title="Apagar manutenção", annotations=DESTRUCTIVE)
async def delete_maintenance(
    maintenance: Annotated[str, Field(description="maintenanceid numérico ou nome exato.", min_length=1)],
    ctx: Ctx,
    confirm: Annotated[
        bool, Field(description="Precisa ser true. Só use depois de o usuário confirmar.")
    ] = False,
) -> dict[str, Any]:
    """Apaga uma janela de manutenção."""
    _ensure_writable(ctx)
    ref = maintenance.strip()
    params: dict[str, Any] = {"output": ["maintenanceid", "name"]}
    if ref.isdigit():
        params["maintenanceids"] = [ref]
    else:
        params["filter"] = {"name": [ref]}
    found = await _api(ctx, "maintenance.get", params)
    if not found and ref.isdigit():
        found = await _api(ctx, "maintenance.get", {"output": ["maintenanceid", "name"], "filter": {"name": [ref]}})
    if not found:
        raise ToolError(f"Manutenção não encontrada: {maintenance!r}. Use list_maintenances.")
    if len(found) > 1:
        raise ToolError(f"Mais de uma manutenção com o nome {maintenance!r}; use o maintenanceid.")
    target = found[0]
    _require_confirm(confirm, f"apagar a manutenção {target['name']!r} (id {target['maintenanceid']})")
    await _api(ctx, "maintenance.delete", [target["maintenanceid"]])
    return {"deleted": True, "maintenanceid": target["maintenanceid"], "name": target["name"]}


@mcp.tool(title="Chamar a API do Zabbix", annotations=DESTRUCTIVE)
async def api_call(
    method: Annotated[
        str, Field(description="Método da API, ex.: 'proxy.get' ou 'mediatype.get'.", pattern=r"^[a-z]+\.[A-Za-z]+$")
    ],
    ctx: Ctx,
    params: Annotated[
        dict[str, Any] | list[Any] | None,
        Field(description="Parâmetros do método. Em '.get', prefira 'output' enxuto e 'limit'."),
    ] = None,
) -> dict[str, Any]:
    """Chama qualquer método da API JSON-RPC do Zabbix. Use quando nenhuma outra
    Tool cobrir o caso. Métodos que não são de leitura (create, update, delete...)
    alteram o Zabbix de verdade e são recusados em modo somente leitura."""
    if method in ("user.login", "user.logout"):
        raise ToolError("A sessão é gerenciada pelo servidor; user.login e user.logout não são permitidos.")
    if method != "apiinfo.version" and not method.endswith(".get"):
        _ensure_writable(ctx)
    result = await _api(ctx, method, params)
    size = len(json.dumps(result, ensure_ascii=False))
    if size > MAX_RESULT_CHARS:
        raise ToolError(
            f"Resposta grande demais ({size} caracteres). Reduza com 'output', filtros e 'limit'."
        )
    return {"method": method, "result": result}


# --- Health, Resource e Prompts ---------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Verificação de processo (só no transporte HTTP). Não consulta o Zabbix."""
    return JSONResponse({"status": "ok", "server": "mcp-zabbix", "version": VERSION})


GUIDE: Final = """# Zabbix MCP Server

Gerencia um Zabbix pela API JSON-RPC. Não contém modelo de linguagem: quem
interpreta os resultados é a aplicação de IA conectada.

## Consultar

| Tool | Para quê |
| --- | --- |
| `get_overview` | Versão, contagens, hosts indisponíveis, problemas por severidade |
| `list_hosts` / `get_host` | Hosts e o detalhe de um host (interfaces, macros, problemas) |
| `list_host_groups` / `list_templates` | Grupos e templates, para usar nos nomes exatos |
| `list_items` / `get_item_history` | Itens, último valor e histórico |
| `list_triggers` / `list_problems` | Alertas configurados e problemas ativos |
| `list_maintenances` | Janelas de manutenção |

## Alterar

| Tool | Para quê |
| --- | --- |
| `create_host_group`, `create_host`, `update_host`, `set_host_macro` | Cadastro de hosts |
| `create_item`, `create_trigger` | Coleta e alertas |
| `acknowledge_problem` | Reconhecer um problema |
| `create_maintenance` | Janela de manutenção |
| `delete_host`, `delete_maintenance` | Apagar (exigem `confirm=true`) |
| `api_call` | Qualquer outro método da API |

Com `ZABBIX_READ_ONLY=true` todas as Tools que alteram recusam a chamada.

## Severidades

`not_classified` < `information` < `warning` < `average` < `high` < `disaster`.

## Expressões de trigger (Zabbix 6+)

`função(/host/chave)operador valor`, por exemplo:

- `last(/web-prod-01/system.cpu.load[all,avg1])>5`
- `avg(/db-prod-01/vfs.fs.dependent.size[/,pused],10m)>90`
- `nodata(/app-hml-01/agent.ping,5m)=1`

## Observações

- Hosts novos levam até um minuto para a primeira coleta.
- O nome de grupo `Pai/Filho` forma hierarquia; filtrar por `Pai` inclui os filhos.
- Valores de macros secretas nunca são devolvidos.
"""


@mcp.resource("zabbix://guide", title="Guia do servidor", mime_type="text/markdown")
def guide() -> str:
    """Guia em Markdown com as Tools, severidades e exemplos de trigger."""
    return GUIDE


@mcp.prompt(title="Diagnosticar host")
def diagnose_host(
    host: Annotated[str, Field(description="Nome técnico, nome visível ou hostid do host.")],
) -> list[Message]:
    """Orienta o assistente a diagnosticar um host usando as Tools de consulta."""
    return [
        UserMessage(
            f"Diagnostique o host {host}.\n\n"
            f"1. Chame get_host com host='{host}' (interfaces, templates, problemas ativos).\n"
            f"2. Chame list_triggers com host='{host}' e only_in_problem=true.\n"
            f"3. Se houver interface indisponível ou item sem dados, chame list_items com "
            f"host='{host}' para ver erros e último valor coletado.\n"
            "4. Responda em português: estado atual, causa provável de cada problema e "
            "próximos passos.\n\n"
            "Use só os dados devolvidos pelas Tools. Não altere nada no Zabbix."
        )
    ]


@mcp.prompt(title="Triagem de problemas")
def triage_problems(
    min_severity: Annotated[
        Severity, Field(description="Severidade mínima a considerar.")
    ] = "average",
) -> list[Message]:
    """Orienta o assistente a priorizar os problemas ativos."""
    return [
        UserMessage(
            "Faça a triagem dos problemas ativos do Zabbix.\n\n"
            "1. Chame get_overview para ter o panorama.\n"
            f"2. Chame list_problems com min_severity='{min_severity}'.\n"
            "3. Agrupe por host e por causa em comum (por exemplo, vários hosts "
            "indisponíveis na mesma rede).\n"
            "4. Responda em português com a ordem de prioridade e a justificativa.\n\n"
            "Só consulte; não reconheça problemas nem crie manutenções sem pedido."
        )
    ]
