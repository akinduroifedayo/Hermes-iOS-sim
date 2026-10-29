# Hermes-iOS-sim operations

Hermes-iOS-sim is an experimental community plugin. Plugin and data identifiers remain `hermes-ios`. See the [README](../README.md) for installation instructions, [release status](../plugin/RELEASE-STATUS.md) for compatibility limits, and [CONTRIBUTING.md](../CONTRIBUTING.md) for the development environment.

Original project code is MIT-licensed under [LICENSE](../LICENSE), also included as `LICENSE.md` at the packaged-plugin root. The MIT grant excludes third-party materials. `serve-sim` remains Apache-2.0, and all third-party licenses and notices remain theirs. This is component-wise licensing, not whole-project dual licensing. [Third-party notices](../plugin/THIRD-PARTY-NOTICES.md) records the dependency licenses and incomplete native redistribution review. Setup downloads dependencies separately; the plugin archive does not vendor them.

## Fresh-user prerequisites

Use a trusted local Mac with Hermes Desktop, selected Xcode, an available iOS Simulator runtime/device, Node **>=20** and npm. Complete Xcode's license and first-launch tasks in Xcode itself. Missing Apple tools/runtimes are user prerequisites, not permission for this plugin to install them. The app worktree must be explicitly authorized.

```sh
xcode-select -p
xcodebuild -checkFirstLaunchStatus
xcrun simctl list devices available --json
node --version
npm --version
```

Dependency provisioning requires direct access to `registry.npmjs.org`. The npm subprocess uses an isolated configuration with an explicit registry and does not inherit proxy variables or your npm settings. The separate Python archive-verification download can use environment or system proxies; locked integrity is still checked. Proxy-only networks and custom registry mirrors are not qualified. Do not bypass provisioning integrity or approval checks to work around this limit.

A nonzero first-launch status means finish setup in Xcode before building. Do not infer license state from an empty diagnostic.

## Normal install, pinned update and enable

Run in the intended Hermes profile. If using a separate trial profile/home, select it explicitly for **both CLI and Desktop**; a different CLI home does not move the already-running Desktop. Do not copy credentials, chats or Browser storage into a trial home.

```sh
hermes plugins install --help
hermes plugins doctor --help
hermes plugins validate --help
hermes plugins enable --help
hermes plugins remove --help

# Trusted local Git checkout, after committing the candidate:
hermes plugins install 'file:///absolute/path/to/hermes-ios#plugin' --ref <full-40-character-commit> --no-enable
hermes plugins doctor hermes-ios --ci
hermes plugins enable hermes-ios
```

Install and replacement flags differ across host versions; inspect `install --help` before adding optional flags. The Hermes launcher may prepare its own managed runtime. `serve-sim` provisioning is a separate, approval-gated `ios_setup` operation after loading. No scan override, forged approval, or `--yes-deps` is needed. The normal install clones Git; raw extracted folders/tar archives are packaging/inspection artifacts, not install identifiers on this host.

Enable the host-managed Desktop half in **Hermes Desktop → Plugins**. Backend enable and Desktop opt-in are separate. Start a fresh Desktop chat if its tool list was captured before activation, and load `hermes-ios:ios-development`. Verify the actual loaded backend, skill and managed Desktop copy, not only the checkout bundle. No manual second-root copy is required.

Pinned plugins do not float under `hermes plugins update`. To update, stop owned work, disable the plugin, preserve a rollback revision and use the same source:

```sh
hermes plugins disable hermes-ios
hermes plugins install 'file:///absolute/path/to/hermes-ios#plugin' --force --ref <new-full-40-character-commit> --no-enable
hermes plugins doctor hermes-ios --ci
hermes plugins enable hermes-ios
```

Re-enable the managed Desktop half and read back the installed revision/loaded bytes before claiming update success. To roll back code, repeat with the previous exact commit; do not assume changing code rolls back retained plugin data. Same-source forced reinstall preserves user/untracked files under the host's install policy. If replacing a differently sourced package, inspect the host's conflict disclosure instead of silently forcing a source migration.

