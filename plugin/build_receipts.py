from __future__ import annotations

from typing import Any


def run_outcomes(evidence: dict[str, Any], *, ok: bool) -> dict[str, Any]:
    """Independent v1 outcomes; historical claim validation below remains historical."""
    stage = evidence.get('stage')
    stages = {'build': {'building', 'resolve_artifact'}, 'install': {'installing', 'verify_install'},
              'launch': {'launching', 'verify_pid', 'verify_challenge'}}
    result = {}
    for axis in ('build', 'install', 'launch'):
        facts = evidence.get(axis, {})
        outcome = facts.get('outcome', 'not_started') if isinstance(facts, dict) else 'unknown'
        if not ok and stage in stages[axis]:
            outcome = 'failed'
        result[axis] = {'outcome': outcome, 'facts': facts}
    server = evidence.get('server')
    result['server'] = {'outcome': server.get('status', 'unknown') if isinstance(server, dict) else 'not_observed', 'facts': server}
    result['preview'] = {'open_requested': False, 'visible_pixels': 'unknown', 'source_provenance': 'unknown'}
    return result
