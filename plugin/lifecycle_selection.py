"""Plugin selection from parsed config layers, and the short-lived child that parses them.

Imported, this is the pure decision table (``classify``). Executed as
``sys.executable -I -B -S -c <this source>`` it is the parse child of the in-host observer
(``runtime_lifecycle.evaluate``): the owning host process captures the context (the bound user
``config.yaml`` bytes, its live managed directory and that directory's ``config.yaml``
bytes) and hands the bytes to this child over stdin as length-prefixed frames. The child parses
both layers with public ruamel safe YAML 1.1 primitives for provenance, writes the same bytes into a
disposable Hermes home and managed directory of its own, and has the host load them through its
documented reader, ``hermes_cli.config.load_config_readonly()`` (host-3403b92
``website/docs/developer-guide/plugins/index.md:1051``). It decides with ``classify`` and writes
ONE fixed JSON of codes to stdout. Nothing else leaves it: host stdout, warnings and log records
are contained here (this is a disposable process, so its process-wide warning/logging state is
its own), and its stderr is DEVNULL.

The disposable home is a fresh directory the parent creates under the plugin-owned observer
directory and deletes after every run, killed or not (``runtime_lifecycle._spawn``); every file
the host writes while loading (its home scaffold, the "good"/"corrupt" config backups) lands
there. It has no last-known-good, so a config the host's processing rejects either raises or
comes back as the host's fallback type: both are ``unknown`` here, as is any parse warning or
WARNING-level log record. The child resolves no context: its ``${VAR}`` expansion is its own and
never evidence (references stay ``unknown`` in ``classify``), and its managed scope is only the
captured bytes, never the default ``/etc/hermes``.

It imports the host from the import roots the host process itself had (``paths``, captured by
``runtime_lifecycle.import_roots``): PM runs Hermes on the bare base interpreter, removes the
interpreter's own site-packages and adds its selected environment with ``site.addsitedir``
(host-3403b92 ``pm/environments.py:493-502``), handing it to children only through PYTHONPATH, which
``-I`` and the minimal env drop. The roots are data, never re-processed (review 11): ``-S`` keeps
site from running here at all, so neither the interpreter's own site-packages nor any ``.pth`` file
reaches this child. Paths a host ``.pth`` added are on the host's ``sys.path`` and arrive as plain
roots; import hooks one installed are not reproduced, so a dependency reachable only through such a
hook is ``lifecycle_host_unavailable``. Roots are appended after the host source root and this
child's own stdlib, with the plugin directory, the observer directory (this run's parent) and
anything not an absolute directory left out again here.

Stdlib only, no package-relative imports: the host keeps this source in memory from plugin
import, so an uninstall or upgrade of the plugin directory never changes what the child runs.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import struct
import sys
import warnings
from pathlib import Path

LISTS = ('enabled', 'disabled')
REFERENCE = '${'
SELECTIONS = ('enabled', 'disabled', 'unknown')
# Larger than any real config; a layer past this is `unknown`, never parsed (review 5 finding 1).
MAX_CONFIG_BYTES = 1024 * 1024
_HEADER = struct.Struct('>I')
# The meta frame, import roots included; past this the frame is refused, never read.
MAX_META = 64 * 1024
# One meta frame plus two capped layers.
MAX_MESSAGE = 2 * MAX_CONFIG_BYTES + MAX_META + 3 * _HEADER.size
# The only decisions a reply may carry; the parent refuses anything else.
DECISIONS = {
    'enabled': frozenset({'enabled'}),
    'disabled': frozenset({'disabled', 'removed'}),
    'unknown': frozenset({
        'lifecycle_plugins_malformed', 'lifecycle_selection_provenance_mismatch', 'lifecycle_selection_malformed',
        'lifecycle_disabled_malformed', 'lifecycle_selection_reference', 'lifecycle_enabled_missing',
        'lifecycle_enabled_malformed', 'lifecycle_read_failed', 'lifecycle_selection_provenance_unavailable',
        'lifecycle_selection_host_rejected', 'lifecycle_host_unavailable', 'lifecycle_selection_child_failed'}),
}
_ERROR = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')


def _merge(base: dict, override: dict) -> dict:
    """The host's overlay merge (host-3403b92 ``hermes_cli/config.py:1487-1502`` ``_deep_merge``)."""
    result = dict(base)
    for key, value in override.items():
        over_dict = isinstance(result.get(key), dict)
        if over_dict and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        elif not (over_dict and value is None):
            result[key] = value
    return result


