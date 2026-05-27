# Hermes Agent: ChatGPT & Gemini Model Wiring Analysis

**Date:** 2025-05-24  
**Context:** Hermes v0.14.0 on Mac Mini. Two ChatGPT subs, one Gemini sub. Deciding native vs proxy vs hybrid.

---

## 1. Native `openai-codex` vs CLIProxyAPI/VibeProxy

### What the native provider does

- Uses ChatGPT subscription OAuth (device-code login) → hits `chatgpt.com/backend-api/codex` (the Codex Responses API)
- Hardcodes `codex_responses` as `api_mode` — you can't override to `chat_completions` ([issue #5718](https://github.com/NousResearch/hermes-agent/issues/5718), still open)
- Model list from live discovery at `/codex/models` + curated fallback: `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.3-codex-spark`, `gpt-5.2-codex`, `gpt-5.1-codex-max`, `gpt-5.1-codex-mini`

### What the proxy does (CLIProxyAPI)

- Same OAuth under the hood, but exposes a standard `/v1/chat/completions` endpoint
- Handles token refresh, multi-account load balancing internally
- Translation layer: Codex CLI session → OpenAI-shaped API response

### Head-to-head

| Dimension | Native `openai-codex` | CLIProxyAPI/VibeProxy |
|---|---|---|
| **Reliability** | Moderate — Responses API returns `status=failed` on quota exhaustion as HTTP 200 (soft failure), and the `response_invalid` handler **skips credential pool rotation** ([bug #24159](https://github.com/NousResearch/hermes-agent/issues/24159), still open). Also, `gpt-5.5` hangs or gets rejected on the Codex backend for some subscription tiers ([bug #21444](https://github.com/NousResearch/hermes-agent/issues/21444): *"The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account"*). `gpt-5.4` works. | Generally more reliable for chat because it uses `/v1/chat/completions` (the standard path). But it's a Go binary middleman — one more thing to crash, misroute, or lag. |
| **Tool calls** | Full support via Codex Responses `tools` parameter | Passes through — CLIProxyAPI translates tool schemas. Known issues with Gemini native tool calls through the proxy ([CLIProxyAPI #931](https://github.com/router-for-me/CLIProxyAPI/issues/931)). For Codex/ChatGPT it works. |
| **Reasoning** | Handled natively via `reasoning: {effort, summary}` in the Responses payload | Passthrough — but the proxy may not forward all Codex-specific response fields (reasoning tokens, etc.) with full fidelity |
| **Image gen** | Hermes's `openai-codex` provider handles image gen natively (already working) | CLIProxyAPI had no image-gen support until recently ([VibeProxy #343](https://github.com/automazeio/vibeproxy/issues/343)). VibeProxy v1.8.138 (Apr 17) predates the fix. Need CLIProxyAPI v6.10+ for reliable image gen through the proxy. |
| **Model access** | Only models the Codex backend accepts. `gpt-5.5` may be **blocked** on some tiers. The Responses API is stricter than Chat Completions. | Wider — proxy routes to `/v1/chat/completions` which accepts `gpt-5.5` even when the Codex backend rejects it. Full ChatGPT chat model lineup. |
| **ToS risk** | Hermes's device-code OAuth is the same flow Codex CLI uses — first-party. Lower risk. | CLIProxyAPI impersonates a CLI client session. Functionally same OAuth, but the proxy wraps it in a way OpenAI may not intend. Same risk tier in practice, but adds indirection that could break silently if OpenAI changes auth flow. |
| **Streaming** | Supports streaming via Responses API SSE | Supports streaming via `/v1/chat/completions` SSE |

### Verdict

The native provider has a key limitation — it's locked to `codex_responses` transport and the Codex backend, which is **stricter** about model support (`gpt-5.5` may be blocked) and has a known bug where quota-exhaustion soft failures bypass pool rotation. The proxy gives you Chat Completions access (wider model support) but at the cost of an extra moving part. For maximum reliability *today*, the proxy is slightly better for chat models, but native is better for image gen (where it already works) and for ToS cleanliness.

---

## 2. Two ChatGPT Subscriptions — Failover/Rotation

### Option A: Hermes native credential pool

The credential pool system supports multiple OAuth entries for `openai-codex`:

```bash
hermes auth add openai-codex --type oauth   # first account
hermes auth add openai-codex --type oauth   # second account (device-code login again)
```

Strategies: `fill_first` (drain #1, then #2), `round_robin`, `least_used`, `random`. Auto-rotates on 429/402. After all pool keys exhausted → falls through to `fallback_model`.

**⚠️ Critical bug:** [Issue #24159](https://github.com/NousResearch/hermes-agent/issues/24159) — when the Codex Responses API returns quota exhaustion as HTTP 200 with `response.status = "failed"`, the `response_invalid` handler **never calls `_recover_with_credential_pool()`**. Pool rotation is dead for this error path. Only the HTTP exception path (429/402 thrown as exceptions) triggers rotation. If your ChatGPT sub depletes via soft-failure rather than HTTP error, the pool won't rotate — Hermes falls straight to fallback provider.

**Workaround from the issue:** Set strategy to `fill_first` (default), and when a sub dies, manually run `hermes auth reset openai-codex` to clear cooldowns, or remove the exhausted entry.

### Option B: CLIProxyAPI multi-account load balancing

CLIProxyAPI handles multiple ChatGPT accounts at the proxy level:

- Configure multiple accounts in CLIProxyAPI's config
- It load-balances across them automatically
- Fails over when one hits limits
- Hermes sees a single endpoint (`127.0.0.1:8317/v1/chat/completions`) — just one "credential" from Hermes's perspective

### Option C: Hybrid — one sub native, one via proxy

Possible but messy:

- Native `openai-codex` with OAuth #1 as primary
- Custom provider pointing to CLIProxyAPI (which holds OAuth #2) as fallback
- Falls through on native failure → proxy kicks in
- Drawback: two different `api_mode` transports (`codex_responses` vs `chat_completions`), potential inconsistency in response handling

### Verdict

**CLIProxyAPI's multi-account balancing is more reliable today** because it doesn't depend on the broken `response_invalid` pool-rotation path. But it's fragile in a different way — you're trusting a third-party proxy's failover logic instead of Hermes's. The native credential pool *should* be the right answer once [bug #24159](https://github.com/NousResearch/hermes-agent/issues/24159) is fixed. If you want to go native, watch for that fix and use `fill_first` strategy as a workaround in the meantime.

---

## 3. Gemini — Native vs Proxy

Hermes has **two native Gemini paths:**

1. **`gemini` provider (API key):** Native `generateContent` adapter. Full tool call support, streaming, multimodal. Uses `GOOGLE_API_KEY` or `GEMINI_API_KEY`. Lowest-risk, officially supported path.

2. **`google-gemini-cli` provider (OAuth):** Uses Cloud Code Assist backend (same as `gemini-cli`). Free tier with generous quota. **But:** Google considers third-party use of the Gemini CLI OAuth client a policy violation (warning in Hermes docs). [Issue #30720](https://github.com/NousResearch/hermes-agent/issues/30720) notes it can silently fail as a fallback.

**No reason to route Gemini through CLIProxyAPI.** The native API-key adapter (`gemini` provider) is strictly better:

- First-party API, no ToS risk
- Direct `generateContent` with native tool call translation
- No middleman
- CLIProxyAPI has known issues with Gemini native tool calls ([issue #931](https://github.com/router-for-me/CLIProxyAPI/issues/931))
- CLIProxyAPI adds latency and a failure point for zero benefit

### Verdict

Go **fully native** for Gemini. API key provider for paid/production, OAuth provider only for free-tier experimentation with awareness of policy risk. **Never proxy Gemini.**

---

## 4. End-State Recommendation

### Go hybrid: native-first, proxy as fallback for ChatGPT only

```
┌─────────────────────────────────────────────────┐
│ Primary: openai-codex (native OAuth)            │
│   ├── gpt-5.4 / gpt-5.4-mini / gpt-5.3-codex  │
│   ├── image gen (native)                        │
│   └── Sub #1 via hermes auth pool               │
│                                                 │
│ Fallback: custom provider → CLIProxyAPI         │
│   ├── Sub #2 via proxy multi-account            │
│   ├── /v1/chat/completions (not codex_responses)│
│   └── gpt-5.5 if your tier supports it          │
│                                                 │
│ Gemini: gemini (API key) — native, never proxy  │
│ Embeddings: direct Gemini key (unchanged)       │
└─────────────────────────────────────────────────┘
```

### Why not fully native yet

- [Bug #24159](https://github.com/NousResearch/hermes-agent/issues/24159) means native credential pool rotation is broken for Codex quota soft-failures. Two native OAuth entries won't auto-rotate reliably.
- `gpt-5.5` may not work on the Codex backend ([bug #21444](https://github.com/NousResearch/hermes-agent/issues/21444)) — depends on your ChatGPT tier.

### Why not fully proxy

- Extra failure point (Go binary, config drift, auth token staleness)
- Image gen support is newer/shakier through CLIProxyAPI
- Native is cleaner for image gen and tool calls
- You lose Hermes's built-in pool observability (`hermes auth list`, `/gquota`)

### Concrete config

```yaml
model:
  provider: openai-codex
  default: gpt-5.4

fallback_model:
  provider: custom:chatgpt-proxy
  model: gpt-5.4

custom_providers:
  - name: chatgpt-proxy
    base_url: http://127.0.0.1:8317/v1
    api_mode: chat_completions

credential_pool_strategies:
  openai-codex: fill_first
```

Then:

```bash
hermes auth add openai-codex --type oauth   # Sub #1 (native)
# CLIProxyAPI config holds Sub #2 separately
```

### When to re-evaluate

Once [issue #24159](https://github.com/NousResearch/hermes-agent/issues/24159) is fixed (pool rotation on `response.status = "failed"`), switch fully native — both subs as pool entries, drop the proxy entirely. That's the clean end-state.

---

## Resolved Open Questions

### Does native `openai-codex` reach the full GPT-5.x chat models, or only Codex-family models?

It reaches **both** chat and Codex models — `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini` are all in `DEFAULT_CODEX_MODELS` (`hermes_cli/codex_models.py`) and are fetched from `/codex/models`. However, the Codex backend is **stricter** — `gpt-5.5` may be rejected depending on your ChatGPT subscription tier ([bug #21444](https://github.com/NousResearch/hermes-agent/issues/21444)). Chat models like `gpt-5.4` work. Models like `gpt-5.3-codex-spark` are Codex-exclusive (not in the public API).

### Does Hermes's native credential pool support multiple OAuth accounts of the same provider with automatic rotation?

**Yes, architecturally** — `hermes auth add openai-codex --type oauth` can be run twice for two different ChatGPT accounts, and the pool supports `fill_first`/`round_robin` strategies. **But in practice**, [bug #24159](https://github.com/NousResearch/hermes-agent/issues/24159) means automatic rotation on Codex quota soft-failures (the most common depletion mode) doesn't fire. Only HTTP 429/402 exceptions trigger rotation. So for now, two native OAuth entries won't auto-failover reliably. Once that bug is fixed, the answer becomes a clean yes.

---

## Key GitHub Issues to Watch

- [#5718](https://github.com/NousResearch/hermes-agent/issues/5718) — `openai-codex` should allow configurable `api_mode` (chat_completions vs codex_responses)
- [#10473](https://github.com/NousResearch/hermes-agent/issues/10473) — Custom GPT-5 endpoint forced to `codex_responses`, ignoring explicit `chat_completions`
- [#21444](https://github.com/NousResearch/hermes-agent/issues/21444) — `gpt-5.5` hangs or is rejected on Codex backend
- [#24159](https://github.com/NousResearch/hermes-agent/issues/24159) — Codex Responses API soft failures bypass credential pool rotation (critical for dual-sub failover)
- [#16678](https://github.com/NousResearch/hermes-agent/issues/16678) — Credential pool not loaded when `/model` switches provider
- [#30720](https://github.com/NousResearch/hermes-agent/issues/30720) — `google-gemini-cli` OAuth silently unusable as fallback
