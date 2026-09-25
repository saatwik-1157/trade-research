# AUTONOMOUS_CONTROL_SECURITY.md

Security of the autonomous control surface. L62 §21 and §22, 2026-09-06.

---

**`SECURITY.md` (508 lines) is the platform's security document.** This covers
only what is specific to autonomous control, and points there for the rest.

## AI is advisory, enforced by type

The strongest statement in this document is not a check. From
`app/execution/ai.py`:

> The seat can only subtract. `AiVerdict` has no field by which a model could
> raise a limit, approve an order, size a position or disengage a kill switch,
> because those are not things it is allowed to do. That is the design, not a
> limitation of the current stub: a richer model later still returns this type,
> and this type cannot express an approval.

**A type that cannot express an approval is stronger than a check that rejects
one**, because there is no code path that forgets to run it. INV-02.

The consequence for availability is the useful one: an absent model **removes a
veto and can never add an approval**, so AI being unavailable is strictly more
conservative than AI being present. Deterministic fallback is therefore not a
separate mechanism — it is what absence already means. INV-23.

## What AI may not do

| Forbidden | Enforced by |
|---|---|
| bypass the RiskEngine | `AiVerdict` has no approval field |
| increase risk limits | `classify()` → FORBIDDEN, no approval path |
| enable live trading | same; `live_trading` is a hard limit |
| submit MT5 orders directly | `test_only_the_oms_reaches_a_venue_to_write` |
| bypass approval | `may_auto_apply` requires `not approval_required` |
| remove account isolation | fingerprint includes the account |

The forbidding is **by effect, not by name**. A blocklist of action names is
one rename away from a hole, so an action that loosens a hard constraint is
refused whatever it is called — proved by classifying two deliberately
innocuous names, `a_name_nobody_thought_of` and `innocuous_sounding_tweak`.

## Server-side authorization

Section 22 requires that a user cannot manipulate frontend state to bypass
backend policy. That was already true and already tested before L62, and is
**not re-tested** in the safety suite — a second, weaker version of an
authorization test is how the weaker one ends up being the one that gets
maintained.

The existing guards include:

- `test_a_plain_user_cannot_change_risk_configuration`
- `test_a_plain_user_cannot_reach_order_submission`
- `test_a_plain_user_cannot_reach_broker_routes`
- `test_a_plain_user_cannot_reach_the_ai_layer`
- `test_a_client_cannot_raise_its_own_risk_ceiling`
- `test_risk_cannot_be_bypassed_by_forging_an_approval`
- `test_no_route_can_reach_a_broker`
- `test_no_route_response_contains_a_credential`
- `test_a_user_cannot_reach_another_users_session`

`test_an_approval_required_action_is_never_auto_applied` covers the half that
is the safety package's own: a proposal that needs approval must never come
back auto-appliable, however it got that way.

## Secrets

Nothing in `app/safety/` reads a credential, opens a connection or logs a
value. It receives conditions as parameters and returns a result. The package
import guard limits it to `app.portfolio.control` and `app.portfolio.decision`,
so it cannot reach configuration that holds secrets even accidentally.

## Known limit

The architecture scan is **syntactic**: it matches a call by method name, so a
venue write reached through `getattr(adapter, name)()` would evade it. No such
call exists today. The scan proves none is written directly; it does not prove
none can be.
