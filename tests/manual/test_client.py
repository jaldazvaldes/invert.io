from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from invertio.manual.client import BASE_URL, RevolutXClient, RevolutXClientError

KEY = "k" * 64  # An ephemeral fake API key, never loaded from the environment.
NOW = 1785313433816
CLIENT_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
VENUE_ID = "7a52e92e-8639-4fe1-abaa-68d3a2d5234b"


@pytest.fixture
def signing_key() -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.generate()


def pem(key: ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture
async def client(signing_key: ed25519.Ed25519PrivateKey) -> AsyncIterator[RevolutXClient]:
    async with httpx.AsyncClient() as transport:
        instance = RevolutXClient(
            KEY,
            pem(signing_key),
            client=transport,
            timestamp_ms=lambda: NOW,
            request_interval=0,
        )
        yield instance
        await instance.close()


def order_response(**overrides: Any) -> dict[str, Any]:
    return {
        "id": VENUE_ID,
        "client_order_id": CLIENT_ID,
        "symbol": "BTC/EUR",
        "side": "buy",
        "type": "limit",
        "quantity": "0.002",
        "filled_quantity": "0.001",
        "leaves_quantity": "0.001",
        "amount": "200",
        "filled_amount": "99.95",
        "price": "100000",
        "average_fill_price": "99950",
        "total_fee": "0.0000009",
        "fee_currency": "BTC",
        "status": "partially_filled",
        "time_in_force": "ioc",
        "execution_instructions": ["allow_taker"],
        "created_date": NOW - 1000,
        "updated_date": NOW,
        **overrides,
    }


def acknowledgement(**overrides: Any) -> dict[str, Any]:
    return {
        "data": {
            "venue_order_id": VENUE_ID,
            "client_order_id": CLIENT_ID,
            "state": "new",
            **overrides,
        }
    }


def verify_signature(request: httpx.Request, key: ed25519.Ed25519PrivateKey) -> None:
    assert request.headers["X-Revx-API-Key"] == KEY
    assert request.headers["X-Revx-Timestamp"] == str(NOW)
    path, _, query = request.url.raw_path.partition(b"?")
    message = str(NOW).encode() + request.method.encode() + path + query + request.content
    key.public_key().verify(base64.b64decode(request.headers["X-Revx-Signature"]), message)


@respx.mock
async def test_signed_balance_request_preserves_decimal_strings(
    client: RevolutXClient,
    signing_key: ed25519.Ed25519PrivateKey,
) -> None:
    route = respx.get(f"{BASE_URL}/api/1.0/balances").respond(
        200,
        json=[
            {
                "currency": "EUR",
                "available": "50.000000000000001",
                "reserved": "0",
                "total": "50.000000000000001",
            }
        ],
    )
    rows = await client.balances()
    assert rows[0]["available"] == "50.000000000000001"
    request = route.calls[0].request
    assert request.content == b""
    verify_signature(request, signing_key)
    assert b"PRIVATE KEY" not in request.content


@respx.mock
async def test_private_configuration_and_order_book_use_account_routes(
    client: RevolutXClient,
    signing_key: ed25519.Ed25519PrivateKey,
) -> None:
    pair = {
        "base": "BTC",
        "quote": "EUR",
        "base_step": "0.00000001",
        "quote_step": "0.01",
        "min_order_size": "0.00001",
        "max_order_size": "9000",
        "min_order_size_quote": "1",
        "status": "active",
    }
    respx.get(f"{BASE_URL}/api/1.0/configuration/pairs").respond(200, json={"BTC/EUR": pair})
    route = respx.get(f"{BASE_URL}/api/1.0/order-book/BTC-EUR", params={"limit": 50}).respond(
        200,
        json={
            "data": {
                "asks": [{"p": "101", "q": "2"}, {"p": "100.01", "q": "1.5"}],
                "bids": [{"p": "99.9", "q": "3"}],
            },
            "metadata": {"timestamp": NOW - 500},
        },
    )
    assert (await client.configuration())["BTC/EUR"] == pair
    book = await client.order_book("BTC/EUR")
    assert book["asks"] == [["100.01", "1.5"], ["101", "2"]]
    assert book["bids"] == [["99.9", "3"]]
    assert book["timestamp"] == NOW - 500 and book["timestamp_source"] == "exchange"
    verify_signature(route.calls[0].request, signing_key)


@respx.mock
async def test_limit_ioc_posts_exact_signed_minified_body_once(
    client: RevolutXClient,
    signing_key: ed25519.Ed25519PrivateKey,
) -> None:
    route = respx.post(f"{BASE_URL}/api/1.0/orders").respond(200, json=acknowledgement())
    result = await client.submit_limit(
        CLIENT_ID, "BTC/EUR", "buy", Decimal("0.0005"), Decimal("99999.99")
    )
    assert result == acknowledgement()["data"]
    assert route.call_count == 1
    request = route.calls[0].request
    body = json.loads(request.content)
    assert body == {
        "client_order_id": CLIENT_ID,
        "symbol": "BTC-EUR",
        "side": "buy",
        "order_configuration": {
            "limit": {
                "base_size": "0.0005",
                "price": "99999.99",
                "time_in_force": "ioc",
                "execution_instructions": ["allow_taker"],
            }
        },
    }
    assert b" " not in request.content
    verify_signature(request, signing_key)


@respx.mock
async def test_order_details_preserve_partial_fill_and_base_currency_fee(
    client: RevolutXClient,
) -> None:
    respx.get(f"{BASE_URL}/api/1.0/orders/{VENUE_ID}").respond(200, json={"data": order_response()})
    row = await client.order(VENUE_ID)
    assert row["filled_quantity"] == "0.001"
    assert row["filled_amount"] == "99.95"
    assert row["average_fill_price"] == "99950"
    assert row["fee_currency"] == "BTC"
    assert row["total_fee"] == "0.0000009"
    assert row["status"] == "partially_filled"


@pytest.mark.parametrize("endpoint", ["active", "historical"])
@respx.mock
async def test_order_lists_paginate_and_sign_exact_encoded_cursor(
    client: RevolutXClient,
    signing_key: ed25519.Ed25519PrivateKey,
    endpoint: str,
) -> None:
    first = order_response(total_fee="0", fee_currency="EUR")
    second = order_response(id=str(uuid4()), client_order_id=str(uuid4()), status="cancelled")
    route = respx.get(f"{BASE_URL}/api/1.0/orders/{endpoint}").mock(
        side_effect=[
            httpx.Response(
                200, json={"data": [first], "metadata": {"timestamp": NOW, "next_cursor": "a+/="}}
            ),
            httpx.Response(
                200, json={"data": [second], "metadata": {"timestamp": NOW, "next_cursor": None}}
            ),
        ]
    )
    rows = await (
        client.active_orders() if endpoint == "active" else client.historical_orders(NOW - 60000)
    )
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert route.call_count == 2
    for call in route.calls:
        verify_signature(call.request, signing_key)
    request = route.calls[1].request
    assert request.url.params["cursor"] == "a+/="
    assert b"cursor=a%2B%2F%3D" in request.url.query
    if endpoint == "historical":
        assert request.url.params["start_date"] == str(NOW - 60000)
        assert request.url.params["end_date"] == str(NOW)


@respx.mock
async def test_cancel_only_acknowledges_http_204_and_is_signed(
    client: RevolutXClient,
    signing_key: ed25519.Ed25519PrivateKey,
) -> None:
    route = respx.delete(f"{BASE_URL}/api/1.0/orders/{VENUE_ID}").respond(204)
    assert await client.cancel(VENUE_ID) is None
    verify_signature(route.calls[0].request, signing_key)
    assert route.call_count == 1


@respx.mock
async def test_submission_timeout_is_ambiguous_sanitized_and_never_retried(
    client: RevolutXClient,
) -> None:
    route = respx.post(f"{BASE_URL}/api/1.0/orders").mock(side_effect=httpx.ReadTimeout(KEY))
    with pytest.raises(RevolutXClientError) as raised:
        await client.submit_limit(CLIENT_ID, "BTC/EUR", "buy", Decimal("0.001"), Decimal("10000"))
    assert raised.value.ambiguous
    assert raised.value.code == "network_error"
    assert KEY not in str(raised.value) and KEY not in repr(raised.value)
    assert raised.value.__suppress_context__
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("status", "ambiguous"),
    [(400, False), (401, False), (409, True), (429, False), (500, True), (201, True), (302, True)],
)
@respx.mock
async def test_submission_http_errors_have_safe_metadata_and_no_retries(
    client: RevolutXClient,
    status: int,
    ambiguous: bool,
) -> None:
    route = respx.post(f"{BASE_URL}/api/1.0/orders").respond(
        status,
        json={"error": f"server echoed {KEY}"},
        headers={"Retry-After": "1500", "Location": "https://elsewhere.invalid/leak"},
    )
    with pytest.raises(RevolutXClientError) as raised:
        await client.submit_limit(CLIENT_ID, "BTC/EUR", "sell", Decimal("0.001"), Decimal("10000"))
    assert raised.value.status_code == status
    assert raised.value.ambiguous == ambiguous
    assert raised.value.retry_after_seconds == (1.5 if status == 429 else None)
    assert KEY not in str(raised.value)
    assert route.call_count == 1