### Same-ID standalone Desktop conflict

Inspect the intended home's `desktop-plugins/hermes-ios/plugin.js` and `.hermes-package.json`. A marker naming package `hermes-ios` and source `<home>/plugins/hermes-ios/desktop` identifies the unified managed half. Markerless or differently marked same-ID code is a conflict—even if its bytes happen to match.

Disable the old Desktop half before activation. With explicit owner authorization, move its **whole directory** to a named backup outside the entire `desktop-plugins` discovery tree. Preserve its files and hash inventory; do not delete apps, app data or historical evidence. Use a non-symlinked destination outside discovery, and never load both copies. For example, after verifying the exact path and creating an unused backup parent:

```sh
mv '<selected-home>/desktop-plugins/hermes-ios' '<backup-outside-discovery>/hermes-ios-legacy'
```

The qualification helper's `preserveLegacy`/`rollbackLegacy` functions enforce changed-byte, containment and occupied-destination refusals. They are not an automatic daily migration. Rollback first disables/removes the candidate through the normal manager, verifies its managed slot is absent, then restores the preserved whole directory. Never overwrite the candidate during rollback.

## Optional controls and upgrade behavior

The following describes the implemented controls contract, not completed live acceptance. An installed-app check observed the palette command, open, close with ⌘W, no reopen from chat status, and explicit reopen. The receipts do not establish which Desktop bundle was loaded during that check. Repeat-command fronting, reload and restart persistence, target discovery, prerequisite handling, Cancel, and Stop within the controls remain unverified.

Enable the managed Desktop half, then use **⌘K → Open Hermes-iOS-sim controls**. This creates a native closeable **Hermes-iOS-sim** tab in the main workspace, not an always-on sidebar. Repeating the command fronts the same tab. Its Close control (or ⌘W while it is active) removes the host-owned registration without disabling the plugin; the command stays available. Dismissal survives reload/remount and app restart because registration never recreates a tab automatically. Reload also closes an open controls tab; reopen explicitly. Opening, dismissing or unloading controls sends no native runtime action and leaves Browser/other panes alone.

An upgrade stops registering the old `hermes-ios:native-controls` right pane. A remembered old layout position is left to the host; no global layout reset, sidebar toggle or storage edit is performed. The new host-owned id is `plugin-workspace:hermes-ios:controls`, so explicit open cannot revive the old sidebar placement. If the old single-pane Close already disabled the plugin, re-enable the managed Desktop half first. Older hosts without public `host.openWorkspace` show an update notice; native tools remain usable.

SDK contract: [official Desktop Plugin SDK](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk). `host.openWorkspace` opens/re-fronts a stable tab, Close tears down its registration and calls `onClose`, and its returned disposer closes it on plugin unload. The permanent `PANES_AREA` contract instead disables a single-pane plugin on Close, so it is deliberately not used for these on-demand controls.

## Discover, setup and run

In the originating Desktop chat, use `ios_iteration(action="discover", worktree=...)` and `ios_iteration(action="preflight", worktree=...)`. Resolve the canonical authorized worktree, project/workspace, scheme, configuration, available runtime and exact UDID. Ambiguity requires a selection, not a guessed device.

If stock is missing, request `ios_setup(action="provision")` through normal host approval, then recheck preflight/status. Decline or unattended block means no provisioning. Setup uses exactly serve-sim **0.1.49-beta.139.1** and its checked-in npm lock in official profile-scoped plugin data; no global install. It downloads JavaScript, native `.node`, camera and HID/render payloads. Stock exposes **unauthenticated local simulator stream/HID/start/shutdown routes**. The plugin configuration requires a loopback Host and rejects foreign-Host HTTP and WebSocket requests. Shell execution is disabled: the plugin never passes `--allow-exec`, and the execution token is not in the preview HTML. Loopback `/exec-ws` remains available for non-shell controls but rejects shell command frames. Origin checks do not constrain non-browser local clients. This is trusted-local control, not a sandbox; loopback is not authorization. Registry byte integrity and separately verified Sigstore provenance establish different facts; neither proves runtime safety. The runtime reports `provenance=not_verified_by_runtime`, not a new claim that independent provenance verification failed.

