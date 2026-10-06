# Portfolio Audit

| Repository | Problem | Severity | Proposed change | Reason | Verification method |
|---|---|---|---|---|---|
| Folding-JimiJimi | README hard-codes a model shutdown date that conflicts with current official documentation | P1 | Replace the date with a durable deprecation-documentation link and a non-speculative note | Avoids time-sensitive misinformation | Compare README with Google’s official deprecations page |
| Folding-JimiJimi | Development/build artifact tracking and ignore policy need verification | P1 | Remove only confirmed generated artifacts and update ignore rules intentionally | Keeps the public repository reproducible and clean | `git ls-files`, diff, and clean build/smoke checks |
| Folding-JimiJimi | README architecture/security claims need code-level reconciliation | P1 | Correct only claims that do not match current code; add a brief transparent development note | Keeps portfolio claims technically defensible | Static searches, tests, typecheck, build, and smoke |
