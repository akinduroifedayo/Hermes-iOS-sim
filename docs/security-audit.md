# Security audit and release decision

## Verdict

**The dependency Host issue is remediated in the selected pin; native preview remains unqualified on this beta.** The plugin now locks exactly `serve-sim@0.1.49-beta.139.1`, including the upstream Host fix. Its configuration requires a loopback Host, never enables `--allow-exec`, and does not expose an execution token in preview HTML. Never expose, forward, or tunnel the preview port. Localhost is not authorization against other local processes.

The behavior regression installs `plugin/dependencies/package.json` and `package-lock.json` with `npm ci --ignore-scripts` in an isolated temporary directory, then mounts the real exported `serve-sim/middleware` in a Node HTTP server on `127.0.0.1`. It mirrors the CLI's preview path, exact-device selection and helper-proxy option, and forwards WebSocket upgrades. The CLI itself starts simulator work before listening, which cannot run on Linux without `simctl`.

On historical 0.1.48, a foreign Host received HTTP 200 with the execution token. A matching foreign Origin and that token reached the missing-command check (400), and `/exec-ws` upgraded (101). On the new pin, foreign preview, POST `/exec`, and `/exec-ws` upgrade requests all return 403. Loopback preview returns 200 without an execution token, POST `/exec` returns 403, and a loopback WebSocket command frame reports `exec disabled` with exit code 126 and empty stdout. The loopback WebSocket remains available for non-shell controls. HTTP probes submit no command; the disabled-command WebSocket probe submits only the harmless `true` command, which the beta rejects before execution.

These results exercise the locked package's actual HTTP and WebSocket code, not a complete DNS-rebinding attack in a particular browser or a live native simulator. Historical 0.1.48 provenance and security fixtures remain labeled historical and are not evidence for the beta. Registry integrity verifies published bytes; this update does not independently verify native source provenance or qualify macOS capture/HID behavior.

## Scope and method

The audit covers the tracked development tree, reachable development Git history, Python runtime and native ownership code, Desktop source and generated bundle, cards, dependency provisioning, packaging, and public documentation. Two independent read-only reviews partitioned runtime/native authority and web/dependency/distribution code. Both used Claude Opus 5.5 in separate contexts. This is independent review, not model-diverse review or a formal security assessment.

The parent checked selected findings with isolated executable probes and ran Gitleaks, Bandit, npm audit, Bun audit, and pip-audit. Scanner output was classified instead of suppressed. Whole-tree history scans used all local reachable refs. Remote-only refs, deleted objects, forks, GitHub caches, and inaccessible external copies are not covered.

The audit is not a certification. Scanner results are time-bound. Native dependency binaries were not reverse-engineered, fuzzed, or proven reproducible. Another-Mac installation and broad browser attack testing remain unqualified.

## Findings and disposition

| Finding | Evidence | Disposition |
| --- | --- | --- |
| Preview dependency accepts foreign Host values | Regression fails on historical 0.1.48 and passes on the checked-in 0.1.49-beta.139.1 tree for preview HTTP, execution POST and WebSocket upgrade | **Fixed in the selected pin.** Loopback Host required in plugin configuration, shell execution disabled, execution token absent from HTML. Native preview still needs macOS qualification |
| Unauthenticated local stream and helper input routes | Historical fixtures record 0.1.48 behavior; the beta adds request guards but local simulator control remains unauthenticated | Remaining local trust boundary. The Host/exec regression does not qualify native simulator control or authorize other local clients |
| Approval paths contain raw newline and bidi characters | Production registration hooks emitted crafted paths unchanged in Run, Preview, and Handoff messages | Fixed by JSON-escaping displayed paths without altering target identity. Regression failed for all three actions before repair and passed afterward |
| Release tar headers disclose local account identity | Actual package archive contained the packager's numeric and named owner/group | Fixed with neutral UID, GID, user, and group headers. Regression failed before repair and passed afterward |
| Personal references in the public tree and history | Tree and all-local-refs inventory found personal authorship, machine metadata, development identifiers, and private evidence | Candidate tree sanitized. Private development history retained. Publish only a newly reviewed fresh-history snapshot, never by changing visibility of this development repository |
| Build fingerprint does not cover all executable inputs | Changing a synthetic Pods script left the production build-input fingerprint unchanged | Disclosures corrected. The fingerprint covers selected inputs, not dependencies or external scripts. Comprehensive build-input authorization remains unimplemented |
| Child commands inherit the host environment | A synthetic environment marker reached a discovery child | Documented trust limit. Build projects must be trusted. Environment isolation and relative-PATH hardening remain open work |
| Git fingerprint commands honor repository configuration | Source review of the Git subprocess path | Open hardening item. External diff/textconv/fsmonitor behavior was not exercised in this audit |
| Dashboard plugin routes rely on host authentication | Plugin code has no independent HTTP guard. Inspected host code applies API token/native authentication and Host validation | Not established as an unauthenticated plugin exploit. Full installed HTTP mount and other host versions remain unverified |
| Proxy-isolation claim was broader than implementation | npm uses a restricted environment; Python urllib uses its default proxy handling | Documentation corrected. Locked integrity is still verified |
| Isolated installation helpers check listed members, not all extra files | Source review | Open low-severity limitation of optional qualification tooling. Release archive verification separately compares the complete member set |
| Direct Python development requirements are pinned, transitive requirements are not | Fresh-environment dependency resolution | Disclosed reproducibility limit. A transitive hash lock remains future work |
| Crash recovery may retain an unresolved attempt that blocks a target | Source-review hypothesis | Not reproduced here. Keep fail-closed behavior. Do not edit state files, reset budgets, or broaden cleanup to force recovery |
| Remembered approval scope depends on the host | Source-review hypothesis about rule-key persistence | Not reproduced. Conversation claims still require host-derived identity. Validate host permission persistence before promising per-session approval caching |

