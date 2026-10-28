"""The plugin's one owner-approved use of a host internal: a read-only compatibility seam.

Plugin-only automatic cleanup on disable/removal needs to know whether this process's config
readers read the profile's files. The host offers no documented surface for that. Its readers
ask ``agent.safe_worker_policy.worker_config_snapshot()`` first and serve its snapshot, never
the files, whenever it is not None (``load_config`` via ``_worker_config_snapshot``,
config.py:1887-1897 and :2325-2328; ``load_user_config_effective``, config_effective.py:63-69).
That module calls itself "Private bootstrap policy" (safe_worker_policy.py:1). The owner approved
this one read-only call and nothing else from it; tests/agent/test_host_surface.py allows the
import below in this file only.

The shape is established from the archived host 3403b92 (safe_worker_policy.py:77-80): a
callable taking no arguments that returns None (no policy bound) or the frozen session config, a
fresh ``json.loads`` of a dict the host serialized at bind time (:54-56, :62), so exactly a
``dict``, possibly empty (the host serves ``{}`` too). It raises ``RuntimeError`` in a forked
child (:66-69). Anything else means the host changed under us.

Only the type of the return is looked at, and the reference is dropped at once. The snapshot can
hold the worker's secrets and an exception's text can carry config, so neither is logged,
stored, formatted or returned: the result is one of three authorities and a fixed reason code.
Nothing here writes to the host module.

A same-shaped semantic change (for example None returned while the process is in fact frozen)
returns a well-formed answer and cannot be detected here.
"""
from __future__ import annotations

PROFILE, FROZEN, UNKNOWN = 'profile', 'frozen', 'unknown'
# Fixed reasons for UNKNOWN; nothing else is ever reported.
COMPAT_REASONS = ('compat_missing', 'compat_not_callable', 'compat_raised', 'compat_invalid_return')


def config_authority() -> tuple[str, str | None]:
    """``(profile, None)`` when this process's config readers read the profile's files,
    ``(frozen, None)`` when they serve a bound worker snapshot, else ``(unknown, <reason>)``."""
    try:
        from agent.safe_worker_policy import worker_config_snapshot
    except ImportError:
        return UNKNOWN, 'compat_missing'
    except Exception:
        return UNKNOWN, 'compat_raised'
    if not callable(worker_config_snapshot):
        return UNKNOWN, 'compat_not_callable'
    try:
        snapshot = worker_config_snapshot()
    except Exception:
        return UNKNOWN, 'compat_raised'
    if snapshot is None:
        return PROFILE, None
    frozen = type(snapshot) is dict
    del snapshot
    return (FROZEN, None) if frozen else (UNKNOWN, 'compat_invalid_return')
