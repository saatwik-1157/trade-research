# PREDICTIVE_RISK_POLICY.md

L66 sections 12, 29, 30 and 47. What a prediction is allowed to do.

---

## The critical rule

    PREDICTION -> RECOMMENDATION -> POLICY VERIFICATION -> CERTIFICATION
      -> SAFETY -> PORTFOLIO RISK -> RISK ENGINE -> OMS -> BROKER

There is no `PREDICTION -> EXECUTION` edge, and it is not prevented by a check.
Nothing under `app/` imports the scenario module, so a prediction has no path
to anything -- `test_the_safety_architecture_does_not_depend_on_the_scenario_engine`
walks every module and asserts the importer list is empty.

That also answers section 46's TEST 9: the scenario engine being unavailable
changes nothing, because nothing depends on it.

## Every predictive statement says it is one

`Outlook.as_dict()` and `Warning_.as_dict()` both carry `kind: "PREDICTION"`
and an authority line saying it is not an observation and not an instruction.

Section 42 asks the frontend to distinguish PREDICTION, SIMULATION and OBSERVED
RESULT. Putting the distinction in the serialised record rather than only in the
view means a consumer that is not the frontend cannot lose it.

## Every predictive indicator carries its provenance

Methodology, timestamp, validity window, data version, model version, and
component confidence. An indicator without a methodology is a number with an
opinion attached.

## Predictions expire

An `Outlook` has a validity window and `expired()` is checked against a passed
`now`. Section 29's rule applied to forecasts: a projection is about the moment
it was made.

## Warnings are not facts

Section 30's last line. A warning names its scenario, its evidence, its
confidence and its validity -- so a reader can tell what would have to be true
for it to matter, rather than being handed a conclusion.
