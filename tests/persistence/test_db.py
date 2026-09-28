from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import inspect, select

from invertio.config import Settings
from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
from invertio.persistence.models import OrderRow


async def test_upgrade_creates_schema_and_roundtrips_decimal_and_utc(
    tmp_path: Path, t0: datetime
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    upgrade_db(settings)
    assert settings.db_path.exists()

    engine = create_engine_async(settings)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert {"signals", "orders", "fills", "equity_snapshots", "audit_log"} <= tables

        sessions = session_factory(engine)
        async with sessions() as session:
            session.add(
                OrderRow(
                    client_order_id="abc",
                    mode="paper",
                    venue="revolutx",
                    symbol="BTC/EUR",
                    side="buy",
                    type="limit",
                    quantity=Decimal("0.00012345"),
                    limit_price=Decimal("91234.56"),
                    post_only=True,
                    strategy_id="ema_cross",
                    status="new",
                    filled_quantity=Decimal(0),
                    created_at=t0,
                )
            )
            await session.commit()

        async with sessions() as session:
            row = (await session.execute(select(OrderRow))).scalar_one()
        assert row.quantity == Decimal("0.00012345")
        assert row.limit_price == Decimal("91234.56")
        assert row.created_at == t0
        assert row.created_at.tzinfo is not None
    finally:
        await engine.dispose()


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    upgrade_db(settings)
    upgrade_db(settings)