### Ordinary native tooling (supported)

Xcode/simctl or available native tooling is a legitimate build/install/launch path. Respect its normal approval policy and read its actual output. Set the following variables from the selected target and actual build products; these are placeholders, not a fixture-only default:

```sh
# PROJECT is the exact in-worktree .xcodeproj; use -workspace for .xcworkspace.
# SCHEME, CONFIGURATION, UDID and BUILD_DIR are explicitly selected values.
xcodebuild -project "$PROJECT" -scheme "$SCHEME" -configuration "$CONFIGURATION" \
  -destination "platform=iOS Simulator,id=$UDID" -derivedDataPath "$BUILD_DIR" build
# Only if the selected device was Shutdown:
xcrun simctl boot "$UDID"
xcrun simctl bootstatus "$UDID" -b
# APP is the actual built .app path; BUNDLE_ID comes from that app's Info.plist.
xcrun simctl install "$UDID" "$APP"
xcrun simctl launch "$UDID" "$BUNDLE_ID"
```

Do not install a stale product after a failed compile. Do not erase/uninstall/reset to rebuild. Read the actual product path and bundle identifier from the successful build; do not invent them. Keep the exact runtime/UDID and retain app data.

After successful external launch on the booted device, call `ios_iteration(action="preview")` with `worktree`, `project`, `scheme`, `configuration`, `runtime`, `udid`. Its approval disclosure is bound to the exact target/action; it verifies the selected available booted runtime and starts/reuses the **same shared owned stock server**. It performs no build/install/launch/boot and issues no coordinator native receipt. Do not start another serve-sim process beside it.

### Optional coordinator

`ios_iteration(action="run")` with those same target fields coordinates build/install/launch and stock start/reuse. Its normal public approval is bound to request metadata and the trusted-build fingerprint. Changes to fingerprinted inputs require fresh permission. Dependency directories, external inputs, and some script types are not covered by that fingerprint. Approval remains trust in the whole checkout and its dependencies, not a content sandbox. No model boolean, command array, profile claim or renderer handshake grants execution. Keep this path when its native receipts, locking and cancellation/drain are useful; it is not mandatory for ordinary native builds.

## Browser feedback, reuse and resume

- Successful explicit run/preview returns exact `preview_url`/`preview_action`. Execute its public `desktop_preview(action="open", url=...)` in the same originating agent turn. Never return only a URL as completion. Do not bypass session/window guards or invoke private core handlers.
- Open success is an **open request**, not a visible-pixel receipt. Observe the actual app in Hermes Browser. Build/install/launch, server readiness, open request and pixels remain separate facts.
- The person annotates in Hermes Browser, reviews the native image/comment and presses ordinary **Send**. The agent calls `vision_analyze` on that exact submitted image **before editing**, loads the full image first, and describes what it actually saw. Missing/unreadable images stop feedback-driven editing, not permission to infer pixels.
- Edit the authorized worktree and rebuild/install/launch the same target through either native path. The same target and Browser tab can be reused, but the server may restart during rebuild. Unchanged PID/generation and uninterrupted streaming are not guaranteed. An external rebuild needs no extra coordinator run. Observe updated pixels in that same preview before claiming visual success.
- Closing Browser does not stop the server or trigger reopening. `status` is observational and never starts/reopens anything. An explicit `preview` resumes/reveals the booted app after a closed preview or Hermes restart, reusing the owned server where it remains valid. If device/app was stopped, boot/launch it through the selected native tooling first. Do not infer ownership from a port or PID alone.
- Before the first server launch, fresh port selection tries at most three total candidates. Only an address-in-use validation failure permits another allocation. Allocation errors and other validation errors fail immediately. Both loopback and wildcard addresses are checked. Three is a conservative work bound, not a measured optimum. A recorded URL always keeps its port and fails closed on conflict. These checks do not reserve the port; a competitor can still win after validation, so owned-process readiness and cleanup remain required. There is no startup retry after launch.
- The small SDK panel shares the registered backend for target/preflight/status/cancel/stop. It neither submits chat nor creates a second preview.

