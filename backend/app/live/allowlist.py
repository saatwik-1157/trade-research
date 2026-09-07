"""Which accounts may trade live. An empty allowlist permits nothing.

Brief §7: *"Only explicitly approved account identifiers may be used for live
trading. If the connected account is not allowlisted: LIVE TRADING BLOCKED.
This must be fail-closed."*

The failure this guards against is specific and has happened to other people:
a terminal is logged into two accounts over a week, the configuration still
names the first, and the run that was reviewed against a demo balance sends its
orders to the real one. Comparing a configured identifier against the
*connected* identifier is the only check that catches it, because every other
signal -- the balance, the symbol list, the server name in a window title --
looks plausible on both.

**Empty is not "allow all".** It is "allow none", and that asymmetry is the
entire value of the control: a deployment that forgot to configure the
allowlist must trade nothing, not everything. `permits()` returns False for an
empty allowlist without consulting its argument.

Identifiers are compared as **strings, exactly**, after stripping surrounding
whitespace. MT5 logins are integers and it is tempting to compare them as such;
they are not arithmetic and a leading zero is not a rounding error, so the
comparison stays textual and a mismatch stays a mismatch.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Allowlist:
    """The set of account identifiers explicitly approved for live trading."""

    accounts: frozenset[str] = frozenset()

    @classmethod
    def of(cls, values: Iterable[str] | None) -> Allowlist:
        """Build from configuration, discarding blanks.

        A blank entry is dropped rather than stored, because an allowlist
        containing "" would permit an account whose identifier could not be
        read -- which is the one case that must refuse hardest.
        """
        if not values:
            return cls()
        cleaned = {str(value).strip() for value in values}
        return cls(frozenset(entry for entry in cleaned if entry))

    @property
    def configured(self) -> bool:
        return bool(self.accounts)

    def permits(self, account_id: str | int | None) -> bool:
        """True only for an identifier explicitly listed.

        An unconfigured allowlist permits nothing, and an absent identifier is
        permitted by nothing.
        """
        if not self.accounts or account_id is None:
            return False
        return str(account_id).strip() in self.accounts

    def describe(self) -> dict[str, object]:
        """Safe to log and to return from an endpoint.

        The identifiers themselves are included: an MT5 login is not a
        credential -- it is half of one, and the half that is printed on every
        statement. The password is what is secret, and this class has never
        seen one.
        """
        return {
            "configured": self.configured,
            "count": len(self.accounts),
            "accounts": sorted(self.accounts),
        }
