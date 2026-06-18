---
title: "fix: Restore Telegram restart through native launchd service repair"
type: fix
status: active
date: 2026-06-08
origin: Telegram diagnosis request from Tobi
---

# fix: Restore Telegram restart through native launchd service repair

## Summary

Use Hermes' native gateway service management instead of adding custom restart machinery. Current Hermes already generates launchd plists with unconditional `KeepAlive`, self-heals stale or unloaded launchd jobs during `gateway start`, and implements drain-aware service restart through the native CLI path.

No source patch is the first move. Repair the stale default and `hermes-scout` service definitions with the native CLI, verify Telegram restart behavior, and only escalate to code if the native repair fails under real `/restart` smoke testing.

---

## Problem Frame

Telegram `/restart` stranded Navi and Akira because their launchd jobs are stale: status reports the installed plists do not match the current Hermes install and launchd shows old `OnDemand = true` behavior. Jarvis survives because its `hermes-ops` launchd job already matches the current generated service definition.

External and local research both point to the same native recovery path:

- Hermes docs describe `hermes gateway start`, `hermes gateway restart`, and profile-scoped `hermes -p <profile> gateway <action>` as the supported managed-service lifecycle for macOS LaunchAgents.
- Current Hermes source generates launchd plists with unconditional `KeepAlive` and `RunAtLoad`.
- Current Hermes source makes `launchd_start()` refresh stale plists, bootstrap unloaded jobs, then kickstart the service.
- Current Hermes source makes `launchd_restart()` use SIGUSR1 for drain-aware self-restart when a PID exists, falling back to `launchctl kickstart` and bootstrap recovery when needed.

The earlier plan overreached by making a gateway code patch the primary fix. That is the wrong first move. The platform already has the service-repair primitive we need; use it.

---

## Requirements

**Native recovery**

- R1. The recovery must use Hermes' native gateway lifecycle commands before any custom code is considered.
- R2. Default/Akira and `hermes-scout` launchd plists must match the current Hermes generated service definition after repair.
- R3. Repaired services must be loaded and running under launchd with a PID.

**Telegram restart behavior**

- R4. Telegram `/restart` must bring the same profile back online and emit the normal restarted notification or otherwise respond after restart.
- R5. Verification must cover default/Akira, `hermes-scout`, and `hermes-ops` separately because each profile has its own LaunchAgent and bot token.

**Scope control**

- R6. No gateway source patch, canary cron, watchdog, or custom helper is in scope unless native service repair fails during verification.
- R7. Live service actions require explicit operator approval before execution because they change running bot state.

---

## Key Technical Decisions

- KTD1. Prefer native service repair over code changes: `hermes gateway status` already identifies stale launchd definitions and tells the operator to run `gateway start`; source confirms that path rewrites stale plists and reloads launchd.
- KTD2. Treat current generated `KeepAlive = true` as the intended macOS contract: it makes launchd restart the gateway on any exit, including a clean Telegram-triggered restart.
- KTD3. Verify with Telegram smoke tests, not launchd state alone: launchd can be loaded while the bot is unusable due to token conflicts, polling failures, or provider errors.
- KTD4. Keep source changes as a fallback only: the existing `/restart` handler still has a systemd/container-only service-manager check, but the repaired launchd plist should make that path unnecessary for the observed failure. Patch only if the repaired native path still strands a bot.

---

## Implementation Units

### U1. Confirm native launchd contract and current profile state

**Goal:** Establish whether each profile needs native repair and capture the pre-repair state.

**Requirements:** R1, R2, R3, R5

**Dependencies:** none

**Files:**

- `hermes_cli/gateway.py`
- `gateway/run.py`
- `docs/plans/2026-06-08-001-fix-launchd-telegram-restart-plan.md`

**Approach:** Use native status output for default, `hermes-scout`, and `hermes-ops`. Compare it with the source contract in `generate_launchd_plist()`, `launchd_start()`, and `launchd_restart()`. Record only status facts needed to decide repair; do not inspect or print secrets.

**Patterns to follow:**

- `hermes_cli/gateway.py` launchd helpers for generated plist, stale detection, start repair, restart behavior.
- Hermes docs: Running Many Gateways at Once, CLI Commands Reference, Slash Commands Reference.

**Test scenarios:**

- Default profile status reports whether `ai.hermes.gateway.plist` matches current install.
- `hermes-scout` status reports whether `ai.hermes.gateway-hermes-scout.plist` matches current install.
- `hermes-ops` status remains the known-good comparison profile.
- No status output includes Telegram tokens, provider keys, or credential values.