@pytest.mark.parametrize(
    "body",
    [
        {"data": {}},
        acknowledgement(client_order_id=str(uuid4())),
        {"data": [acknowledgement()["data"], acknowledgement()["data"]]},
        acknowledgement(state="unexpected"),
    ],
)
@respx.mock
async def test_unverifiable_acknowledgement_stays_ambiguous(
    client: RevolutXClient,
    body: dict[str, Any],
) -> None:
    route = respx.post(f"{BASE_URL}/api/1.0/orders").respond(200, json=body)
    with pytest.raises(RevolutXClientError) as raised:
        await client.submit_limit(CLIENT_ID, "BTC/EUR", "buy", Decimal("0.001"), Decimal("10000"))
    assert raised.value.ambiguous and raised.value.code == "invalid_response"
    assert route.call_count == 1


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", 1.5, None])
@respx.mock
async def test_invalid_balance_amounts_are_not_coerced_to_money(
    client: RevolutXClient, amount: Any
) -> None:
    respx.get(f"{BASE_URL}/api/1.0/balances").respond(
        200,
        json=[
            {
                "currency": "EUR",
                "available": amount,
                "reserved": "0",
                "total": "50",
            }
        ],
    )
    with pytest.raises(RevolutXClientError, match="respuesta"):
        await client.balances()


