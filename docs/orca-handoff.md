# Orca Handoff

## Task

Adaptive Coordinator v0.2 closed-loop routing

## Status

PASS

## A. Baseline

- Baseline main commit: `2c05edf30e0590aafc49b2f62b486ef7897a6ecd`.
- Production installed commit before v0.2: `743d59d4346f2ee391f72c96d0cda9bed7b85503` (`0.1.0`).
- Initial work branch: `feature/adaptive-v0.2-closed-loop`; implementation and lifecycle fixes were reviewed through PRs and merged rather than developed directly on `main`.
- Working tree at start: clean.
- Baseline tests: 43/43 PASS.

## B. Changes

| Files | Purpose | Core change |
|---|---|---|
| `adaptive_coordinator/models.py` | State contract | Failure taxonomy, adaptive decisions, verification modes/outcomes, logical Gate and attempt metadata, bounded evidence and cost metrics |
| `adaptive_coordinator/routing.py` | Routing policy | Central capability ladder, rank/+1 progression, risk floor, bilingual TaskBrief normalization and negation handling |
| `adaptive_coordinator/runner.py` | Closed loop | Evidence Gates, decision loop, retry/no-progress/xhigh budgets, partial rerun, WRITE diagnosis/fencing, deterministic-first verification, schema v2 trace |
| `adaptive_coordinator/orca.py` | Orca contract | Structured lifecycle normalization, question handling, deadlines, trusted-relay evidence and cleanup fencing |
| `adaptive_coordinator/worker_report.py`, `result_sentinel.py` | Durable result transport | Bounded structured result helper and terminal marker without workspace/temp writes |
| `adaptive_coordinator/benchmark.py`, `scripts/run-v02-benchmark.py` | Evaluation | Reproducible v0.1/all-Sol/v0.2 corpus comparison |
| `adaptive_coordinator/cli.py`, `pyproject.toml` | Public contract | Additive result schema and `0.2.0` version/installed-commit reporting |
| `tests/` | Regression/invariants | Closed-loop, payload, CLI, benchmark, installer, lifecycle and worker-report coverage |
| `AGENTS.md`, `docs/adaptive-model-routing.md` | Durable policy | v0.2 invariants, evidence-first decisions, verification and cost-quality rules |

## C. Final Capability Ladder

| Rank | Model | Effort |
|---:|---|---|
| 0 | `gpt-5.6-luna` | `low` |
| 1 | `gpt-5.6-terra` | `medium` |
| 2 | `gpt-5.6-terra` | `high` |
| 3 | `gpt-5.6-sol` | `medium` |
| 4 | `gpt-5.6-sol` | `high` |
| 5 | `gpt-5.6-sol` | `xhigh` |

Initial xhigh is forbidden. Capability failure moves exactly +1; a newly proven auth/security/DB/data-integrity risk may apply a separate Sol risk floor. Capability never raises filesystem authority, a logical Gate never automatically downgrades, and xhigh is the automatic ceiling with one attempt per Gate and one per run by default.

## D. Failure Classification Matrix

| Failure class | Default decision | Same-level retry | Capability escalation | Terminal condition |
|---|---|---:|---:|---|
| `INSUFFICIENT_SUCCESS_EVIDENCE` | `RESULT_REPAIR` / `COLLECT_EVIDENCE` | No full replay | No | Evidence remains unavailable |
| `EVIDENCE_GAP` | `COLLECT_EVIDENCE` | Focused collection | No | Required evidence cannot be obtained |
| `STALE_EVIDENCE` | `COLLECT_EVIDENCE` | Correct target | No | Current target unavailable |
| `ENVIRONMENT_MISMATCH` | `COLLECT_EVIDENCE` | Correct environment | No | Correct environment unavailable |
| `TARGET_IDENTITY_MISMATCH` | `COLLECT_EVIDENCE` | Correct target | No | Identity cannot be reconciled |
| `TRANSIENT_FAILURE` | `RETRY_SAME_CAPABILITY` | Once with material delta | No by default | Retry budget exhausted |
| `RECOVERABLE_IMPLEMENTATION_FAILURE` | `RETRY_SAME_CAPABILITY` | Once with material delta | Not on first failure | Repeated/no-progress failure |
| `CAPABILITY_FAILURE` | `ESCALATE_CAPABILITY` | No identical retry | Exactly +1 | Ceiling/budget exhausted |
| `AMBIGUOUS_FAILURE` | focused retry | Once | +1 if repeated | Ceiling/no-progress |
| `DECOMPOSITION_FAILURE` | `REPLAN` / READ-only diagnosis | Replanned only | No first response | Safe plan unavailable |
| `MISSING_CONTEXT` | acquire context / `BLOCKED` | No | No | Required context unavailable |
| `EXTERNAL_BLOCKER` | `BLOCKED` | No | No | Permission/plan/service blocker persists |
| `USER_ACTION_REQUIRED` | `BLOCKED` | No | No | User action is required |
| `ORCHESTRATION_FAILURE` | recover or `TERMINAL` | Only with a material runtime delta | No | Safe evidence/recovery unavailable |
| `TERMINAL_FAILURE` | `FAILED` / `BLOCKED` | No | No | Further execution has no safe value |

