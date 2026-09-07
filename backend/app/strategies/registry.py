"""The strategy registry and factory.

Resolves a key to an implementation, validates a configuration against it, and
returns an instance. What it replaces is the alternative: `if strategy == "x"
... elif strategy == "y"`, scattered across the engine, the bot runner and the
backtester, disagreeing about which names exist.

**Nothing here executes code from a name or a payload.** The factory looks a
key up in a dict of classes registered at import time. There is no `eval`, no
`exec`, no `importlib` on a caller-supplied string and no path a user-provided
strategy definition can take to become a running function. A visual strategy
builder (L13) will produce *configuration* that a registered implementation
consumes; it will not produce code.

An unknown key is refused with the list of known ones, never resolved to the
closest match. Two strategies whose names differ by a character are two
strategies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.strategies.base import ConfigError, Strategy, StrategyMetadata, StrategyTier

log = logging.getLogger("app.strategies")


class UnknownStrategy(ConfigError):
    """No such strategy. Never resolved to a similar name."""


class StrategyNotAvailable(ConfigError):
    """Registered, but not permitted in this context. Carries why."""


@dataclass
class StrategyRegistry:
    """Key -> implementation. Populated at import, never from user input."""

    implementations: dict[str, type[Strategy]] | None = None

    def __post_init__(self) -> None:
        if self.implementations is None:
            self.implementations = {}

    def register(self, implementation: type[Strategy]) -> type[Strategy]:
        key = implementation().metadata().key
        assert self.implementations is not None
        if key in self.implementations:
            raise ValueError(f"strategy {key!r} is already registered")
        self.implementations[key] = implementation
        return implementation

    def keys(self) -> list[str]:
        assert self.implementations is not None
        return sorted(self.implementations)

    def implementation(self, key: str) -> type[Strategy]:
        assert self.implementations is not None
        try:
            return self.implementations[key]
        except KeyError as exc:
            raise UnknownStrategy(
                f"no strategy {key!r}; registered strategies are {', '.join(self.keys()) or 'none'}"
            ) from exc

    def metadata(self, key: str) -> StrategyMetadata:
        return self.implementation(key)().metadata()

    def describe(self) -> list[dict[str, object]]:
        assert self.implementations is not None
        out: list[dict[str, object]] = []
        for key in self.keys():
            instance = self.implementations[key]()
            out.append(
                {
                    **instance.metadata().as_dict(),
                    "required_data": instance.required_data().as_dict(),
                }
            )
        return out

    # ------------------------------------------------------------- factory

    def create(
        self,
        key: str,
        config: dict[str, Any] | None = None,
        *,
        required_tier: StrategyTier | None = None,
    ) -> Strategy:
        """Build a validated instance, or refuse.

        `required_tier` is the gate that stops a research candidate from being
        run somewhere it has not earned. Every rule in this repository is
        `research_only`, so asking for `live_approved` refuses all of them --
        which is the correct answer, not an inconvenience.
        """
        implementation = self.implementation(key)
        instance = implementation(config or {})  # validate_config runs here
        if required_tier is not None:
            tier = instance.metadata().tier
            if not _tier_at_least(tier, required_tier):
                raise StrategyNotAvailable(
                    f"strategy {key!r} is {tier} and this context requires "
                    f"{required_tier}. Raising a tier is a deliberate act backed by "
                    "a validation report, not a configuration change"
                )
        log.info(
            "strategy instantiated",
            extra={
                "event": "strategy_created",
                "strategy": key,
                "tier": str(instance.metadata().tier),
                # The validated config keys, never their values: a parameter
                # set is not a secret but logging it wholesale is how one
                # eventually is.
                "config_keys": sorted(instance.config),
            },
        )
        return instance


_ORDER = (
    StrategyTier.research_only,
    StrategyTier.paper_approved,
    StrategyTier.live_approved,
)


def _tier_at_least(have: StrategyTier, need: StrategyTier) -> bool:
    return _ORDER.index(have) >= _ORDER.index(need)


def default_registry() -> StrategyRegistry:
    """Every built-in strategy, registered at import time.

    All three are `research_only`, and that is a measurement rather than
    caution: none separates from a coin flip at this broker's spreads.
    """
    from app.strategies.rules import BUILT_IN

    registry = StrategyRegistry()
    for implementation in BUILT_IN:
        registry.register(implementation)
    return registry
