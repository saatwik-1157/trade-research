"""Registering the MT5 DEMO venue: the seat that was empty for eleven levels.

`MT5Adapter` has existed since L10 and had no runtime registration path, so the
platform's execution machinery had never run against a venue that was not a
simulator. `_ADAPTERS["mt5_demo"]` is that path.

Every test here is about what the route REFUSES, plus one that proves it can
still succeed — a registration route that always failed would be
indistinguishable from a broken one.

The adapter is stubbed rather than driven against a real terminal. What changed
is the route's logic: which failures register nothing, which release the
connection, and whether the account the terminal actually holds is the one
somebody named. A live terminal would test MetaTrader, not that.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from app.auth.csrf import CSRF_COOKIE, CSRF_HEADER
from app.auth.models import Role, User
from app.brokers.base import Account, AccountMode, ConnectionState, NotConnected, RefuseToTrade
from app.core.settings import Settings, TradingMode
from app.db.base import Base
from app.main import create_app
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

ALICE = {"email": "alice@example.org", "password": "correct-horse-battery-staple"}

#: The MetaTrader5 package is Windows-only, so the one test below that drives a
#: REAL terminal cannot run in CI or in the Linux API image. It is skipped
#: rather than deleted: on the machine that has a terminal it is the only test
#: in this repository that exercises the route against a venue nobody here
#: wrote, and that is the gap `KNOWN_TEST_LIMITATIONS.md` opens with.
_MT5_INSTALLED = importlib.util.find_spec("MetaTrader5") is not None


# --------------------------------------------------------------- fixtures


async def _app_in(mode: TradingMode) -> AsyncIterator[FastAPI]:
    """An application on an in-memory database, running in one trading mode.

    The route's mode fence requires the platform's `TRADING_MODE` to equal the
    adapter's, so both modes are needed here: demo to reach the success path,
    paper to prove the fence refuses.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    import app.auth.models  # noqa: F401 - register the auth tables
    import app.models  # noqa: F401 - register every platform table

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    application = create_app(Settings(_env_file=None, trading_mode=mode), checks={}, engine=engine)
    yield application
    await engine.dispose()


@pytest.fixture
async def demo_app() -> AsyncIterator[FastAPI]:
    async for application in _app_in(TradingMode.demo):
        yield application


@pytest.fixture
async def paper_app() -> AsyncIterator[FastAPI]:
    async for application in _app_in(TradingMode.paper):
        yield application