## Stop and remove

`ios_iteration(action="cancel")` drains active coordinator work. `ios_iteration(action="stop")` uses the same exact target and retires verified owned server/native resources. Externally launched apps/devices are not adopted as plugin-owned native work and remain running. Stop does not erase devices, uninstall apps or reset app data. PID reuse, ambiguous ownership, failed persistence or cancellation not drained remains an unresolved obligation; do not broadly kill processes or take over.

### Unload, reload, Stop, disable and removal

Unloading the backend registration is not a stop. Hermes unloads it for ordinary rediscovery or a targeted reload as well as for a disable, and the unload callback cannot tell these apart. So the plugin applies this policy:

- **Unload (reload/rediscovery):** the registration detaches and the implementation attempts to preserve supervision while the owning host and enabled profile remain valid. A newer registration can adopt an eligible preview. This does not guarantee an unchanged server process across every rebuild or reload. Already-admitted native work is not cancelled merely by unload; the unloaded registration admits no new work.
- **Stop/Cancel:** explicit `stop` stops the exact generation immediately, attached or detached. Explicit `cancel`/`stop` issued through a newer registration in the same Hermes process also reaches native work admitted before the reload, after the usual conversation-claim check.
- **Disable/removal:** while a generation is detached, the owning Hermes process itself re-reads its profile's `plugins.enabled`/`plugins.disabled` (user `config.yaml` through Hermes's own defaults merge and normalization, plus the managed overlay, never a last-known-good fallback) every ~1.5 s, from its own live managed-scope directory, on one background thread that survives the plugin's unload. It publishes each reading to a small file beside the plugin's lifecycle store. The watchdog stops the generation (`lifecycle_disabled`) only when that file shows a listed disable, or an enabled list without the plugin (as left by supported removal), on two consecutive reads, for this exact release of this exact lease, and was refreshed within the last 10 s. The lease records which one it saw (`disabled` or `removed`).
- **Unknown config:** an unreadable, missing or malformed `config.yaml`, one Hermes's own config processing rejects or falls back from (for example `agent: 7`, where Hermes keeps serving the last good config), one whose parse warns, a missing `plugins.enabled` list, a non-string list entry, any `${VAR}` reference in either selection list of the user or managed layer, or a managed-scope directory that moved during one read is recorded on the lease and does not stop the preview. Hermes resolves a reference for its own plugin loading in the caller's profile scope, which this reader cannot reproduce, so only literal names count; a literal disable still stops it. A missing, stale or superseded reading, or a parse child that fails or runs past 5 s, is unknown too. So is any Hermes process whose config is frozen: a managed worker serves the session config it was started with, never the profile's files, so a disable on disk never reaches its plugin loading. The plugin checks this at registration, at every unload and on every read, through the compatibility seam below; if it cannot tell, the selection is unknown. Stop it explicitly, or fix the config.
- **Host compatibility seam:** whether this Hermes process is a frozen worker has no documented plugin surface. Hermes's config readers decide it with `agent.safe_worker_policy.worker_config_snapshot()`, which Hermes calls private. The owner approved this one read-only call as an exception to the no-private-API rule; nothing else from that module is used, and `plugin/host_compat.py` is the only file that names it (`tests/agent/test_host_surface.py` enforces this). The plugin only checks the type of what it returns and drops it at once: `None` means the profile's files decide, a `dict` means frozen. A missing module or function, a non-callable, an exception or any other return makes automatic cleanup on disable/removal unavailable, with a fixed reason (`compat_missing`, `compat_not_callable`, `compat_raised`, `compat_invalid_return`). The snapshot and exception text are never logged, stored or shown. Running previews then stay supervised until Stop or Hermes exits, and `ios_iteration(action="status")` returns `runtime.lifecycle_cleanup` with a `warning`: "Automatic cleanup on plugin disable/removal is unavailable: host compatibility check failed (<reason>). Running previews stay supervised; use Stop to end them." A frozen worker gets an informational `note` there instead, and an ordinary host shows `automatic: true`. The notice is per detached lease, not just the live seam: a lease bound while the seam was failing stays `automatic: false` with a Stop-naming `warning` even after the seam recovers, since that lease itself never drains until the seam and its own binding both read `profile` again. The same holds when the lease's latest reading shows the Hermes process supervising it could not read the selection at all (`lifecycle_host_unavailable`, a failed or timed-out parse child, or a failed, missing or stale observer, kept in `last_handoff` once a newer registration adopts it): `automatic: false`, that `reason`, and a `warning` naming Stop. With no reading recorded yet, `status` reports from config authority alone; it never runs a read of its own. Limit: the check sees shape, not meaning. A future Hermes that keeps the same function and return types but changes what they mean (for example returns `None` while the process is in fact frozen) cannot be detected at runtime; a disable on disk could then stop a preview the worker still has enabled. Re-qualify this seam against each Hermes upgrade.
- **Host exit:** the death of the owning Hermes process stops the generation (`host_exit`).
- **Fallback:** only a registration that could not bind its profile at all keeps the bounded five-second handoff (`handoff_expired`). A registration whose binding is present but unusable detaches with its selection unknown; only Stop or host exit end it.
- **Another Hermes process of the same profile** (for example the gateway and the dashboard): it uses a preview the first one started, in the same conversation, as a client. Its preview or run returns the same URL, PID and generation, with `server.owner_use` `foreign_supervised` (the owner is attached) or `foreign_detached` (the owner is reloading the plugin). It needs the same profile, plugin, state store and plugin build, and the usual conversation claim and approval. It writes nothing to the lease and never adopts the preview: the starting process keeps supervising it, and its own unload, disable, exit or Stop can still end a preview the other process just returned. A preview started by a plugin build that records no build identity is never used this way; Stop it and preview again. Stop from either process works and claims the preview before its first signal, so a concurrent unload, adoption or exit cannot leave it half stopped. When the owning process exits while the other holds the device (a long build), the preview stays supervised and `draining` until the device is free, then stops; it is never left unsupervised or replaced meanwhile. Cancel and Stop never reach native work running in the other process. A Stop holds the device's operation lock until it holds the device, so a run the other process starts meanwhile is refused with `operation_lock_contended` or waits for the Stop to finish.

