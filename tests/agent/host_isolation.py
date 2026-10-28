"""Disposable roots for every child that imports the archived host.

Host import is execution: without ``HERMES_HOME`` the host falls back to ``~/.hermes`` and
initializes it (host-3403b92 ``hermes_constants.py:51-58``, ``hermes_cli/config_home.py:39-69``),
and without ``HOME`` that is the real account home. So every host child gets its account home,
Hermes home, runtime, XDG, temp and cwd under one fixture root, and ``require_contained``
refuses to launch one that does not.
"""
from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

SENTINEL = '.hermes-ios-account-standin'
ROOTED = ('HOME', 'HERMES_HOME', 'HERMES_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
          'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR')


def resolved_homes() -> dict:
    """Child-side readback of every home the host could fall back to (call after host import)."""
    import tempfile
    from hermes_constants import get_hermes_home
    return {'home': str(Path.home().resolve()), 'expanduser': os.path.realpath(os.path.expanduser('~')),
            'hermes_home': str(Path(get_hermes_home()).resolve()),
            'tmp': os.path.realpath(tempfile.gettempdir()), 'cwd': os.path.realpath(os.getcwd())}


# The same readback for ``-I -c`` children, which cannot import this module.
HOMES = 'import os\nfrom pathlib import Path\n' + inspect.getsource(resolved_homes)


def _under(path: object, root: Path) -> bool:
    return isinstance(path, str) and os.path.isabs(path) and Path(path).resolve().is_relative_to(root)


def fixture_env(root: Path, **extra: str) -> dict:
    """Allowlisted, credential-free env whose every home-like root is a fresh dir under ``root``."""
    root = Path(root).resolve()
    paths = {'HOME': root / 'account', 'HERMES_HOME': root / 'hermes', 'HERMES_RUNTIME_DIR': root / 'runtime-root',
             'XDG_CONFIG_HOME': root / 'xdg' / 'config', 'XDG_CACHE_HOME': root / 'xdg' / 'cache',
             'XDG_DATA_HOME': root / 'xdg' / 'data', 'XDG_STATE_HOME': root / 'xdg' / 'state',
             'XDG_RUNTIME_DIR': root / 'xdg' / 'runtime', 'TMPDIR': root / 'tmp'}
    for path in (*paths.values(), root / 'cwd'):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    sentinel = paths['HOME'] / SENTINEL
    if not sentinel.exists():
        sentinel.write_text(str(root))
    return {'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1',
            **{key: str(path) for key, path in paths.items()}, **extra}


def require_contained(env: dict, root: Path, cwd: object) -> None:
    """Refuse a host child whose HOME/HERMES_HOME (or any other set root, or cwd) escapes ``root``."""
    root = Path(root).resolve()
    for key in ('HOME', 'HERMES_HOME'):
        if not _under(env.get(key), root):
            raise AssertionError(f'refusing host child: {key}={env.get(key)!r} is not under fixture root {root}')
    for key in ROOTED:
        if key in env and not _under(env[key], root):
            raise AssertionError(f'refusing host child: {key}={env[key]!r} is not under fixture root {root}')
    if not _under(str(cwd) if cwd is not None else None, root):
        raise AssertionError(f'refusing host child: cwd={cwd!r} is not under fixture root {root}')


def run_host_child(argv: list[str], *, root: Path, env: dict, cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    require_contained(env, root, cwd)
    return subprocess.run(argv, env=env, cwd=str(cwd), **kwargs)


def snapshot(path: Path) -> dict:
    """Every entry under ``path`` with its size and mtime, to show what a child touched."""
    path = Path(path)
    return {str(item.relative_to(path)): (item.is_dir(), item.lstat().st_size, item.lstat().st_mtime_ns)
            for item in sorted(path.rglob('*'))}


def assert_homes_contained(homes: dict, root: Path, env: dict) -> None:
    """The child's own resolved homes are the fixture stand-ins, so they are the only homes it touched."""
    root = Path(root).resolve()
    account = str(Path(env['HOME']).resolve())
    if homes.get('home') != account or homes.get('expanduser') != account:
        raise AssertionError(f'host child resolved a home outside the account stand-in: {homes}')
    for key in ('hermes_home', 'tmp', 'cwd'):
        if not _under(homes.get(key), root):
            raise AssertionError(f'host child resolved {key} outside fixture root {root}: {homes}')
    if (Path(account) / SENTINEL).read_text() != str(root):
        raise AssertionError('account stand-in sentinel changed')