**Verification:** The implementer can name which profiles need native repair and which already match the current launchd contract.

### U2. Repair stale launchd services using Hermes native CLI

**Goal:** Bring default/Akira and `hermes-scout` onto the current generated launchd definitions.

**Requirements:** R1, R2, R3, R7

**Dependencies:** U1

**Files:**

- `hermes_cli/gateway.py`

**Approach:** After explicit operator approval, run the profile-scoped native `gateway start` action for each stale profile. This is the supported repair path: it refreshes stale plist content, reloads an unloaded launchd job if needed, and kickstarts the service. Do not hand-edit plists.

**Execution note:** Live runtime action. Stop and get approval immediately before running it.

**Patterns to follow:**

- `launchd_start()` refreshes stale plists, bootstraps unloaded jobs, and kickstarts launchd.
- Multi-profile docs distinguish the default profile from named profiles.

**Test scenarios:**

- Default stale plist becomes current after native repair.
- `hermes-scout` stale plist becomes current after native repair.
- `hermes-ops` remains current and is not unnecessarily rewritten.
- Each repaired launchd service is loaded and reports a running PID.

**Verification:** Native status reports `Service definition matches the current Hermes install` and launchd reports a PID for default/Akira and `hermes-scout`.

### U3. Smoke-test Telegram restart through the repaired native path

**Goal:** Prove `/restart` works from Telegram for each live profile after native repair.

**Requirements:** R4, R5

**Dependencies:** U2

**Files:**

- `gateway/run.py`
- `gateway/status.py`

**Approach:** Send a Telegram `/restart` to each profile's bot/chat, wait for the restarted notification or a successful follow-up response, then confirm launchd still reports the service loaded and running. Treat a launchd-only pass as insufficient.

**Execution note:** Live runtime action. Run one profile at a time to avoid confusing Telegram polling and topic routing.

**Patterns to follow:**

- `/restart` notification and dedup marker handling in `gateway/run.py`.
- Launchd status and PID handling in `gateway/status.py` and `hermes_cli/gateway.py`.

**Test scenarios:**

- Default/Akira responds after `/restart` and remains launchd-managed.
- `hermes-scout` responds after `/restart` and remains launchd-managed.
- `hermes-ops` still responds after `/restart`, proving the known-good path did not regress.
- If a model/provider error occurs after restart, separate it from gateway liveness; a provider 429 is not a restart failure.

**Verification:** All three profiles can answer a post-restart Telegram smoke message and launchd reports each service running with a PID.

### U4. Escalate only if native repair fails

**Goal:** Define the fallback boundary without making custom code the default path.

**Requirements:** R6

**Dependencies:** U3

**Files:**

- `gateway/run.py`
- `tests/gateway/test_restart_notification.py`

**Approach:** If a repaired launchd plist still lets Telegram `/restart` strand a profile, then add a narrow regression test and patch `_handle_restart_command` to recognize launchd-managed gateways as service-managed. The likely marker is a Darwin process with `XPC_SERVICE_NAME` matching the Hermes launchd label. This fallback stays out of scope unless native repair fails.

**Execution note:** Test-first only if escalation is triggered.

**Patterns to follow:**

- Existing systemd restart routing test in `tests/gateway/test_restart_notification.py`.
- Existing service-restart exit behavior in `gateway/run.py`.

**Test scenarios:**

- With Darwin launchd markers, `/restart` requests `via_service=True`.
- With unrelated Darwin markers, detached/manual behavior remains unchanged.
- Existing systemd and container restart behavior remains unchanged.
- Manual foreground gateway restart remains detached and does not rely on launchd.

**Verification:** Focused regression test fails before the fallback patch and passes after it; only run this unit if U3 fails.

---

## Risks & Dependencies

- Native service repair is live runtime work. It can bring bots online, kill stale gateway processes, and expose Telegram token conflicts if duplicate processes exist.
- The current shell/session profile can make a bare `hermes gateway status` target `hermes-ops`. Use explicit `HERMES_HOME` or profile selection when checking default vs named profiles.
- Launchd state is not enough. Telegram polling, topic routing, and provider errors can make a bot appear down even when launchd is healthy.
- The current `/restart` handler does not explicitly detect launchd in the service-manager branch. That is only a source-change risk if repaired native launchd definitions still fail the Telegram smoke test.

---

## Sources & Research

