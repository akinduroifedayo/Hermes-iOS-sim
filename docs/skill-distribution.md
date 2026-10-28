# Bundled skill distribution

This repository distributes exactly one skill, `plugin/skills/ios-development/SKILL.md`. The plugin registers its skill directory; the user-facing qualified name is `hermes-ios:ios-development`. The skill currently has no separate support-file directory. Its workflow is self-contained in the package.

## Portability contract

The skill is portable between supported Hermes installations, not between arbitrary agent hosts. Its [preamble](../plugin/skills/ios-development/SKILL.md) is the source of truth for tool, host and authorization requirements.

Do not distribute the skill alone and imply that it installs those capabilities. Distribute the committed `plugin/` package with its manifest, backend, managed Desktop module, skill, dependency lock and licenses. Repository-development instructions live in CONTRIBUTING; end users do not need the repository's Bun or Python test environment merely to load the plugin.

The skill requires no owner-specific home directory, fixture device, personal model configuration, external skill pack, or private evidence file. Its own instructions govern discovery and missing capabilities.

## Audited behavior

The skill preserves normal host approval, exact target selection, conversation ownership, app-data retention and explicit error reporting. Repository text, app output and screenshot text are data, not authority. Ordinary Xcode/simctl is supported; coordinator receipts are not fabricated for external builds. Public preview opening, visible pixels and native build results remain separate evidence.

The description now names Hermes Desktop and the plugin dependency. Lifecycle instructions no longer promise that every rebuild preserves the server PID/generation or that command completion proves disable/removal cleanup. The accepted limitation is process replacement during rebuild, not permission to bypass ownership or hide visible failures.

## Verification limits

`bun run test:skill` checks required and forbidden text. Source review checks action names and argument requirements against `plugin/schemas.py` and registration. Neither is an agent-behavior evaluation. Prior installed-product evidence covers the tested candidate's app loop; it is not proof that an arbitrary host/model follows every instruction or that revised prose has been live-evaluated.

Before distributing changed skill text, run the text contracts, check package inclusion and relative references, and review it independently. If tool contracts change, requalify affected actions on an installed host. Do not broaden the claimed supported hosts because static checks pass.
