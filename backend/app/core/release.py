"""What is actually running: version, commit, build time, schema head.

Step 25. Before this, `/health` reported `"version": "0.2.0"` -- a string in
`settings.py` that nobody had changed since L02 and that says nothing about
which build is serving the request. Two deployments a month apart reported the
same version, which makes "is the fix deployed?" unanswerable and makes a
rollback impossible to confirm.

**Everything here is read from the environment, and every field says
`"unknown"` when it was not supplied.** That is the whole design:

  * A build stamps `GIT_COMMIT` and `BUILD_TIME` in as build arguments, and the
    image carries them as environment variables.
  * A `docker run` of the same image without those arguments reports
    `"unknown"` rather than inheriting a stale value or guessing.

**It never shells out to git.** A production container has no `.git` directory
and no git binary; a version function that tries and fails is a version
function that raises inside a health check. The build knows the commit; the
running process only has to repeat what it was told.

**Nothing here is a secret and nothing here can become one.** The fields are a
fixed list of identifiers, and `describe()` returns exactly them -- there is no
passthrough of arbitrary environment variables, which is how a version endpoint
usually ends up leaking a connection string.

The schema head is read from Alembic's own metadata rather than from a
constant, because a version that claims a migration state it does not have is
worse than one that says it does not know.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

UNKNOWN = "unknown"

#: The environment variables a build sets. Named here so the Dockerfile, the
#: compose file and the workflow can be checked against one list.
ENV_COMMIT = "GIT_COMMIT"
ENV_BUILD_TIME = "BUILD_TIME"
ENV_RELEASE = "RELEASE_VERSION"


def _clean(value: str | None, *, limit: int = 64) -> str:
    """Trim and bound. A build argument is attacker-influenced in a supply
    chain, and an unbounded string reaches a log line and a JSON response."""
    if not value:
        return UNKNOWN
    text = value.strip()
    if not text:
        return UNKNOWN
    return text[:limit]


@dataclass(frozen=True)
class Release:
    """One build's identity. Every field is safe to show anybody."""

    version: str
    commit: str
    built_at: str

    @property
    def short_commit(self) -> str:
        return self.commit[:12] if self.commit != UNKNOWN else UNKNOWN

    @property
    def stamped(self) -> bool:
        """Whether this build actually knows what it is.

        False for a `docker build` that did not pass the build arguments, and
        for a process started straight from a source tree. The deployment guide
        treats an unstamped image as not deployable to production, because a
        release you cannot identify is one you cannot roll back to.
        """
        return self.commit != UNKNOWN and self.built_at != UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "commit": self.short_commit,
            "built_at": self.built_at,
            "stamped": self.stamped,
        }


@lru_cache(maxsize=1)
def current(default_version: str = "0.0.0") -> Release:
    """Read once. The environment does not change under a running process."""
    return Release(
        # The build's release tag wins over the in-code version when set;
        # `settings.app_version` is the fallback for a source-tree run.
        version=_clean(os.environ.get(ENV_RELEASE)) or default_version
        if os.environ.get(ENV_RELEASE)
        else default_version,
        commit=_clean(os.environ.get(ENV_COMMIT)),
        built_at=_clean(os.environ.get(ENV_BUILD_TIME), limit=32),
    )


def schema_head() -> str:
    """The migration revision this code expects, from Alembic's own scripts.

    Read from the migration directory rather than hard-coded, so it cannot
    drift. Returns `"unknown"` if the directory is absent -- which is a real
    deployment (an image built without `alembic/`) and not an error worth
    raising inside a health check.

    This is the revision the CODE expects. What the DATABASE is actually at is a
    different question, answered by `alembic current` against the database, and
    the deployment guide compares the two before starting the application.
    """
    try:
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parents[2]
        ini = root / "alembic.ini"
        if not ini.exists():
            return UNKNOWN
        script = ScriptDirectory.from_config(Config(str(ini)))
        head = script.get_current_head()
        return head or UNKNOWN
    except Exception:  # noqa: BLE001 - a health check must not raise
        return UNKNOWN


def describe(settings: Any) -> dict[str, Any]:
    """The full release identity, for `/health` and the admin surface.

    A fixed set of fields. There is deliberately no "and any other
    `RELEASE_*` variable" passthrough: that is how a version endpoint ends up
    reporting a connection string somebody parked in the environment.
    """
    release = current(settings.app_version)
    return {
        "service": settings.app_name,
        **release.as_dict(),
        "schema_head": schema_head(),
    }


__all__ = [
    "ENV_BUILD_TIME",
    "ENV_COMMIT",
    "ENV_RELEASE",
    "UNKNOWN",
    "Release",
    "current",
    "describe",
    "schema_head",
]
