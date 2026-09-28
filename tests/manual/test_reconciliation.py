from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest

from invertio.api.context import ApiContext
from invertio.manual.client import RevolutXClientError
from invertio.manual.service import ManualError, ManualService
from tests.manual.test_service import FakeClient
from tests.manual.test_service import setup as setup

Harness = tuple[ManualService, FakeClient, ApiContext]


async def test_key_rotation_blocks_existing_preview_and_new_buys(setup: Harness) -> None:
    service, client, _ = setup
    original = await service.preview("ABC/EUR", "buy", budget_eur="20")
    rotated = ManualService(
        service.repository, client, enabled=True, account_id="new-key", clock=lambda: client.now
    )
    # Preparar con otra huella aún es seguro mientras no se haya enviado ninguna orden.
    other_preview = await rotated.preview("ABC/EUR", "buy", budget_eur="20")
    await service.confirm(original["id"])
    before = await service.repository.orders()
    with pytest.raises(ManualError, match="clave API ha cambiado"):
        await rotated.confirm(other_preview["id"])
    with pytest.raises(ManualError, match="clave API ha cambiado"):
        await rotated.preview("ABC/EUR", "buy", budget_eur="50")
    with pytest.raises(ManualError, match="clave API ha cambiado"):
        await rotated.preview("ABC/EUR", "sell", quantity="0.1")
    status = await rotated.refresh()
    assert status["enabled"] is False
    assert "clave API ha cambiado" in status["error"]
    assert await service.repository.orders() == before
    assert client.sent == 1


async def test_key_rotation_cannot_ignore_unknown_submission(setup: Harness) -> None:
    service, client, _ = setup
    client.timeout, client.keep_remote = True, False
    preview = await service.preview("ABC/EUR", "buy", budget_eur="50")
    assert (await service.confirm(preview["id"]))["status"] == "unknown"
    rotated = ManualService(
        service.repository, client, enabled=True, account_id="new-key", clock=lambda: client.now
    )
    with pytest.raises(ManualError, match="clave API ha cambiado"):
        await rotated.preview("ABC/EUR", "buy", budget_eur="1")
    assert (await rotated.status())["enabled"] is False
    original_status = await service.status()
    assert original_status["committed_eur"] == preview["max_debit_eur"]
    assert original_status["orders"][0]["status"] == "unknown"
    assert client.sent == 1


@pytest.mark.parametrize(
    "invalid",
    [
        {"price": "120"},
        {"time_in_force": "gtc"},
        {"type": "market"},
        {"filled_quantity": "0", "filled_amount": "0", "total_fee": "0"},
        {"filled_amount": "99999"},
        {"filled_amount": None},
        {"average_fill_price": "120"},
        {"total_fee": None},
        {"total_fee": "-1"},
        {"total_fee": "NaN"},
        {"total_fee": "0.01", "fee_currency": "USD"},
        {"total_fee": "1", "fee_currency": "ABC"},
        {
            "status": "cancelled",
            "filled_quantity": "0",
            "filled_amount": "0",
            "total_fee": "0.01",
            "fee_currency": "EUR",
        },
    ],
    ids=[
        "changed-price",
        "changed-tif",
        "changed-type",
        "filled-without-fill",
        "inconsistent-amount",
        "missing-amount",
        "above-limit-average",
        "missing-fee",
        "negative-fee",
        "nonfinite-fee",
        "unknown-fee-currency",
        "excessive-base-fee",
        "fee-without-fill",
    ],
)
async def test_inconsistent_execution_stays_reserved_across_restart(
    setup: Harness, monkeypatch: pytest.MonkeyPatch, invalid: dict[str, Any]
) -> None:
    service, client, _ = setup
    original_order = client.order

    async def inconsistent_order(venue_order_id: str) -> dict[str, Any]:
        remote = await original_order(venue_order_id)
        remote.update(invalid)
        return remote

    monkeypatch.setattr(client, "order", inconsistent_order)
    preview = await service.preview("ABC/EUR", "buy", budget_eur="20")
    confirmed = await service.confirm(preview["id"])
    assert confirmed["status"] == "pending"
    assert confirmed["filled_quantity"] == "0"
    assert client.sent == 1
    restarted = ManualService(
        service.repository, client, enabled=True, account_id="account-a", clock=lambda: client.now
    )
    status = await restarted.refresh()
    assert status["committed_eur"] == preview["max_debit_eur"]
    assert status["positions"] == []
    assert "No se pudo conciliar" in status["error"]
    assert (await restarted.confirm(preview["id"]))["status"] == "pending"
    with pytest.raises(ManualError, match="pendiente o incierta"):
        await restarted.preview("ABC/EUR", "buy", budget_eur="10")
    assert client.sent == 1


