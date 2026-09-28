from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from invertio.api.app import create_app
from invertio.api.context import ApiContext
from invertio.config.settings import Settings
from invertio.data.store import BarStore
from invertio.manual.repository import ManualRepository
from invertio.manual.service import ManualError, ManualService
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from tests.helpers import repo_config


class FakeClient:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 28, 16, tzinfo=UTC)
        self.cash = "1000"
        self.base_balance = "0"
        self.ask = "100"
        self.bid = "99.99"
        self.book_age = 0
        self.sent = 0
        self.cancelled = 0
        self.timeout = False
        self.keep_remote = True
        self.fill_ratio = Decimal("1")
        self.fee_currency = "EUR"
        self.fee = "0.01"
        self.remotes: dict[str, dict[str, Any]] = {}

    async def balances(self) -> list[dict[str, Any]]:
        return [
            {"currency": "EUR", "available": self.cash, "reserved": "0", "total": self.cash},
            {
                "currency": "ABC",
                "available": self.base_balance,
                "reserved": "0",
                "total": self.base_balance,
            },
        ]

    async def configuration(self) -> dict[str, dict[str, Any]]:
        return {
            "ABC/EUR": {
                "base": "ABC",
                "quote": "EUR",
                "status": "active",
                "base_step": "0.0001",
                "quote_step": "0.01",
                "min_order_size": "0.001",
                "max_order_size": "10000",
                "min_order_size_quote": "1",
            }
        }

    async def order_book(self, symbol: str) -> dict[str, Any]:
        return {
            "ts": (self.now - timedelta(seconds=self.book_age)).isoformat(),
            "asks": [[self.ask, "1000"]],
            "bids": [[self.bid, "1000"]],
        }

    async def submit_limit(
        self, client_order_id: str, symbol: str, side: str, quantity: Decimal, price: Decimal
    ) -> dict[str, Any]:
        self.sent += 1
        ident = str(uuid4())
        filled = quantity * self.fill_ratio
        if self.keep_remote:
            self.remotes[ident] = {
                "id": ident,
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side,
                "quantity": str(quantity),
                "type": "limit",
                "time_in_force": "ioc",
                "filled_quantity": str(filled),
                "price": str(price),
                "status": "filled" if self.fill_ratio == 1 else "cancelled",
                "total_fee": self.fee,
                "fee_currency": self.fee_currency,
                "filled_amount": str(filled * price),
                "average_fill_price": str(price),
            }
            self.base_balance = str(filled)
        if self.timeout:
            raise TimeoutError("Request secret must not leak")
        return {"venue_order_id": ident, "client_order_id": client_order_id, "state": "pending"}

    async def order(self, venue_order_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.remotes[venue_order_id])

    async def active_orders(self) -> list[dict[str, Any]]:
        return []

    async def historical_orders(self, start_date_ms: int) -> list[dict[str, Any]]:
        return list(self.remotes.values())

    async def cancel(self, venue_order_id: str) -> None:
        self.cancelled += 1
        self.remotes[venue_order_id]["status"] = "cancelled"

    async def close(self) -> None:
        pass


@pytest.fixture
async def setup(tmp_path: Path) -> AsyncIterator[tuple[ManualService, FakeClient, ApiContext]]:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    db = create_engine_async(settings)
    sessions = session_factory(db)
    client = FakeClient()
    service = ManualService(
        ManualRepository(sessions),
        client,
        enabled=True,
        account_id="account-a",
        clock=lambda: client.now,
    )
    ctx = ApiContext(
        settings, repo_config(), None, sessions, BarStore(tmp_path / "bars"), manual=service
    )
    yield service, client, ctx
    await db.dispose()


async def test_preview_is_readonly_and_budget_includes_fees(setup: Any) -> None:
    service, client, _ = setup
    row = await service.preview("ABC/EUR", "buy", budget_eur="50")
    assert client.sent == 0
    assert Decimal(row["max_debit_eur"]) <= 50
    assert Decimal(row["fee_reserve_eur"]) > 0
    assert Decimal(row["quantity"]) * Decimal(row["limit_price"]) < 50
    assert (await service.status())["committed_eur"] == "0"
    service.enabled = False
    with pytest.raises(ManualError, match="desactivadas"):
        await service.confirm(row["id"])
    assert client.sent == 0


async def test_double_confirmation_and_restart_send_once(setup: Any) -> None:
    service, client, _ = setup
    row = await service.preview("ABC/EUR", "buy", budget_eur="20")
    results = await asyncio.gather(service.confirm(row["id"]), service.confirm(row["id"]))
    assert client.sent == 1
    assert all(r["status"] == "filled" for r in results)
    restarted = ManualService(
        service.repository, client, enabled=True, account_id="account-a", clock=lambda: client.now
    )
    assert (await restarted.confirm(row["id"]))["status"] == "filled"
    assert client.sent == 1


async def test_timeout_reconciles_by_original_client_id_no_retry(setup: Any) -> None:
    service, client, _ = setup
    client.timeout = True
    row = await service.preview("ABC/EUR", "buy", budget_eur="20")
    assert (await service.confirm(row["id"]))["status"] == "filled"
    assert client.sent == 1
    await service.confirm(row["id"])
    assert client.sent == 1


