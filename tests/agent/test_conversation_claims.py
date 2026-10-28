from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from plugin.agent.native_plan import build_runtime_identity
from plugin.conversation_claims import (
    ClaimBook, ClaimRefused, ClaimRequest, HandoffApproval, RequestKey, SessionOwner,
    authorize_mutation, claim_device,
)
from plugin.runtime_state import LockTimeout, RuntimeStateStore


class LockedClaimFixture:
    """In-memory fixture, not durable production claim storage."""

    def __init__(self, root: Path):
        self.store = RuntimeStateStore(root / 'runtime-state.json')
        self.book = ClaimBook()
        self.effects = []
        self.ledger_lock = threading.Lock()

    def claim(self, request, *, timeout=None):
        with self.store.device_lock(request.target.udid, timeout=timeout), self.ledger_lock:
            decision = claim_device(self.book, request)
            self.book = decision.book
            return decision

    def mutate(self, owner, target, generation, action, effect=None):
        with self.store.device_lock(target.udid):
            with self.ledger_lock:
                claim = authorize_mutation(self.book, owner=owner, target=target, generation=generation)
            if effect is not None:
                effect()
            self.effects.append((action, claim))
            return claim


class ConversationClaimsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.alice = SessionOwner('isolated', 'session-a')
        self.bob = SessionOwner('isolated', 'session-b')
        self.target = build_runtime_identity(
            profile='isolated', worktree=self.root / 'repo',
            project=self.root / 'repo' / 'Fixture.xcodeproj', scheme='Fixture',
            configuration='Debug', runtime='iOS fixture',
            udid='00000000-0000-4000-8000-000000000101',
        )
        self.other = replace(self.target, udid='00000000-0000-4000-8000-000000000102',
                             target_fingerprint='sha256:other', domain_key='domain:other')
        self.fixture = LockedClaimFixture(self.root)

    def request(self, owner=None, *, call='call-1', target=None, generation=0, handoff=None):
        return ClaimRequest(RequestKey(owner or self.alice, 'task', 'turn', call),
                            target or self.target, generation, handoff)

    def transfer(self, current, *, owner=None, target=None, call='handoff', approved=True):
        request = self.request(owner or self.bob, call=call, target=target,
                               generation=current.generation)
        approval = HandoffApproval(request.key, current, request.target, current.owner, approved)
        return replace(request, handoff=approval)

    def assert_refused(self, code, function, *args, **kwargs):
        with self.assertRaises(ClaimRefused) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_authenticated_host_metadata_requires_session_and_call(self):
        valid = {'session_id': 'session-a', 'tool_call_id': 'call'}
        key = RequestKey.from_host_metadata('isolated', valid)
        self.assertEqual(key, RequestKey(self.alice, '', '', 'call'))
        for field in ('session_id', 'tool_call_id'):
            for value in (None, '', ' ', 1, True):
                with self.subTest(field=field, value=value):
                    self.assert_refused(
                        'claim_authenticated_session_required' if field == 'session_id'
                        else 'claim_request_metadata_required',
                        RequestKey.from_host_metadata, 'isolated', {**valid, field: value},
                    )
        for field in ('task_id', 'turn_id'):
            for value in (0, False, {}, []):
                with self.subTest(field=field, value=value):
                    self.assert_refused('claim_request_metadata_required', RequestKey.from_host_metadata,
                                        'isolated', {**valid, field: value})

    def test_card_or_filesystem_data_cannot_supply_missing_host_session(self):
        for metadata in ({'owner': 'session-a', 'tool_call_id': 'call'},
                         {'worktree': str(self.root), 'tool_call_id': 'call'},
                         {'conversation_id': 'session-a', 'tool_call_id': 'call'}):
            self.assert_refused('claim_authenticated_session_required', RequestKey.from_host_metadata,
                                'isolated', metadata)

    def test_session_task_turn_call_and_profile_are_distinct_request_keys(self):
        key = self.request().key
        variants = (replace(key, owner=self.bob), replace(key, task_id='other'),
                    replace(key, turn_id='other'), replace(key, tool_call_id='other'),
                    replace(key, owner=SessionOwner('other-profile', 'session-a')))
        self.assertEqual(len({key, *variants}), 6)

    def test_first_claim_and_repeated_same_target_have_one_generation(self):
        initial = ClaimBook()
        first = claim_device(initial, self.request())
        self.assertEqual(initial, ClaimBook())
        self.assertEqual((first.claim.owner, first.claim.target, first.claim.generation),
                         (self.alice, self.target, 1))
        again = claim_device(first.book, self.request(call='call-2', generation=1))
        self.assertEqual(again.claim, first.claim)
        self.assertFalse(again.replayed)
        self.assertEqual(len(again.book.claims), 1)
        with self.assertRaises(FrozenInstanceError):
            again.claim.generation = 20

    def test_two_sessions_sequentially_conflict_before_mutation_or_target_reassignment(self):
        first = self.fixture.claim(self.request())
        before = self.fixture.book
        changed_target = replace(self.target, scheme='Other')
        self.assert_refused('claim_owner_conflict', self.fixture.claim,
                            self.request(self.bob, target=changed_target, generation=1))
        self.assert_refused('claim_owner_conflict', self.fixture.mutate,
                            self.bob, self.target, 1, 'ensure-reassign')
        self.assertEqual(self.fixture.book, before)
        self.assertEqual(self.fixture.effects, [])
        self.assertEqual(first.claim.target, self.target)
        self.fixture.mutate(self.alice, self.target, 1, 'native')
        self.assertEqual(self.fixture.effects, [('native', first.claim)])

    def test_thousands_of_reuses_keep_only_current_device_transitions(self):
        first = self.fixture.claim(self.request()).claim
        other = self.fixture.claim(self.request(self.bob, target=self.other)).claim
        before = self.fixture.book
        for index in range(3000):
            again = self.fixture.claim(self.request(call=f'reuse-{index}', generation=1))
            self.assertEqual(again.claim, first)
            self.assertEqual(again.book, before)
        self.assertEqual(len(self.fixture.book.receipts), 2)
        retarget = self.request(call='retarget', generation=1,
                                target=replace(self.target, scheme='Other'))
        changed = self.fixture.claim(retarget)
        self.assertEqual(changed.claim.generation, 2)
        self.assertTrue(self.fixture.claim(retarget).replayed)
        self.assert_refused('claim_stale_generation', self.fixture.claim, self.request())
        self.assertEqual(len(changed.book.receipts), 2)
        self.assertEqual(changed.book.for_target(self.other), other)

    def test_two_sessions_concurrently_claim_device_with_one_winner(self):
        barrier = threading.Barrier(2)

        def compete(owner):
            barrier.wait(timeout=5)
            try:
                return self.fixture.claim(self.request(owner)).claim
            except ClaimRefused as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, (self.alice, self.bob)))
        self.assertEqual(results.count('claim_stale_generation'), 1)
        winner = next(result for result in results if not isinstance(result, str))
        self.assertEqual(winner.generation, 1)
        self.assertEqual(self.fixture.book.claims, (winner,))
        self.assertEqual(self.fixture.effects, [])

    def test_declined_transfer_preserves_owner_target_and_unrelated_device(self):
        first = self.fixture.claim(self.request()).claim
        other = self.fixture.claim(self.request(self.bob, target=self.other)).claim
        before = self.fixture.book
        declined = self.transfer(first, target=replace(self.target, scheme='Other'), approved=False)
        self.assert_refused('claim_handoff_declined', self.fixture.claim, declined)
        self.assertEqual(self.fixture.book, before)
        self.assertEqual(self.fixture.book.for_target(self.target), first)
        self.assertEqual(self.fixture.book.for_target(self.other), other)
        self.assertEqual(self.fixture.effects, [])

    def test_accepted_transfer_requires_exact_prior_owner_generation_and_destination(self):
        first = self.fixture.claim(self.request()).claim
        request = self.transfer(first, target=replace(self.target, scheme='Other'))
        bad_approvals = (
            replace(request.handoff, previous=replace(first, generation=2)),
            replace(request.handoff, previous=replace(first, owner=self.bob)),
            replace(request.handoff, approving_owner=self.bob),
            replace(request.handoff, request_key=replace(request.key, tool_call_id='different')),
            replace(request.handoff, target=self.target),
        )
        before = self.fixture.book
        for approval in bad_approvals:
            with self.subTest(approval=approval):
                self.assert_refused('claim_handoff_mismatch', self.fixture.claim,
                                    replace(request, handoff=approval))
                self.assertEqual(self.fixture.book, before)
        transferred = self.fixture.claim(request).claim
        self.assertEqual((transferred.owner, transferred.target, transferred.generation),
                         (self.bob, request.target, 2))
        self.fixture.mutate(self.bob, request.target, 2, 'native')
        self.assertEqual(self.fixture.effects, [('native', transferred)])

    def test_same_owner_retarget_changes_generation_and_invalidates_old_stop(self):
        first = self.fixture.claim(self.request()).claim
        target = replace(self.target, configuration='Release')
        second = self.fixture.claim(self.request(call='retarget', target=target, generation=1)).claim
        self.assertEqual((second.owner, second.target, second.generation), (self.alice, target, 2))
        self.assert_refused('claim_stale_generation', self.fixture.mutate,
                            self.alice, first.target, 1, 'stop')
        self.assert_refused('claim_target_mismatch', self.fixture.mutate,
                            self.alice, first.target, 2, 'stop')
        self.assertEqual(self.fixture.effects, [])
        self.fixture.mutate(self.alice, target, 2, 'stop')
        self.assertEqual(self.fixture.effects, [('stop', second)])
        self.assertEqual(self.fixture.book.for_target(target), second)

    def test_stale_transfer_and_stop_cannot_touch_successor(self):
        first = self.fixture.claim(self.request()).claim
        transfer = self.transfer(first)
        second = self.fixture.claim(transfer).claim
        self.assert_refused('claim_stale_generation', self.fixture.claim,
                            self.transfer(first, call='late-transfer'))
        self.assert_refused('claim_stale_generation', self.fixture.mutate,
                            self.alice, self.target, first.generation, 'stop')
        self.assert_refused('claim_owner_conflict', self.fixture.mutate,
                            self.alice, self.target, second.generation, 'stop')
        self.assertEqual(self.fixture.effects, [])
        self.assertEqual(self.fixture.book.for_target(self.target), second)

    def test_transfer_wins_before_concurrent_old_stop_without_native_effect(self):
        first = self.fixture.claim(self.request()).claim
        transferred = threading.Event()

        def transfer():
            result = self.fixture.claim(self.transfer(first))
            transferred.set()
            return result.claim

        def old_stop():
            self.assertTrue(transferred.wait(timeout=5))
            self.assert_refused('claim_stale_generation', self.fixture.mutate,
                                self.alice, self.target, 1, 'stop')

        with ThreadPoolExecutor(max_workers=2) as pool:
            stop_future = pool.submit(old_stop)
            current = pool.submit(transfer).result(timeout=5)
            stop_future.result(timeout=5)
        self.assertEqual(self.fixture.book.for_target(self.target), current)
        self.assertEqual(self.fixture.effects, [])

    def test_claim_and_handoff_replays_are_idempotent(self):
        request = self.request()
        first = self.fixture.claim(request)
        replay = self.fixture.claim(request)
        self.assertTrue(replay.replayed)
        self.assertIs(replay.book, first.book)
        self.assertEqual(replay.claim.generation, 1)
        transfer = self.transfer(first.claim)
        second = self.fixture.claim(transfer)
        replay = self.fixture.claim(transfer)
        self.assertTrue(replay.replayed)
        self.assertIs(replay.book, second.book)
        self.assertEqual(replay.claim.generation, 2)
        self.assertEqual(len(replay.book.receipts), 1)

    def test_handoff_cannot_enter_during_authorized_mutation(self):
        first = self.fixture.claim(self.request()).claim
        entered, release = threading.Event(), threading.Event()

        def native_fixture():
            entered.set()
            self.assertTrue(release.wait(timeout=5))

        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.fixture.mutate, self.alice, self.target, 1, 'native', native_fixture)
            try:
                self.assertTrue(entered.wait(timeout=5))
                with self.assertRaises(LockTimeout):
                    self.fixture.claim(self.transfer(first), timeout=0.01)
                self.assertEqual(self.fixture.book.for_target(self.target), first)
            finally:
                release.set()
            self.assertEqual(running.result(timeout=5), first)
        second = self.fixture.claim(self.transfer(first)).claim
        self.assertEqual(second.generation, 2)
        self.assertEqual(self.fixture.effects, [('native', first)])

    def test_reused_request_key_cannot_change_target_device_generation_or_handoff(self):
        request = self.request()
        self.fixture.claim(request)
        for changed in (replace(request, target=replace(self.target, scheme='Other')),
                        replace(request, target=self.other), replace(request, expected_generation=1)):
            self.assert_refused('claim_request_replay_conflict', self.fixture.claim, changed)
        current = self.fixture.book.for_target(self.target)
        transfer = self.transfer(current)
        self.fixture.claim(transfer)
        self.assert_refused('claim_request_replay_conflict', self.fixture.claim,
                            replace(transfer, handoff=replace(transfer.handoff, approved=False)))

    def test_old_receipts_never_resurrect_claim_even_when_owner_returns(self):
        initial_request = self.request()
        first = self.fixture.claim(initial_request).claim
        transfer = self.transfer(first)
        second = self.fixture.claim(transfer).claim
        third = self.fixture.claim(self.transfer(second, owner=self.alice, call='return')).claim
        self.assertEqual(third.generation, 3)
        for old in (initial_request, transfer):
            self.assert_refused('claim_stale_generation', self.fixture.claim, old)
        self.assert_refused('claim_stale_generation', self.fixture.mutate,
                            self.alice, self.target, 1, 'stop')
        self.assertEqual(self.fixture.book.for_target(self.target), third)

    def test_each_runtime_identity_field_is_part_of_exact_target(self):
        first = self.fixture.claim(self.request()).claim
        for field in self.target.__dataclass_fields__:
            if field == 'udid':
                changed = self.other
                code = 'claim_required'
            else:
                changed = replace(self.target, **{field: getattr(self.target, field) + '-other'})
                code = 'claim_target_mismatch'
            with self.subTest(field=field):
                self.assert_refused(code, self.fixture.mutate, self.alice, changed, 1, 'native')
        self.assertEqual(self.fixture.book.for_target(self.target), first)
        self.assertEqual(self.fixture.effects, [])

    def test_unrelated_devices_survive_concurrent_claims_and_transfer(self):
        barrier = threading.Barrier(2)

        def claim(target):
            barrier.wait(timeout=5)
            return self.fixture.claim(self.request(call=target.udid, target=target)).claim

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, other = pool.map(claim, (self.target, self.other))
        self.fixture.claim(self.transfer(first))
        self.assertEqual(self.fixture.book.for_target(self.other), other)
        self.fixture.mutate(self.alice, self.other, 1, 'stop')
        self.assertEqual(self.fixture.effects, [('stop', other)])
        self.assertEqual(len(self.fixture.book.claims), 2)

    def test_device_key_is_case_insensitive_without_second_claim(self):
        self.fixture.claim(self.request())
        lower = replace(self.target, udid=self.target.udid.lower())
        self.assert_refused('claim_owner_conflict', self.fixture.claim,
                            self.request(self.bob, generation=1, target=lower))
        self.assertEqual(len(self.fixture.book.claims), 1)

    def test_mutation_without_claim_is_refused(self):
        self.assert_refused('claim_required', self.fixture.mutate,
                            self.alice, self.target, 1, 'stop')
        self.assertEqual(self.fixture.effects, [])

    def test_malformed_targets_profiles_generations_and_approvals_fail_closed(self):
        for udid in ('', '../device', 'all', 'booted', self.target.udid + '/extra'):
            self.assert_refused('claim_exact_udid_required', self.request,
                                target=replace(self.target, udid=udid))
        self.assert_refused('claim_profile_mismatch', self.request,
                            target=replace(self.target, profile='other'))
        for generation in (True, False, -1, 1.5, '1', None):
            self.assert_refused('claim_generation_required', self.request, generation=generation)
            self.assert_refused('claim_generation_required', self.fixture.mutate,
                                self.alice, self.target, generation, 'stop')
        current = self.fixture.claim(self.request()).claim
        self.assert_refused('claim_handoff_decision_required', self.transfer, current, approved='yes')
        self.assert_refused('claim_handoff_not_applicable', self.fixture.claim,
                            self.transfer(current, owner=self.alice, call='self-transfer'))


if __name__ == '__main__':
    unittest.main()
