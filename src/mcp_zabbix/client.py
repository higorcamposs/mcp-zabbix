"""Cliente assíncrono da API JSON-RPC do Zabbix.

Autenticação, em ordem de preferência:

1. `ZABBIX_API_TOKEN`: token de API, enviado como `Authorization: Bearer`.
2. `ZABBIX_USER` + `ZABBIX_PASSWORD`: o cliente faz `user.login` na primeira
   chamada, refaz o login uma vez se a sessão expirar e encerra a sessão
   (`user.logout`) no desligamento.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any

import httpx2

from .config import Settings

logger = logging.getLogger("mcp_zabbix.client")

# Trechos (em minúsculas) das mensagens do Zabbix que indicam sessão inválida.
AUTH_ERROR_MARKERS = ("session terminated", "not authori", "re-login")


class ZabbixError(Exception):
    """Base de todas as falhas ao falar com o Zabbix."""


class ZabbixConfigError(ZabbixError):
    """Configuração ausente ou inválida (por exemplo, sem credenciais)."""


class ZabbixConnectionError(ZabbixError):
    """Rede, tempo esgotado ou resposta que não é da API do Zabbix."""


class ZabbixAuthError(ZabbixError):
    """Credenciais recusadas ou sessão que não pôde ser renovada."""


class ZabbixAPIError(ZabbixError):
    """O Zabbix respondeu com um objeto `error` do JSON-RPC."""

    def __init__(self, method: str, code: int | None, message: str, data: str | None) -> None:
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        detail = f"{message} {data}".strip() if data else message
        super().__init__(f"{method}: {detail}")

    @property
    def is_auth_error(self) -> bool:
        text = f"{self.message} {self.data or ''}".lower()
        return any(marker in text for marker in AUTH_ERROR_MARKERS)


class ZabbixClient:
    """Faz chamadas `método + params` e devolve o campo `result`."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._http = httpx2.AsyncClient(
            timeout=httpx2.Timeout(settings.timeout_seconds),
            verify=settings.verify_tls,
            transport=transport,
        )
        self._ids = itertools.count(1)
        self._session_token: str | None = None
        self._login_lock = asyncio.Lock()

    @property
    def endpoint(self) -> str:
        return self.settings.endpoint

    # --- transporte -------------------------------------------------------

    async def _post(self, method: str, params: Any, token: str | None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": next(self._ids),
        }
        headers = {"Content-Type": "application/json-rpc"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            response = await self._http.post(
                self.endpoint, content=json.dumps(payload).encode(), headers=headers
            )
        except httpx2.TimeoutException as exc:
            raise ZabbixConnectionError(
                f"Tempo esgotado ({self.settings.timeout_seconds:g}s) ao chamar {method} em {self.endpoint}."
            ) from exc
        except httpx2.ConnectError as exc:
            raise ZabbixConnectionError(
                f"Não consegui conectar em {self.endpoint}. O Zabbix está no ar? ({exc})"
            ) from exc
        except httpx2.HTTPError as exc:
            raise ZabbixConnectionError(f"Erro de rede ao chamar {self.endpoint}: {exc}") from exc

        if response.status_code != 200:
            raise ZabbixConnectionError(
                f"HTTP {response.status_code} ao chamar {method} em {self.endpoint}."
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ZabbixConnectionError(
                f"A resposta de {self.endpoint} não é JSON. A ZABBIX_URL aponta para o Zabbix?"
            ) from exc
        if not isinstance(body, dict) or ("result" not in body and "error" not in body):
            raise ZabbixConnectionError(
                f"Resposta inesperada de {self.endpoint}: não é uma resposta JSON-RPC."
            )

        error = body.get("error")
        if error:
            raise ZabbixAPIError(
                method,
                error.get("code") if isinstance(error, dict) else None,
                str(error.get("message", "")) if isinstance(error, dict) else str(error),
                str(error.get("data")) if isinstance(error, dict) and error.get("data") else None,
            )
        return body["result"]

    # --- sessão -----------------------------------------------------------

    async def _login(self) -> str:
        settings = self.settings
        if not (settings.user and settings.password):
            raise ZabbixConfigError(
                "Sem credenciais do Zabbix. Defina ZABBIX_API_TOKEN ou ZABBIX_USER e ZABBIX_PASSWORD."
            )
        try:
            token = await self._post(
                "user.login",
                {"username": settings.user, "password": settings.password},
                None,
            )
        except ZabbixAPIError as exc:
            raise ZabbixAuthError(f"Login recusado pelo Zabbix: {exc}") from exc
        if not isinstance(token, str) or not token:
            raise ZabbixAuthError("O Zabbix não devolveu um token de sessão no login.")
        logger.info("login no Zabbix feito como %s", settings.user)
        return token

    async def _token(self) -> str:
        if self.settings.api_token:
            return self.settings.api_token
        async with self._login_lock:
            if self._session_token is None:
                self._session_token = await self._login()
            return self._session_token

    async def _renew(self, stale: str) -> str:
        async with self._login_lock:
            # Outra chamada concorrente pode já ter renovado a sessão.
            if self._session_token == stale or self._session_token is None:
                self._session_token = await self._login()
            return self._session_token

    # --- API pública ------------------------------------------------------

    async def call(self, method: str, params: Any = None, *, auth: bool = True) -> Any:
        """Chama um método da API. `auth=False` só para `apiinfo.version`."""
        body = {} if params is None else params
        if not auth:
            return await self._post(method, body, None)

        token = await self._token()
        try:
            return await self._post(method, body, token)
        except ZabbixAPIError as exc:
            if not exc.is_auth_error:
                raise
            if self.settings.api_token:
                raise ZabbixAuthError(f"Token de API recusado pelo Zabbix: {exc}") from exc
            logger.info("sessão expirada; renovando o login e repetindo %s", method)
            token = await self._renew(token)
            return await self._post(method, body, token)

    async def version(self) -> str:
        return str(await self.call("apiinfo.version", [], auth=False))

    async def aclose(self) -> None:
        """Encerra a sessão aberta por este cliente (se houver) e fecha o HTTP."""
        if self._session_token and not self.settings.api_token:
            try:
                await self._post("user.logout", [], self._session_token)
            except ZabbixError:
                logger.debug("user.logout falhou; seguindo com o desligamento")
            self._session_token = None
        await self._http.aclose()
