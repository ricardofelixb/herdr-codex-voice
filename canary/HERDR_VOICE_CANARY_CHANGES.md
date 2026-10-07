# Community canary modification notice

This source tree is modified from Herdr v0.9.3, commit
`7b116c05bfda646af39d2524c54e70c751f57ee8`, for the Herdr Codex Voice plugin.
It is an independent community canary, not an official Herdr release.

Changes dated 2026-10-06 add optional JSON endpoint origin metadata stamped by
the SSH bridge, process-local per-terminal input attribution, the
`pane.last_input` API and CLI command, and the `pane_last_input` capability in
raw ping and CLI status. API input invalidates human attribution. The frozen
endpoint generation 1 codecs and protocol version 22 are unchanged.

The bridge continues to support empty-stdin server preparation and preserves
stdin buffering. Focused tests and draft API documentation accompany the change.
Input origin is local-user metadata, not an authentication boundary. It does not
survive server restarts or handoff and does not identify arbitrary SSH processes.

Herdr is licensed under Apache-2.0; its original LICENSE and source notices
remain in this tree. Vendored components keep their own licenses. These
community changes are provided under Apache-2.0. No upstream endorsement is
claimed, and this package does not grant rights to upstream trademarks.

Modified or added files:

- `docs/next/api/herdr-api.schema.json`
- `docs/next/website/src/content/docs/socket-api.mdx`
- `src/api/schema.rs`
- `src/api/schema/panes.rs`
- `src/api/schema/response.rs`
- `src/api/schema/server.rs`
- `src/api/schema/tests.rs`
- `src/api/server.rs`
- `src/app/api.rs`
- `src/cli/pane.rs`
- `src/cli/status.rs`
- `src/client/handshake.rs`
- `src/protocol/endpoint.rs`
- `src/remote/host.rs`
- `src/server/client_transport.rs`
- `src/server/clients.rs`
- `src/server/headless.rs`
- `src/server/headless/client_views.rs`
- `src/server/headless/input_origin.rs`
- `src/server/headless/tests/input_origin.rs`
- `src/server/headless/tests/mod.rs`
- `src/server/headless/tests/surface_interest.rs`
- `src/update.rs`
- `HERDR_VOICE_CANARY_CHANGES.md`