| Code | Meaning | What to do |
|---|---|---|
| `stock_foreign_owner_draining` | The other process's preview is being stopped: it exited, the plugin was disabled there, or a Stop runs | Retry shortly; a new preview starts after cleanup |
| `stock_foreign_owner_releasing` | The other process is reloading the plugin with a deadline, or reports a disable not yet confirmed | Retry; if it re-attaches, the preview is reused |
| `stock_foreign_code_unverified` | The preview was started by an older plugin build that records no build identity | Stop, then preview again |
| `stock_foreign_code_mismatch` | The other process runs a different plugin build | Reload the other process, or Stop and preview again |
| `stock_foreign_registration_mismatch` | Different plugin, profile, state store or store lifetime | Not usable from here |
| `stock_foreign_target_mismatch` | The preview serves another project, scheme, configuration, runtime or worktree on this simulator | Stop, then preview this target |
| `stock_foreign_process_mismatch` | The preview server is no longer the attested child of the process that owns it | Stop to clear it |
| `stock_foreign_not_ready` | The server exists but is not ready or not supervised | Retry or Stop |
| `stock_foreign_supervision_lost` | The other process's supervisor is gone | Retry |
| `stock_foreign_generation_changed` | The preview changed while it was being checked | Retry |
| `stock_handoff_draining` | This process's own preview is being stopped | Retry shortly |
| `operation_running_in_other_host` | Cancel/Stop: this device's native work runs in another Hermes process | Cancel or Stop from the conversation turn running it, or wait |
| `operation_lock_unavailable` | Cancel/Stop could not open this device's operation lock | Check the plugin's state directory permissions, then retry |

