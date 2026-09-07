# POLICY_COMPLEXITY_POLICY.md

L64 section 19. Prefer simple policies.

---

`Candidate.complexity()` is the number of parameters a candidate moves.

It enters selection as the **last** element of the ordering tuple, negated. Of
two policies equal on safety, robustness and stability, the simpler wins. It
can never outweigh any of the three above it.

That placement is the whole policy. A complexity term inside a weighted sum
would let a sufficiently simple policy win on being simple, which is not what
section 19 asks for - it asks for simplicity as a preference between equals.

## Why it matters here specifically

`CLAUDE.md` documents a 36-cell bracket sweep in which the **random** rule
scored higher in-sample (t = 1.76) than the best real candidate (0.83), and a
41-candidate search whose best in-sample t of 1.29 went negative out of sample.
More parameters is more chances to fit noise, and this repository has measured
that on its own data rather than assuming it.

A governance policy has far fewer observations available than a trading rule
does. The case for simplicity is correspondingly stronger.
