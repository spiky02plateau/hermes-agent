---
title: Runtime footer streaming fix was implemented but not deployed
date: 2026-06-18
category: runtime-errors
module: hermes-agent
problem_type: runtime_error
component: service_object
symptoms:
  - Runtime footer streaming fix existed in historical commit 2e34898fd but was not active on main or live profiles.
  - Streamed final replies could miss native runtime footer metadata in the final user-visible message.
  - Old gateway behavior risked sending the footer as a standalone trailing message after streamed content was already delivered.
root_cause: missing_workflow_step
resolution_type: code_fix
severity: medium
tags:
  - gateway
  - streaming
  - runtime-footer
  - profile-activation
  - regression-test
related_components:
  - tooling
  - development_workflow
  - testing_framework
---

# Runtime footer streaming fix was implemented but not deployed

## Problem

The runtime-footer streaming fix had already been implemented, but it lived on a stale branch/commit and was not active on current `main` or the running profile gateways. Current deployable code still had the old split-footer failure mode: streamed final content was delivered first, then footer metadata could be sent separately as its own trailing platform message.

## Symptoms

- A proven fix existed on `fix/runtime-footer-streaming-token` at commit `2e34898fd`, but current `main` and the live profile checkout did not contain it.
- `gateway/run.py` still had footer append behavior gated by `not already_sent`, which skipped normal footer insertion after streaming had already sent the response body.
- The already-sent branch still allowed a separate `_footer_line` send, creating the exact user-visible footer-only trailing message the fix was meant to eliminate.
- Native token footer rendering needed to be verified under profile-like `HOME`/`HERMES_HOME` because a previous `.pth` monkeypatch path could drift per profile.
- Passing targeted tests in a checkout was not enough to claim the live Telegram/connected profile behavior was fixed; the running gateway processes still needed explicit activation.

## What Didn't Work

- Treating the old fixed branch as “shipped” did not make the running profiles use it. The fix had to be present on a clean deployable branch based on current `main`. (session history)
- Leaving footer rendering to a `.pth` monkeypatch was fragile because profile gateway processes may resolve `~` differently from the shared install. Native renderer support was the durable path. (session history)
- Sending a footer after streaming finalized the body made the metadata visible but reintroduced the product bug: a standalone footer message.
- Restarting live gateways as part of implementation would have crossed the approval boundary. Activation had to remain a separate operational step.

## Solution

Create a clean branch from current `main`, cherry-pick the proven footer commit, reconcile it against the current gateway code, and add a direct regression guard for the deployment-gap failure mode.

```bash
git checkout main
git pull --ff-only
git checkout -b fix/runtime-footer-deployable-activation
git cherry-pick 2e34898fd
```

In this incident, the resulting branch contained the deployable fix series:

```text
627efb167 fix(gateway): keep runtime footer in streamed finals
cb7b7d13b test(gateway): guard runtime footer routing
8abd6fa79 refactor(gateway): tighten runtime footer finalization
```

The reconciled implementation preserves three boundaries:

1. `GatewayStreamConsumer` owns final streamed suffixing because it knows when a stream event is the true final answer rather than commentary, tool progress, a segment boundary, or overflow.
2. Non-streaming final replies append the footer in the regular final response path.
3. The `already_sent` branch is forbidden from sending `_footer_line` as its own platform message.

The new regression guard lives in:

```text
tests/gateway/test_runtime_footer_routing.py
```

It asserts the already-sent branch does not contain the old footer-only send shape and that the stream consumer receives the footer suffix before `finish()` finalizes the stream.

Native token rendering was verified under a profile-like environment:

```text
footer_patch_imported False
footer 12K/100K · 12%
missing_length <empty>
```

Targeted verification passed:

```bash
PYTEST_ADDOPTS= python -m pytest \
  tests/gateway/test_runtime_footer.py \
  tests/gateway/test_stream_consumer_draft.py \
  tests/gateway/test_runtime_footer_routing.py \
  -q
```

```text
51 passed
```

Lint passed on the affected files:

```bash
python -m ruff check \
  gateway/run.py \
  gateway/runtime_footer.py \
  gateway/stream_consumer.py \
  tests/gateway/test_runtime_footer.py \
  tests/gateway/test_stream_consumer_draft.py \
  tests/gateway/test_runtime_footer_routing.py
```

```text
All checks passed
```

Example read-only live state checks found the three profile gateways running, but they were not restarted:

```text
default      running
hermes-ops   running
hermes-scout running
```

## Why This Works

The bug had two separate failure classes: streaming finalization and activation drift. The code fix solves the streaming class by moving footer insertion into the component that knows true finality. The branch/deployment work solves the activation class by getting the known-good implementation onto a current-main branch that can actually be deployed.

`GatewayStreamConsumer.set_final_suffix(...)` keeps runtime footer metadata attached to the final content delivery path. Because suffix application happens only when the consumer processes the true stream end, segment boundaries and commentary/tool-progress updates do not receive the footer. The suffix helper is idempotent, so retries or alternate finalization paths do not duplicate the footer.

The already-sent branch now returns without a footer-only send. That makes the failure mode obvious: if the stream path fails to include a footer, the footer is omitted rather than emitted as a standalone metadata bubble. Missing metadata is better than a recurring extra message because it avoids user-visible spam and makes the remaining bug easier to detect.

Native token rendering removes the profile-HOME dependency entirely. Instead of relying on a `.pth` patch whose path may resolve differently under each profile, `gateway/runtime_footer.py` renders compact context-token fields directly.

## Prevention

- When a fix exists on an old branch, verify it is present on current `main` or the intended deployable branch before calling it active.
- Keep branch integration separate from live activation: code can be correct in the checkout while running gateway processes still hold old modules in memory.
- Add direct regression tests for routing bugs, not just renderer output. In this case, the important guard is that `already_sent=True` must never call the platform adapter to send `_footer_line` separately.
- Probe runtime behavior under profile-like `HOME`/`HERMES_HOME` when previous fixes depended on venv `.pth` loading or user-home expansion.
- Treat gateway restarts as operational activation. Report profile state first, then restart only after explicit approval.
- Leave deployment-gap plans active until live activation and smoke verification are complete; green tests alone do not prove a running profile is fixed.

## Related Issues

- `docs/plans/2026-06-16-001-fix-runtime-footer-streaming-token-plan.md` — original streaming/token-rendering fix plan.
- `docs/plans/2026-06-18-002-fix-runtime-footer-deployment-gap-plan.md` — deployment-gap plan that identified the stale fixed commit and approval-gated activation path.
- `profiles/hermes-ops/skills/devops/hermes-operations/references/gateway-streaming-runtime-footer.md` — operational reference for this failure class in the incident environment.
- GitHub issue #35427 — adjacent runtime footer field expansion context.
- GitHub issues #42376, #42524, #43475, #45361 — adjacent macOS launchd/gateway restart hazards relevant to activation.
