# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Agent Runtime

### Hermes Agent
The personal AI agent runtime that shares one core conversation engine across command-line, messaging, desktop, scheduled, and delegated execution surfaces.

### Profile
An isolated Hermes operating context with its own configuration, memory, credentials, logs, and gateway process lifecycle.

### Gateway
The Hermes runtime surface that receives messages from external platforms and turns them into agent conversations, then delivers the agent's responses back through those platforms.

### Runtime Footer
A compact metadata suffix attached to a completed assistant reply to expose turn-runtime details such as model, working directory, usage, or context-token budget.

The Runtime Footer belongs to the final assistant reply. When a reply streams through a platform, footer delivery must follow the same final-message path rather than becoming a separate platform message.

### Streamed Final
The last user-visible assistant message produced by an incremental response stream.

A Streamed Final is distinct from progress updates, commentary, tool activity, segment boundaries, and overflow handling; only the Streamed Final is eligible for final-only suffixes such as a Runtime Footer.

### Live Activation
The operational step where a code change becomes effective in running Hermes processes.

Live Activation is separate from implementation: a fix can exist in source control and pass tests while running profiles still use old code until their process lifecycle is explicitly advanced.
