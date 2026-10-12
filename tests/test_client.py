"""Cliente da API: login, renovação de sessão e tradução de falhas."""

from __future__ import annotations

import httpx2
import pytest

from mcp_zabbix.client import (
    ZabbixAPIError,
    ZabbixAuthError,
    ZabbixClient,
    ZabbixConfigError,
    ZabbixConnectionError,
)

from .conftest import FakeZabbix, make_settings

pytestmark = pytest.mark.anyio


def client_for(fake: FakeZabbix, **overrides: object) -> ZabbixClient:
    return ZabbixClient(make_settings(**overrides), transport=httpx2.MockTransport(fake.handle))


async def test_logs_in_once_and_reuses_the_session(fake: FakeZabbix) -> None:
    client = client_for(fake)

    await client.call("hostgroup.get")
    await client.call("hostgroup.get")

    assert fake.logins == 1
    assert fake.methods_called().count("user.login") == 1


async def test_login_sends_username_and_password(fake: FakeZabbix) -> None:
    client = client_for(fake)

    await client.call("hostgroup.get")

    method, params = fake.calls[0]
    assert method == "user.login"
    assert params == {"username": "Admin", "password": "secret"}


async def test_api_token_skips_the_login(fake: FakeZabbix) -> None:
    fake.api_token = "token-de-api"
    client = client_for(fake, api_token="token-de-api", user=None, password=None)

    result = await client.call("hostgroup.get")

    assert fake.logins == 0
    assert len(result) == 2


async def test_rejected_api_token_is_an_auth_error(fake: FakeZabbix) -> None:
    client = client_for(fake, api_token="errado", user=None, password=None)

    with pytest.raises(ZabbixAuthError, match="Token de API recusado"):
        await client.call("hostgroup.get")


async def test_expired_session_is_renewed_and_the_call_repeated(fake: FakeZabbix) -> None:
    client = client_for(fake)
    await client.call("hostgroup.get")
    fake.expire_sessions()

    result = await client.call("hostgroup.get")

    assert len(result) == 2
    assert fake.logins == 2


async def test_wrong_password_is_an_auth_error(fake: FakeZabbix) -> None:
    client = client_for(fake, password="errada")

    with pytest.raises(ZabbixAuthError, match="Login recusado"):
        await client.call("hostgroup.get")


async def test_missing_credentials_are_a_config_error(fake: FakeZabbix) -> None:
    client = client_for(fake, user=None, password=None)

    with pytest.raises(ZabbixConfigError, match="ZABBIX_API_TOKEN"):
        await client.call("hostgroup.get")
    assert fake.calls == []  # nem chegou a falar com o Zabbix


async def test_api_error_carries_method_message_and_data(fake: FakeZabbix) -> None:
    client = client_for(fake)

    with pytest.raises(ZabbixAPIError) as caught:
        await client.call("host.nope")

    assert caught.value.code == -32601
    assert "host.nope" in str(caught.value)
    assert caught.value.is_auth_error is False


async def test_version_needs_no_authentication(fake: FakeZabbix) -> None:
    client = client_for(fake, user=None, password=None)

    assert await client.version() == "7.4.0"


async def test_connection_failure_is_explained() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("recusada", request=request)

    client = ZabbixClient(make_settings(), transport=httpx2.MockTransport(refuse))

    with pytest.raises(ZabbixConnectionError, match="Não consegui conectar"):
        await client.call("apiinfo.version", auth=False)


async def test_timeout_is_explained() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("lento", request=request)

    client = ZabbixClient(make_settings(), transport=httpx2.MockTransport(slow))

    with pytest.raises(ZabbixConnectionError, match="Tempo esgotado"):
        await client.call("apiinfo.version", auth=False)


@pytest.mark.parametrize(
    ("response", "needle"),
    [
        (httpx2.Response(502, text="bad gateway"), "HTTP 502"),
        (httpx2.Response(200, text="<html>login</html>"), "não é JSON"),
        (httpx2.Response(200, json=["nada", "de", "rpc"]), "não é uma resposta JSON-RPC"),
    ],
)
async def test_responses_that_are_not_zabbix_are_rejected(response: httpx2.Response, needle: str) -> None:
    client = ZabbixClient(make_settings(), transport=httpx2.MockTransport(lambda request: response))

    with pytest.raises(ZabbixConnectionError, match=needle):
        await client.call("apiinfo.version", auth=False)


async def test_close_ends_the_session_it_opened(fake: FakeZabbix) -> None:
    client = client_for(fake)
    await client.call("hostgroup.get")

    await client.aclose()

    assert fake.logouts == 1


async def test_close_does_not_logout_when_using_a_token(fake: FakeZabbix) -> None:
    fake.api_token = "token-de-api"
    client = client_for(fake, api_token="token-de-api")
    await client.call("hostgroup.get")

    await client.aclose()

    assert fake.logouts == 0