## E. Success Evidence Gate

- Investigation: material conclusion, concrete evidence, checked files/tools, and unresolved questions field.
- Assessment: risks, material impact, rollback/recovery, explicit `write_ready=true`, and no blocking uncertainty.
- Implementation: actual diff equals both `files_modified` and `workspace_diff`; requirements, tests, strict deterministic pass results, and unexecuted verification are reported. Test results are either one accepted framework summary or all named `check: passed (detail)` entries; the forms cannot be mixed.
- Verification: explicit `VERIFIED`; `NOT_VERIFIED`, `INCONCLUSIVE`, and `TARGET_FAILED` are distinct non-success outcomes.
- `worker_done` settles lifecycle only. A completion word, stale target, contradictory diff, failed test, or incomplete evidence cannot produce task SUCCESS.

## F. Logical Gate and Attempt Trace

1. Repeated `NOT_VERIFIED`: verification focused retry, repeated ambiguity, verification capability +1, then resolution; successful assessment/WRITE Gates are not replayed.
2. Permission/plan limitation: `EXTERNAL_BLOCKER` → `BLOCKED`; capability rank unchanged.
3. Assessment SUCCESS + Implementation SUCCESS + Verification failure: only Verification reruns.
4. Concrete verifier `TARGET_FAILED`: prior Implementation Gate is explicitly invalidated/reopened, fixed, then freshly verified.
5. Sol/high unresolved with qualifying ambiguity/data-integrity evidence: budget and ceiling checks permit exactly one Sol/xhigh attempt.
6. Implementation/deployment commit mismatch: `TARGET_IDENTITY_MISMATCH` → correct target collection → verification, with no model escalation.
7. Complex low-risk WRITE with sufficient deterministic tests: `DETERMINISTIC_ONLY`; no Fresh Verifier dispatch.

Attempt traces record Gate/attempt IDs, parentage, capability rank, authority, classification/confidence, decision/reason, material delta, fingerprints, changed files, verification mode, elapsed time, and invalidation evidence. Evidence packets are bounded; raw transcripts and secrets are not carried forward.

## G. Authority Verification

Tests exercise `Terra/high READ_ONLY → Sol/medium READ_ONLY → Sol/high READ_ONLY → Sol/xhigh READ_ONLY`. Authority remains READ_ONLY at every capability rank. WRITE ownership remains a fenced Lead Gate; no second mutation attempt begins while its predecessor is active. `danger-full-access` has no automatic route.

## H. Korean/English Equivalence

| Meaning | English/Korean result |
|---|---|
| Inventory / 목록 조사 | Luna/low, READ_ONLY |
| Local validation helper implementation / 로컬 검증 헬퍼 구현 | Terra/medium, workspace-write |
| Async retry/timeout integration / 비동기 재시도·타임아웃 연동 | Terra/high |
| Ordinary authorization assessment / 일반 권한 영향 분석 | Sol/medium, READ_ONLY assessment |
| Concrete destructive rollback risk / 파괴적 변경·롤백 불확실 | Sol/high with concrete reason |

Korean and English negations such as “do not modify the database”, “authentication is out of scope”, and “inspect without changing files” do not create WRITE or Critical positive signals.

## I. xhigh Smoke Test

- Actual Codex READ_ONLY smoke: `gpt-5.6-sol`, `model_reasoning_effort=xhigh`.
- Result: `XHIGH_READ_ONLY_OK`.
- Usage observed: input 15,252; cached input 9,984; output 9. No filesystem authority increase occurred.
- Unsupported xhigh terminates at Sol/high; it never falls through to max or an arbitrary model.

## J. Tests

- Final unit/contract suite: 177/177 PASS.
- Compile/static checks: PASS.
- Shell syntax: PASS.
- `git diff --check`: PASS.
- Orca payload contract: PASS for worker_done, escalation, question, timeout with/without evidence, permission denial, plan limitation, placement failure, cleanup failure, and trusted-relay marker shapes.
- Fixture provenance: two sanitized actual Orca captures plus deterministic synthetic edge cases; synthetic cases are explicitly labeled and not represented as production captures.
- State-machine invariants: PASS for monotonic capability, finite attempts, xhigh ceiling/budget, no duplicate successful WRITE, mutation fencing, authority independence, external-blocker no-escalation, final-WRITE verification freshness, and no identical retry.

