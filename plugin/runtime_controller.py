from __future__ import annotations

from typing import Callable, Literal
from .agent.native_plan import RuntimeIdentity
from .runtime_process import ServeSimProcessManager
from . import runtime_handoff, runtime_rendezvous

import asyncio
import threading
import uuid
import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass

from .runtime_state import LockTimeout


class StockOperationRetired(ValueError):
    def __init__(self, cause: Literal['lease_cancelled', 'controller_retired']):
        if cause not in ('lease_cancelled', 'controller_retired'):
            raise ValueError('stock_retirement_cause_invalid')
        super().__init__('stock_operation_retired')
        self.cause = cause


@dataclass(frozen=True)
class StockOperationLease:
    identity: RuntimeIdentity
    operation_id: str
    generation: int
    cancel: threading.Event

    def check(self) -> None:
        if self.cancel.is_set():
            raise StockOperationRetired('lease_cancelled')


class StockRuntimeController:
    """One shared lifecycle seam for U3 tool/native-control callers.

    run/run_async hold the device lease for the entire supplied mutation, not
    merely server start. The worker must honor lease.cancel in native drain.
    No build authority, launch protocol, Browser open or image claim is added.

    Unload (close with handoff_seconds) retires admission only: already-admitted
    work keeps running with this registration's process manager until it leaves,
    and the last one out pays the owed release. Explicit Cancel/Stop, deadlines
    and caller cancellation still set lease.cancel. A hard close (no
    handoff_seconds) cancels and retires admitted work as before.
    """
    def __init__(self, *, process_manager: ServeSimProcessManager, profile: str):
        self.process_manager = process_manager
        self.profile = profile
        self._condition = threading.Condition()
        self._session = threading.RLock()
        self._active: dict[str, StockOperationLease] = {}
        self._generation = 1
        self._closed = False
        # Handoff seconds of a close a drain timeout deferred; the last retired operation pays it.
        self._owed_close: tuple[float | None] | None = None
        self.last_cleanup: list[dict] = []
        self._handoff_scope = None
        self._dispose_handoffs = None

    def watch_handoffs(self) -> None:
        with self._condition:
            if self._closed or self.process_manager._closed.is_set():
                return
            if self._handoff_scope is None:
                self._handoff_scope = runtime_rendezvous.scope(self.process_manager, epoch=_CODE_EPOCH)
            if self._handoff_scope is not None and self._dispose_handoffs is None:
                self._dispose_handoffs = runtime_rendezvous.subscribe(self._handoff_scope, self._adopt_handoffs)
        if self._handoff_scope is not None:
            runtime_rendezvous.request_scan(self._handoff_scope)
        else:
            self._adopt_handoffs()

    def _adopt_handoffs(self) -> None:
        with self._condition:
            if self._closed or self.process_manager._closed.is_set():
                return
        for result in runtime_handoff.adopt(self.process_manager):
            logging.getLogger(__package__).info('Preview registration adoption %s', result)

    def _publish_releases(self, results: list[dict]) -> None:
        if self._handoff_scope is not None and any(result.get('reason') in ('handoff_pending', 'handoff_detached')
                                                   for result in results):
            runtime_rendezvous.request_scan(self._handoff_scope)

    def ensure_server(self, lease: StockOperationLease) -> dict:
        lease.check()
        result = self.process_manager.ensure(lease.identity, cancel=lease.cancel)
        lease.check()
        return result

    @contextmanager
    def _session_lock(self, timeout: float | None):
        if not self._session.acquire(timeout=-1 if timeout is None else timeout):
            raise LockTimeout('stock_session_lock_timeout')
        try:
            yield
        finally:
            self._session.release()

    def run(self, identity: RuntimeIdentity, mutation: Callable, *, cancel: threading.Event | None = None,
            bounded_lock: bool = False):
        """bounded_lock (Stop) reports LockTimeout instead of waiting on a lock holder forever."""
        with self._condition:
            if self._closed:
                raise ValueError('stock_lifecycle_closed')
            if identity.profile != self.profile:
                raise ValueError('stock_profile_mismatch')
            lease = StockOperationLease(identity, uuid.uuid4().hex, self._generation, cancel or threading.Event())
            self._active[lease.operation_id] = lease
        releases = []
        try:
            # One local target session, with a UDID lock shared across processes.
            store = self.process_manager.store
            timeout = store.cleanup_lock_seconds if bounded_lock else None
            with self._session_lock(timeout), store.device_lock(identity.udid, timeout=timeout):
                lease.check()
                before = self.process_manager.store.server_snapshot(identity.udid)
                try:
                    result = mutation(lease)
                    with self._condition:
                        # Unload closes admission only; a hard close bumps the generation.
                        if lease.generation != self._generation:
                            raise StockOperationRetired('controller_retired')
                        lease.check()
                        return result
                except BaseException:
                    current = self.process_manager.store.server_snapshot(identity.udid)
                    if current and (before is None or current['generation'] != before['generation']):
                        self.process_manager.cleanup(current, trigger='mutation_failure')
                    raise
                finally:
                    # Still under the device lock, so a waiting successor sees the handoff, not the gap.
                    releases.extend(self._leave(lease))
        finally:
            releases.extend(self._leave(lease))
            self._publish_releases(releases)

    def _leave(self, lease: StockOperationLease) -> list[dict]:
        with self._condition:
            self._active.pop(lease.operation_id, None)
            self._condition.notify_all()
            owed = None
            if not self._active:
                owed, self._owed_close = self._owed_close, None
        if owed is not None:
            # No retired mutation can act now, so the generation may finally be handed off.
            results = self.process_manager.close(handoff_seconds=owed[0])
            self.last_cleanup = results
            return results
        return []

    async def run_async(self, identity: RuntimeIdentity, mutation: Callable, *, bounded_lock: bool = False):
        cancel = threading.Event()
        # Shield: cancelling the coroutine is not cancelling/draining its worker.
        # Return worker errors as data until the shield has settled: some Python
        # versions log a cancelled shield's later exception even after retrieval.
        def execute():
            try:
                return True, self.run(identity, mutation, cancel=cancel, bounded_lock=bounded_lock)
            except BaseException as exc:
                return False, exc
        worker = asyncio.create_task(asyncio.to_thread(execute))
        try:
            ok, result = await asyncio.shield(worker)
            if not ok:
                raise result
            return result
        except asyncio.CancelledError:
            cancel.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if worker.done() and not worker.cancelled():
                worker.exception()
            raise

    def observe(self, identity: RuntimeIdentity) -> dict | None:
        return self.process_manager.observe(identity)

    def close(self, *, drain_seconds: float = 10.0, handoff_seconds: float | None = None) -> list[dict]:
        """Synchronous public on_unload callback; returns without waiting on admitted work.

        With handoff_seconds (unload) no new operation is admitted, admitted ones
        are not cancelled, and a ready server is released (detached, or leased with
        that fallback deadline), not stopped. While admitted work remains, its
        generation stays owned by this retired registration; the last operation to
        leave pays the deferred release.

        Without handoff_seconds (hard close) admitted work is cancelled. If a U3
        mutation ignores cancellation, report an unresolved obligation; do not
        claim task cancellation or callback return is verified cleanup.
        """
        import time
        with self._condition:
            self._closed = True
            if self._dispose_handoffs is not None:
                self._dispose_handoffs()
                self._dispose_handoffs = None
            if handoff_seconds is not None and self._active:
                self._owed_close = (handoff_seconds,)
                self.last_cleanup = [{'server_stopped': False, 'reason': 'stock_release_deferred',
                                      'operation_ids': list(self._active)}]
                return self.last_cleanup
            if handoff_seconds is None:
                self._generation += 1
                for lease in self._active.values():
                    lease.cancel.set()
                self.process_manager.retire()
            deadline = time.monotonic() + drain_seconds
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._owed_close = (handoff_seconds,)
                    self.last_cleanup = [{'server_stopped': False, 'reason': 'stock_operation_drain_timeout',
                                          'operation_ids': list(self._active)}]
                    return self.last_cleanup
                self._condition.wait(remaining)
        results = self.process_manager.close(handoff_seconds=handoff_seconds)
        self.last_cleanup = results
        self._publish_releases(results)
        return results


_CODE_EPOCH = runtime_rendezvous.code_epoch(
    sys.modules[__name__], runtime_rendezvous, runtime_handoff,
    sys.modules[ServeSimProcessManager.__module__], sys.modules[LockTimeout.__module__])
