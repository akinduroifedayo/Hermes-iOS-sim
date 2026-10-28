"""Native v1 controls. Never construct a runtime owner from dashboard requests.

The host imports this file independently (also in the plugin host process).
Resolve only this package's registered instance, by its exact source path.
A different host process or missing registration is a truthful unavailable gate.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from fastapi import APIRouter, HTTPException

router = APIRouter()
PLUGIN_ROOT = Path(__file__).resolve().parents[1]

def _registration():
    target = PLUGIN_ROOT / '__init__.py'
    for module in tuple(sys.modules.values()):
        file = getattr(module, '__file__', None)
        if file and Path(file).resolve() == target:
            registered = getattr(module, '_registration', None)
            if registered:
                return registered
    return None

def registration():
    value = _registration()
    if value is None or value['stock_runtime'] is None:
        raise HTTPException(503, detail={'error': 'registration_owned_runtime_unavailable',
            'recovery': 'Enable/reload the candidate through the supported plugin host. Do not create a second owner.'})
    binding = value.get('binding')
    if binding is None:
        raise HTTPException(503, detail={'error': 'runtime_identity_unavailable'})
    if binding.retired:
        raise HTTPException(503, detail={'error': 'registration_retired'})
    try:
        saved, live = binding.saved, binding.live()
    except Exception:
        raise HTTPException(503, detail={'error': 'runtime_identity_unavailable'}) from None
    if saved is None:
        raise HTTPException(503, detail={'error': 'runtime_identity_unavailable'})
    if live.profile != saved.profile:
        raise HTTPException(409, detail={'error': 'runtime_profile_mismatch'})
    if live.home != saved.home:
        raise HTTPException(409, detail={'error': 'runtime_home_mismatch'})
    if binding.retired:
        raise HTTPException(503, detail={'error': 'registration_retired'})
    return value

@router.get('/status')
def status():
    value = registration()
    return {'ok': True, 'profile': value['profile'], 'native_dispatch': 'backend_owned',
            'preview': 'host_owned', 'panel_actions': ['target', 'preflight', 'status', 'cancel', 'stop']}

@router.post('/runtime')
async def runtime_action(payload: dict):
    value = registration()
    if payload.get('action') not in {'discover', 'preflight', 'status', 'cancel', 'stop'}:
        raise HTTPException(400, detail={'error': 'unsupported_action', 'recovery': 'Run from the originating agent turn.'})
    if payload.get('action') in {'cancel', 'stop'}:
        raise HTTPException(409, detail={'error': 'claim_unsupported_origin',
            'recovery': 'Cancel or Stop from the authenticated owning conversation with the exact target and claim generation.'})
    # Import only our registered package, never core tool dispatch/private host APIs.
    backend_module = sys.modules[type(value['backend']).__module__]
    result = json.loads(await backend_module.handle_tool(value['backend'], payload,
        authority=value['authority'], stock_runtime=value['stock_runtime'], data_dir=value['data_dir']))
    if result.get('ok') is False:
        raise HTTPException(409, detail=result)
    return result

# Historical protocols are rejected, not left able to start a custom helper.
@router.post('/runway/{path:path}')
@router.post('/stream/{path:path}')
@router.post('/placements')
@router.post('/placements/{path:path}')
def unsupported(path: str = ''):
    raise HTTPException(410, detail={'error': 'unsupported_action',
                                   'recovery': 'Use native /runtime controls or ios_iteration in chat.'})
