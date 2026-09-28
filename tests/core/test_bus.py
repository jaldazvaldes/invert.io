from dataclasses import dataclass
from datetime import datetime

import pytest

from invertio.core.bus import EventBus
from invertio.core.clock import SimClock
from invertio.core.events import EngineState, EngineStateChanged, Event


@dataclass(frozen=True, slots=True)
class Ping(Event):
    n: int


@dataclass(frozen=True, slots=True)
class Pong(Event):
    n: int


async def test_handlers_run_in_subscription_order_and_nested_publish_is_depth_first() -> None:
    bus = EventBus(strict=True)
    calls: list[str] = []

    async def on_ping(event: Ping) -> None:
        calls.append(f"ping{event.n}")
        await bus.publish(Pong(event.n))

    def on_ping_sync(event: Ping) -> None:
        calls.append(f"ping-sync{event.n}")

    bus.subscribe(Ping, on_ping)
    bus.subscribe(Ping, on_ping_sync)
    bus.subscribe(Pong, lambda e: calls.append(f"pong{e.n}"))

    await bus.publish(Ping(1))
    assert calls == ["ping1", "pong1", "ping-sync1"]


async def test_base_class_subscription_receives_all_events() -> None:
    bus = EventBus(strict=True)
    seen: list[Event] = []
    bus.subscribe(Event, seen.append)
    await bus.publish(Ping(1))
    await bus.publish(EngineStateChanged(EngineState.HALTED, "pánico"))
    assert [type(e) for e in seen] == [Ping, EngineStateChanged]


async def test_unsubscribe() -> None:
    bus = EventBus(strict=True)
    seen: list[Event] = []
    unsubscribe = bus.subscribe(Ping, seen.append)
    unsubscribe()
    await bus.publish(Ping(1))
    assert seen == []


async def test_strict_bus_propagates_errors() -> None:
    bus = EventBus(strict=True)

    def boom(event: Ping) -> None:
        raise RuntimeError("fallo")

    bus.subscribe(Ping, boom)
    with pytest.raises(RuntimeError):
        await bus.publish(Ping(1))


async def test_lenient_bus_isolates_failing_handler() -> None:
    bus = EventBus(strict=False)
    seen: list[Event] = []

    def boom(event: Ping) -> None:
        raise RuntimeError("fallo")

    bus.subscribe(Ping, boom)
    bus.subscribe(Ping, seen.append)
    await bus.publish(Ping(1))
    assert seen == [Ping(1)]


def test_sim_clock_is_monotonic(t0: datetime) -> None:
    clock = SimClock(t0)
    clock.set(t0.replace(minute=35))
    with pytest.raises(ValueError, match="retroceder"):
        clock.set(t0)
