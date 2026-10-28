from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_state import RuntimeStateStore


class U8InterruptedWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()


    def test_runtime_state_replace_failure_preserves_previous_committed_record(self) -> None:
        path = self.root / "runtime" / "runtime-state.json"
        store = RuntimeStateStore(path)
        worktree = self.root / "worktree"
        project = worktree / "Fixture.xcodeproj"
        project.mkdir(parents=True)
        identity = build_runtime_identity(
            profile="test-profile",
            worktree=worktree,
            project=project,
            scheme="Fixture",
            configuration="Debug",
            runtime="iOS Simulator",
            udid="00000000-0000-0000-0000-000000000001",
        )
        store.reconcile_target(identity, "sha256:first", now=1.0)
        before = path.read_bytes()

        with patch("plugin.runtime_state.os.replace", side_effect=OSError("injected replace failure")):
            with self.assertRaisesRegex(OSError, "injected replace failure"):
                store.reconcile_target(identity, "sha256:second", now=2.0)

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_malformed_authority_files_fail_closed_without_repairing_or_overwriting_them(self) -> None:
        runtime_path = self.root / "runtime" / "runtime-state.json"
        runtime_path.parent.mkdir(parents=True)
        runtime_path.write_text("[]", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "runtime_state_malformed"):
            RuntimeStateStore(runtime_path)._read()
        self.assertEqual(runtime_path.read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()
