# Security

## Supported scope

This plugin is intended for a trusted local macOS host. Development used a Hermes v0.22.5-derived build, not a qualified stock v0.22.5 release. No minimum compatible host release is established. See [release status](plugin/RELEASE-STATUS.md). Tests and scans reduce risk; they do not establish that the plugin or downloaded binaries are safe against every threat.

## Accepted risk for the release

This localhost-only release has a known unresolved Host-validation issue in the pinned preview dependency. The maintainer accepted deferring remediation. Never expose, forward, or tunnel its preview port. This restriction is not a guarantee against other local processes or malicious browser content. See the [security audit](docs/security-audit.md) for verified scope and release conditions. Token and Origin checks alone do not establish protection against DNS rebinding.

## Trust boundaries

- Xcode builds can execute scripts and dependencies from the selected app checkout. Read access to a repository is not permission to execute it.
- The build-input fingerprint covers selected project inputs, not every dependency or external executable. Discovery and native children inherit the host environment; do not place unrelated credentials in that environment. Use an absolute, trusted PATH. Git fingerprinting honors repository configuration. These mechanisms are not a sandbox for untrusted projects.
- Dependency setup downloads executable JavaScript and native binaries. It uses a pinned lock, integrity checks and normal host approval. Matching registry bytes is not proof of provenance, reproducible native builds, or safe execution.
- Stock `serve-sim` exposes unauthenticated simulator streaming, input, start and shutdown routes. Shell execution requires a token disclosed by the unauthenticated preview page. Origin checks do not constrain non-browser local clients, and Host validation is missing. Those checks must not be described as protecting all routes.
- Loopback is not authorization. Other local processes can reach a localhost service. Hostile browser content can also attempt localhost requests; exploitability depends on route and browser defenses and is not exhaustively evaluated here. Do not tunnel or expose the preview port. Stop the preview when it is no longer needed.
- Restricting read-only discovery and preflight to the user-authorized worktree is agent policy, not a filesystem sandbox. Those APIs accept paths from their authenticated caller.
- Conversation claims, target validation, approval and process identity checks restrict the plugin's own actions. They do not sandbox the downloaded preview server or application build scripts.
- Unknown ownership or failed cleanup remains unresolved. Do not kill guessed PIDs, erase data, bypass approval, or substitute another profile to clear a refusal.
- App screens and annotations can contain sensitive data. Review content before sharing. Never commit user screenshots, raw session logs, config dumps, bearer material or credentials as test evidence.

## Reporting a vulnerability

Do not put secrets or a working sensitive exploit in a public issue. Use GitHub private vulnerability reporting if the repository exposes that option. Otherwise contact the repository owner through an existing private channel; a reporting mailbox or response SLA is not promised here. You can open a public issue asking for a private contact without including exploit details.

Include the affected commit, Hermes/macOS versions, minimal reproduction, expected and actual behavior, and redacted impact evidence. Do not test against someone else's device, session, or machine. Stop testing if you encounter unrelated private data.

## Before public distribution

Review the current tree and all reachable Git history, not just the latest diff. Historical author metadata can disclose personal emails and hostnames even when code has no secrets. For this preparation, preserve the private development repository and publish only a separately reviewed fresh-history export with deliberate public commit metadata. Do not force-push or rewrite history as an automatic cleanup step.

Run secret scans with redaction. Classify synthetic secret fixtures by exact location and purpose rather than disabling scanning for whole directories. Review dependencies, licenses, generated assets, release package membership and the private host API caveat. Keep Apple runtimes and downloaded native dependencies out of the source/plugin archive unless their redistribution obligations have been resolved.
