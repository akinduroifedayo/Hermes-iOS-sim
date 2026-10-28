# Contributing

## Scope and prerequisites

This repository contains a Hermes plugin, its bundled agent skill, a Desktop module, and verification fixtures. It is not a general-purpose iOS build service. Native execution and process-identity tests require macOS. Use Python 3.11 or newer, Bun, and Git. The current package build was checked with Bun 1.4.2; no minimum Bun version is established. Installed-app work additionally requires Hermes Desktop, Node 20+, npm, Xcode, and an iOS Simulator runtime.

Work on a branch. Do not change an active Hermes profile, its permissions, another conversation's simulator, or user app data to make a test pass. Do not commit raw screenshots, session dumps, caches, credentials, or machine-specific evidence. Reviewed crops of test-app pixels may accompany acceptance documentation if they contain no personal information or embedded metadata. Keep uncropped originals outside the repository and record their hashes and crop rectangles. Inspect staged paths before committing.

## Set up a local test environment

From the checkout root:

```sh
python3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
bun install --frozen-lockfile --ignore-scripts
export PYTHON="$(pwd -P)/.venv/bin/python"
mkdir -p .test-tmp
export TMPDIR="$(pwd -P)/.test-tmp"
bun run verify
```

The direct Python dependencies are pinned to versions used in qualification. This is not a transitive Python lockfile. The explicit absolute temporary path avoids macOS `/var` versus `/private/var` spelling differences in path-isolation tests. All these folders are local development artifacts, not package members. Do not point temporary or test homes at an active account's configuration.

`verify` runs typecheck, Desktop build, backend, contracts, Desktop tests, skill-text contracts, fault, privacy, accessibility, and integration suites. Read individual pass/skip/fail counts. A green exit does not mean optional host/stock integration executed, or that the app appeared in Desktop.

## Optional host and stock integration

Some suites skip unless these inputs are explicitly configured:

| Variable | Value |
| --- | --- |
| `HERMES_IOS_HOST_SOURCE` | Absolute path to a trusted Hermes source checkout at the release being qualified |
| `HERMES_IOS_HOST_PYTHON` | Absolute path to that checkout's prepared Python interpreter |
| `HERMES_IOS_STOCK_ROOT` | Absolute path to an independently prepared matching stock serve-sim package tree |
| `HERMES_IOS_TEST_PYTHON` | Optional producer-test interpreter override; otherwise the shared runner honors `PYTHON` |
| `HERMES_IOS_HOST_DEPS` | Optional dependency import roots used by host-specific fixtures; inspect the selected suite before setting |

Inspect the suite's prerequisites before enabling it. Use a disposable host environment and allow the repository isolation helpers to create separate account/config/runtime homes. Importing Hermes executes host initialization. Setting only `HERMES_HOME` is not complete host isolation. Never supply a production Hermes home as a fixture.

The extracted-loader tests use `HERMES_IOS_HOST_SOURCE`; an unset value skips them, while an explicitly invalid checkout fails. They extract selected loader methods and do not establish whole-host compatibility. Release evidence must report skipped tests separately.

## Useful commands

| Command | Purpose |
| --- | --- |
| `bun run verify` | All local stages; optional integration can skip |
| `bun run test:agent` | Canonical backend test command |
| `bun scripts/python-tests.ts --pattern test_runtime_rendezvous.py` | One backend module |
| `bun run test:skill` | Bundled skill text contracts, not an agent execution eval |
| `bun run test:desktop` | Desktop logic contracts |
| `bun run test:integration` | Configured host/stock contracts |
| `bun run build:plugin` | Generate `dist/plugin.js` from Desktop source |
| `bun run package:release -- --output dist/my-fresh-candidate` | Build a package in a new destination |

`test:backend` and `test:u3` are retained aliases of `test:agent`. `test:u2` groups Desktop build, Desktop tests and backend tests. They are not additional independent coverage. `qualify` is historical receipt validation, not an automatic installed-app test. `install:preview`, `uninstall:preview`, and `scripts/install_isolated.py` are isolated qualification helpers, not ordinary user installation commands. `build:fixture` runs Xcode and requires an explicitly selected available simulator target; inspect the script first.

## Package from committed source

1. Review and commit the intended source changes. Keep unrelated work out of the release tree.
2. Run local checks and retain their exact exits and skip counts outside the package.
3. Build into a fresh output directory. The packager refuses an occupied destination, but reads the current worktree, including uncommitted changes.
4. Packaging copies the built Desktop module into `plugin/desktop/plugin.js`. If that changes, review and commit the generated file, then repeat from the new commit.
5. Compare every package member and its bytes with the committed `plugin/` tree. Confirm `dist/plugin.js` equals `plugin/desktop/plugin.js` and inspect `git status` afterward.
6. Verify the external manifest and archive digest. The manifest agreeing with itself is not proof that the archive matches source.

The package includes the backend, managed Desktop module, skill, dependency manifests/lock and license notices. It excludes npm payloads, Simulator runtimes, private evidence and writable caches. Do not vendor native dependency binaries without the licensing review described in the notices.

## Changes and reviews

Keep runtime behavior, documentation claims, and actual observations distinct. Preserve approval, conversation ownership, exact targets, fail-closed cleanup and app-data retention. For a bug, reproduce the failing behavior and add a regression before claiming a fix. For skill edits, check every referenced action against live schemas and retain executable safety rules. Text assertions alone do not show that an agent followed the skill.

Check the public diff and reachable history before changing repository visibility. A new clean snapshot does not remove sensitive data from old commits. History rewrites, branch deletion, releases, merges, and public visibility changes require explicit maintainer authorization.

Original contributions use the repository's MIT license. Third-party code retains its upstream licensing and attribution. Report suspected security problems privately as described in [SECURITY.md](SECURITY.md).
