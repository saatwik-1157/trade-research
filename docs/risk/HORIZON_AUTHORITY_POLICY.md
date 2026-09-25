# HORIZON_AUTHORITY_POLICY.md

L65 sections 7 and 48. Who outranks whom.

---

## The order

    EMERGENCY (0) > IMMEDIATE_RISK (1) > SHORT_TERM (2)
      > MEDIUM_TERM (3) > LONG_TERM (4)

Lower number wins. A higher-numbered horizon can never override a
lower-numbered **safety** decision.

## How it is enforced

Not by a second comparison. Each horizon maps to a `Layer` and the resolution
is L60's `decide()`, which takes the most restrictive verdict and cannot
express a lower layer relaxing a higher one.

| Horizon | Layer | Why |
|---|---|---|
| 0 | EMERGENCY | `HARD_SAFETY` (7) | 0:05:00 | yes |
| 1 | IMMEDIATE_RISK | `RISK` (6) | 0:15:00 | yes |
| 2 | SHORT_TERM | `STRATEGY` (4) | 4:00:00 | no |
| 3 | MEDIUM_TERM | `PORTFOLIO` (5) | 2 days, 0:00:00 | no |
| 4 | LONG_TERM | `OPTIMIZATION` (2) | 14 days, 0:00:00 | no |

The long-term horizon enters as `OPTIMIZATION`, the second-weakest layer of
seven. **A strategic view cannot outrank a portfolio constraint by being
strategic** - which is section 44's TEST 8, and it holds without a rule about
AI specifically.

## The authority model is unchanged

Emergency controls, RiskEngine (final veto), PortfolioRiskOrchestrator,
PositionSizer, OMS, BrokerAdapter, PortfolioControlPolicy,
PolicyVerificationEngine, Certification, StrategyEngine, AI (advisory),
Research (discovery).

Nothing in this module reaches any of them.
`test_the_module_reaches_nothing_that_trades` asserts it, and L62's
whole-codebase scan covers it as it covers everything else.