async def test_unknown_order_remains_reserved_across_restart(setup: Any) -> None:
    service, client, _ = setup
    client.timeout, client.keep_remote = True, False
    row = await service.preview("ABC/EUR", "buy", budget_eur="50")
    assert (await service.confirm(row["id"]))["status"] == "unknown"
    restarted = ManualService(
        service.repository, client, enabled=True, account_id="account-a", clock=lambda: client.now
    )
    await restarted.refresh()
    with pytest.raises(ManualError, match="pendiente o incierta"):
        await restarted.preview("ABC/EUR", "buy", budget_eur="10")
    assert Decimal((await restarted.status())["committed_eur"]) > 49
    await restarted.confirm(row["id"])
    assert client.sent == 1


async def test_stored_submitting_from_crash_does_not_resend(setup: Any) -> None:
    service, client, _ = setup
    row = await service.preview("ABC/EUR", "buy", budget_eur="20")
    row["status"] = "submitting"
    await service.repository.save(row)
    await service.refresh()
    assert (await service.confirm(row["id"]))["status"] == "unknown"
    assert client.sent == 0


async def test_expired_preview_cannot_send(setup: Any) -> None:
    service, client, _ = setup
    row = await service.preview("ABC/EUR", "buy", budget_eur="20")
    client.now += timedelta(seconds=31)
    assert (await service.confirm(row["id"]))["status"] == "expired"
    assert client.sent == 0


@pytest.mark.parametrize("change", ["balance", "price", "stale"])
async def test_revalidate_before_submission(setup: Any, change: str) -> None:
    service, client, _ = setup
    row = await service.preview("ABC/EUR", "buy", budget_eur="20")
    if change == "balance":
        client.cash = "0"
    elif change == "price":
        client.ask, client.bid = "110", "109.99"
    else:
        client.book_age = 11
    with pytest.raises(ManualError):
        await service.confirm(row["id"])
    assert client.sent == 0


async def test_other_preview_rechecked_against_total_budget(setup: Any) -> None:
    service, client, _ = setup
    first = await service.preview("ABC/EUR", "buy", budget_eur="30")
    second = await service.preview("ABC/EUR", "buy", budget_eur="30")
    await service.confirm(first["id"])
    with pytest.raises(ManualError, match="50"):
        await service.confirm(second["id"])
    assert client.sent == 1


async def test_partial_ioc_only_credits_actual_fill_and_base_fee(setup: Any) -> None:
    service, client, _ = setup
    client.fill_ratio = Decimal("0.5")
    client.fee_currency, client.fee = "ABC", "0.0001"
    row = await service.preview("ABC/EUR", "buy", budget_eur="50")
    done = await service.confirm(row["id"])
    status = await service.status()
    assert done["status"] == "cancelled"
    qty = Decimal(done["filled_quantity"]) - Decimal("0.0001")
    assert Decimal(status["positions"][0]["quantity"]) == qty
    assert Decimal(status["committed_eur"]) < 26
    # A preexisting account balance cannot authorize selling unrelated holdings.
    client.base_balance = "100"
    with pytest.raises(ManualError, match="comprada desde este panel"):
        await service.preview("ABC/EUR", "sell", quantity=str(qty + Decimal("0.001")))
    sell = await service.preview("ABC/EUR", "sell", quantity=str(qty))
    assert Decimal(sell["quantity"]) <= qty
    assert client.sent == 1


async def test_account_separation_prevents_selling_another_account_holdings(setup: Any) -> None:
    service, client, _ = setup
    row = await service.preview("ABC/EUR", "buy", budget_eur="20")
    await service.confirm(row["id"])
    other = ManualService(
        service.repository, client, enabled=True, account_id="account-b", clock=lambda: client.now
    )
    assert (await other.status())["orders"] == []
    assert (await other.status())["enabled"] is False
    with pytest.raises(ManualError, match="clave API ha cambiado"):
        await other.preview("ABC/EUR", "sell", quantity="0.1")
    with pytest.raises(ManualError, match="clave API ha cambiado"):
        await other.preview("ABC/EUR", "buy", budget_eur="20")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "50.01", "1.001"])
async def test_invalid_buy_amounts_do_not_send(setup: Any, value: str) -> None:
    service, client, _ = setup
    with pytest.raises(ManualError):
        await service.preview("ABC/EUR", "buy", budget_eur=value)
    assert client.sent == 0


async def test_api_token_confirmation_and_no_implicit_orders(setup: Any) -> None:
    _service, fake, ctx = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/manual/status")).status_code == 200
        body = {"symbol": "ABC/EUR", "side": "buy", "budget_eur": "20"}
        assert (await client.post("/api/manual/preview", json=body)).status_code == 403
        headers = {"X-Invertio-Token": ctx.token}
        response = await client.post("/api/manual/preview", json=body, headers=headers)
        assert response.status_code == 200
        row = response.json()
        assert fake.sent == 0
        confirmed = {"preview_id": row["id"], "confirm": False}
        assert (
            await client.post("/api/manual/confirm", json=confirmed, headers=headers)
        ).status_code == 400
        confirmed["confirm"] = True
        assert (await client.post("/api/manual/confirm", json=confirmed)).status_code == 403
        changed = {**confirmed, "quantity": "50"}
        assert (
            await client.post("/api/manual/confirm", json=changed, headers=headers)
        ).status_code == 422
        assert fake.sent == 0
        assert (await client.post("/api/manual/confirm", json=confirmed, headers=headers)).json()[
            "status"
        ] == "filled"
        assert fake.sent == 1
        for table in ("orders", "fills", "analysis_opportunities", "signals"):
            from sqlalchemy import text

            async with ctx.sessions() as session:
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