- Hermes docs: `user-guide/multi-profile-gateways` documents profile-scoped managed gateway lifecycle on macOS LaunchAgents.
- Hermes docs: `reference/cli-commands` documents `hermes gateway` as the native run/manage command family.
- Hermes docs: `reference/slash-commands` documents messaging gateway slash commands and their central registry.
- GitHub issue `NousResearch/hermes-agent#26198` records the historical macOS launchd restart failure and points at `gateway start` / `kickstart` style recovery.
- GitHub issue `NousResearch/hermes-agent#27592` records Telegram self-restart leaving launchd unloaded and notes native `hermes gateway start` as recovery.
- Local source: `hermes_cli/gateway.py` generates `KeepAlive = true`, repairs stale plists in `launchd_start()`, and implements drain-aware `launchd_restart()`.
- Local source: `gateway/run.py` handles Telegram `/restart`, restart notification markers, and service-restart exit behavior.

---

## Acceptance Criteria

- Default/Akira and `hermes-scout` are repaired with native Hermes gateway lifecycle commands, not hand-edited plists.
- Default/Akira, `hermes-scout`, and `hermes-ops` all report current launchd service definitions.
- Default/Akira, `hermes-scout`, and `hermes-ops` all report loaded/running launchd services with PIDs.
- Telegram `/restart` succeeds for each profile and each bot answers afterward.
- No source patch is made unless a repaired native launchd profile still fails Telegram `/restart` verification.

---

## 2026-06-08 execution note: GUI-domain launchd repair

After approval, default/Akira and `hermes-scout`/Navi were repaired using native launchctl against the domain where macOS 26 had actually loaded the jobs: `gui/501`, not `user/501`.

Actions performed:

- Booted out stale GUI-domain jobs for `ai.hermes.gateway` and `ai.hermes.gateway-hermes-scout`.
- Killed the detached fallback gateway processes that `hermes gateway start` had created after its `user/501` launchctl failure.
- Bootstrapped the repaired plists from `/Users/akira/Library/LaunchAgents/` into `gui/501`.
- Kickstarted both labels.

Verified state after repair:

- default/Akira: `launchctl print gui/501/ai.hermes.gateway` shows `state = running`, PID `17688`, `working directory = /Users/akira/.hermes`, and `properties = keepalive | runatload | inferred program`.
- `hermes-scout`/Navi: `launchctl print gui/501/ai.hermes.gateway-hermes-scout` shows `state = running`, PID `17648`, `working directory = /Users/akira/.hermes/profiles/hermes-scout`, and `properties = keepalive | runatload | inferred program`.
- Detached fallback duplicates were removed; process scans showed one matching gateway process per repaired profile, owned by launchd.
- Both gateways reconnected to Telegram in polling mode after launchd start.

Operator-side restart smoke:

- `hermes-scout`/Navi survived `launchctl kill SIGUSR1 gui/501/ai.hermes.gateway-hermes-scout`; launchd relaunched it with a new PID and Telegram reconnected.
- default/Akira handled SIGUSR1 as a planned restart and shut down the gateway loop, but the process did not exit promptly; `launchctl kickstart -k gui/501/ai.hermes.gateway` restored it and Telegram reconnected. This is not yet enough to declare user-originated Telegram `/restart` proven for default.

User-originated Telegram `/restart` verification:

- default/Akira: Tobi sent `/restart` from Telegram. Logs show shutdown at `19:45:23`, `Gateway stopped` and cron stopped at `19:45:26`, launchd restarted it, Telegram reconnected at `19:46:28`, and Hermes sent the restart notification at `19:46:29`. `launchctl print gui/501/ai.hermes.gateway` stayed `state = running` with PID `20091`, `last exit code = 0`, `runs = 4`, `properties = keepalive | runatload | inferred program`.
- `hermes-scout`/Navi: Tobi sent `/restart` from Telegram. Logs show shutdown at `19:47:11`, `Gateway stopped` and cron stopped at `19:47:14`, launchd restarted it, Telegram reconnected at `19:47:15`, and Hermes sent the restart notification at `19:47:17`. `launchctl print gui/501/ai.hermes.gateway-hermes-scout` stayed `state = running` with PID `20253`, `last exit code = 0`, `runs = 4`, `properties = keepalive | runatload | inferred program`.
- Process scan after both smokes showed exactly one matching gateway process for each repaired profile, both parented to launchd.

Conclusion: native GUI-domain launchd repair succeeded. No Hermes source patch is needed for the observed Telegram `/restart` stranding failure.

## Deferred to Follow-Up Work

- Add a read-only restart canary only if Tobi wants ongoing monitoring; do not bundle it with the native repair.
- Patch launchd detection inside `_handle_restart_command` only if the native repair path fails after current plists are restored.
- Improve status output so a bare command in a profile-scoped shell makes the target profile unmistakable before printing launchd state.
