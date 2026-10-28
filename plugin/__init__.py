from __future__ import annotations

from pathlib import Path
from contextvars import ContextVar
import threading
import json
import asyncio
import logging
from dataclasses import asdict, dataclass
from functools import partial

from . import runtime_handoff, runtime_lifecycle
from .agent.ios_iteration import handle_tool
from .agent.native_runtime import native_runtime_dispatch
from .runtime_authority import RuntimeAuthority
from .schemas import IOS_ITERATION_SCHEMA, IOS_SETUP_SCHEMA
from .serve_sim_dependency import StockDependency
from .prerequisites import preflight
from .runtime_controller import StockRuntimeController
from .runtime_process import ServeSimProcessManager
from .runtime_state import RuntimeStateStore
from .conversation_claims import DeviceClaim, RequestKey, SessionOwner
from .card_integration import ContextualCards, validate_card_id
import shutil


def register_stock_lifecycle(ctx, dependency: StockDependency, *, on_unload=None,
                             profile=None, data_dir=None) -> StockRuntimeController:
    """Create one U3-consumable owner using only public storage/unload APIs.

    Registration never checks/downloads/executes stock. Native orchestration
    callers must share this returned service; no second dashboard owner.
    """
    node = shutil.which('node')
    # Missing Node is reported at explicit ensure, not an implicit installation.
    node_path = Path(node) if node else None
    data_dir = Path(ctx.state.data_dir) if data_dir is None else data_dir
    profile = ctx.profile_name if profile is None else profile
    store = RuntimeStateStore(data_dir/'stock-lifecycle.json')
    # The host has bound the owning profile now; this host process later observes its selection.
    lifecycle = runtime_lifecycle.binding(names=(ctx.plugin_id, getattr(getattr(ctx, 'manifest', None), 'name', None)),
                                          observer_home=data_dir/runtime_lifecycle.OBSERVER_DIR)
    manager = ServeSimProcessManager(dependency=dependency, node_path=node_path, store=store,
        runtime_dir=data_dir/'stock-runtime', registration=runtime_handoff.registration_identity(
            plugin_id=ctx.plugin_id, profile=profile, store=store), lifecycle=lifecycle)
    try:
        service = StockRuntimeController(process_manager=manager, profile=profile)
    except Exception:
        closer = partial(manager.close, handoff_seconds=runtime_handoff.HANDOFF_SECONDS)
        if on_unload is not None:
            on_unload(closer)
        else:
            closer()
        raise
    # on_unload cannot tell rediscovery from disable: detach (no deadline) while the profile's
    # selection is observable, and let a successor adopt. Disable/removal, Stop or host exit drain it.
    (on_unload or ctx.on_unload)(partial(service.close, handoff_seconds=runtime_handoff.HANDOFF_SECONDS))
    service.watch_handoffs()
    return service


class RegistrationUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class Identity:
    profile: str
    home: str


def _home() -> str:
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    if not home:
        raise ValueError('runtime_identity_unavailable')
    return str(Path(home).resolve())


