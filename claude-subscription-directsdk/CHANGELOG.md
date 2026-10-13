# Changelog

## [0.3.1] - Unreleased

### Fixed

- A rebuilt warm native now reads the cache of the native it replaces. A live native keeps the context it built for its first request (the date line, the account reminder) where it first put it, and holds MCP tool results as one text block; a fresh native replaying the same history put that context after the newest frame and sent string results, so the prefixes split at the launch frame and every rebuild re-wrote the history. The warm relay now moves every `role: system` message to one trailing system message and the launch reminder behind the newest frame, and replays send string tool results as one text block. Paid A/B (Sonnet 5.5, synthetic): forced rebuild cache read 10,126 / write 4,174 before, 13,984 / 0 after. Cost: native's ~500-token environment and date context is now sent uncached on every warm call.
- Accept Hermes Relay's host-local `traceparent` request metadata without forwarding it to the native CLI or upstream, preventing full agent and fallback requests from failing on `extra_headers`.
- Continue rejecting arbitrary headers, credential overrides, and malformed header containers at the transport boundary.
