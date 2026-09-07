"""What a model artifact is here, and how its integrity is checked.

**The artifact is structured JSON in the database, not a file.** L24 decided
that deliberately — the parameters of these three families are a handful of
floats each, and keeping them structured means a version can be inspected,
diffed and queried rather than only loaded. Section 34 asks to reuse existing
storage; this *is* the existing storage.

That decision answers most of section 33 by construction:

  * **Nothing is deserialised.** There is no pickle, no joblib, no torch.load,
    no `__reduce__`, no code path that turns bytes into behaviour. An artifact
    is parsed as JSON and read field by field into a frozen dataclass by
    `app/ai/loader.py`, which refuses an unrecognised `kind` rather than
    guessing.
  * **There is no upload route, and no path field.** A caller cannot name a
    file, a URL or a directory, so there is nothing for a path traversal to
    traverse. The only way an artifact enters this platform is a training run.
  * **An artifact is data, never code.** A metadata field that executed
    something would need something to execute it, and nothing here does.

What is *not* free is integrity. A JSON column can still be edited by anything
holding a database connection — a migration, a console, a bug — and a model
whose coefficients changed under it would keep predicting, confidently and
wrongly. So a digest is taken at registration and re-checked on every load.

**The digest covers the artifact, not the row.** Metrics, status, timestamps and
the deployment history all change legitimately over a version's life; the fitted
parameters do not, and §6 says a change to them is a new version. Hashing the
whole row would make the check fire on every ordinary write and be switched off
within a week.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.models.ai import ModelVersion

#: The digest algorithm, named in the stored value so a future change is
#: visible in the data rather than only in the code that reads it.
ALGORITHM = "sha256"

#: What an artifact may contain. A `kind` this deployment cannot load is
#: refused at registration rather than at inference -- §7 asks that integrity be
#: verified before a model is loaded for trading, and a kind nobody recognises
#: fails that test at the earliest point it can be asked.
KNOWN_KINDS: frozenset[str] = frozenset({"logistic", "quantile_cuts", "robust_baseline"})

#: A ceiling on artifact size. These are floats: a logistic model with 22
#: features serialises to well under 4KB, and the largest of the three families
#: is smaller. A megabyte here would not be a big model, it would be a bug or
#: someone using the column for something it is not.
MAX_ARTIFACT_BYTES = 1_048_576


class ArtifactError(Exception):
    """An artifact that cannot be trusted. Never loaded for trading."""


def canonical(artifact: dict[str, Any]) -> bytes:
    """The bytes a digest is taken over.

    Sorted keys and no whitespace, so two dictionaries that differ only in
    insertion order or formatting produce the same digest. Without this the
    check would fire the first time SQLAlchemy round-tripped a row through
    JSONB, which reorders keys, and the fix would have been to delete the check.
    """
    return json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(artifact: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(artifact)).hexdigest()


def size_of(artifact: dict[str, Any]) -> int:
    return len(canonical(artifact))


@dataclass(frozen=True)
class ArtifactCheck:
    """Whether a version's artifact is what it was when it was registered."""

    intact: bool
    reason: str
    kind: str | None = None
    expected: str | None = None
    actual: str | None = None
    size_bytes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intact": self.intact,
            "reason": self.reason,
            "kind": self.kind,
            "algorithm": ALGORITHM,
            # The digest is safe to show: it is a hash of parameters that are
            # themselves visible on the version, and it is what makes the claim
            # checkable by someone who does not trust this endpoint.
            "expected": self.expected,
            "actual": self.actual,
            "size_bytes": self.size_bytes,
            "storage": (
                "structured JSON on the model version row. There is no file path, no "
                "upload route and no deserialisation: an artifact is data read field by "
                "field, never bytes turned into behaviour."
            ),
        }


def artifact_of(row: ModelVersion) -> dict[str, Any]:
    """The fitted parameters on a version row, or a refusal."""
    params = row.params or {}
    artifact = params.get("artifact")
    if not isinstance(artifact, dict) or not artifact.get("kind"):
        raise ArtifactError(
            f"model version {row.id} carries no artifact. A version with no fitted "
            "parameters cannot be registered: there is nothing to verify and nothing "
            "to load."
        )
    return artifact


def inspect(artifact: dict[str, Any]) -> None:
    """Refuse an artifact that is the wrong shape, kind or size. Section 33."""
    kind = str(artifact.get("kind") or "")
    if kind not in KNOWN_KINDS:
        raise ArtifactError(
            f"artifact kind {kind!r} is not one this deployment can load "
            f"({', '.join(sorted(KNOWN_KINDS))}). Refused at registration rather than "
            "discovered at inference: §7 asks that integrity be verified before a model "
            "is loaded for trading."
        )
    size = size_of(artifact)
    if size > MAX_ARTIFACT_BYTES:
        raise ArtifactError(
            f"the artifact is {size} bytes, above the {MAX_ARTIFACT_BYTES} ceiling. "
            "These families serialise to a few kilobytes; something this large is not a "
            "big model, it is a column being used for something it is not."
        )


def stamp(row: ModelVersion) -> tuple[str, int, str]:
    """The digest, the size and the kind, computed from the row's own artifact.

    Called once at registration. Returns rather than writes, so the caller
    controls the transaction and this function stays testable without a session.
    """
    artifact = artifact_of(row)
    inspect(artifact)
    return digest(artifact), size_of(artifact), str(artifact["kind"])


def verify(row: ModelVersion) -> ArtifactCheck:
    """Is this version's artifact what it was when it was registered?

    Called before every load that could reach a trading decision. A version with
    no recorded digest has never been registered, and that is reported as such
    rather than as a pass — an unverifiable artifact and a verified one must not
    look the same.
    """
    try:
        artifact = artifact_of(row)
    except ArtifactError as exc:
        return ArtifactCheck(intact=False, reason=str(exc))

    kind = str(artifact.get("kind") or "")
    actual = digest(artifact)
    size = size_of(artifact)

    if not row.artifact_sha256:
        return ArtifactCheck(
            intact=False,
            reason=(
                "this version carries no recorded digest, so its artifact cannot be "
                "verified. Not the same as a corrupted one -- it has never been "
                "registered -- but it is equally not loadable for trading."
            ),
            kind=kind,
            actual=actual,
            size_bytes=size,
        )

    if row.artifact_sha256 != actual:
        return ArtifactCheck(
            intact=False,
            reason=(
                f"the artifact digest does not match the one recorded at registration. "
                f"Expected {row.artifact_sha256[:16]}, computed {actual[:16]}. The "
                "parameters have changed since this version was registered, which §6 "
                "says must never happen in place: a changed model is a new version."
            ),
            kind=kind,
            expected=row.artifact_sha256,
            actual=actual,
            size_bytes=size,
        )

    try:
        inspect(artifact)
    except ArtifactError as exc:
        return ArtifactCheck(
            intact=False,
            reason=str(exc),
            kind=kind,
            expected=row.artifact_sha256,
            actual=actual,
            size_bytes=size,
        )

    return ArtifactCheck(
        intact=True,
        reason=f"{ALGORITHM} digest matches the one recorded at registration",
        kind=kind,
        expected=row.artifact_sha256,
        actual=actual,
        size_bytes=size,
    )
