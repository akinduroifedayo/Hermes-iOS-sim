# Release status

Hermes-iOS-sim is a community plugin. It is not official, endorsed by Nous Research, or catalog-admitted. Its plugin, repository, and retained-data identifiers are `hermes-ios`.

## Localhost-only operation

The locked preview dependency is `serve-sim@0.1.49-beta.139.1`. Its upstream Host fix requires a loopback Host in the plugin configuration. Shell execution is disabled because the plugin never passes `--allow-exec`, and the execution token is not in the preview HTML. The checked-in-tree regression rejects foreign-Host preview, execution POST, and WebSocket upgrade requests while preserving loopback preview access. It also verifies disabled loopback shell execution. This replaces the deferred 0.1.48 Host issue; it is not a security clearance. Use only on a trusted local Mac; never expose, forward, or tunnel the preview port. Localhost is not authorization against other local processes. Native preview on this beta still needs macOS qualification.

## Observed behavior

The development baseline completed a real edit, build, install, launch, preview, and button-interaction loop on a local iOS Simulator. App-data retention was checked with an unchanged sentinel. The preview-readiness repair has regression tests for valid supervision handoff transitions.

A separate isolated Desktop exercise checked installation, docs-only replacement, disable, enable, and removal. It did not qualify a changed-runtime or data-schema migration. Those observations came from the development environment, not an independent second Mac. Private screenshots and machine-specific receipts are deliberately not included in this source distribution.

The public-preparation audit records the current source, dependency, test, and packaging results separately. An earlier native run does not verify a later runtime change. See `docs/security-audit.md` in the source repository for the current audit scope and unresolved findings.

## Compatibility

- A trusted local macOS host, Xcode, iOS Simulator, Node.js 20+, and npm are required.
- Development used a Hermes v0.22.5-derived build. This does not establish support for the stock v0.22.5 tag.
- No minimum Hermes, macOS, Xcode, or Bun version is established. Other machines and host versions need their own qualification.
- The backend depends on host plugin registration, state, hooks, middleware, and unload behavior. Optional Desktop controls require `host.openWorkspace`.
- `host_compat.py` uses the read-only private host function `agent.safe_worker_policy.worker_config_snapshot()`. This is an intentional compatibility dependency, not a stable public API. Host changes can break automatic disable/removal cleanup.
- Lifecycle selection depends on the host's ruamel parser and managed-directory layout. Missing or incompatible host state fails closed and reports a cleanup warning. Use the originating conversation's explicit Stop action when automatic cleanup is unavailable.

## Known limits

- The full annotation, ordinary Send, image inspection, edit, rebuild, and same-preview interaction flow remains unverified as one continuous workflow.
- The preview server may restart during a rebuild. Reuse of a URL does not prove uninterrupted streaming or an unchanged server generation.
- An intermittent `lsof` startup timeout was observed on the unchanged development baseline. The readiness handoff fix does not resolve that timeout.
- Stop can terminate a coordinator-owned app as well as its preview service. Stop does not uninstall apps, erase simulators, or reset app data.
- Cancel and Stop cannot cancel native work running in another Hermes process. Use its originating process or wait for it to finish.
- Another-Mac installation, declined-prerequisite setup, broader version compatibility, every optional control, and less-common device-switch/restart scenarios are not fully qualified.
- A successful tool result does not prove that the Browser pane displayed the app or that a tap reached it. Verify pixels and behavior separately.

## Dependency and security boundaries

Approved setup downloads exactly `serve-sim` 0.1.49-beta.139.1 and its checked-in locked tree. The retained 0.1.48 fixtures are historical observations, not qualification of this beta. The npm subprocess requires direct access to `registry.npmjs.org` and does not inherit custom registry or proxy settings. The separate Python archive download can use environment or system proxies and verifies locked integrity.

Local simulator stream, input, start, and shutdown routes remain unauthenticated. The plugin requires a loopback Host and does not configure remote authentication. Shell execution is disabled without `--allow-exec`; the plugin never enables it, and no execution token is disclosed by the preview HTML. The loopback `/exec-ws` connection still supports non-shell preview controls; command frames are rejected. Origin checks do not constrain non-browser local clients. Loopback is not authorization. Do not expose or tunnel the preview service, and do not treat this plugin as a security boundary against other local processes or malicious web content.

The archive contains no downloaded npm payloads, native dependency binaries, Apple components, screenshots, or private acceptance receipts. Registry integrity does not prove safe execution or reproducible native builds. [Third-party notices](THIRD-PARTY-NOTICES.md) describe the separate review needed before redistributing native dependency binaries.
