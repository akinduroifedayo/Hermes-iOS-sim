from __future__ import annotations

import json
from pathlib import Path


_RUNTIME_FIELDS = {"worktree", "project", "scheme", "configuration", "runtime", "udid"}
_OPTIONAL_FIELDS = {"display_rotation_degrees"}
_ALLOWED_ROTATIONS = {0, 90, 180, 270}


def _normalized_target_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or not _RUNTIME_FIELDS.issubset(payload) or not set(payload).issubset(_RUNTIME_FIELDS | _OPTIONAL_FIELDS):
        raise ValueError("runway_target_malformed")
    if not all(isinstance(payload[field], str) and payload[field].strip() for field in _RUNTIME_FIELDS):
        raise ValueError("runway_target_malformed")
    rotation = payload.get("display_rotation_degrees", 0)
    if not isinstance(rotation, int) or isinstance(rotation, bool) or rotation not in _ALLOWED_ROTATIONS:
        raise ValueError("runway_target_malformed")
    result: dict[str, object] = {field: payload[field].strip() for field in _RUNTIME_FIELDS}
    result["display_rotation_degrees"] = rotation
    return result


def validate_target_selection(payload: object, *, runner=None) -> dict:
    """Validate explicit choices without changing storage or launching anything."""
    from .runtime_discovery import discover_runtime_selection, run_command, _credible_settings, product_facts, assess_destination
    try:
        normalized = _normalized_target_payload(payload)
        root = Path(str(normalized['worktree'])).expanduser().resolve()
        project = Path(str(normalized['project'])).expanduser().resolve()
        if not root.is_dir() or not project.is_dir() or not project.is_relative_to(root) or project.suffix not in {'.xcodeproj', '.xcworkspace'}:
            raise ValueError('project_kind_or_scope_invalid')
        normalized.update(worktree=str(root), project=str(project))
    except (ValueError, OSError) as exc:
        return {'valid': False, 'stage': 'target_validation', 'reasons': [str(exc)], 'recovery': 'Choose an existing in-worktree Xcode project/workspace.'}
    execute = runner or run_command
    discovery = discover_runtime_selection(root, runner=execute, remembered=normalized)
    if discovery['status'] != 'selected':
        return {'valid': False, 'stage': 'target_validation', 'discovery': discovery, 'recovery': 'Choose an available exact scheme, configuration, runtime and UDID.'}
    flag = '-workspace' if project.suffix == '.xcworkspace' else '-project'
    argv = ('xcodebuild', '-showBuildSettings', '-json', flag, str(project), '-scheme', str(normalized['scheme']), '-configuration', str(normalized['configuration']), '-destination', 'id=' + str(normalized['udid']), '-sdk', 'iphonesimulator')
    result = execute(argv, root)
    try:
        products = [x['buildSettings'] for x in json.loads(result.stdout) if isinstance(x, dict) and isinstance(x.get('buildSettings'), dict) and _credible_settings(x['buildSettings'])]
        device = next(item for item in discovery['devices'] if item['udid'] == normalized['udid'] and item['runtime'] == normalized['runtime'])
        assessment = {'status': 'unknown', 'reasons': ['application_product_unverified']}
        if result.returncode == 0 and len(products) == 1:
            product = product_facts(products[0], configuration=str(normalized['configuration']))
            assessment = assess_destination(product, device)
        valid = assessment['status'] == 'native'
    except (ValueError, TypeError, KeyError):
        assessment = {'status': 'unknown', 'reasons': ['application_product_unverified']}
        valid = False
    return {'assessment': assessment, 'reasons': assessment['reasons'], 'valid': valid, 'stage': 'target_validation', 'selection': normalized if valid else None, 'command': list(argv), 'returncode': result.returncode, 'stderr': result.stderr[-4096:], 'recovery': None if valid else 'Choose one simulator application product compatible with the exact destination.'}
