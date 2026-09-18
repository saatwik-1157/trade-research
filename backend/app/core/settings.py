"""Runtime configuration, read from the environment.

The three settings that matter most are the ones that decide whether money
moves, and they fail closed:

  ENVIRONMENT   development | test | production      (default development)
  TRADING_MODE  paper | demo | live                  (default paper)
  LIVE_TRADING  true | false                         (default false)

PAPER is the internal simulator, DEMO is the MT5 demo account at the real
broker, LIVE is a real account. LIVE is not reachable from any setting alone:
``live_execution_allowed`` is true only when every gate in ``LIVE_GATES`` has
been built and verified, and at this level none has. A setting can widen what
the platform *wants*; only code that has been tested can widen what it *can*.

The code-level demo fence in ``tools/mt5_paper.assert_demo`` sits underneath
all of this and is not affected by anything here.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    development = "development"
    test = "test"
    production = "production"


class TradingMode(StrEnum):
    paper = "paper"  # internal simulator; no broker involved
    demo = "demo"  # MT5 demo account; broker's play money; assert_demo enforced
    live = "live"  # real account; refused until every LIVE_GATES entry is true


# Safety gates that must exist and be verified before live execution can be
# considered. Each flips to True only in the level that builds it, together
# with the test that proves it. The test suite asserts that nothing here is
# True at the foundation level, so a later flip has to be deliberate.
LIVE_GATES: dict[str, bool] = {
    "risk_engine_veto": False,  # L17
    # BUILT AT L18 and held False deliberately, as L17 held its own two.
    # A gate is a claim that live execution may rely on the mechanism, and
    # flipping them is an operator decision taken in one reviewed pass --
    # not a side effect of the level that wrote the code. Three tests assert
    # every entry here is False, so a flip has to be deliberate and visible.
    "position_sizing_refusal": False,  # L18: app/sizing, refuses below the venue minimum
    "kill_switch": False,  # L17
    "order_idempotency": False,  # L19
    "unknown_status_reconciliation": False,  # L19
    "broker_sync_on_connect": False,  # L10 / L38
    "restart_reconciliation": False,  # L38
    "structured_execution_logging": False,  # L19
    "monitoring_and_alerting": False,  # L37
    "live_broker_adapter": False,  # does not exist
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.development
    trading_mode: TradingMode = TradingMode.paper
    live_trading: bool = False

    app_name: str = "trade-research-platform"
    app_version: str = "0.2.0"

    log_level: str = "INFO"
    log_json: bool = True

    # Ports 5440 / 6390 on the host: other projects on the development machine
    # already hold 5432-5434 and 6379-6380. Inside Compose the service names
    # resolve and the defaults are overridden by the environment there.
    #
    # 127.0.0.1, not localhost: on Windows "localhost" resolves to ::1 first,
    # Docker publishes on IPv4 only, and the IPv6 attempt hangs until the
    # timeout instead of being refused. Measured, not assumed.
    #
    # asyncpg, not psycopg: uvicorn forces the Proactor event loop on Windows
    # and psycopg's async driver refuses it. The MT5 worker has to run on a
    # Windows host, so the platform standardises on the driver that works there.
    database_url: str = Field(
        default="postgresql+asyncpg://trade:trade@127.0.0.1:5440/trade",
        description="SQLAlchemy URL. Never logged.",
    )
    redis_url: str = Field(default="redis://127.0.0.1:6390/0", description="Never logged.")

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    health_timeout_seconds: float = 2.0

    # --- Authentication (L04) ---
    session_cookie_name: str = "tr_session"
    session_ttl_hours: int = 12
    # None: secure cookies in production only, so local http development works.
    cookie_secure: bool | None = None
    allow_registration: bool = True
    # Double-submit CSRF token on state-changing requests that carry a
    # session. Off only for a deliberate, documented reason.
    csrf_enabled: bool = True
    # False keeps the rate limiter per-process, which is correct for one
    # worker and wrong for several; True shares it through Redis.
    rate_limit_shared: bool = False

    @property
    def cookie_secure_effective(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.environment is Environment.production

    # --- TradingView webhook (L09) ---
    # Empty means the receiver REFUSES every alert. That is deliberate and
    # differs from the CLI tool in `tools/tv_webhook.py`, which warns and
    # continues: a tool an operator is watching may run unauthenticated, but a
    # server endpoint that accepted anything because it was misconfigured
    # would be an open write path into the signal table.
    #
    # Never logged, never returned by an endpoint, never sent to the frontend.
    tv_webhook_secret: str = Field(default="", description="Shared secret. Never logged.")
    # How old an alert may be. A stale alert refers to a bar that has closed
    # and a price that is gone, so acting on one is acting on a market that no
    # longer exists.
    tv_webhook_max_age_seconds: int = 120
    tv_webhook_future_tolerance_seconds: int = 30
    # Only meaningful when the port is exposed directly. Behind a tunnel the
    # source address is the tunnel's, so this is off by default rather than a
    # silent lie about who called.
    tv_webhook_restrict_ips: bool = False

    # --- Infrastructure (L02) ---
    # False routes events through the in-process bus instead of Redis, which
    # is what a single process with no Redis should do. It never silently
    # falls back: the chosen bus is logged at startup and reported by /health.
    events_enabled: bool = True

    # --- Notifications (L34) ---
    # False stops the consumer subscribing and the delivery worker starting.
    # Events still flow to browsers over the realtime hub; nothing is
    # persisted as a notification. Off is a valid deployment, not a fault.
    notifications_enabled: bool = True
    # Where the platform is reachable, for links in outbound messages. Empty
    # means no link is included -- section 31 of L35: never a hard-coded
    # localhost, and never a URL carrying a token.
    app_base_url: str = ""

    # --- Email delivery (L34 section 25) ---
    # Empty SMTP_HOST means the email channel reports NOT_CONFIGURED and every
    # email delivery is SKIPPED. That is a correctly configured platform that
    # does not send email, and it is different from one whose provider broke.
    email_notifications_enabled: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    # Never logged, never returned by an endpoint, never sent to the frontend.
    # `public_summary` does not mention it and neither does the channel's
    # `describe()`, which reports only whether credentials are set.
    smtp_password: str = Field(default="", description="SMTP password. Never logged.")
    smtp_use_tls: bool = True
    smtp_from: str = "notifications@localhost"

    # --- Discord delivery (L35) ---
    # Optional, off by default, and never required for trading. A deployment
    # with DISCORD_ENABLED=false gets the L34 seat back: routed deliveries are
    # recorded SKIPPED rather than FAILED, because "switched off" is not
    # "broken". Nothing here can fail startup -- section 41.
    discord_enabled: bool = False
    # A webhook, not a bot token. Section 4: the platform needs outbound
    # notification and nothing else, and a webhook does that without a gateway
    # connection or a token with guild-wide reach.
    #
    # SYSTEM-WIDE, deliberately (section 34): one destination for the
    # deployment, which is why the Discord channel defaults to OFF for every
    # category and every user. A user enabling it is consenting to their own
    # notifications appearing in a shared channel.
    #
    # Never logged, never returned by an endpoint, never sent to the frontend.
    # `public_summary` does not mention it, the channel's `describe()` reports
    # only whether it is configured, and the adapter redacts it out of any
    # provider message before that message is stored.
    discord_webhook_url: str = Field(default="", description="Discord webhook. Never logged.")
    discord_username: str = "trade-research"
    discord_timeout_seconds: float = 10.0

    # --- Monitoring (L37) ---
    # False stops the collection worker. Health endpoints still answer -- they
    # are L02's and probe on request -- so a deployment with monitoring off is
    # observable, just not continuously.
    monitoring_enabled: bool = True
    monitoring_interval_seconds: float = 15.0
    # Section 44: every threshold in one place. These three are the ones a
    # deployment plausibly needs to move; the rest live in
    # `app/observability/thresholds.py` and are served by the API.
    market_data_stale_seconds: float = 900.0
    queue_backlog_warning: int = 50

    # --- Deployment (L41) ---
    #
    # False stops this process starting the notification consumer, the
    # notification delivery worker and the monitoring worker. It exists because
    # **the API and the workers are the same image and the same process today**:
    # scaling the API to two replicas would give the platform two notification
    # consumers and two monitoring workers, each doing the same work.
    #
    # The duplicate work is caught rather than prevented -- `notifications`
    # has a UNIQUE dedup key and monitoring writes `system_events` -- so
    # duplicates are wasteful rather than dangerous. But "wasteful rather than
    # dangerous" is a property of today's workers, not a rule, and the honest
    # deployment is one API replica with workers OFF plus one worker replica
    # with them ON. `docker-compose.prod.yml` is wired that way.
    #
    # Default True so a single-process deployment -- which is what every
    # environment before this level was -- behaves exactly as it did.
    workers_enabled: bool = True

    # --- The execution worker (L38 registered it; L51 gives it a switch) ---
    #
    # **This is the one worker that acts on trading signals**, which is why it
    # has never been started with the platform and why the others are. It
    # claims `signals` rows in `new` and runs them through the nine gates of
    # `ExecutionPipeline` -- Risk, Sizing, OMS, adapter.
    #
    # Default **False**, and that is the opposite of every other worker flag
    # here on purpose. `notifications_enabled` and `monitoring_enabled` default
    # True because a platform whose monitoring starts only when somebody
    # remembers to switch it on is a platform that misses the first outage.
    # This one is the reverse: a platform that begins consuming signals because
    # somebody deployed it is a platform that traded without anybody deciding
    # to.
    #
    # Turning it on does NOT enable live trading and cannot: `TRADING_MODE` and
    # `LIVE_TRADING` are untouched by it, the RiskEngine still refuses any mode
    # outside paper and demo, and an account with no registered order manager
    # still refuses with `no_venue`.
    execution_worker_enabled: bool = False

    # --- The OMS reconcile sweep (L19 built it; nothing polled it) ---
    #
    # `OrderManager.reconcile` is the only exit from `unknown`, and until this
    # flag existed its only production caller was POST
    # /v1/orders/{id}/reconcile. An order parked at 02:00 blocked its intent,
    # its account's bot recovery and safe mode's reason list until a human
    # posted.
    #
    # Default **False**, for `execution_worker_enabled`'s reason rather than
    # by copying it. This sweep does not send, but it does DECIDE: a
    # reconciliation that finds nothing at the venue writes `failed`, which is
    # the one state a fresh order for the same intent may follow. Something
    # that unblocks re-sending on a timer is an operator decision, not a side
    # effect of the process booting.
    #
    # It is not gated on safe mode, deliberately: an unresolved order is one of
    # the conditions that latches safe mode, so a sweep that refused while safe
    # mode was engaged could never clear the thing it was engaged for. It
    # releases nothing -- `SafeMode.release` takes an actor and stays a human
    # act.
    oms_reconcile_enabled: bool = False
    # Slower than monitoring (15s) and the bot supervisor (30s) on purpose:
    # `reconcile` makes two adapter round trips PER ORDER, and an unresolved
    # order that waits one more minute is not worse off.
    oms_reconcile_interval_seconds: float = 60.0
    # Orders settled per pass, per account. A bound so a backlog cannot turn
    # one tick into a hundred venue round trips; the remainder is taken next
    # pass and reported as deferred.
    oms_reconcile_max_per_pass: int = 10

    # --- Position management (L21 built it; this is its switch) ---
    #
    # False and registered, exactly like `execution_worker_enabled`: this
    # worker CLOSES positions, and a process that began closing because
    # somebody deployed it would be a process that traded without anybody
    # deciding to. `POST /v1/positions/sweep` runs one pass on demand.
    position_monitor_enabled: bool = False
    position_monitor_interval_seconds: float = 5.0
    # ATR MULTIPLES, never absolute distances. A price distance is per
    # instrument -- 0.0001 on EURUSD is 0.10 on XAUUSD -- so a deployment-wide
    # absolute would be wrong for every symbol but one. None leaves the policy
    # inert, which is exactly what `PolicySet.default()` already does.
    #
    # OFF by default, and that is a MEASUREMENT rather than caution. A 3.0 ATR
    # trail had a median out-of-sample expectancy of -217 at D1 against -58
    # for the fixed 1.5x1.5 bracket, and move-to-breakeven -123, across a 16x8
    # grid in which all eight exits were negative
    # (`reports/exit_search_d1.json`). Turning either on by default would be
    # overruling the repository's own data from a config file.
    position_trail_atr_multiple: Decimal | None = None
    position_break_even_atr_multiple: Decimal | None = None
    # Also a multiple, for the same reason. A break-even stop placed exactly
    # at the entry loses the spread every time it fires, which is not
    # break-even -- and the spread is per instrument, so only an ATR-relative
    # buffer can be stated once for every symbol.
    position_break_even_buffer_atr: Decimal | None = None
    # Hours. None or 0 means no time exit, which is today's behaviour.
    position_max_hold_hours: float | None = None
    # The bars the two multiples above are measured against. Without an ATR
    # both stop movers refuse -- "no ATR, no trail: nothing is invented" -- so
    # a configured deployment with no bar source silently does nothing, which
    # is why this is a named setting and not an assumption.
    position_atr_timeframe: str = "H1"
    position_atr_period: int = 14

    # --- Recovery (L38) ---
    # False skips the startup reconciliation sequence. It exists for tests and
    # for a process that is deliberately not the one reconciling; a deployment
    # that turns it off is a deployment that starts without having checked
    # anything, and the recovery status says so.
    recovery_startup_checks: bool = True

    # Browser origins allowed to call the API directly (the Next.js dev server).
    # Behind nginx the UI and API share an origin and this list is unused.
    # MEASURED, not assumed: pydantic-settings parses a `list[str]` from the
    # environment as JSON only. `CORS_ORIGINS=http://a,http://b` raises
    # SettingsError at startup rather than splitting on the comma, so the
    # environment value must be a JSON array. The description said
    # "comma-separated or JSON" and was wrong; a setting whose documented form
    # refuses to boot is worse than an undocumented one.
    cors_origins: list[str] = Field(
        default=["http://127.0.0.1:3000", "http://localhost:3000"],
        description='JSON list in the environment, e.g. ["https://app.example.com"].',
    )

    # --- The MT5 demo venue (L70b) ---
    #
    # Where terminal64.exe lives. Empty means the toolkit's own default, which
    # is what `tools/mt5_paper.connect` already resolves -- this exists so a
    # non-standard install does not need the toolkit edited.
    #
    # It does NOT decide whether MT5 is reachable. Registering the `mt5_demo`
    # adapter does, and that route still requires TRADING_MODE=demo, step-up
    # re-authentication, and `assert_demo` accepting the connected account.
    mt5_terminal_path: str = ""

    # --- Controlled live activation (L70) ---
    #
    # None of these can enable live trading. `LIVE_GATES` is untouched by every
    # one of them, so `live_execution_allowed` stays False whatever they say.
    # What they do is give the live gate something to compare against, and an
    # unset value is a refusal rather than a default.
    #
    # A comma-separated STRING rather than a `list[str]`, deliberately: the
    # environment cannot express a list except as JSON (see above), and an
    # allowlist that raises SettingsError on the obvious spelling is an
    # allowlist somebody clears to make the process boot.
    live_allowed_account_ids: str = Field(
        default="",
        description=(
            "Comma-separated account identifiers approved for live trading. "
            "EMPTY PERMITS NOTHING -- it is not a wildcard."
        ),
    )
    #: The account the operator approved. Compared against the account the
    #: terminal reports it is CONNECTED to; a mismatch refuses rather than
    #: switching.
    live_expected_account_id: str = ""
    live_expected_server: str = ""
    #: Must equal `app.live.gate.CONFIRMATION_PHRASE` for the live gate's
    #: confirmation check to pass. Long and unpleasant to type by accident,
    #: which is the point.
    enable_live_trading_confirmation: str = Field(
        default="",
        description="Set to YES_I_UNDERSTAND to satisfy the live confirmation check.",
    )
    #: How old a quote may be before the live gate calls it stale. Much tighter
    #: than `market_data_stale_seconds`, which governs the dashboard: a bar
    #: fifteen minutes old is fine to look at and not fine to trade on.
    live_quote_max_age_seconds: float = 60.0

    @property
    def live_allowed_accounts(self) -> tuple[str, ...]:
        """The allowlist as a tuple. Blanks are dropped, not stored."""
        return tuple(
            entry.strip() for entry in self.live_allowed_account_ids.split(",") if entry.strip()
        )

    # --- Security (L39) ---
    # Per-user WebSocket connections. The hub already caps subscriptions per
    # connection (50) and frame size (4 KB); nothing capped how many
    # connections one account could open, so a script could hold sockets until
    # the process ran out of them and take the feed down for everybody. A
    # browser needs one; a few tabs need a few.
    ws_max_connections_per_user: int = 8
    # Seconds a step-up re-authentication stays valid. Long enough to type a
    # reason, too short to still be open when the laptop is left unlocked.
    step_up_ttl_seconds: int = 300
    # False disables the step-up requirement on dangerous actions. It exists
    # for tests and for a first-run deployment with one operator; the security
    # posture reports it as a control that is not in force, in as many words.
    step_up_required: bool = True

    @model_validator(mode="after")
    def _credentialed_cors_may_not_be_open(self) -> Settings:
        # `allow_credentials=True` with a wildcard origin is the combination
        # that makes any website an authenticated caller of this API: the
        # browser sends the session cookie and Starlette reflects the
        # requesting origin back, so every same-site protection this platform
        # has is bypassed by a page the victim merely visits.
        #
        # Refusing to start is the right response rather than warning. A
        # warning in a log is read after the incident; a process that will not
        # boot is read before it. The same reasoning as the live-mode check
        # below -- ambiguity about who may call this API with somebody's
        # cookies is resolved by refusing, not by picking a reading.
        if any(origin.strip() in ("*", "null") for origin in self.cors_origins):
            raise ValueError(
                "CORS_ORIGINS may not contain '*' or 'null': this API is called "
                "with credentials, and a wildcard origin would let any site make "
                "authenticated requests with a visitor's session cookie. List the "
                "origins explicitly."
            )
        for origin in self.cors_origins:
            candidate = origin.strip()
            parsed = urlsplit(candidate)
            # An origin is scheme://host[:port] and nothing else. A trailing
            # path, a query or a fragment does not narrow the match -- the
            # browser compares the origin, so an entry carrying one matches
            # NOTHING, and does so without an error at startup or in the
            # response. Silence is the failure mode worth refusing: the symptom
            # is "CORS is broken" long after the deploy that caused it.
            wrong = (
                parsed.scheme not in ("http", "https")
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
            )
            if wrong:
                raise ValueError(
                    f"CORS_ORIGINS entry {origin!r} is not an origin. An origin is a "
                    "scheme, a host and optionally a port -- no path, no query, no "
                    "trailing slash. A value that is not one silently matches nothing, "
                    f"so write {parsed.scheme or 'https'}://"
                    f"{parsed.netloc or 'app.example.com'} instead."
                )
        return self

    @model_validator(mode="after")
    def _live_mode_needs_explicit_flag(self) -> Settings:
        # A live mode without the explicit flag is an ambiguous configuration,
        # and ambiguity about whether real money is in play is resolved by
        # refusing to start rather than by picking a reading.
        if self.trading_mode is TradingMode.live and not self.live_trading:
            raise ValueError(
                "TRADING_MODE=live requires LIVE_TRADING=true; refusing to start "
                "with an ambiguous live configuration"
            )
        return self

    def live_execution_blockers(self) -> list[str]:
        """Why live execution is not allowed right now. Empty means allowed."""
        reasons: list[str] = []
        if self.trading_mode is not TradingMode.live:
            reasons.append(f"trading_mode is {self.trading_mode.value}, not live")
        if not self.live_trading:
            reasons.append("LIVE_TRADING is false")
        reasons.extend(f"gate not built: {name}" for name, ok in LIVE_GATES.items() if not ok)
        return reasons

    @property
    def live_execution_allowed(self) -> bool:
        return not self.live_execution_blockers()

    def public_summary(self) -> dict[str, object]:
        """What is safe to show on a health endpoint. No URLs, no secrets.

        `tv_webhook_secret` is deliberately absent, and so is any derived value
        that would narrow it. Whether one is configured is reported by the
        webhook status route, which is gated; how long it is, is nobody's
        business.
        """
        return {
            "service": self.app_name,
            "version": self.app_version,
            "environment": self.environment.value,
            "trading_mode": self.trading_mode.value,
            "live_trading": self.live_trading,
            "live_execution_allowed": self.live_execution_allowed,
            "live_execution_blockers": self.live_execution_blockers(),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