def _composed(user: object, managed: object) -> object:
    """Raw plugins section as the host composes it, before expansion: a non-mapping user document
    is an empty layer (``config_effective.py:95``) and an empty managed layer is skipped
    (``managed_scope.py:134-136``), else the managed layer merges on top (``:146``)."""
    user = user if isinstance(user, dict) else {}
    composed = _merge(user, managed) if isinstance(managed, dict) and managed else user
    return composed.get('plugins')


def _agrees(effective: object, raw: object) -> bool:
    """Expansion only rewrites strings (``config.py:1583-1591``), so the effective lists must have
    the raw lists' shape, and every literal raw entry must be unchanged in the effective read."""
    if not isinstance(effective, dict) or not isinstance(raw, dict):
        return False
    for key in LISTS:
        if (key in effective) != (key in raw):
            return False
        value, source = effective.get(key), raw.get(key)
        if isinstance(value, list) != isinstance(source, list) or \
                (isinstance(value, list) and len(value) != len(source)):
            return False
        for item, origin in zip(value, source) if isinstance(value, list) else ():
            if isinstance(origin, str):
                if not isinstance(item, str) or (REFERENCE not in origin and item != origin):
                    return False
            elif type(item) is not type(origin) or item != origin:
                return False
    return True


def _entries(*sections: object):
    for section in sections:
        if isinstance(section, dict):
            for key in LISTS:
                if isinstance(section.get(key), list):
                    yield from section[key]


def classify(config: object, names, *, user: object, managed: object) -> dict:
    """Selection of names from the effective config and both raw layers; reason says why.

    ``config`` is the effective view; ``user`` and ``managed`` are the same layers before
    ``${VAR}`` expansion. The parse child passes what the host's own ``load_config_readonly()``
    made of the same bytes (DEFAULT_CONFIG merge, normalization, managed overlay) as ``config``,
    so ``_agrees`` is a real cross-check that the host's processing left the selection lists as
    the raw layers wrote them (``lifecycle_selection_provenance_mismatch`` otherwise). A plugins
    section absent from both raw layers is an empty one: the host's composition then carries
    only its defaults' plugins section, which has no lists (``config_defaults.py:1710-1712``).
    Provenance comes only from the raw layers (review 2 finding 1): a reference may resolve
    differently in the owning host, so any reference in either list of either layer is
    ``unknown`` unless a literal disable decides. The host turns each list into a set and
    discards one holding an unhashable entry (``plugins_discovery.py:112-119``, gated at
    ``:278``/``:299``), so any non-string entry is ``unknown`` before any name is used as
    evidence (review 2 finding 2). Nothing is filtered.
    """
    plugins = config.get('plugins') if isinstance(config, dict) else None
    if not isinstance(plugins, dict):
        return {'selection': 'unknown', 'reason': 'lifecycle_plugins_malformed'}
    raw = _composed(user, managed)
    raw = {} if raw is None else raw
    if not _agrees(plugins, raw):
        return {'selection': 'unknown', 'reason': 'lifecycle_selection_provenance_mismatch'}
    layers = [layer.get('plugins') if isinstance(layer, dict) else None for layer in (user, managed)]
    if any(not isinstance(item, str) for item in _entries(plugins, *layers)):
        return {'selection': 'unknown', 'reason': 'lifecycle_selection_malformed'}
    disabled = plugins.get('disabled', [])
    if not isinstance(disabled, list):
        return {'selection': 'unknown', 'reason': 'lifecycle_disabled_malformed'}
    names = set(names)
    # Only an entry that was literal before expansion (``_agrees``: unchanged by it) is evidence.
    if names & {item for item in raw.get('disabled', []) if REFERENCE not in item}:
        return {'selection': 'disabled', 'reason': 'disabled'}
    if any(REFERENCE in item for item in _entries(plugins, *layers)):
        return {'selection': 'unknown', 'reason': 'lifecycle_selection_reference'}
    enabled = plugins.get('enabled')
    # The host reads a missing enabled list as "nothing selected yet", not as a disable.
    if 'enabled' not in plugins:
        return {'selection': 'unknown', 'reason': 'lifecycle_enabled_missing'}
    if not isinstance(enabled, list):
        return {'selection': 'unknown', 'reason': 'lifecycle_enabled_malformed'}
    if names & set(enabled):
        return {'selection': 'enabled', 'reason': 'enabled'}
    # Supported removal clears the selection aliases; an unselected plugin never loads.
    return {'selection': 'disabled', 'reason': 'removed'}