## K. Cost and Quality Benchmark

| Policy | Verified success | False success | Workers/attempts | Compute proxy | Cost / verified success |
|---|---:|---:|---:|---:|---:|
| v0.1 modeled | 61.54% | 1 | 13/13 | 26 | 3.25 |
| all-Sol/medium modeled | 92.31% | 0 | 23/23 | 92 | 7.67 |
| v0.2 deterministic corpus replay | 92.31% | 0 | 23/23 | 58 | 4.83 |

v0.2 retained v0.1 Routine/Standard initial routes and added zero happy-path LLM dispatches. Duplicate successful WRITE, external-blocker escalation, identical retry, authority auto-escalation, and Routine/Standard initial xhigh were all zero. v0.2 matched all-Sol verified quality at 36.96% lower normalized compute. Token usage was unavailable for the deterministic corpus, so model/effort calls, attempts, elapsed time and normalized compute are the declared proxy; the actual xhigh smoke reports available token usage separately.

## L. Deployment Result

- Merged production code commit before this handoff: `5ef224266a2f517419e5d3c17d9e996bf2ff3cc5` (PR #22 is the final result-contract fix).
- Installed exact commit before this handoff: `5ef224266a2f517419e5d3c17d9e996bf2ff3cc5`.
- Package version: `0.2.0`; `/home/user/.local/bin/orca-adaptive --version` reported the same installed commit.
- READ_ONLY pilot: PASS (`run_f09248a75193`, installed commit
  `7033ad0872020fb63bb339b7b14041c3eb0d7c7f`), Luna/low/read-only, one
  worker/attempt, no file changes, trusted settlement, released. This proves the
  v0.2 READ path but is not represented as a final-commit deployment pilot.
- Low-risk WRITE pilot: PASS (`run_1e72c8e5eafb`), Terra/medium/workspace-write, one worker/attempt, deterministic named pass evidence, trusted settlement, released; the temporary probe was removed and the tree stayed clean.
- A broader preceding WRITE probe (`run_36ab81291fa2`) returned no safe evidence by its deadline; it was correctly classified `ORCHESTRATION_FAILURE`, not escalated, fenced and released. A materially narrower pilot then established the required WRITE production evidence.
- Rollback: launcher switched to preserved exact snapshot `c653e299bde67d1bc3725fce18419785c3a6524d`, reported that commit, and was restored to `5ef2242`; no snapshot was deleted.
- Closure procedure after this document is merged: fast-forward local `main`,
  install that exact merge commit, rerun a READ_ONLY pilot, and verify
  local/GitHub/installed commit equality before reporting completion.

## Fresh Verifier

- Independent READ_ONLY verifier: PASS for the final worker-result contract.
- Verified strict parser/prompt agreement, false-success protection, 177 tests, benchmark zero-invariants, authority independence, lifecycle settlement and cleanup regression.
- The post-handoff READ_ONLY verification against the merged exact commit is a
  remaining closure step; its outcome is reported only after it runs.

## Git / GitHub

- Branch policy: feature/fix branches, independent review, merge commits; no direct `main` development.
- Repository: `jgkang2365-afk/orca-adaptive-routing` (PRIVATE), remote `github`.
- Pushes are normal non-force. No rebase, squash, reset, or history rewrite was used.
- Final HEAD: the merge commit containing this handoff; local main, GitHub main and installed snapshot are made identical before closure.

## Safety

- WSL `/home`, READ_ONLY/workspace-write sandbox policy, Critical Safety Gate, trusted relay, verifier independence and cleanup failure gate remain intact.
- No sandbox expansion, danger-full-access, destructive Git command, remote replacement, other-project modification, credential persistence, or uncontrolled external mutation occurred.
- Completed pilot workers were released; exact probe paths are absent and unrelated Orca resources were untouched.

## M. Remaining Limits

- Cross-run attempt persistence and billing-grade token accounting remain intentionally out of v0.2 scope.
- Synthetic payload edge fixtures supplement, rather than impersonate, the two sanitized real Orca captures; future real payload shapes should be added as redacted fixtures when observed.
- Orca/terminal transport can still fail independently of model capability. v0.2 terminates safely without escalation or false success when no trustworthy result evidence is available.

## Decision Required

None.

## Next Start Point

Run the next real project task through installed `orca-adaptive run`, provide only the business objective and project constraints, and inspect the schema-v2 Gate/attempt trace only when recovery or escalation occurs.