@respx.mock
async def test_invalid_order_detail_id_is_rejected(client: RevolutXClient) -> None:
    respx.get(f"{BASE_URL}/api/1.0/orders/{VENUE_ID}").respond(
        200,
        json={"data": order_response(id=str(uuid4()))},
    )
    with pytest.raises(RevolutXClientError) as raised:
        await client.order(VENUE_ID)
    assert raised.value.code == "invalid_response"


@respx.mock
async def test_repeating_cursor_never_returns_a_partial_list(client: RevolutXClient) -> None:
    route = respx.get(f"{BASE_URL}/api/1.0/orders/active").respond(
        200,
        json={
            "data": [order_response()],
            "metadata": {"timestamp": NOW, "next_cursor": "repeat"},
        },
    )
    with pytest.raises(RevolutXClientError) as raised:
        await client.active_orders()
    assert raised.value.code == "pagination_incomplete"
    assert route.call_count == 2


@respx.mock
async def test_invalid_inputs_do_not_send_network_requests(client: RevolutXClient) -> None:
    for side, quantity, price in [
        ("BUY", Decimal("1"), Decimal("2")),
        ("buy", Decimal("0"), Decimal("2")),
        ("sell", Decimal("1"), Decimal("NaN")),
    ]:
        with pytest.raises(RevolutXClientError) as raised:
            await client.submit_limit(CLIENT_ID, "BTC/EUR", side, quantity, price)
        assert raised.value.code == "invalid_request" and not raised.value.ambiguous
    with pytest.raises(RevolutXClientError):
        await client.cancel("../../orders")
    with pytest.raises(RevolutXClientError):
        await client.order_book("BTC/EUR?leak=1")
    with pytest.raises(RevolutXClientError) as raised:
        await client.historical_orders(NOW - 31 * 86_400_000)
    assert raised.value.code == "historical_range_too_long"
    assert len(respx.calls) == 0


def test_constructor_rejects_invalid_or_non_ed25519_credentials(
    signing_key: ed25519.Ed25519PrivateKey,
) -> None:
    with pytest.raises(RevolutXClientError) as raised:
        RevolutXClient("bad-secret", pem(signing_key))
    assert "bad-secret" not in str(raised.value)
    with pytest.raises(RevolutXClientError):
        RevolutXClient(KEY, b"not-a-pem-secret")
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(RevolutXClientError):
        RevolutXClient(KEY, pem(rsa_key))