The build-input, environment, and Git items do not turn a trusted local build into a sandbox. Public documentation must not promise that they do.

## Secret and dependency scans

Gitleaks 8.30.1 reported four current-tree matches and four history matches. Each was classified as a synthetic test sentinel used to assert that parser warnings and malformed configuration do not leak secrets. The fixtures remain intact; no scanner allowlist or bypass was added. No real credential was identified by these scans. That does not prove that no undiscovered credential exists.

The dependency checks reported no known vulnerabilities in the locked npm tree, the Bun development tree, or the resolved Python development requirements at audit time. Those historical advisory scans did not detect the 0.1.48 Host issue; they are not a new scan of the beta.

Bandit reported 19 observations. Subprocess imports and argument-vector calls account for most low-severity results. Its wildcard-bind warning points to a temporary port-availability check, not a network listener. Its URL-opening warning points to an npmjs-only, locked-integrity archive download. Neither warning proves safe redirects or trusted native code. The native and web reviews examined the surrounding code separately.

## Privacy changes

The candidate removes private screenshot/evidence directories and historical internal review documents. Real-looking development device identifiers in fixtures are replaced with clearly synthetic identifiers. The historical helper-route fixture documents its identifier normalization and does not claim to be an unmodified receipt.

Personal repository references are removed from installation instructions. Original-code attribution uses the project contributor label at the owner's request. Third-party attribution and license text remain intact. No downloaded native dependency or Apple runtime is included in the plugin archive.

The prepared source export excludes `.git`, caches, environment files, screenshots, and local audit receipts. Development history is not sanitized by this export. A future public repository must begin from the reviewed export with deliberate non-personal author metadata and must be scanned again before publication.

## Verification boundaries

The complete local verification after the approval-display repair passed all ten declared stages. Its retained `verify-02.log` reports 821 Python cases, comprising 758 passes and 63 skips, plus 36 JavaScript passes and six skips. Runtime inputs stayed unchanged during that run; three documentation files changed. Later qualification-helper cleanup is checked separately. Exact source/package verification is a separate required step; use the accompanying artifact receipt rather than attributing older results to later bytes.

The earlier audit delta covered escaped approval-display paths and risk disclosures. This update additionally changes the locked preview dependency and its version checks, with live middleware regressions on Linux. It has not been installed into the working Hermes environment or qualified through a new native-device run. Existing native-app evidence applies to the preceding runtime baseline, not automatically to this candidate.

## Follow-up and release verification

1. Qualify native preview on macOS with the repaired dependency. The locked-tree HTTP and WebSocket regressions now pass on Linux; those checks do not exercise Simulator capture/HID.
2. Review the final source export, package manifest, archive metadata, and complete archive membership by hash.
3. Choose the public repository destination and deliberately set public commit metadata. Preserve the private development history separately.
4. Enable and verify a private vulnerability-reporting channel on that destination. This preparation does not claim that GitHub private reporting is already enabled.
5. Publish an immutable source revision and rerun installation from that actual public transport. Keep the documented compatibility limits visible.

The audit does not establish catalog admission, security certification, or independent-Mac compatibility. The published release notes record the actual source revision, installation verification, and artifact checksums. Private development history is not part of this distribution.
