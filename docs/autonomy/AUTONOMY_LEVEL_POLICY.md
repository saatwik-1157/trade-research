# AUTONOMY_LEVEL_POLICY.md

What the platform may do unattended. L63 §15, 2026-09-06.
Source: `backend/app/safety/autonomy.py`.

---

## The levels

| Level | Name | Auto-applies? |
|---|---|---|
| 0 | OBSERVE | no |
| 1 | RECOMMEND | no |
| 2 | APPROVAL | no |
| 3 | BOUNDED | **yes**, inside the safety envelope |
| 4 | CERTIFIED | **yes** |

`AutonomyLevel` is an `IntEnum`, so a comparison **is** the ordering rather
than a lookup table that could disagree with this page.

## Certification sets a ceiling, never a level

| Certification state | Ceiling | May auto-apply |
|---|---|---|
| `CERTIFIED` | CERTIFIED (4) | yes |
| `MONITORED` | CERTIFIED (4) | yes |
| `CONDITIONALLY_CERTIFIED` | BOUNDED (3) | yes |
| `DEGRADED` | APPROVAL (2) | no |
| `REVALIDATING` | RECOMMEND (1) | no |
| `REVALIDATION_REQUIRED` | RECOMMEND (1) | no |
| `SUSPENDED` | RECOMMEND (1) | no |
| `CERTIFICATION_EXPIRED` | RECOMMEND (1) | no |
| `NOT_CERTIFIED` | OBSERVE (0) | no |
| `CERTIFICATION_REVOKED` | OBSERVE (0) | no |

**An unrecognised state returns OBSERVE.** Not a default, not the previous
value. A state added later that nobody mapped is exactly the case where a
permissive default is silently wrong, and §16 says unknown safety state fails
closed.

The platform is currently `CONDITIONALLY_CERTIFIED`, so its ceiling is
**BOUNDED (3)** — and `PolicyVerificationEngine` defaults to that rather than
to CERTIFIED, so the engine cannot start out claiming more than the
certification behind it.

## Escalation needs no enforcement

**There is no function in `autonomy.py` that returns a level higher than the
one it was given.** "Never allow automatic escalation above the certified
level" is therefore not a rule anybody has to remember — it is not
expressible.

`test_autonomy_is_a_ceiling_and_nothing_here_raises_it` checks it across the
whole cross-product of levels and failure conditions rather than at a sample.

## Where it is enforced

At the **action**, not at the certificate. `PolicyVerificationEngine.verify()`
applies the ceiling last, and below BOUNDED nothing is auto-appliable whatever
every other check concluded. A suspended certification that only appeared in a
report would not stop anything.