@pytest.mark.parametrize("http_status", [404, 500])
async def test_failed_order_lookup_never_releases_submission_or_reposts(
    setup: Harness, monkeypatch: pytest.MonkeyPatch, http_status: int
) -> None:
    service, client, _ = setup

    async def unavailable(venue_order_id: str) -> dict[str, Any]:
        raise RevolutXClientError("http_error", status_code=http_status)

    monkeypatch.setattr(client, "order", unavailable)
    preview = await service.preview("ABC/EUR", "buy", budget_eur="20")
    assert (await service.confirm(preview["id"]))["status"] == "pending"
    await service.refresh()
    assert (await service.confirm(preview["id"]))["status"] == "pending"
    assert (await service.status())["committed_eur"] == preview["max_debit_eur"]
    assert client.sent == 1


async def test_valid_reconciliation_after_rejection_of_bad_response_credits_once(
    setup: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, client, _ = setup
    original_order = client.order

    async def wrong_price(venue_order_id: str) -> dict[str, Any]:
        remote = await original_order(venue_order_id)
        remote["price"] = "200"
        return remote

    monkeypatch.setattr(client, "order", wrong_price)
    preview = await service.preview("ABC/EUR", "buy", budget_eur="20")
    assert (await service.confirm(preview["id"]))["status"] == "pending"
    monkeypatch.setattr(client, "order", original_order)
    status = await service.refresh()
    assert status["orders"][0]["status"] == "filled"
    assert status["positions"] == [{"symbol": "ABC/EUR", "quantity": preview["quantity"]}]
    snapshot = copy.deepcopy(status)
    assert await service.refresh() == snapshot
    assert client.sent == 1


@pytest.mark.parametrize(
    ("actual_amount", "fee_currency", "fee", "expected_committed", "expected_quantity"),
    [
        ("10.0000100", "EUR", "0.01", "10.02", "0.1000001"),
        ("10.0000100", "EUR", "0.10", "10.11", "0.1000001"),
        ("9", "EUR", "0.01", "10.02", "0.1000001"),
        ("10.0000100", "ABC", "0.0001", "10.02", "0.0999001"),
    ],
    ids=["separate-rounding", "real-fee-larger", "conservative-limit", "net-base-fee"],
)
async def test_budget_uses_conservative_and_real_debits_without_recycling_sales(
    setup: Harness,
    actual_amount: str,
    fee_currency: str,
    fee: str,
    expected_committed: str,
    expected_quantity: str,
) -> None:
    service, _, _ = setup
    purchased = {
        "symbol": "ABC/EUR",
        "side": "buy",
        "status": "filled",
        "filled_quantity": "0.1000001",
        "limit_price": "100",
        "max_debit_eur": "10.02",
        "execution": {
            "filled_amount": actual_amount,
            "fee_currency": fee_currency,
            "total_fee": fee,
        },
    }
    committed, holdings = service._ledger([purchased])
    assert committed == Decimal(expected_committed)
    assert holdings["ABC/EUR"] == Decimal(expected_quantity)
    sold = {
        "symbol": "ABC/EUR",
        "side": "sell",
        "status": "filled",
        "filled_quantity": expected_quantity,
        "limit_price": "200",
        "execution": {"filled_amount": "19", "fee_currency": "EUR", "total_fee": "0.02"},
    }
    after_sale, holdings = service._ledger([purchased, sold])
    assert after_sale == committed
    assert holdings["ABC/EUR"] == 0
