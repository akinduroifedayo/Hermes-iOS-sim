from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from plugin.agent.ios_iteration import NativeOperation
from plugin.agent.native_runtime import (
    _SIMCTL_BOOT_STATUS_TIMEOUT_SECONDS,
    _SIMCTL_INSTALL_TIMEOUT_SECONDS,
    _SIMCTL_MUTATION_TIMEOUT_SECONDS,
    _SIMCTL_QUERY_TIMEOUT_SECONDS,
    _XCODEBUILD_TIMEOUT_SECONDS,
    _cleanup_partial_native,
    _parse_host_process_observation,
    _bundle_metadata,
    _simulator_state,
    native_runtime_dispatch,
)

class ExactProductFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, fail=None):
        import plistlib
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            derived = root / 'derived'
            product = derived / 'Build/Products/Release-iphonesimulator/Actual Product.app'
            product.mkdir(parents=True)
            (product / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'fixture.actual', 'CFBundleExecutable': 'Actual Product'}))
            (product / 'Actual Product').write_bytes(b'compiled-v1')
            (product.parent / 'Scheme.app').mkdir()
            installed = root / 'installed/Actual Product.app'
            sentinel = root / 'data/sentinel'
            sentinel.parent.mkdir()
            sentinel.write_text('keep preference')
            udid = '00000000-0000-0000-0000-000000000001'
            commands = []
            async def run(argv, cancel, *, timeout):
                commands.append(argv)
                if fail == 'building' and argv[-1] == 'build':
                    return 65, '', 'fixture.swift: compile diagnostic'
                if fail == 'booting' and 'bootstatus' in argv:
                    return 1, '', 'boot failed'
                if fail == 'installing' and 'install' in argv:
                    return 1, '', 'install failed'
                if fail == 'launching' and 'launch' in argv:
                    return 1, '', 'launch failed'
                if '-showBuildSettings' in argv:
                    return 0, json.dumps([{'buildSettings': {'PRODUCT_TYPE': 'com.apple.product-type.application',
                        'TARGET_BUILD_DIR': str(product.parent), 'FULL_PRODUCT_NAME': product.name}}]), ''
                if 'list' in argv:
                    return 0, json.dumps({'devices': {'com.apple.CoreSimulator.SimRuntime.iOS-26-0': [{'udid': udid, 'state': 'Booted'}]}}), ''
                if 'install' in argv:
                    installed.parent.mkdir(exist_ok=True)
                    shutil.copytree(Path(argv[-1]), installed, dirs_exist_ok=True)
                if 'get_app_container' in argv:
                    if fail == 'verify_install':
                        (installed / 'Actual Product').write_bytes(b'wrong executable')
                    return 0, str(installed), ''
                if 'launch' in argv:
                    return 0, 'fixture.actual: no pid' if fail == 'launch_pid' else 'fixture.actual: 321', ''
                if argv[0] == '/bin/ps':
                    if fail == 'verify_pid':
                        return 0, f'321 Wed Aug 19 11:06:53 2026 /unrelated/process', ''
                    return 0, f'321 Wed Aug 19 11:06:53 2026 {installed}/Actual Product', ''
                if 'procinfo' in argv:
                    return 0, f'program path = {installed}/Actual Product', ''
                return 0, '', ''
            argv = ('/usr/bin/xcodebuild', '-workspace', str(root / 'Exact.xcworkspace'), '-scheme', 'Scheme',
                '-configuration', 'Release', '-destination', f'platform=iOS Simulator,id={udid}',
                '-derivedDataPath', str(derived), 'CODE_SIGNING_ALLOWED=NO', 'build')
            op = NativeOperation('operation:fixture', 'domain:fixture', 'sha256:target', argv,
                'sha256:args', 'build:fixture', 'sha256:source', runtime='iOS 25.0' if fail == 'runtime_mismatch' else 'iOS 26.0')
            with patch('plugin.agent.native_runtime._run', new=run):
                result = await native_runtime_dispatch(op, asyncio.Event())
            self.assertEqual(sentinel.read_text(), 'keep preference')
            self.assertFalse(any(set(command) & {'erase', 'uninstall', 'reset', 'screenshot', 'shutdown'} for command in commands))
            self.assertEqual(commands[0], argv)
            if result.ok:
                self.assertIn(('/usr/bin/xcrun', 'simctl', 'install', udid, str(product.resolve())), commands)
                self.assertIn(('/usr/bin/xcrun', 'simctl', 'launch', '--terminate-running-process', udid, 'fixture.actual'), commands)
                self.assertEqual(commands[1], argv[:-1] + ('-showBuildSettings', '-json'))
            if fail in {'building', 'runtime_mismatch'}:
                self.assertFalse(any('install' in command or 'launch' in command for command in commands))
            return result
    async def test_exact_product_not_scheme_installs_and_preserves_data_without_frame_gate(self):
        result = await self.exercise()
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(result.evidence['launch']['outcome'], 'verified')
    async def test_failures_report_exact_stage_and_do_not_install_stale_build(self):
        for stage in ['building', 'booting', 'installing', 'launching']:
            with self.subTest(stage=stage):
                result = await self.exercise(stage)
                self.assertFalse(result.ok)
                self.assertEqual(result.evidence['stage'], stage)
                if stage == 'building':
                    self.assertIn('compile diagnostic', result.evidence['detail'])
    async def test_exact_runtime_mismatch_prevents_install_and_launch(self):
        result = await self.exercise('runtime_mismatch')
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'simulator_runtime_mismatch')
        self.assertEqual(result.evidence['stage'], 'booting')

    async def test_install_and_pid_corroboration_failures_are_not_verified_outcomes(self):
        from plugin.build_receipts import run_outcomes
        for fault, stage, axis in [('verify_install', 'verify_install', 'install'),
                                  ('launch_pid', 'launching', 'launch'), ('verify_pid', 'verify_pid', 'launch')]:
            with self.subTest(fault=fault):
                result = await self.exercise(fault)
                self.assertFalse(result.ok)
                self.assertEqual(result.evidence['stage'], stage)
                self.assertNotEqual(result.evidence[axis]['outcome'], 'verified')
                outcomes = run_outcomes(result.evidence, ok=False)
                self.assertEqual(outcomes[axis]['outcome'], 'failed')
                self.assertEqual(outcomes['build']['outcome'], 'verified')