async def _client_for(application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as c:
        yield c


@pytest.fixture
async def demo_client(demo_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async for c in _client_for(demo_app):
        yield c


@pytest.fixture
async def paper_client(paper_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async for c in _client_for(paper_app):
        yield c


def _csrf(client: AsyncClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


async def _admin(app: FastAPI, client: AsyncClient) -> None:
    await client.post("/auth/register", json=ALICE)
    async with app.state.session_factory() as db:
        user = await db.scalar(select(User).where(User.email == ALICE["email"]))
        assert user is not None
        user.role = Role.admin.value
        await db.commit()


async def _step_up(client: AsyncClient, subject: str) -> None:
    r = await client.post(
        "/v1/security/step-up",
        headers=_csrf(client),
        json={
            "password": ALICE["password"],
            "scope": "BROKER_CREDENTIALS",
            "subject": subject,
        },
    )
    assert r.status_code == 201, r.text


async def _register(
    client: AsyncClient, *, adapter: str = "mt5_demo", account_id: str = "acct-d", **extra: Any
):
    body = {"account_id": account_id, "adapter": adapter, "reason": "demo venue pilot"}
    body.update(extra)
    return await client.post("/v1/brokers/adapters", headers=_csrf(client), json=body)


class StubTerminal:
    """An `MT5Adapter`-shaped stub: one connect outcome, and a close counter."""

    name = "mt5"
    mode = "demo"

    def __init__(self, *, outcome: Any, login: str = "5055473926") -> None:
        self._outcome = outcome
        self._login = login
        self.state = ConnectionState.disconnected
        self.disconnects = 0

    async def connect(self) -> Account:
        if isinstance(self._outcome, Exception):
            # As the real adapter does: the terminal handle is already open
            # when the fence refuses, which is why disconnect has to happen.
            self.state = ConnectionState.error
            raise self._outcome
        self.state = ConnectionState.connected
        return Account(
            login=self._login,
            server="MetaQuotes-Demo",
            currency="USD",
            balance=Decimal("100000"),
            equity=Decimal("100000"),
            free_margin=Decimal("99000"),
            mode=AccountMode.demo,
            trade_allowed=True,
        )

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.state = ConnectionState.disconnected


def _stub(monkeypatch: pytest.MonkeyPatch, stub: StubTerminal) -> StubTerminal:
    import app.api.v1.brokers as routes

    monkeypatch.setattr(routes, "_build_adapter", lambda key, mode, settings: stub)
    return stub


# ------------------------------------------------------- the map itself


def test_no_registrable_adapter_is_a_live_venue() -> None:
    """The invariant that replaced "the simulator only".

    Adding the demo venue widened what can be registered. It must not have
    widened it to a live one, and the check is on the VALUES: a key named
    innocently that mapped to `live` would pass a check on the keys.
    """
    from app.api.v1.brokers import _ADAPTERS

    assert set(_ADAPTERS) == {"simulator", "mt5_demo"}
    assert set(_ADAPTERS.values()) == {"paper", "demo"}
    assert "live" not in _ADAPTERS.values()


def test_every_adapter_key_has_a_constructor() -> None:
    """A key in the map with no branch in the builder would register nothing.

    `_build_adapter` raises rather than returning None for exactly that case;
    this asserts the two never drift apart in the first place.
    """
    from app.api.v1.brokers import _ADAPTERS, _build_adapter

    settings = Settings(_env_file=None)
    for key, mode in _ADAPTERS.items():
        adapter = _build_adapter(key, mode, settings)
        assert adapter.mode == mode, key


def test_the_builder_returns_the_real_mt5_adapter() -> None:
    """Not a copy, not a wrapper -- the adapter that already wraps the toolkit."""
    from app.api.v1.brokers import _build_adapter
    from app.brokers.mt5 import MT5Adapter

    built = _build_adapter("mt5_demo", "demo", Settings(_env_file=None))
    assert isinstance(built, MT5Adapter)
    assert built.mode == "demo"


def test_the_builder_refuses_an_unknown_key() -> None:
    from app.api.v1.brokers import _build_adapter
    from app.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        _build_adapter("mt5_live", "live", Settings(_env_file=None))


# ------------------------------------------------------------ the route


async def test_a_demo_venue_is_refused_on_a_paper_platform(
    paper_app: FastAPI, paper_client: AsyncClient
) -> None:
    """The mode fence, and it is why this route cannot become a live one.

    The default test app runs paper. A demo adapter on a paper platform is a
    configuration nobody meant, so it is refused rather than reconciled.
    """
    await _admin(paper_app, paper_client)
    await _step_up(paper_client, "acct-d")

    r = await _register(paper_client)
    assert r.status_code == 409, r.text
    assert "environment the platform is not in" in r.json()["error"]["detail"]
    assert "acct-d" not in paper_app.state.brokers.adapters
    assert "acct-d" not in paper_app.state.order_managers.managers


async def test_registering_the_demo_venue_still_requires_step_up(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _stub(monkeypatch, StubTerminal(outcome=None))
    await _admin(demo_app, demo_client)

    r = await _register(demo_client)
    assert r.status_code in (401, 403), r.text
    assert "acct-d" not in demo_app.state.brokers.adapters
    # It never got as far as opening a terminal.
    assert stub.disconnects == 0


async def test_the_demo_fence_refusal_registers_nothing(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The test that matters.**

    `assert_demo` refuses REAL, CONTEST and any `trade_mode` it does not
    recognise. When it does, the route must leave both registries empty and
    release the terminal handle the adapter had already opened -- and it must
    not present the refusal as a connection problem to be retried.
    """
    stub = _stub(
        monkeypatch,
        StubTerminal(
            outcome=RefuseToTrade(
                "Account 900123 on RealBroker-Live is a REAL account. "
                "This tool only runs on DEMO accounts"
            )
        ),
    )
    # Pinned, exactly as the two tests below pin it. The route tells a fence
    # refusal from a missing package by asking whether MetaTrader5 imports, so
    # a test that leaves that to the host is asking about the developer's
    # laptop: this passed on Windows, where the package is present, and
    # returned 503 on the Linux runner, where the fence refusal this test
    # exists to check can never be reported as one.
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    await _admin(demo_app, demo_client)
    await _step_up(demo_client, "acct-d")

    r = await _register(demo_client)
    assert r.status_code == 409, r.text
    detail = r.json()["error"]["detail"]
    assert "REAL account" in detail
    assert "Nothing was registered" in detail

    assert "acct-d" not in demo_app.state.brokers.adapters
    assert "acct-d" not in demo_app.state.order_managers.managers
    assert stub.disconnects == 1, "a refused registration leaked the terminal handle"


async def test_an_unreachable_terminal_registers_nothing(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry entry for a venue that never answered is worse than none.

    An empty seat stops the execution path at `no_venue`. A filled one makes it
    try.
    """
    stub = _stub(monkeypatch, StubTerminal(outcome=NotConnected("terminal not running")))
    await _admin(demo_app, demo_client)
    await _step_up(demo_client, "acct-d")

    r = await _register(demo_client)
    assert r.status_code == 503, r.text
    assert "Nothing was registered" in r.json()["error"]["detail"]
    assert "acct-d" not in demo_app.state.brokers.adapters
    assert "acct-d" not in demo_app.state.order_managers.managers
    assert stub.disconnects == 1


async def test_an_unexpected_account_is_refused_and_never_switched(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registering is the moment the platform commits to an account.

    A terminal logged into a different account than the operator named is the
    accident this refuses: every other signal -- the balance, the symbol list,
    the window title -- looks plausible on both accounts.
    """
    stub = _stub(monkeypatch, StubTerminal(outcome=None, login="5055473926"))
    await _admin(demo_app, demo_client)
    await _step_up(demo_client, "acct-d")

    r = await _register(demo_client, expect_account="9999999")
    assert r.status_code == 409, r.text
    detail = r.json()["error"]["detail"]
    assert "5055473926" in detail
    assert "no account was switched" in detail
    assert "acct-d" not in demo_app.state.brokers.adapters
    assert stub.disconnects == 1


async def test_a_matching_expected_account_is_accepted(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, StubTerminal(outcome=None, login="5055473926"))
    await _admin(demo_app, demo_client)
    await _step_up(demo_client, "acct-d")

    r = await _register(demo_client, expect_account="5055473926")
    assert r.status_code == 201, r.text
    assert r.json()["venue_account"]["login"] == "5055473926"


async def test_registering_the_demo_venue_fills_both_registries(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the change.

    The execution path reads `OrderManagerRegistry`; the observation routes read
    `BrokerRegistry`. Filling one and not the other gives a platform that
    reports a healthy venue and refuses every signal with `no_venue`, or the
    reverse.
    """
    _stub(monkeypatch, StubTerminal(outcome=None))
    await _admin(demo_app, demo_client)
    await _step_up(demo_client, "acct-d")

    r = await _register(demo_client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["mode"] == "demo"
    assert body["adapter"] == "mt5_demo"

    assert "acct-d" in demo_app.state.brokers.adapters
    assert "acct-d" in demo_app.state.order_managers.managers
    assert demo_app.state.order_managers.get("acct-d").mode == "demo"


async def test_the_response_reports_the_account_the_venue_holds(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Echoing back what the caller typed would tell them nothing."""
    _stub(monkeypatch, StubTerminal(outcome=None, login="5055473926"))
    await _admin(demo_app, demo_client)
    await _step_up(demo_client, "acct-d")

    body = (await _register(demo_client)).json()
    venue = body["venue_account"]
    assert venue["login"] == "5055473926"
    assert venue["server"] == "MetaQuotes-Demo"
    assert venue["mode"] == "demo"
    assert venue["trade_allowed"] is True
    assert "live execution remains blocked" in body["note"]


async def test_registering_a_demo_venue_does_not_enable_live_execution(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The safety test for this change.**

    Giving the platform a real venue is the largest step it has taken toward
    execution. It must move nothing at all toward live: the gates, the flag and
    the blocker count are the same afterwards as before.
    """
    _stub(monkeypatch, StubTerminal(outcome=None))
    await _admin(demo_app, demo_client)
    await _step_up(demo_client, "acct-d")

    settings = demo_app.state.settings
    before = settings.live_execution_blockers()
    assert (await _register(demo_client)).status_code == 201

    after = settings.live_execution_blockers()
    assert after == before
    assert settings.live_execution_allowed is False
    assert settings.live_trading is False
    assert settings.trading_mode is TradingMode.demo

    from app.core.settings import LIVE_GATES

    assert not any(LIVE_GATES.values()), "registering a venue flipped a live gate"


async def test_a_venue_is_still_never_silently_replaced(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, StubTerminal(outcome=None))
    await _admin(demo_app, demo_client)

    await _step_up(demo_client, "acct-d")
    assert (await _register(demo_client)).status_code == 201

    await _step_up(demo_client, "acct-d")
    second = await _register(demo_client)
    assert second.status_code == 409
    assert "already has a venue" in second.json()["error"]["detail"]


async def test_a_missing_mt5_package_is_a_dependency_problem_not_a_fence_refusal(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503, not 409, and the difference is not cosmetic.

    The toolkit raises `RefuseToTrade` for a MISSING MetaTrader5 package as well
    as for the demo fence. Reported as a 409 the message reads "the terminal
    refused this registration", which sends an operator to check their account
    when the actual fix is that the API is running on Linux, where the package
    does not exist. The two are told apart by whether the module can be
    imported at all -- a fact, not a substring of the message.
    """
    _stub(
        monkeypatch,
        StubTerminal(outcome=RefuseToTrade("MetaTrader5 is not installed.")),
    )
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: None if name == "MetaTrader5" else object()
    )
    await _admin(demo_app, demo_client)
    await _step_up(demo_client, "acct-d")

    r = await _register(demo_client)
    assert r.status_code == 503, r.text
    detail = r.json()["error"]["detail"]
    assert "Windows-only" in detail
    assert "Nothing was registered" in detail
    assert "acct-d" not in demo_app.state.brokers.adapters


async def test_a_real_fence_refusal_is_still_a_conflict(
    demo_app: FastAPI, demo_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same fork: package present, account wrong."""
    _stub(monkeypatch, StubTerminal(outcome=RefuseToTrade("is a REAL account")))
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    await _admin(demo_app, demo_client)
    await _step_up(demo_client, "acct-d")

    r = await _register(demo_client)
    assert r.status_code == 409, r.text
    assert "the terminal refused this registration" in r.json()["error"]["detail"]


# ------------------------------------------------- against a real terminal


@pytest.mark.skipif(not _MT5_INSTALLED, reason="MetaTrader5 is Windows-only")
async def test_the_real_adapter_registers_against_a_live_demo_terminal(
    demo_app: FastAPI, demo_client: AsyncClient
) -> None:
    """No stub. The route builds a real `MT5Adapter` and opens a real terminal.

    This is the only test here that can be wrong about MetaTrader rather than
    about the route, which is the point: everything else in this file agrees
    with a fake this repository wrote.

    It registers and reads. It sends nothing -- `connect` runs `assert_demo`
    with `live=False`, so it does not even require the terminal's Algo Trading
    switch, and `POST /v1/orders` remains the only submission door.
    """
    await _admin(demo_app, demo_client)
    await _step_up(demo_client, "acct-d")

    r = await _register(demo_client)
    if r.status_code == 503:
        pytest.skip(f"no usable MetaTrader terminal here: {r.json()['error']['detail']}")
    assert r.status_code == 201, r.text

    body = r.json()
    assert body["mode"] == "demo"
    assert body["state"] == "connected"
    # The fence let it through, so the terminal is on a demo account. If it
    # ever reports anything else, `assert_demo` has stopped working.
    assert body["venue_account"]["mode"] == "demo"
    assert body["venue_account"]["login"]

    assert "acct-d" in demo_app.state.brokers.adapters
    assert demo_app.state.order_managers.get("acct-d").mode == "demo"

    health = await demo_app.state.brokers.get("acct-d").health()
    assert health.usable, health.as_dict()

    # Leave no terminal handle behind: the fixture tears down the app, not the
    # connection the route opened.
    await demo_app.state.brokers.get("acct-d").disconnect()


# ------------------------------------------------- acting vs seeing


def test_the_adapter_does_not_share_the_toolkits_magic() -> None:
    """**The fix for a real incident.**

    They shared 770315 until 2026-09-07, deliberately, "so both see the same
    positions". Seeing was never the problem -- ACTING was: `close_own` closes
    every position carrying its magic, so the running harness harvested two
    positions the platform had opened and the platform's rows went stale
    underneath it.
    """
    from app.brokers import mt5 as adapter

    assert adapter.MAGIC != adapter.TOOLKIT_MAGIC
    assert adapter.TOOLKIT_MAGIC == 770315, "the harness's tag is not ours to change"


def test_the_adapter_sees_every_position_not_only_its_own() -> None:
    """Separating the tags must not blind reconciliation.

    An unexpected position -- a hand trade, or the harness's -- is a finding
    reconciliation has to be able to reach. A default that filtered to our own
    tag would make it unreachable, which would trade one silent failure for
    another.
    """
    import inspect

    from app.brokers.mt5 import MT5Adapter

    for name in ("get_positions", "get_orders", "get_order_history"):
        signature = inspect.signature(getattr(MT5Adapter, name))
        assert signature.parameters["magic"].default is None, (
            f"{name} filters by magic by default; reconciliation would stop "
            "seeing what the platform did not open"
        )


def test_the_adapter_tags_what_it_sends_with_its_own_magic() -> None:
    """Asserted at the call sites, because the default is the toolkit's."""
    import inspect

    from app.brokers.mt5 import MT5Adapter

    for method in (MT5Adapter.place_order, MT5Adapter.close_position):
        source = inspect.getsource(method)
        assert "MAGIC," in source, f"{method.__name__} does not pass our magic"


def test_the_toolkit_still_defaults_to_its_own_magic() -> None:
    """The harness's behaviour is unchanged: it passes no magic and gets its own."""
    import inspect
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tools"))
    import mt5_paper

    for name in ("place", "close_own", "own_positions"):
        signature = inspect.signature(getattr(mt5_paper, name))
        assert signature.parameters["magic"].default == mt5_paper.MAGIC, name


async def test_a_terminal_that_cannot_list_symbols_is_unreadable_not_empty() -> None:
    """`symbols_get()` answers None when the terminal cannot serve, and `or
    []` turned that into "this venue lists no symbols".

    The difference reaches an order. `OrderManager.submit` reads the venue's
    contract terms before sending, and it has two codes: VENUE_SPEC_REFUSED
    for a symbol the venue does not list, and VENUE_SPEC_UNREADABLE for terms
    it could not read. An empty list is the first; None is the second, and
    collapsing them here would have made every order look like an unlisted
    instrument whenever the terminal hiccupped -- and cached that empty
    snapshot for five minutes.
    """
    from app.brokers.mt5 import MT5Adapter

    adapter = MT5Adapter()
    adapter._state = ConnectionState.connected

    class Silent:
        def symbols_get(self) -> None:
            return None

    adapter._mt5 = Silent()
    with pytest.raises(NotConnected, match="no symbol list"):
        await adapter.get_symbols()

    class Empty:
        def symbols_get(self) -> tuple[Any, ...]:
            return ()

    # An empty answer is still an answer, and stays one.
    adapter._mt5 = Empty()
    assert await adapter.get_symbols() == []