class _Registration:
    def __init__(self, ctx):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._phase = 'declared'
        self._reason = 'registration_initializing'
        self._runtime = None
        self._closer = None
        self.cleanup_result = None
        self._unload_declared = False
        self.saved = None
        global _latest_sequence
        with _publication:
            _latest_sequence += 1
            self._sequence = _latest_sequence

    def declare_unload(self):
        try:
            on_unload = self.ctx.on_unload
        except AttributeError:
            return
        on_unload(self.retire)
        self._unload_declared = True

    def live(self):
        profile = self.ctx.profile_name
        if not isinstance(profile, str) or not profile:
            raise ValueError('runtime_identity_unavailable')
        return Identity(profile, _home())

    @property
    def retired(self):
        with self._lock:
            return self._phase == 'retired'

    def _hold_close(self, closer):
        with self._lock:
            retired = self._phase == 'retired'
            if not retired:
                self._closer = closer
        if retired:
            self._dispose(closer)

    def _dispose(self, closer):
        try:
            result = closer()
        except Exception as error:
            result = [{'server_stopped': False, 'reason': 'registration_cleanup_failed',
                       'exception_type': type(error).__name__}]
            logging.getLogger(__name__).warning('registration_cleanup_failed exception_type=%s', type(error).__name__)
        with self._lock:
            self.cleanup_result = result
        return result

    def _close_once(self):
        with self._lock:
            closer, self._closer = self._closer, None
        return self._dispose(closer) if closer else self.cleanup_result

    def initialize(self):
        with self._lock:
            if self._phase != 'declared':
                return
            self._phase = 'initializing'
        try:
            try:
                state = self.ctx.state
            except AttributeError:
                state = None
            if state is None or not self._unload_declared:
                with self._lock:
                    if self._phase != 'retired':
                        self._phase = 'unavailable'
                        self._reason = 'public_stock_lifecycle_registration_required'
                return
            data_dir = Path(state.data_dir)
            profile = self.ctx.profile_name
            if not isinstance(profile, str) or not profile:
                raise ValueError('runtime_identity_unavailable')
            try:
                saved = Identity(profile, _home())
            except Exception:
                saved = None
            dependency = StockDependency(data_dir)
            stock_runtime = register_stock_lifecycle(self.ctx, dependency,
                on_unload=self._hold_close, profile=profile, data_dir=data_dir)
            authority = RuntimeAuthority(profile=profile,
                store=RuntimeStateStore(data_dir / 'native-runs.json'))
            backend = authority.backend(dispatcher=native_runtime_dispatch, stock_runtime=stock_runtime,
                operation_timeout_seconds=360.0, drain_grace_seconds=3.0,
                lock_root=data_dir / 'operation-locks', build_data_dir=data_dir)
            runtime = {'stock_runtime': stock_runtime, 'authority': authority, 'backend': backend,
                'profile': profile, 'data_dir': data_dir, 'dependency': dependency,
                'cards': ContextualCards(authority, data_dir), 'binding': self}
            if self.ctx.profile_name != profile or (saved is not None and self.live() != saved):
                raise ValueError('runtime_identity_changed')
            with self._lock:
                if self._phase == 'retired':
                    return
                self.saved = saved
                self._runtime = runtime
                self._phase = 'ready'
                global _registration
                with _publication:
                    if self._sequence == _latest_sequence:
                        _registration = runtime
        except Exception as error:
            self._close_once()
            with self._lock:
                retired = self._phase == 'retired'
                if not retired:
                    self._phase = 'unavailable'
                    self._reason = 'runtime_initialization_failed'
            logging.getLogger(__name__).warning('registration_runtime_unavailable exception_type=%s', type(error).__name__)
            if not retired:
                raise ValueError('runtime_initialization_failed') from None

    def runtime(self):
        with self._lock:
            if self._phase == 'ready':
                return self._runtime
            raise RegistrationUnavailable('registration_retired' if self._phase == 'retired' else self._reason)

    def retire(self):
        global _registration
        with self._lock:
            if self._phase == 'retired':
                return None
            self._phase = 'retired'
            self._runtime = None
            with _publication:
                if _registration is not None and _registration.get('binding') is self:
                    _registration = None
        return self._close_once()


_registration = None
_publication = threading.Lock()
_latest_sequence = 0