class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_product_is_resolved_from_settings_not_scheme_named_stale_app(self) -> None:
        from plugin.agent.native_runtime import _single_app
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            products = root / 'Build/Products/Release-iphonesimulator'
            (products / 'Scheme.app').mkdir(parents=True)
            (products / 'ActualProduct.app').mkdir()
            settings = json.dumps([{'buildSettings': {
                'PRODUCT_TYPE': 'com.apple.product-type.application',
                'TARGET_BUILD_DIR': str(products), 'FULL_PRODUCT_NAME': 'ActualProduct.app'
            }}])
            self.assertEqual(_single_app(root, settings), (products / 'ActualProduct.app').resolve())

    def test_host_process_observation_parses_pid_start_generation_and_command(self) -> None:
        observation = _parse_host_process_observation(
            " 30403 Wed Aug 19 11:06:53 2026 /tmp/VisualIterationFixture\n",
            30403,
        )
        self.assertEqual(observation, {
            "pid": 30403,
            "process_generation": "Wed Aug 19 11:06:53 2026",
            "command": "/tmp/VisualIterationFixture",
        })
        self.assertIsNone(_parse_host_process_observation("", 30403))

    async def test_launch_records_host_process_generation_immediately_after_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "Fixture.app"
            app.mkdir()
            executable = app / "Fixture"
            executable.write_bytes(b"fixture")
            installed_app = root / "installed.app"
            installed_app.mkdir()
            installed_executable = installed_app / "Fixture"
            installed_executable.write_bytes(b"fixture")
            screenshot_count = 0

            async def checked(argv, _cancel_event, *, timeout=180.0):
                nonlocal screenshot_count
                del timeout
                if argv[0] == "/bin/ps":
                    return f" 30403 Wed Aug 19 11:06:53 2026 {installed_executable}\n"
                if "list" in argv:
                    return json.dumps({"devices": {"iOS": [{"udid": "DEVICE", "state": "Shutdown"}]}})
                if "get_app_container" in argv:
                    return str(installed_app)
                if "launch" in argv:
                    return "dev.hermes.Fixture: 30403"
                if "procinfo" in argv:
                    return f"program path = {installed_executable}\n"
                if "screenshot" in argv:
                    screenshot_count += 1
                    Path(argv[-1]).write_bytes(b"png")
                return ""

            operation = NativeOperation(
                operation_id="operation:generation",
                domain_key="domain:test",
                target_fingerprint="sha256:target",
                argv=(
                    "/usr/bin/xcodebuild", "-project", str(root / "Fixture.xcodeproj"), "-scheme", "Fixture",
                    "-configuration", "Debug", "-destination", "platform=iOS Simulator,id=DEVICE",
                    "-derivedDataPath", str(root / "derived"), "build",
                ),
                args_hash="sha256:args",
                build_attempt_id="build:generation",
                qualifying_source_fingerprint="sha256:source",
            )
            with patch("plugin.agent.native_runtime._checked", new=checked), \
                 patch("plugin.agent.native_runtime._derived_data", return_value=root / "derived"), \
                 patch("plugin.agent.native_runtime._single_app", return_value=app), \
                 patch("plugin.agent.native_runtime._bundle_metadata", return_value=("dev.hermes.Fixture", "Fixture", executable, False)), \
                 patch("plugin.agent.native_runtime._sha256_tree", return_value="sha256:app"), \
                 patch("plugin.agent.native_runtime._sha256_file", return_value="sha256:exe"):
                result = await native_runtime_dispatch(operation, asyncio.Event())

        self.assertTrue(result.ok)
        self.assertEqual(screenshot_count, 0)
        self.assertEqual(result.evidence["launch"]["pid"], 30403)
        self.assertEqual(result.evidence["launch"]["process_generation"], "Wed Aug 19 11:06:53 2026")
        self.assertEqual(result.evidence["launch"]["host_program_path"], str(installed_executable.resolve()))
    def test_bundle_metadata_requires_an_explicit_boolean_fixture_marker(self) -> None:
        with self.subTest("representative app defaults to no fixture challenge"):
            with patch("plugin.agent.native_runtime.plistlib.loads", return_value={"CFBundleIdentifier": "test.real", "CFBundleExecutable": "Real"}):
                with patch.object(Path, "resolve", return_value=Path("/tmp/Real.app/Real")):
                    with patch.object(Path, "read_bytes", return_value=b"plist"):
                        _, _, _, required = _bundle_metadata(Path("/tmp/Real.app"))
            self.assertFalse(required)

        with self.subTest("fixture opts into the challenge"):
            with patch("plugin.agent.native_runtime.plistlib.loads", return_value={"CFBundleIdentifier": "test.fixture", "CFBundleExecutable": "Fixture", "HermesFixtureChallengeRequired": True}):
                with patch.object(Path, "resolve", return_value=Path("/tmp/Fixture.app/Fixture")):
                    with patch.object(Path, "read_bytes", return_value=b"plist"):
                        _, _, _, required = _bundle_metadata(Path("/tmp/Fixture.app"))
            self.assertTrue(required)
    def test_real_core_simulator_steps_have_cold_start_headroom_inside_outer_fence(self) -> None:
        self.assertEqual(_XCODEBUILD_TIMEOUT_SECONDS, 300)
        self.assertEqual(_SIMCTL_QUERY_TIMEOUT_SECONDS, 60)
        self.assertEqual(_SIMCTL_MUTATION_TIMEOUT_SECONDS, 120)
        self.assertEqual(_SIMCTL_BOOT_STATUS_TIMEOUT_SECONDS, 180)
        self.assertEqual(_SIMCTL_INSTALL_TIMEOUT_SECONDS, 180)
        self.assertLess(_SIMCTL_BOOT_STATUS_TIMEOUT_SECONDS, 360)
        self.assertLess(_SIMCTL_INSTALL_TIMEOUT_SECONDS, 360)

    def test_exact_runtime_mismatch_is_rejected_in_device_observation(self) -> None:
        payload = '{"devices":{"com.apple.CoreSimulator.SimRuntime.iOS-26-0":[{"udid":"DEVICE","state":"Booted"}]}}'
        with self.assertRaisesRegex(RuntimeError, 'simulator_runtime_mismatch'):
            _simulator_state(payload, 'DEVICE', runtime='iOS 25.0')
        self.assertEqual(_simulator_state(payload, 'DEVICE', runtime='iOS 26.0'), 'Booted')
        self.assertEqual(_simulator_state(payload, 'DEVICE', runtime='iOS Simulator'), 'Booted')

    def test_simulator_state_requires_one_exact_available_udid(self) -> None:
        payload = '{"devices":{"runtime":[{"udid":"DEVICE","state":"Booted"}]}}'
        self.assertEqual(_simulator_state(payload, "DEVICE"), "Booted")
        with self.assertRaisesRegex(RuntimeError, "simulator_state_unavailable"):
            _simulator_state('{"devices":{}}', "DEVICE")

    async def test_build_failure_keeps_backend_attempt_and_reports_phase_before_subprocess(self) -> None:
        phases = []
        operation = NativeOperation(
            operation_id="operation:test",
            domain_key="domain:test",
            target_fingerprint="sha256:target",
            argv=(
                "/usr/bin/xcodebuild", "-project", "/tmp/Fixture.xcodeproj", "-scheme", "Fixture",
                "-configuration", "Debug", "-destination", "platform=iOS Simulator,id=00000000-0000-0000-0000-000000000001",
                "-derivedDataPath", "/tmp/derived", "build",
            ),
            args_hash="sha256:args",
            build_attempt_id="build:backend-issued",
            qualifying_source_fingerprint="sha256:source",
            phase_reporter=lambda op, phase, evidence: phases.append((op.build_attempt_id, phase, evidence)),
        )
        with patch("plugin.agent.native_runtime._checked", new=AsyncMock(side_effect=RuntimeError("native_command_failed:xcodebuild"))):
            result = await native_runtime_dispatch(operation, asyncio.Event())
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "native_command_failed")
        self.assertEqual(result.evidence["build_attempt_id"], "build:backend-issued")
        self.assertEqual(result.evidence["stage"], "building")
        self.assertEqual(phases, [("build:backend-issued", "building", {"tool": "xcodebuild"})])

    async def test_boot_timeout_retains_owned_simulator_obligation_before_mutation_returns(self) -> None:
        operation = NativeOperation(
            operation_id="operation:boot-timeout",
            domain_key="domain:test",
            target_fingerprint="sha256:target",
            argv=(
                "/usr/bin/xcodebuild", "-project", "/tmp/Fixture.xcodeproj", "-scheme", "Fixture",
                "-configuration", "Debug", "-destination", "platform=iOS Simulator,id=00000000-0000-0000-0000-000000000001",
                "-derivedDataPath", "/tmp/derived", "build",
            ),
            args_hash="sha256:args",
            build_attempt_id="build:boot-timeout",
            qualifying_source_fingerprint="sha256:source",
        )

        async def checked(argv, _cancel_event, *, timeout=180.0):
            del timeout
            if "simctl" in argv and "boot" in argv:
                raise RuntimeError("native_command_failed:xcrun:boot timeout")
            return ""

        with patch("plugin.agent.native_runtime._checked", new=checked), \
             patch("plugin.agent.native_runtime._derived_data", return_value=Path("/tmp/derived")), \
             patch("plugin.agent.native_runtime._single_app", return_value=Path("/tmp/Fixture.app")), \
             patch("plugin.agent.native_runtime._bundle_metadata", return_value=("dev.hermes.Fixture", "Fixture", Path("/tmp/Fixture.app/Fixture"), False)), \
             patch("plugin.agent.native_runtime._sha256_tree", return_value="sha256:app"), \
             patch("plugin.agent.native_runtime._sha256_file", return_value="sha256:exe"), \
             patch("plugin.agent.native_runtime._simulator_state", return_value="Shutdown"), \
             patch("plugin.agent.native_runtime._cleanup_partial_native", new=AsyncMock(return_value={"verified": False})):
            result = await native_runtime_dispatch(operation, asyncio.Event())
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "native_command_failed")
        self.assertEqual(result.evidence["simulator"]["ownership"], "attached")
        self.assertTrue(result.evidence["simulator"]["mutation_attempted"])
        self.assertEqual(result.evidence["simulator"]["outcome"], "unknown")

    async def test_partial_native_cleanup_uses_only_attested_launch_and_owned_simulator(self) -> None:
        commands: list[tuple[str, ...]] = []

        async def run(argv, _cancel_event, *, timeout):
            del timeout
            commands.append(argv)
            if "procinfo" in argv:
                return 0, "program path = /tmp/Fixture.app/Fixture\n", ""
            if "list" in argv:
                return 0, '{"devices":{"iOS":[{"udid":"DEVICE","state":"Booted"}]}}', ""
            return 0, "", ""

        evidence = {
            "launch": {
                "udid": "DEVICE", "bundle_id": "dev.hermes.Fixture",
                "program_path": "/tmp/Fixture.app/Fixture", "pid": 42,
            },
            "simulator": {"udid": "DEVICE", "ownership": "owned"},
        }
        with patch("plugin.agent.native_runtime._run", new=run):
            cleanup = await _cleanup_partial_native(evidence)
        self.assertTrue(cleanup["verified"])
        self.assertTrue(cleanup["app"]["app_stopped"])
        self.assertTrue(cleanup["simulator"]["simulator_stopped"])
        self.assertTrue(any("terminate" in command for command in commands))
        self.assertTrue(any("shutdown" in command for command in commands))

    async def test_partial_native_cleanup_preserves_attached_simulator(self) -> None:
        async def run(*_args, **_kwargs):
            raise AssertionError("attached cleanup must not invoke simctl")

        with patch("plugin.agent.native_runtime._run", new=run):
            cleanup = await _cleanup_partial_native({"simulator": {"udid": "DEVICE", "ownership": "attached"}})
        self.assertTrue(cleanup["verified"])
        self.assertTrue(cleanup["simulator"]["attached_simulator_preserved"])

    async def test_partial_native_cleanup_marks_changed_generation_exited_and_shuts_owned_simulator(self) -> None:
        commands: list[tuple[str, ...]] = []

        async def run(argv, _cancel_event, *, timeout):
            del timeout
            commands.append(argv)
            if argv[0] == "/bin/ps":
                return 0, " 42 Wed Aug 19 11:07:01 2026 /tmp/Fixture.app/Fixture\n", ""
            if "list" in argv:
                return 0, '{"devices":{"iOS":[{"udid":"DEVICE","state":"Booted"}]}}', ""
            return 0, "", ""

        evidence = {
            "launch": {
                "udid": "DEVICE", "bundle_id": "dev.hermes.Fixture", "program_path": "/tmp/Fixture.app/Fixture",
                "executable_hash": "sha256:" + "a" * 64, "pid": 42,
                "process_generation": "Wed Aug 19 11:06:53 2026",
            },
            "simulator": {"udid": "DEVICE", "ownership": "owned"},
        }
        with patch("plugin.agent.native_runtime._run", new=run):
            cleanup = await _cleanup_partial_native(evidence)
        self.assertTrue(cleanup["verified"])
        self.assertTrue(cleanup["app"]["app_already_exited"])
        self.assertTrue(cleanup["simulator"]["simulator_stopped"])
        self.assertFalse(any("terminate" in command for command in commands))

    async def test_ambiguous_procinfo_keeps_cleanup_unverified_and_retains_launch_tuple(self) -> None:
        async def run(argv, _cancel_event, *, timeout):
            del timeout
            if "procinfo" in argv:
                return 130, "", "timeout"
            return 0, '{"devices":{"iOS":[{"udid":"DEVICE","state":"Shutdown"}]}}', ""

        evidence = {
            "launch": {"udid": "DEVICE", "bundle_id": "dev.hermes.Fixture", "program_path": "/tmp/Fixture.app/Fixture", "pid": 42},
            "simulator": {"udid": "DEVICE", "ownership": "owned"},
        }
        with patch("plugin.agent.native_runtime._run", new=run):
            cleanup = await _cleanup_partial_native(evidence)
        self.assertFalse(cleanup["verified"])
        self.assertEqual(cleanup["app"]["reason"], "process_observation_ambiguous")
        self.assertNotIn("app_already_exited", cleanup["app"])
        self.assertTrue(cleanup["simulator"]["simulator_stopped"])

    async def test_succeeded_launch_without_pid_uses_exact_bundle_cleanup(self) -> None:
        commands: list[tuple[str, ...]] = []

        async def run(argv, _cancel_event, *, timeout):
            del timeout
            commands.append(argv)
            if "list" in argv:
                return 0, '{"devices":{"iOS":[{"udid":"DEVICE","state":"Booted"}]}}', ""
            if "terminate" in argv:
                return 0, "", ""
            return 0, "", ""

        with patch("plugin.agent.native_runtime._run", new=run):
            cleanup = await _cleanup_partial_native({
                "launch": {
                    "udid": "DEVICE",
                    "bundle_id": "dev.hermes.Fixture",
                    "program_path": "/tmp/Fixture.app/Fixture",
                    "pid": None,
                    "command_succeeded": True,
                },
                "simulator": {"udid": "DEVICE", "ownership": "owned"},
            })
        self.assertTrue(cleanup["verified"])
        self.assertTrue(cleanup["app"]["app_stopped"])
        self.assertEqual(cleanup["app"]["cleanup_mode"], "exact_bundle")
        self.assertTrue(any("terminate" in command and command[-1] == "dev.hermes.Fixture" for command in commands))

    async def test_provisional_launch_tuple_is_retained_for_retry(self) -> None:
        async def run(argv, _cancel_event, *, timeout):
            del timeout
            if "list" in argv:
                return 0, '{"devices":{"iOS":[{"udid":"DEVICE","state":"Booted"}]}}', ""
            return 0, "", ""

        with patch("plugin.agent.native_runtime._run", new=run):
            cleanup = await _cleanup_partial_native({
                "launch": {"udid": "DEVICE", "bundle_id": "dev.hermes.Fixture", "program_path": None, "pid": 42},
                "simulator": {"udid": "DEVICE", "ownership": "owned"},
            })
        self.assertFalse(cleanup["verified"])
        self.assertEqual(cleanup["app"]["reason"], "launch_tuple_incomplete")
        self.assertTrue(cleanup["simulator"]["simulator_stopped"])

if __name__ == "__main__":
    unittest.main()
