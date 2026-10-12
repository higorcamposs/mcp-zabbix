"""Ponto de entrada: `python -m mcp_zabbix` ou o comando `mcp-zabbix`.

Transporte (variável `MCP_TRANSPORT`):

* `stdio` (padrão): o cliente MCP inicia o processo e conversa pela entrada e
  saída padrão. Nada além de protocolo pode ir para o stdout, então os logs
  seguem para o stderr.
* `streamable-http`: o servidor fica de pé em `MCP_HOST:MCP_PORT/mcp`. Não há
  autenticação nesse endpoint e as Tools de escrita estão ativas (a menos que
  `ZABBIX_READ_ONLY=true`), por isso o padrão escuta só em 127.0.0.1.
"""

from __future__ import annotations

import logging
import os
import sys

from .config import load_env_file, parse_bool

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _csv(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _allowlist(port: int, scheme: bool, extra_env: str) -> list[str]:
    base = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    if scheme:
        base = [f"http://{item}" for item in base]
    return list(dict.fromkeys([*base, *_csv(extra_env)]))


def main() -> None:
    # O .env precisa entrar antes de importar o servidor: MCP_LOG_LEVEL é lido na importação.
    env_file = load_env_file()

    from mcp.server.transport_security import TransportSecuritySettings

    from .server import mcp

    logger = logging.getLogger("mcp_zabbix")
    if env_file:
        logger.info("variáveis carregadas de %s", env_file)

    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    if transport not in ("streamable-http", "http"):
        sys.exit(f"MCP_TRANSPORT inválido: {transport!r}. Use 'stdio' ou 'streamable-http'.")

    host = os.getenv("MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("MCP_PORT", "8000"))
    except ValueError:
        sys.exit("MCP_PORT precisa ser um número.")
    if host not in LOOPBACK_HOSTS and not parse_bool(os.getenv("ZABBIX_READ_ONLY"), False):
        logger.warning(
            "MCP_HOST=%s expõe um endpoint SEM autenticação com Tools que alteram o Zabbix; "
            "considere ZABBIX_READ_ONLY=true ou manter 127.0.0.1.",
            host,
        )

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowlist(port, False, "MCP_EXTRA_ALLOWED_HOSTS"),
        allowed_origins=_allowlist(port, True, "MCP_EXTRA_ALLOWED_ORIGINS"),
    )
    logger.info("Zabbix MCP Server em http://%s:%s/mcp (Streamable HTTP)", host, port)
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        transport_security=security,
    )


if __name__ == "__main__":
    main()
