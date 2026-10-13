# Changelog

## [0.3.1] - Unreleased

### Added

- Per-profile `claude_config_dir` setting selects the Claude login for every native spawn in a multiplexed gateway (#34).

### Fixed

- Accept Hermes tool names of 51–64 characters through stable 50-character aliases, mapping native tool calls back to the original names and preserving replay.
- A rebuilt warm native now reads the cache of the native it replaces. A live native keeps the context it built for its first request (the date line, the account reminder) where it first put it, and holds MCP tool results as one text block; a fresh native replaying the same history put that context after the newest frame and sent string results, so the prefixes split at the launch frame and every rebuild re-wrote the history. The warm relay now moves every `role: system` message to one trailing system message and the launch reminder behind the newest frame, and replays send string tool results as one text block. Paid A/B (Sonnet 5.5, synthetic): forced rebuild cache read 10,126 / write 4,174 before, 13,984 / 0 after. Cost: native's ~500-token environment and date context is now sent uncached on every warm call.
- Accept Hermes Relay's host-local `traceparent` request metadata without forwarding it to the native CLI or upstream, preventing full agent and fallback requests from failing on `extra_headers`.
- Continue rejecting arbitrary headers, credential overrides, and malformed header containers at the transport boundary.
- Carry the login identity Claude Code's local check reads (`USER`/`LOGNAME`, `USERNAME` on Windows) into the child env, derived from the parent process or the running uid: without it a perfectly logged-in CLI reports `loggedIn: false` instantly, with no stderr and no upstream request — the confirmed mechanism behind gateway-only `ClaudeCodeLoggedOut` failures (#44).
- Capture native stderr (bounded, drained off-thread) and include its first lines in `ClaudeCodeLoggedOut` and native-failure errors, so a gateway-only failure is diagnosable instead of vanishing into `DEVNULL` (#44).
