from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from plugin.agent.native_plan import build_runtime_identity
from plugin.conversation_claims import (ClaimBook, ClaimRefused, ClaimRequest,
    HandoffApproval, RequestKey, SessionOwner, claim_device)
from plugin.ownership_state import load_book, save_book


class OwnershipStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.target = build_runtime_identity(profile='fixture', worktree=root,
            project=root / 'Fixture.xcodeproj', scheme='Fixture', configuration='Debug',
            runtime='iOS fixture', udid='00000000-0000-4000-8000-000000000101')
        self.alice = SessionOwner('fixture', 'alice')
        self.bob = SessionOwner('fixture', 'bob')
        request = ClaimRequest(RequestKey(self.alice, '', '', 'initial'), self.target, 0)
        self.first = claim_device(ClaimBook(), request)
        key = RequestKey(self.bob, '', '', 'transfer')
        self.transfer = ClaimRequest(key, self.target, 1,
            HandoffApproval(key, self.first.claim, self.target, self.alice, True))
        self.current = claim_device(self.first.book, self.transfer)
        self.other = build_runtime_identity(profile='fixture', worktree=root,
            project=root / 'Fixture.xcodeproj', scheme='Other', configuration='Debug',
            runtime='iOS fixture', udid='00000000-0000-4000-8000-000000000102')

    def encoded(self, book):
        data = {}
        save_book(data, book)
        return json.loads(json.dumps(data))

    def test_current_transition_loads_without_historical_receipts(self):
        data = self.encoded(replace(self.current.book, receipts=self.current.book.receipts[-1:]))
        loaded = load_book(data)
        self.assertEqual(loaded.claims, (self.current.claim,))
        self.assertEqual(len(loaded.receipts), 1)
        self.assertTrue(claim_device(loaded, self.transfer).replayed)
        with self.assertRaisesRegex(ClaimRefused, 'claim_stale_generation'):
            claim_device(loaded, self.first.book.receipts[0].request)

    def test_reuse_serialization_stays_bounded_and_preserves_other_device(self):
        book = claim_device(self.first.book,
            ClaimRequest(RequestKey(self.bob, '', '', 'other'), self.other, 0)).book
        original = self.encoded(book)
        for index in range(1000):
            book = claim_device(load_book(self.encoded(book)),
                ClaimRequest(RequestKey(self.alice, '', '', f'reuse-{index}'), self.target, 1)).book
        self.assertEqual(self.encoded(book), original)
        self.assertEqual(len(book.claims), len(book.receipts))

    def test_malformed_current_claim_receipt_links_fail_closed(self):
        good = self.encoded(replace(self.current.book, receipts=self.current.book.receipts[-1:]))
        changes = (
            lambda row: row.update(version=1),
            lambda row: row.update(receipts=[]),
            lambda row: row['claims'].append(copy.deepcopy(row['claims'][0])),
            lambda row: row['receipts'].append(copy.deepcopy(row['receipts'][0])),
            lambda row: row['claims'][0].update(generation=True),
            lambda row: row['claims'][0]['owner'].update(profile='other'),
            lambda row: row['receipts'][0]['claim'].update(generation=3),
            lambda row: row['receipts'][0]['request'].update(expected_generation=0),
            lambda row: row['receipts'][0]['request']['key']['owner'].update(session_id='wrong'),
            lambda row: row['receipts'][0]['request'].update(target=asdict(self.other)),
            lambda row: row['receipts'][0]['request']['handoff'].update(approved=False),
            lambda row: row['receipts'][0]['request']['handoff']['previous'].update(generation=2),
            lambda row: row['receipts'][0]['request']['handoff']['previous'].update(target=asdict(self.other)),
            lambda row: row['receipts'][0]['request']['handoff'].update(target=asdict(self.other)),
            lambda row: row['receipts'][0]['request']['handoff']['approving_owner'].update(session_id='wrong'),
            lambda row: row['receipts'][0]['request']['handoff']['request_key'].update(tool_call_id='wrong'),
            lambda row: row['receipts'][0]['request']['handoff']['previous']['owner'].update(session_id='bob'),
        )
        for change in changes:
            data = copy.deepcopy(good)
            change(data['ownership'])
            before = copy.deepcopy(data)
            with self.subTest(data=data), self.assertRaisesRegex(ClaimRefused, 'claim_state_malformed'):
                load_book(data)
            self.assertEqual(data, before)
