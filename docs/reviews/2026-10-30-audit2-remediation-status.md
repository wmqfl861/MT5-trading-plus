# Audit 2 remediation status — 2026-10-30

Baseline: `d67476a10d03b3d479a9ebcf9f00dbc09060b489` (main before remediation).
Implementation branch: `audit-live-safety-2026-10-30`.
Pull request: https://github.com/wmqfl861/MT5-trading-plus/pull/3

## Code remediation

| Finding | Change | Current evidence |
|---|---|---|
| H2-01 order result unknown after broker send | Durable `execution_unknown` marker before send; pending intent recovered as UNKNOWN; exceptions no longer write a false confirmed intent; opening legs blocked while risk-reducing closes remain possible | New executor tests; CI pending |
| H2-01 operator recovery | `scripts/live_ack_execution.py` requires runner lock released, matching decision ID, no pending orders, successful broker state/history reads, and explicit operator confirmation; it submits no orders | New script and guard test; CI pending |
| H2-02 stale protective deal reused for later exit | Per-position consumed deal-ticket watermark and unmatched observed exit volume; already-consumed partial SL/TP/SO tickets are excluded from later exit explanations; visible CLIENT/unknown exits remain manual-only until event-bound acknowledgement | Regression test for partial SL + delayed manual exit; CI pending |
| H2-03 base currency absent from official snapshot | `currency_base` now flows MT5 row → `SymbolSpec` → snapshot/diff; old rows stay `None` and are not guessed from profit currency | Separate PR #2 pytest passed: 2700 passed, 3 skipped |
| Demo stage F stalled risk loop | Added independent watchdog thread; a stalled risk polling loop writes a fail-closed `halt.json` and alert without placing orders | Deterministic unit test added; CI pending |

## Research and statistical disposition

- X-01 through X-06 and P2-01 through P2-09 were addressed in the existing T103–T105 commits already on main.
- T106 `cluster-permutation-v1` remains **not adopted** because the preregistered terminal w3 CP95 upper bound was 6.02%, above the 5% requirement. This is the correct research decision; no threshold or seed change is authorized.
- T91 remains archived as “not testable under current legal data, registered Δp, frozen cost and power requirements”. No empty R2 or T92 formal run is authorized.
- P2-10 small-sample coverage/undercoverage study remains a T92 prerequisite, not an adopted model or a passed result. It must be independently designed and validated before T92.

## Operational work still requiring a connected Demo machine

The source changes above do not substitute for the real stage-F rehearsal. The registered Desktop Commander machine was offline during this remediation. No MT5 terminal was connected; no Demo/real account was queried; no process was restarted; no order was submitted.

Before declaring stage F complete, operations must:
1. Deploy the exact reviewed SHA to the Demo machine during a maintenance window.
2. Record runner/risk/reconcile process IDs and version SHA.
3. Deliberately stall the risk polling loop beyond watchdog timeout and verify `halt.json`, alert, and rejection of new opens.
4. Run killSwitch stop → verify no new opens → remove stop signal under the approved recovery procedure.
5. Verify runner/risk/reconcile recovery and broker-vs-local positions/orders/deals reconciliation.
6. Resume the 10-trading-day evidence window only under the existing runbook; record interruptions and extensions. Do not backfill missing operational evidence.

## Repository governance still requiring owner settings

At audit time, `main` had `protected=false`. The connected GitHub integration can read repository protection but does not expose the write operation needed to enable it. Recommended owner-side settings:

- Protect `main`; prohibit force-push and branch deletion.
- Require pull requests for ordinary development and require the `pytest` status check.
- Require resolved review conversations.
- Keep the research-path allowlist intact; do not weaken it to permit research agents to change live/gateway/risk/CI files. Main-maintainer-only integration remains required for those paths.
- Permit only authorized maintainers to bypass the research-role allowlist for explicitly approved maintenance changes.

## Acceptance boundary

A code change is not a Demo acceptance. Final closure requires the latest branch pytest to pass, an independent review of the changed live state machine, a reviewed main integration commit, and the real stage-F operational evidence above. This report intentionally distinguishes code-level remediation from account-side operational proof.