Every unload and adoption advances a lease revision so stale selection observations cannot stop a newer lease. Profile-selection parsing runs in an isolated child and uses safe ruamel YAML parsing with bounded input and output. Unknown or incompatible host state is not interpreted as removal. This is host-version-sensitive behavior, not a public compatibility guarantee; see release status.

Before removal, stop owned work and verify its result. Disable the Desktop half and backend, then remove code through the manager:

```sh
hermes plugins disable hermes-ios
hermes plugins remove hermes-ios
hermes plugins list
```

Verify the exact selected home's backend directory and managed Desktop copy are absent/unloaded. Host versions differ in managed-copy cleanup; if a same-ID Desktop directory remains, inspect its marker/identity and explicitly preserve it outside discovery rather than delete unrelated code. Remove stale enabled/disabled config entries through `hermes config set` only if the host leaves them; preserve unrelated entries. Official plugin data, simulator devices, app containers and historical evidence are retained. Data deletion is a separate owner decision, not uninstall.

## Local checks and evidence boundaries

```sh
bun install --frozen-lockfile --ignore-scripts
bun run verify
bun run package:release -- --output dist/my-fresh-candidate
```

Use a fresh output directory. Packaging refuses to delete an existing caller destination. `manifest.json` inventories the unified package; npm caches are not vendored. `plugin/desktop/plugin.js` is generated from the SDK/React-externalized Desktop bundle. Python contracts require Python 3.11+ and FastAPI; follow the portable virtual-environment setup in [CONTRIBUTING.md](../CONTRIBUTING.md). Harmless process fixtures test lifecycle contracts, not stock pixels or runtime safety.

Earlier validation passed with a warning for the private worker snapshot import. The current CLI installation check used Hermes `v0.22.5+1859.gfa59cb9`, not the stock v0.22.5 tag. The exact host build for earlier native acceptance is not established by the retained report. Declaration-only registration and real runtime registration are handled separately. Doctor and validator success do not establish catalog admission or installed visual behavior. No minimum compatible host release is established. See [release status](../plugin/RELEASE-STATUS.md).

Full admission can prepare the host's Python environment. Run `hermes plugins validate <directory> --json --install-deps` only in a disposable stock host installation with its own account home, Hermes home, runtime store, XDG directories, and temporary directory. A different `HERMES_HOME` alone does not isolate a launcher tied to an active source checkout. Preserve stdout, stderr, exit status, the exact host revision, and dependency provenance.

For source-bound packaging, commit first and build from the clean commit. Compare the archive's complete member set and hashes against the committed `plugin/` tree. The build output must equal the committed Desktop bundle. The external manifest and archive are generated packaging outputs. Reject unexpected members even when the package's own manifest includes them. Keep private screenshots and acceptance receipts outside the package.

Synthetic helpers (`scripts/install-local.ts`, `scripts/install_isolated.py`, `scripts/uninstall-local.ts`) retain isolated-home restrictions intentionally: they are test harnesses, not the normal installation interface. Their marker/readback proves candidate bytes only, not loaded Desktop or live behavior. Pin a committed transport and isolated launcher/environment when using them; never clone an older commit then overwrite files and call that the installed candidate.

`bun run qualify` retains the historical coordinator-oriented receipt matrix and fails nonzero/unproven without `HERMES_IOS_QUALIFICATION_HOME`, `HERMES_IOS_RELEASE_MANIFEST`, `HERMES_IOS_INSTALLED_RECEIPTS`, and `HERMES_IOS_EVIDENCE_ROOT`. It checks artifact-bound receipts; it does not perform or prove the live loop itself. That exhaustive historical matrix is optional evidence tooling, not a wrapper requirement for the supported ordinary-native workflow.

Fresh-user delivery evidence must record the actual installed revision/loaded paths, permitted setup, real build/install/launch, Browser annotation/ordinary Send, submitted image inspection, source edit/rebuild, updated same-preview pixels and restart/resume. Fixture screenshots, synthetic receipts and successful direct commands do not by themselves prove those observations. Historical evidence remains unchanged and does not qualify new bytes. No publication or blanket runtime-safety claim follows from local tests or provenance verification.