def _unknown(reason: str, **detail) -> dict:
    return {'selection': 'unknown', 'reason': reason, **detail}


def valid_reply(reply: object) -> bool:
    """A reply carries a known decision and at most an exception TYPE name: codes only."""
    return isinstance(reply, dict) and set(reply) <= {'selection', 'reason', 'error'} and \
        reply.get('selection') in DECISIONS and reply.get('reason') in DECISIONS[reply['selection']] and \
        ('error' not in reply or (isinstance(reply['error'], str) and _ERROR.fullmatch(reply['error']) is not None))


def frame(*parts: bytes) -> bytes:
    return b''.join(_HEADER.pack(len(part)) + part for part in parts)


def unframe(data: bytes, count: int) -> list[bytes]:
    parts, offset = [], 0
    for _ in range(count):
        if offset + _HEADER.size > len(data):
            raise ValueError('lifecycle_frame_short')
        size, = _HEADER.unpack_from(data, offset)
        offset += _HEADER.size
        if offset + size > len(data):
            raise ValueError('lifecycle_frame_short')
        parts.append(data[offset:offset + size])
        offset += size
    if offset != len(data):
        raise ValueError('lifecycle_frame_trailing')
    return parts


_LOGGED: list = []


class _Discard(logging.Handler):
    """Child only, the root's one handler: counts records at WARNING and above, emits nothing, so
    no record reaches stderr or a log file. The host's import adds no handler of its own."""

    def emit(self, record):
        if record.levelno >= logging.WARNING:
            _LOGGED.append(record.levelno)


def _contain_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(_Discard())
    root.setLevel(logging.WARNING)


def _text(data: bytes) -> io.TextIOWrapper:
    """The bytes as ``open(path, encoding='utf-8-sig')`` reads a file: BOM dropped, universal newlines."""
    return io.TextIOWrapper(io.BytesIO(data), encoding='utf-8-sig')


def parse_yaml(data: bytes) -> object:
    """Safe YAML 1.1 provenance, using ruamel's public instance API.

    https://yaml.dev/doc/ruamel.yaml/basicuse/ documents C/pure differences;
    https://yaml.dev/doc/ruamel.yaml/api/ requires fresh instances after failure.
    Keep both attempts' warnings for the caller's fail-closed check.
    """
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    with _text(data) as stream:
        document = stream.read()
    for pure in (False, True):
        parser = YAML(typ='safe', pure=pure)
        parser.version = (1, 1)
        parser.allow_duplicate_keys = False
        try:
            return parser.load(document)
        except YAMLError:
            if pure:
                raise


def managed_directory() -> Path | None:
    """Live filesystem scope, https://hermes-agent.nousresearch.com/docs/user-guide/managed-scope.

    The released host additionally suppresses default system policy inside pytest;
    preserve that test isolation without suppressing an explicit bootstrap override.
    """
    location = os.environ.get('HERMES_MANAGED_DIR', '').strip()
    if not location:
        if 'PYTEST_CURRENT_TEST' in os.environ:
            return None
        location = '/etc/hermes'
    directory = Path(location)
    return directory if directory.is_dir() else None


def _write(path: Path, data: bytes) -> None:
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(data)


def snapshot(host_config, user: bytes, managed: bytes | None) -> None:
    """The captured layers, written where this child's host reads them: the user bytes at its
    config path (``get_config_path``, config.py:431-433), which must be ``config.yaml`` in the
    disposable ``HERMES_HOME``; the managed bytes in the disposable ``HERMES_MANAGED_DIR``.
    That directory exists only when a managed layer was captured. The plugin-owned resolver
    checks its placement and existence, matching released v0.22.5 managed_scope.py:45-59.
    This does not verify the host's resolver. ``_agrees`` checks selection-list agreement
    with the host reader, but cannot detect a managed layer omitted from the capture."""
    path, home = host_config.get_config_path(), Path(os.environ['HERMES_HOME'])
    if path != home / 'config.yaml':
        raise RuntimeError('lifecycle_snapshot_misplaced')
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(path, user)
    directory = Path(os.environ['HERMES_MANAGED_DIR'])
    if managed is not None:
        directory.mkdir(mode=0o700)
        _write(directory / 'config.yaml', managed)
    if managed_directory() != (directory if managed is not None else None):
        raise RuntimeError('lifecycle_snapshot_managed_scope')


