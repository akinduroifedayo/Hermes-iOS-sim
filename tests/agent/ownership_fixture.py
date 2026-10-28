"""Authenticated host metadata for tests exercising authority below registration."""
from plugin.conversation_claims import RequestKey


def bind_conversation(authority, identity):
    key = RequestKey.from_host_metadata(identity.profile, {
        'session_id': 'fixture-session', 'task_id': 'fixture-task',
        'turn_id': 'fixture-turn', 'tool_call_id': 'fixture-approved-call'})
    with authority.store.device_lock(identity.udid):
        claim = authority.admit_conversation(key, identity, 0)
    authority.active_claim.set(claim)
