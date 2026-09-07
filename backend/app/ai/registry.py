"""Which model version a caller gets, and why it is never a surprise.

**This is not a second model registry.** Section 34: the `models` and
`model_versions` tables have existed since L05 and L28 expands them. This is an
in-process resolver over what is loaded in this deployment — the same
relationship `BrokerRegistry` has to `broker_accounts`, and `WorkerRegistry` to
the workers table.

**Resolution is by explicit version, and `latest` has to be asked for.**
Section 50's test is the requirement stated as a scenario: a bot using v1 must
keep using v1 when v2 is installed. So `get(key, version)` is the normal call
and `latest(key)` is a separate, differently-named one. There is no default
that quietly follows the newest thing registered, because that default is
indistinguishable from correct behaviour until the day it is not.

**Registering the same (key, version) twice is refused.** A version is
immutable by the same argument `feature_sets` and `label_sets` are: a caller
holding "regime v1.0" must be holding the same thing tomorrow, and silently
replacing it is the "no silent model replacement" section 32 forbids —
expressed as an error rather than as a rule somebody has to follow.
"""

from __future__ import annotations

from typing import Any

from app.ai.base import BaseModel


class RegistryError(Exception):
    """A model that cannot be resolved or registered. Never a silent fallback."""


class ModelRegistry:
    """The models this process can answer with, keyed by (key, version)."""

    def __init__(self) -> None:
        self._models: dict[tuple[str, str], BaseModel] = {}
        # Insertion order per key, so `latest` means "most recently registered"
        # rather than a string comparison that would put v10 before v2.
        self._order: dict[str, list[str]] = {}

    def register(self, model: BaseModel) -> BaseModel:
        key, version = model.identity.key, model.identity.version
        if (key, version) in self._models:
            raise RegistryError(
                f"{key} v{version} is already registered. A version is immutable: a "
                "caller holding this one must be holding the same thing tomorrow, and "
                "replacing it in place is the silent model swap that is forbidden. "
                "Register a new version instead."
            )
        self._models[(key, version)] = model
        self._order.setdefault(key, []).append(version)
        return model

    def get(self, key: str, version: str) -> BaseModel:
        """One exact model. The call a running bot should make."""
        try:
            return self._models[(key, version)]
        except KeyError as exc:
            known = ", ".join(self._order.get(key, [])) or "none"
            raise RegistryError(
                f"no {key} at version {version}. Registered versions of {key}: {known}. "
                "Nothing is substituted -- a bot pinned to a version that is not here "
                "must be told, not quietly given another one."
            ) from exc

    def latest(self, key: str) -> BaseModel:
        """The most recently registered version. Deliberately a separate call."""
        versions = self._order.get(key)
        if not versions:
            raise RegistryError(f"no model registered under {key!r}")
        return self._models[(key, versions[-1])]

    def versions(self, key: str) -> list[str]:
        return list(self._order.get(key, []))

    def keys(self) -> list[str]:
        return sorted(self._order)

    def has(self, key: str, version: str) -> bool:
        return (key, version) in self._models

    def describe(self) -> list[dict[str, Any]]:
        """Every registered model, newest version of each key last."""
        out: list[dict[str, Any]] = []
        for key in self.keys():
            for version in self._order[key]:
                out.append(self._models[(key, version)].describe())
        return out

    def __len__(self) -> int:
        return len(self._models)
