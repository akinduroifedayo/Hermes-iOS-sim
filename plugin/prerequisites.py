"""Read-only prerequisite checks; Apple tools are never installed automatically."""
from __future__ import annotations

import json
import re
from pathlib import Path
from .runtime_discovery import Runner, run_command
from .serve_sim_dependency import StockDependency


def preflight(worktree: Path, data_dir: Path, *, runner: Runner = run_command) -> dict:
    root = worktree.expanduser().resolve()
    checks = []
    commands = [
        ('xcode_selection', ('xcode-select', '-p'), 'Select an installed Xcode developer directory with xcode-select.'),
        ('xcode_license', ('xcodebuild', '-checkFirstLaunchStatus'), 'Open Xcode, accept its license and finish first-launch components.'),
        ('xcode_toolchain', ('xcrun', '--find', 'swiftc'), 'Select full Xcode, not Command Line Tools alone.'),
        ('simulator_runtimes', ('xcrun', 'simctl', 'list', 'runtimes', '--json'), 'Install an iOS Simulator runtime in Xcode Settings > Components.'),
        ('node', ('node', '--version'), 'Install/select Node >=20; no automatic global upgrade is performed.'),
        ('npm', ('npm', '--version'), 'Install/select npm with your supported Node installation.'),
    ]
    for stage, argv, recovery in commands:
        result = runner(argv, root)
        ok = result.returncode == 0
        if stage == 'xcode_selection':
            ok = ok and '.app/Contents/Developer' in result.stdout.strip()
        if stage == 'node':
            match = re.fullmatch(r'v(\d+)\.\d+\.\d+\s*', result.stdout)
            ok = ok and match is not None and int(match.group(1)) >= 20
        if stage == 'npm':
            ok = ok and re.fullmatch(r'\d+\.\d+\.\d+\s*', result.stdout) is not None
        if stage == 'simulator_runtimes':
            try:
                runtimes = json.loads(result.stdout)['runtimes']
                ok = ok and any(r.get('isAvailable') is True and str(r.get('identifier', '')).startswith('com.apple.CoreSimulator.SimRuntime.iOS-') for r in runtimes)
            except (ValueError, KeyError, TypeError):
                ok = False
        checks.append({'stage': stage, 'command': list(argv), 'ok': bool(ok), 'returncode': result.returncode, 'stdout': result.stdout[-4096:], 'stderr': result.stderr[-4096:], 'recovery': None if ok else recovery})
    dependency = StockDependency(data_dir).status()
    return {'stage': 'preflight', 'ready': all(c['ok'] for c in checks) and dependency['ready'], 'checks': checks, 'dependency': dependency, 'setup_permitted': all(c['ok'] for c in checks if c['stage'] in ('node', 'npm')), 'apple_install_automatic': False}