def register(ctx) -> None:
    registration = _Registration(ctx)
    registration.declare_unload()

    def setup_approval(tool_name='', args=None, **metadata):
        if tool_name != 'ios_setup' or not isinstance(args, dict) or args.get('action') != 'provision':
            return None
        try:
            runtime = registration.runtime()
        except RegistrationUnavailable as error:
            return {'action': 'block', 'message': str(error)}
        return runtime['dependency'].approval_hook(tool_name=tool_name, args=args, **metadata)

    ctx.register_hook('pre_tool_call', setup_approval)

    async def ios_setup(args: dict, **_kwargs) -> str:
        try:
            runtime = registration.runtime()
        except RegistrationUnavailable as error:
            return json.dumps({'ready': False, 'status': str(error)})
        dependency = runtime['dependency']
        if set(args) - {"action", "worktree"}:
            return json.dumps({"ready": False, "status": "invalid_arguments"})
        action = args.get("action")
        if action == "provision":
            result = await dependency.provision()
        elif action == "status":
            result = dependency.status()
        elif action == "preflight" and isinstance(args.get("worktree"), str) and Path(args["worktree"]).is_dir():
            result = preflight(Path(args["worktree"]), runtime['data_dir'])
        else:
            result = {"ready": False, "status": "invalid_arguments"}
        return json.dumps(result)

    ctx.register_tool(name="ios_setup", toolset="hermes_ios", schema=IOS_SETUP_SCHEMA,
                      handler=ios_setup, is_async=True,
                      description=IOS_SETUP_SCHEMA["description"], emoji="📦")
    approvals = {}
    approval_lock = threading.Lock()
    approved_request: ContextVar[dict | None] = ContextVar('hermes_ios_approved_request', default=None)

    def request_key(metadata):
        # Public pre_tool_call and tool_execution share these host fields.
        # Tool handlers do NOT receive tool_call_id on the qualified host.
        return RequestKey.from_host_metadata(ctx.profile_name, metadata)

    def target_arguments(args):
        allowed = {'expected_claim_generation'} if args['action'] in {'run', 'preview', 'cancel', 'stop', 'handoff'} else set()
        if args['action'] in {'run', 'preview'} and 'card_id' in args:
            validate_card_id(args['card_id'])
            allowed.add('card_id')
        if args['action'] == 'handoff':
            allowed.add('recipient_session')
            SessionOwner(ctx.profile_name, args.get('recipient_session'))
        generation = args.get('expected_claim_generation')
        if ('expected_claim_generation' in args or args['action'] in {'cancel', 'stop', 'handoff'}) and (
                type(generation) is not int or generation < (1 if args['action'] in {'cancel', 'stop', 'handoff'} else 0)):
            raise ValueError('claim_generation_required')
        return {key: value for key, value in args.items() if key not in allowed}

    def run_approval(tool_name='', args=None, **metadata):
        if tool_name != 'ios_iteration' or not isinstance(args, dict) or args.get('action') not in {'run', 'preview', 'handoff'}:
            return None
        try:
            runtime = registration.runtime()
            backend, runtime_authority = runtime['backend'], runtime['authority']
            key = request_key(metadata)
            identity = backend._validate({**target_arguments(args), 'action': 'run'})[1]
            current = runtime_authority.conversation_claim(identity)
            generation = args.get('expected_claim_generation', current.generation if current else 0)
            fingerprint = runtime_authority.build_fingerprint(identity) if args['action'] == 'run' else None
            with approval_lock:
                disclosure = (identity.target_fingerprint, fingerprint, args['action'], generation, args.get('recipient_session'), args.get('card_id'))
                previous = approvals.get(key)
                if previous is not None and previous != disclosure:
                    # A reused request identity must never replace its disclosure.
                    approvals[key] = False
                    raise ValueError('request_disclosure_conflict')
                approvals[key] = disclosure
        except (ValueError, OSError) as error:
            return {'action': 'block', 'message': str(error)}
        if args['action'] == 'handoff':
            source = json.dumps(key.owner.session_id)
            recipient = json.dumps(args['recipient_session'])
            profile = json.dumps(key.owner.profile)
            return {'action': 'approve', 'message':
                f"Transfer device coordination from {profile}/{source} to "
                f"{profile}/{recipient} for {identity.udid}, {json.dumps(identity.worktree)}, "
                f"{json.dumps(identity.project)}, scheme {identity.scheme}, configuration {identity.configuration}, "
                f"runtime {identity.runtime}, target {identity.target_fingerprint}, generation {generation}. "
                "No build, launch, server start or running app reassignment.",
                'rule_key': 'hermes-ios:handoff:' + json.dumps([
                    key.owner.profile, key.owner.session_id, args['recipient_session'],
                    identity.target_fingerprint, generation])}
        if args['action'] == 'preview':
            return {'action': 'approve', 'message':
                f"Start/reuse pinned serve-sim for {identity.udid} in {json.dumps(identity.worktree)}. "
                "Local unauthenticated simulator stream/HID/start/shutdown routes expose device control. Shell execution has token and Origin checks, but the unauthenticated preview page discloses the token. Origin checks do not constrain non-browser local clients, and stock Host validation is missing. Malicious browser content remains a risk. Loopback is not authorization; trusted-local use is not a sandbox. "
                "No build, install, launch, boot, erase or reset.",
                'rule_key': 'hermes-ios:preview:' + identity.target_fingerprint}
        return {'action': 'approve', 'message':
            f"Build scripts and dependencies are executable code. Build {json.dumps(identity.project)}, "
            f"scheme {identity.scheme}, configuration {identity.configuration}; install and launch on "
            f"{identity.udid}, preserving app data; start/reuse pinned stock server. "
            f"Selected build-input fingerprint: {fingerprint}. Dependency directories and external inputs are not fully fingerprinted. No erase/uninstall/reset.",
            'rule_key': 'hermes-ios:run:' + identity.target_fingerprint + ':' + fingerprint}
    def forget_request(tool_name='', **metadata):
        if tool_name == 'ios_iteration':
            try:
                key = request_key(metadata)
            except ValueError:
                return
            with approval_lock:
                approvals.pop(key, None)

    # Post-call runs for host denials too; a denied disclosure is not a ticket
    # for a later request and must not accumulate in registration memory.
    ctx.register_hook('post_tool_call', forget_request)
    ctx.register_hook('pre_tool_call', run_approval)

    def bind_approved_request(tool_name, args, next_call, **metadata):
        if tool_name != 'ios_iteration' or args.get('action') not in {'run', 'preview', 'handoff', 'cancel', 'stop', 'card'}:
            return next_call(args)
        try:
            registration.runtime()
            key = request_key(metadata)
        except (ValueError, OSError) as error:
            return json.dumps({'ok': False, 'error': str(error)})
        # The agent executor wraps approval inside tool_execution; direct SDK
        # dispatch places approval before it. Carry identity only: the handler
        # consumes its disclosure after the host has admitted this request.
        token = approved_request.set({'key': key, 'consumed': False})
        try:
            return next_call(args)
        finally:
            approved_request.reset(token)

    ctx.register_middleware('tool_execution', bind_approved_request)

    async def ios_iteration(runtime, args: dict, **_kwargs) -> str:
        backend, runtime_authority = runtime['backend'], runtime['authority']
        stock_runtime, cards, data_dir = runtime['stock_runtime'], runtime['cards'], runtime['data_dir']
        fingerprint = None
        action = args.get('action')
        if action == 'card':
            try:
                request = approved_request.get()
                if request is None or request['consumed']:
                    raise ValueError('card_authenticated_request_required')
                request['consumed'] = True
                if set(args) - {'action', 'card_action', 'card_id', 'worktree', 'project', 'scheme', 'configuration', 'runtime', 'udid'}:
                    raise ValueError('unsupported_argument')
                card_action = args.get('card_action')
                if card_action is not None and card_action not in {'run', 'preview'}:
                    raise ValueError('invalid_card_action')
                target_args = {name: value for name, value in args.items() if name not in {'card_action', 'card_id'}}
                identity = backend._validate({**target_args, 'action': 'run'})[1]
                if 'card_id' in args:
                    result = cards.resolve(args['card_id'], request['key'], identity, card_action)
                elif card_action is not None:
                    result = cards.issue(request['key'], identity, card_action)
                else:
                    raise ValueError('card_action_required')
                return json.dumps(result)
            except (ValueError, OSError, TypeError) as error:
                return json.dumps({'ok': False, 'error': str(error)})
        if action not in {'run', 'preview', 'handoff', 'cancel', 'stop'}:
            return await handle_tool(backend, args, authority=runtime_authority,
                stock_runtime=stock_runtime, data_dir=data_dir)
        try:
            clean_args = target_arguments(args)
            identity = backend._validate({**clean_args, 'action': 'run'})[1]
            request = approved_request.get()
            if request is None or request['consumed']:
                raise ValueError('request_approval_required')
            request['consumed'] = True
            key = request['key']
            if action in {'cancel', 'stop'}:
                claim = DeviceClaim(key.owner, identity, args['expected_claim_generation'])
                token = runtime_authority.active_claim.set(claim)
                try:
                    return await handle_tool(backend, clean_args, authority=runtime_authority,
                        stock_runtime=stock_runtime, data_dir=data_dir)
                finally:
                    runtime_authority.active_claim.reset(token)
            with approval_lock:
                disclosure = approvals.pop(key, None)
            if not disclosure:
                raise ValueError('request_approval_required')
            target, fingerprint, disclosed_action, generation, recipient, disclosed_card_id = disclosure
            if disclosed_card_id != args.get('card_id'):
                raise ValueError('request_disclosure_conflict')
            if target != identity.target_fingerprint:
                raise ValueError('request_target_mismatch')
            if disclosed_action != action:
                raise ValueError('request_action_mismatch')
            if recipient != args.get('recipient_session') or args.get('expected_claim_generation', generation) != generation:
                raise ValueError('request_disclosure_conflict')
            if args.get('card_id') is not None:
                cards.check(args['card_id'], key, identity, action, generation)
        except (ValueError, OSError) as error:
            return json.dumps({'ok': False, 'error': str(error)})

        def admit(lease):
            lease.check()
            if action == 'run' and runtime_authority.build_fingerprint(identity) != fingerprint:
                raise ValueError('build_authorization_mismatch')
            if args.get('card_id') is not None:
                cards.consume(args['card_id'], key, identity, action, generation)
                request['card_consumed'] = True
            claim = runtime_authority.admit_conversation(key, identity, generation, recipient_session=recipient)
            request['admitted_identity'] = identity
            if action == 'run':
                runtime_authority.authorize(identity, fingerprint)
            return claim

        if action == 'handoff':
            try:
                claim = await stock_runtime.run_async(identity, admit, bounded_lock=True)
                return json.dumps({'ok': True, 'action': action, 'claim': asdict(claim)})
            except (ValueError, OSError) as error:
                return json.dumps({'ok': False, 'error': str(error)})
        if action == 'preview':
            # External builds are legitimate; preview starts only the shared
            # stock owner and never manufactures a coordinator build receipt.
            from .agent.native_runtime import _checked, _simulator_state, _SIMCTL_QUERY_TIMEOUT_SECONDS
            from .prerequisites import preflight as preview_preflight
            try:
                check = await asyncio.to_thread(preview_preflight, Path(identity.worktree), data_dir)
                if not check.get('ready'):
                    return json.dumps({'ok': False, 'error': 'prerequisites_required', 'stage': 'preflight', 'preflight': check})
                payload = await _checked(('/usr/bin/xcrun', 'simctl', 'list', 'devices', 'available', '--json'),
                    asyncio.Event(), timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS)
                if _simulator_state(payload, identity.udid, runtime=identity.runtime) != 'Booted':
                    raise ValueError('simulator_not_booted')
                def preview(lease):
                    claim = admit(lease)
                    lease.check()
                    server = stock_runtime.ensure_server(lease)
                    if args.get('card_id') is not None:
                        request['card_operation_id'] = lease.operation_id
                    return claim, server
                claim, server = await stock_runtime.run_async(identity, preview)
                return json.dumps({'ok': True, 'action': 'preview', 'target': asdict(identity),
                    'claim': asdict(claim),
                    'server': server, 'preview_url': server['url'],
                    'preview_action': {'tool': 'desktop_preview',
                        'arguments': {'action': 'open', 'url': server['url']}},
                    'preview': {'open_requested': False, 'visible_pixels': 'unknown'}})
            except (ValueError, OSError, RuntimeError) as error:
                return json.dumps({'ok': False, 'error': str(error)})
        try:
            claim = await stock_runtime.run_async(identity, admit)
            token = runtime_authority.active_claim.set(claim)
            try:
                result = json.loads(await handle_tool(backend, clean_args, authority=runtime_authority,
                    stock_runtime=stock_runtime, data_dir=data_dir,
                    approved_build_fingerprint=fingerprint))
                return json.dumps({**result, 'claim': asdict(claim)})
            finally:
                runtime_authority.active_claim.reset(token)
        except (ValueError, OSError) as error:
            return json.dumps({'ok': False, 'error': str(error)})

    async def contextual_iteration(args: dict, **kwargs) -> str:
        try:
            runtime = registration.runtime()
        except RegistrationUnavailable as error:
            return json.dumps({'ok': False, 'error': str(error)})
        cards = runtime['cards']
        result = json.loads(await ios_iteration(runtime, args, **kwargs))
        request = approved_request.get()
        if request is not None and request.get('card_consumed'):
            try:
                result['card'] = cards.finish(args['card_id'], request['key'], result,
                    preview_operation_id=request.get('card_operation_id'))
            except (ValueError, OSError) as error:
                result['card_status_error'] = str(error)
                result['card_replay_allowed'] = False
        if (result.get('ok') is True and args.get('action') in {'run', 'preview'}
                and request is not None and request.get('admitted_identity') is not None
                and result.get('preview_action', {}).get('tool') == 'desktop_preview'):
            try:
                view = cards.issue(request['key'], request['admitted_identity'], 'preview')
                result['app_card'] = {'status': 'prepared', 'view': view}
            except Exception as error:
                code = str(error) if isinstance(error, ValueError) else ''
                if code not in {'card_capacity_reached', 'card_stale', 'card_state_malformed', 'claim_owner_conflict'}:
                    code = 'app_card_preparation_failed'
                result['app_card'] = {'status': 'unavailable', 'code': code}
        return json.dumps(result)

    ctx.register_tool(
        name="ios_iteration",
        toolset="hermes_ios",
        schema=IOS_ITERATION_SCHEMA,
        handler=contextual_iteration,
        is_async=True,
        description=IOS_ITERATION_SCHEMA["description"],
        emoji="📱",
    )
    ctx.register_skill(
        "ios-development",
        Path(__file__).parent / "skills" / "ios-development" / "SKILL.md",
        "Native iOS build choices, shared preview lifecycle and Browser feedback workflow.",
    )
    registration.initialize()