def _add_roots(paths: list, plugin: str) -> None:
    """The host's import roots as plain entries after everything already on this child's path."""
    excluded = (Path(plugin).resolve(), Path.cwd().resolve().parent)
    known = {os.path.realpath(entry) for entry in sys.path if entry}
    for entry in paths:
        path = os.path.realpath(entry)
        if path not in known and os.path.isdir(path) and \
                not any(Path(path).is_relative_to(directory) for directory in excluded):
            sys.path.append(path)
            known.add(path)


def respond(data: bytes) -> dict:
    """One decision from one framed message: ``{host, names, managed, paths, plugin}``, user bytes,
    managed bytes."""
    try:
        meta, user_bytes, managed_bytes = unframe(data, 3)
        if len(meta) > MAX_META:
            raise ValueError('lifecycle_frame_invalid')
        meta = json.loads(meta)
        root, names, has_managed, paths = meta['host'], meta['names'], meta['managed'], meta['paths']
        plugin = meta['plugin']
        if not isinstance(root, str) or not isinstance(has_managed, bool) or not isinstance(names, list) or \
                not isinstance(plugin, str) or not os.path.isabs(plugin) or \
                not names or not all(isinstance(name, str) and name for name in names) or \
                not isinstance(paths, list) or not all(isinstance(path, str) and os.path.isabs(path) for path in paths) or \
                max(len(user_bytes), len(managed_bytes)) > MAX_CONFIG_BYTES:
            raise ValueError('lifecycle_frame_invalid')
    except Exception:
        return _unknown('lifecycle_selection_child_failed')
    sys.path.insert(0, root)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        try:
            _add_roots(paths, plugin)
            import ruamel.yaml
            from hermes_cli import config as host_config
        except Exception as exc:
            return _unknown('lifecycle_host_unavailable', error=type(exc).__name__)
        # Import loads the config once (provider discovery reads ``plugins.enabled``,
        # providers/__init__.py:555-563) — before the snapshot exists, so it sees none and
        # imports no entry-point plugin the captured selection enables (:578-590). Nothing it
        # warned or logged is about these bytes; from here on, everything is.
        del caught[:], _LOGGED[:]
        try:
            # Provenance: the user layer as config.py:2362-2363 reads it.
            user = parse_yaml(user_bytes)
        except Exception as exc:
            return _unknown('lifecycle_read_failed', error=type(exc).__name__)
        managed = {}
        if has_managed:
            try:
                # The managed layer as managed_scope.py:107-109 reads it; a non-mapping is empty (:104).
                managed = parse_yaml(managed_bytes) or {}
            except Exception as exc:
                # The host logs this and ignores the managed file (managed_scope.py:86-92).
                return _unknown('lifecycle_selection_provenance_unavailable', error=type(exc).__name__)
            managed = managed if isinstance(managed, dict) else {}
        try:
            snapshot(host_config, user_bytes, managed_bytes if has_managed else None)
        except Exception as exc:
            return _unknown('lifecycle_selection_child_failed', error=type(exc).__name__)
        try:
            config = host_config.load_config_readonly()
        except Exception as exc:
            # Processing outside the loader's fallback (``_canonicalize_config``, config.py:2393).
            return _unknown('lifecycle_selection_host_rejected', error=type(exc).__name__)
        if type(config) is not dict:
            # The host's fallback type (config_read_errors.py:114-123), which the host itself
            # tells from a real read this way (update_receipt.py:728): defaults or last-known-good.
            failure = getattr(config, 'read_error', None)
            return _unknown('lifecycle_selection_host_rejected',
                            **({'error': type(failure).__name__} if isinstance(failure, BaseException) else {}))
        if caught or _LOGGED:
            return _unknown('lifecycle_selection_host_rejected')
    return classify(config, names, user=user, managed=managed)


def main() -> None:
    """Child entry: the reply goes to the original stdout only; fd 1 itself is pointed at stderr
    (DEVNULL in the parent) so nothing the host prints can pass for a reply."""
    reply_fd = os.dup(1)
    os.dup2(2, 1)
    _contain_logging()
    try:
        reply = respond(sys.stdin.buffer.read(MAX_MESSAGE + 1))
    except BaseException:
        reply = _unknown('lifecycle_selection_child_failed')
    if not valid_reply(reply):
        reply = _unknown('lifecycle_selection_child_failed')
    os.write(reply_fd, json.dumps(reply).encode('ascii'))


if __name__ == '__main__':
    main()
