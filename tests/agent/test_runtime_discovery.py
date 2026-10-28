from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugin.agent.native_plan import build_runtime_identity
from plugin.runtime_discovery import CommandResult, discover_runtime_selection, trusted_build_input_fingerprint
from plugin.runway_target import validate_target_selection

UDID = "00000000-0000-4000-8000-000000000101"

IPAD = "11111111-1111-1111-1111-111111111111"
PHONE_TYPE = "com.apple.CoreSimulator.SimDeviceType.iPhone-15-Pro"
IPAD_TYPE = "com.apple.CoreSimulator.SimDeviceType.iPad-mini-A17-Pro"
CATALOG = {"devicetypes": [
    {"identifier": PHONE_TYPE, "productFamily": "iPhone"},
    {"identifier": IPAD_TYPE, "productFamily": "iPad"},
]}
APP_SETTINGS = {
    "PRODUCT_TYPE": "com.apple.product-type.application", "WRAPPER_EXTENSION": "app",
    "SUPPORTED_PLATFORMS": "iphonesimulator", "PRODUCT_BUNDLE_IDENTIFIER": "dev.hermes.Fixture",
    "CONFIGURATION": "Debug", "TARGETED_DEVICE_FAMILY": "1", "IPHONEOS_DEPLOYMENT_TARGET": "17.0",
}

class FakeRunner:
    def __init__(self, project_schemes: dict[str, list[str]], settings: dict[tuple[str, ...], dict], devices: dict) -> None:
        self.project_schemes = project_schemes
        self.settings = settings
        self.devices = devices
        self.catalog = CATALOG
        self.calls = []
    def __call__(self, argv: tuple[str, ...], cwd: Path) -> CommandResult:
        self.calls.append(argv)
        if argv == ("xcrun", "simctl", "list", "devicetypes", "--json"):
            return CommandResult(0, json.dumps(self.catalog), "")
        if argv[:5] == ("xcrun", "simctl", "list", "devices", "available"):
            return CommandResult(0, json.dumps(self.devices), "")
        flag = "-workspace" if "-workspace" in argv else "-project"
        project = str(Path(argv[argv.index(flag) + 1]).resolve())
        if "-list" in argv:
            key = "workspace" if flag == "-workspace" else "project"
            return CommandResult(0, json.dumps({key: {"schemes": self.project_schemes.get(project, []), "configurations": ["Debug", "Release"]}}), "")
        scheme = argv[argv.index("-scheme") + 1]
        configuration = argv[argv.index("-configuration") + 1] if "-configuration" in argv else "Debug"
        payload = self.settings.get((project, scheme, configuration), self.settings.get((project, scheme)))
        if payload is None:
            return CommandResult(1, "", "missing")
        return CommandResult(0, json.dumps([{"buildSettings": payload}]), "")

class RuntimeDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        guard = patch("plugin.runtime_discovery.subprocess.run", side_effect=AssertionError("native command forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.project = self.root / "Fixture.xcodeproj"
        (self.project / "xcshareddata" / "xcschemes").mkdir(parents=True)
        (self.project / "project.pbxproj").write_text("build settings")
        (self.project / "xcshareddata" / "xcschemes" / "Fixture.xcscheme").write_text("<Scheme />")
        self.device_json = {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
                    {"name": "iPhone 17 Pro", "udid": UDID, "deviceTypeIdentifier": PHONE_TYPE, "state": "Booted", "isAvailable": True},
                    {"name": "iPad Pro", "udid": IPAD, "deviceTypeIdentifier": IPAD_TYPE, "state": "Shutdown", "isAvailable": True},
                    {"name": "iPhone unavailable", "deviceTypeIdentifier": PHONE_TYPE, "udid": "22222222-2222-2222-2222-222222222222", "state": "Shutdown", "isAvailable": False},
                ]
            }
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def runner(self, schemes: list[str] | None = None, settings: dict | None = None, devices: dict | None = None) -> FakeRunner:
        path = str(self.project.resolve())
        schemes = schemes or ["Fixture"]
        base = dict(APP_SETTINGS)
        return FakeRunner({path: schemes}, settings or {(path, "Fixture"): base}, devices or self.device_json)

    def test_one_credible_app_and_one_iphone_auto_selects(self) -> None:
        result = discover_runtime_selection(self.root, runner=self.runner())
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selection_source"], "only_credible_target")
        self.assertEqual(result["selected"]["scheme"], "Fixture")
        self.assertEqual(result["selected"]["udid"], UDID)
        self.assertEqual(len(result["devices"]), 1)

    def test_multiple_targets_require_choice_with_explainable_recommendation(self) -> None:
        workspace = self.root / "Fixture.xcworkspace"
        workspace.mkdir()
        (workspace / "contents.xcworkspacedata").write_text("workspace")
        project_path = str(self.project.resolve())
        workspace_path = str(workspace.resolve())
        settings = {
            (project_path, "Fixture"): dict(APP_SETTINGS),
            (workspace_path, "Other"): dict(APP_SETTINGS),
        }
        runner = FakeRunner({project_path: ["Fixture"], workspace_path: ["Other"]}, settings, self.device_json)
        result = discover_runtime_selection(self.root, runner=runner)
        self.assertEqual(result["status"], "requires_choice")
        self.assertEqual(len(result["targets"]), 2)
        self.assertIn("workspace", result["recommendation"]["rationale"].lower())

    def test_remembered_target_is_revalidated_and_stale_device_fails_closed(self) -> None:
        remembered = {
            "worktree": str(self.root), "project": str(self.project), "scheme": "Fixture",
            "configuration": "Debug", "runtime": "iOS 26.5", "udid": UDID,
        }
        selected = discover_runtime_selection(self.root, runner=self.runner(), remembered=remembered)
        self.assertEqual(selected["selection_source"], "remembered_validated")
        stale = dict(remembered, udid="33333333-3333-3333-3333-333333333333")
        rejected = discover_runtime_selection(self.root, runner=self.runner(), remembered=stale)
        self.assertEqual(rejected["remembered"]["status"], "stale")
        self.assertIn("device_unavailable", rejected["remembered"]["reasons"])

    def test_test_scheme_and_non_app_are_not_credible(self) -> None:
        path = str(self.project.resolve())
        settings = {
            (path, "FixtureTests"): {"PRODUCT_TYPE": "com.apple.product-type.bundle.unit-test", "WRAPPER_EXTENSION": "xctest", "SUPPORTED_PLATFORMS": "iphonesimulator"},
            (path, "Library"): {"PRODUCT_TYPE": "com.apple.product-type.framework", "WRAPPER_EXTENSION": "framework", "SUPPORTED_PLATFORMS": "iphonesimulator"},
        }
        result = discover_runtime_selection(self.root, runner=self.runner(["FixtureTests", "Library"], settings))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["targets"], [])

    def test_failure_has_command_diagnostics(self):
        result = discover_runtime_selection(self.root, runner=lambda argv, cwd: CommandResult(69, "", "license not accepted"))
        self.assertEqual(result["diagnostics"][0]["returncode"], 69)
        self.assertIn("license", result["diagnostics"][0]["stderr"])

    def test_incompatible_minimum_runtime_never_selects(self):
        path = str(self.project.resolve())
        settings = {(path, 'Fixture'): dict(APP_SETTINGS, IPHONEOS_DEPLOYMENT_TARGET="27.0")}
        result = discover_runtime_selection(self.root, runner=self.runner(settings=settings))
        self.assertIsNone(result['selected'])

    def test_ambiguous_application_products_are_rejected(self):
        base = self.runner()
        def runner(argv, cwd):
            result = base(argv, cwd)
            if '-showBuildSettings' in argv:
                payload = json.loads(result.stdout)
                return CommandResult(0, json.dumps(payload + payload), '')
            return result
        self.assertEqual(discover_runtime_selection(self.root, runner=runner)['targets'], [])

    def test_stale_selection_never_silently_reselects(self):
        remembered = {"worktree": str(self.root), "project": str(self.project), "scheme": "Fixture", "configuration": "Gone", "runtime": "iOS 26.5", "udid": UDID}
        result = discover_runtime_selection(self.root, runner=self.runner(), remembered=remembered)
        self.assertEqual(result["status"], "requires_choice")
        self.assertIsNone(result["selected"])

    def test_workspace_configuration_is_probed_explicitly_when_not_listed(self):
        workspace = self.root/'Fixture.xcworkspace'
        workspace.mkdir()
        base = self.runner()
        def runner(argv, cwd):
            if '-workspace' not in argv:
                return base(argv, cwd) if argv[0] == 'xcrun' else CommandResult(0, json.dumps({'project':{'schemes':[]}}), '')
            if '-list' in argv:
                return CommandResult(0, json.dumps({'workspace':{'schemes':['Fixture']}}), '')
            config = argv[argv.index('-configuration')+1]
            return CommandResult(0, json.dumps([{'buildSettings':dict(APP_SETTINGS, CONFIGURATION=config)}]), '')
        remembered = {'worktree':str(self.root), 'project':str(workspace), 'scheme':'Fixture', 'configuration':'Release', 'runtime':'iOS 26.5', 'udid':UDID}
        result = discover_runtime_selection(self.root, runner=runner, remembered=remembered)
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['selected']['configuration'], 'Release')

    def test_direct_discovery_rejects_returned_configuration_mismatch(self):
        base = self.runner()
        probes = []
        def runner(argv, cwd):
            result = base(argv, cwd)
            if '-showBuildSettings' in argv:
                probes.append(argv)
                payload = json.loads(result.stdout)
                payload[0]['buildSettings']['CONFIGURATION'] = 'Debug'
                return CommandResult(0, json.dumps(payload), '')
            return result
        remembered = {'worktree':str(self.root), 'project':str(self.project), 'scheme':'Fixture', 'configuration':'Release', 'runtime':'iOS 26.5', 'udid':UDID}
        result = discover_runtime_selection(self.root, runner=runner, remembered=remembered)
        self.assertEqual(probes[0][probes[0].index('-configuration')+1], 'Release')
        self.assertEqual(result['targets'], [])
        self.assertIsNone(result['selected'])
        self.assertEqual(result['remembered']['status'], 'stale')
        self.assertTrue(any("configuration_mismatch" in item.get("reasons", []) for item in result["diagnostics"]))


    def remembered(self, **changes):
        return dict({"worktree": str(self.root), "project": str(self.project), "scheme": "Fixture",
                     "configuration": "Debug", "runtime": "iOS 26.5", "udid": UDID}, **changes)

    def family_runner(self, family):
        return self.runner(settings={(str(self.project.resolve()), "Fixture"): dict(APP_SETTINGS, TARGETED_DEVICE_FAMILY=family)})

    def test_renamed_phone_is_offered(self):
        self.device_json["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-5"][0]["name"] = "REA Hermes Preview Fixture"
        result = discover_runtime_selection(self.root, runner=self.runner())
        self.assertEqual([d["udid"] for d in result["devices"]], [UDID])
        self.assertEqual(result["selected"]["device_name"], "REA Hermes Preview Fixture")

    def test_universal_app_offers_ipad(self):
        result = discover_runtime_selection(self.root, runner=self.family_runner("1,2"))
        self.assertEqual(set(result["targets"][0]["compatible_udids"]), {UDID, IPAD})

    def test_universal_app_requires_device_choice(self):
        result = discover_runtime_selection(self.root, runner=self.family_runner("1,2"))
        self.assertEqual(result["status"], "requires_choice")
        self.assertIsNone(result["selected"])

    def test_phone_only_native_choices_include_renamed_phone(self):
        entries = self.device_json["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-5"]
        entries.append(dict(entries[0], name="Preview", udid="33333333-3333-3333-3333-333333333333"))
        result = discover_runtime_selection(self.root, runner=self.family_runner("1"))
        self.assertEqual(set(result["targets"][0]["compatible_udids"]), {UDID, entries[-1]["udid"]})

    def test_ipad_only_native_choices(self):
        result = discover_runtime_selection(self.root, runner=self.family_runner("2"))
        self.assertEqual(result["targets"][0]["compatible_udids"], [IPAD])

    def test_remembered_family_drift_rejects_pair_even_when_other_target_admits_device(self):
        path = str(self.project.resolve())
        runner = self.runner(["Fixture", "Other"], {
            (path, "Fixture"): dict(APP_SETTINGS, TARGETED_DEVICE_FAMILY="2"),
            (path, "Other"): dict(APP_SETTINGS, TARGETED_DEVICE_FAMILY="1,2"),
        })
        result = discover_runtime_selection(self.root, runner=runner, remembered=self.remembered())
        self.assertEqual(result["remembered"]["status"], "stale")
        self.assertIsNone(result["selected"])
        self.assertEqual(result["status"], "requires_choice")

    def test_remembered_ipad_family_drift_with_other_native_target(self):
        path = str(self.project.resolve())
        runner = self.runner(["Fixture", "Other"], {
            (path, "Fixture"): dict(APP_SETTINGS, TARGETED_DEVICE_FAMILY="1,2"),
            (path, "Other"): dict(APP_SETTINGS, TARGETED_DEVICE_FAMILY="1,2"),
        })
        payload = self.remembered(udid=IPAD)
        self.assertEqual(discover_runtime_selection(self.root, runner=runner, remembered=payload)["selected"]["udid"], IPAD)
        runner.settings[(path, "Fixture")] = dict(APP_SETTINGS, TARGETED_DEVICE_FAMILY="1")
        result = discover_runtime_selection(self.root, runner=runner, remembered=payload)
        self.assertIn(IPAD, {device["udid"] for device in result["devices"]})
        self.assertEqual(result["remembered"]["status"], "stale")
        self.assertIn("destination_compatibility_unverified", result["remembered"]["reasons"])
        self.assertIsNone(result["selected"])

    def test_unknown_type_with_phone_name_is_not_offered(self):
        self.device_json["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-5"][0]["deviceTypeIdentifier"] = "unknown"
        result = discover_runtime_selection(self.root, runner=self.runner())
        self.assertEqual(result["devices"], [])
        self.assertIsNone(result["selected"])

    def test_absent_minimum_runtime_is_not_assumed_zero(self):
        settings = dict(APP_SETTINGS)
        del settings["IPHONEOS_DEPLOYMENT_TARGET"]
        result = discover_runtime_selection(self.root, runner=self.runner(settings={(str(self.project.resolve()), "Fixture"): settings}))
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["selected"])

    def test_explicit_ipad_only_on_phone_is_rejected(self):
        self.assertFalse(validate_target_selection(self.remembered(), runner=self.family_runner("2"))["valid"])

    def test_phone_on_ipad_is_unverified_in_discovery_remembered_and_explicit(self):
        runner = self.family_runner("1")
        discovery = discover_runtime_selection(self.root, runner=runner)
        decision = next(item for item in discovery["diagnostics"] if item.get("udid") == IPAD and item.get("stage") == "destination_assessment")
        self.assertEqual(decision["status"], "compatibility_unverified")
        self.assertEqual(decision["reasons"], ["phone_app_on_ipad_compatibility_unverified"])
        remembered = self.remembered(udid=IPAD)
        result = discover_runtime_selection(self.root, runner=runner, remembered=remembered)
        self.assertEqual(result["remembered"]["status"], "stale")
        self.assertIn("destination_compatibility_unverified", result["remembered"]["reasons"])
        explicit = validate_target_selection(remembered, runner=runner)
        self.assertFalse(explicit["valid"])
        self.assertEqual(explicit["discovery"]["remembered"], result["remembered"])

    def test_universal_ipad_remembered_and_explicit_are_native(self):
        runner = self.family_runner("1,2")
        payload = self.remembered(udid=IPAD)
        discovery = discover_runtime_selection(self.root, runner=runner, remembered=payload)
        self.assertEqual(discovery["selected"]["udid"], IPAD)
        self.assertEqual(discovery["selection_source"], "remembered_validated")
        explicit = validate_target_selection(payload, runner=runner)
        self.assertTrue(explicit["valid"], explicit)
        self.assertEqual(explicit["assessment"], {"status": "native", "reasons": []})

    def test_explicit_probe_reassesses_returned_facts(self):
        for changes, udid, reason, status in [
            ({"TARGETED_DEVICE_FAMILY": "2"}, UDID, "device_family_not_targeted", "incompatible"),
            ({"TARGETED_DEVICE_FAMILY": "1"}, IPAD, "phone_app_on_ipad_compatibility_unverified", "compatibility_unverified"),
            ({"TARGETED_DEVICE_FAMILY": "$(INHERITED)"}, UDID, "device_families_unknown", "unknown"),
            ({"IPHONEOS_DEPLOYMENT_TARGET": None}, UDID, "minimum_runtime_unknown", "unknown"),
            ({"IPHONEOS_DEPLOYMENT_TARGET": "27.0"}, UDID, "runtime_below_minimum", "incompatible"),
            ({"CONFIGURATION": "Release"}, UDID, "configuration_mismatch", "unknown"),
        ]:
            with self.subTest(changes=changes, udid=udid):
                base = self.family_runner("1,2")
                def runner(argv, cwd):
                    result = base(argv, cwd)
                    if "-destination" in argv:
                        payload = json.loads(result.stdout)
                        payload[0]["buildSettings"].update(changes)
                        return CommandResult(0, json.dumps(payload), "")
                    return result
                result = validate_target_selection(self.remembered(udid=udid), runner=runner)
                self.assertFalse(result["valid"])
                self.assertIsNone(result["selection"])
                self.assertEqual(result["assessment"]["status"], status)
                self.assertIn(reason, result["reasons"])

    def test_cross_platform_families_preserve_native_ios_selection(self):
        for families, ios_families, devices in [
            ("1,2,6", [1, 2], {UDID, IPAD}),
            ("1,2,7", [1, 2], {UDID, IPAD}),
            ("1,2,6,7", [1, 2], {UDID, IPAD}),
            (" 2, 6,7,2 ", [2], {IPAD}),
        ]:
            with self.subTest(families=families):
                runner = self.family_runner(families)
                result = discover_runtime_selection(self.root, runner=runner)
                self.assertTrue(result["targets"], result)
                self.assertEqual(result["targets"][0]["families"], ios_families)
                self.assertEqual(set(result["targets"][0]["compatible_udids"]), devices)
                for udid in devices:
                    selected = validate_target_selection(self.remembered(udid=udid), runner=runner)
                    self.assertTrue(selected["valid"], selected)
                    self.assertEqual(selected["assessment"], {"status": "native", "reasons": []})

    def test_cross_platform_phone_app_does_not_gain_native_ipad_support(self):
        for families in ["1,6", "1,7", "1,6,7"]:
            with self.subTest(families=families):
                runner = self.family_runner(families)
                phone = validate_target_selection(self.remembered(), runner=runner)
                self.assertTrue(phone["valid"], phone)
                discovery = discover_runtime_selection(self.root, runner=runner)
                decision = next(item for item in discovery["diagnostics"] if item.get("udid") == IPAD and item.get("stage") == "destination_assessment")
                self.assertEqual(decision["status"], "compatibility_unverified")
                self.assertEqual(decision["reasons"], ["phone_app_on_ipad_compatibility_unverified"])
                ipad = validate_target_selection(self.remembered(udid=IPAD), runner=runner)
                self.assertFalse(ipad["valid"])
                self.assertIn("destination_compatibility_unverified", ipad["discovery"]["remembered"]["reasons"])

    def test_malformed_or_missing_family_has_diagnostics(self):
        for family in [None, "", "1,,2", "1,3", "$(TARGETED_DEVICE_FAMILY)", 1, [1, 2], "1 2", "1,", "1.0", "0", "3", "6", "7", "6,7", "1,8", "1,26"]:
            with self.subTest(family=family):
                result = discover_runtime_selection(self.root, runner=self.family_runner(family))
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["selected"])
                self.assertTrue(any("device_families_unknown" in item.get("reasons", []) for item in result["diagnostics"]))

    def test_family_lists_are_sorted_deduplicated_and_json_serializable(self):
        result = discover_runtime_selection(self.root, runner=self.family_runner(" 2, 1,2 "))
        serialized = json.loads(json.dumps(result))
        self.assertEqual(serialized["targets"][0]["families"], [1, 2])
        self.assertEqual(serialized["targets"][0]["configurations"], ["Debug"])
        self.assertEqual({item["family"] for item in serialized["devices"]}, {1, 2})
        self.assertEqual({item["device_type_identifier"] for item in serialized["devices"]}, {PHONE_TYPE, IPAD_TYPE})

    def test_catalog_failure_and_malformed_catalog_fail_honestly(self):
        for reply in [CommandResult(69, "", "catalog failed"), CommandResult(0, "not json", ""),
                      CommandResult(0, "{}", ""), CommandResult(0, '{"devicetypes":false}', "")]:
            with self.subTest(reply=reply):
                base = self.runner()
                def runner(argv, cwd):
                    if argv == ("xcrun", "simctl", "list", "devicetypes", "--json"):
                        return reply
                    return base(argv, cwd)
                result = discover_runtime_selection(self.root, runner=runner)
                self.assertEqual(result["devices"], [])
                self.assertEqual(result["status"], "unavailable")
                self.assertTrue(any(item.get("reason") == "device_type_catalog_unavailable" for item in result["diagnostics"]))
                if reply.returncode:
                    self.assertTrue(any(item.get("returncode") == 69 and item.get("stderr") == "catalog failed" for item in result["diagnostics"]))

    def test_unknown_missing_or_conflicting_device_type_never_uses_phone_name(self):
        cases = [
            ({"devicetypes": []}, PHONE_TYPE),
            ({"devicetypes": [{"identifier": PHONE_TYPE}]}, PHONE_TYPE),
            ({"devicetypes": [{"identifier": PHONE_TYPE, "productFamily": "Mac"}]}, PHONE_TYPE),
            ({"devicetypes": [{"identifier": PHONE_TYPE, "productFamily": "iPhone"}, {"identifier": PHONE_TYPE, "productFamily": "iPad"}]}, PHONE_TYPE),
            (CATALOG, None),
            (CATALOG, "com.apple.CoreSimulator.SimDeviceType.iPhone-Unknown"),
        ]
        for catalog, identifier in cases:
            with self.subTest(catalog=catalog, identifier=identifier):
                base = self.runner()
                base.catalog = catalog
                entries = self.device_json["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-5"]
                entries[0]["deviceTypeIdentifier"] = identifier
                result = discover_runtime_selection(self.root, runner=base)
                self.assertEqual(result["devices"], [])
                self.assertTrue(any(item.get("reason") == "device_family_unknown" and item.get("udid") == UDID for item in result["diagnostics"]))

    def test_device_availability_requires_literal_true(self):
        for availability in [False, None, 1, "true"]:
            with self.subTest(availability=availability):
                entries = self.device_json["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-5"]
                entries[0]["isAvailable"] = availability
                result = discover_runtime_selection(self.root, runner=self.runner())
                self.assertEqual(result["devices"], [])
                reason = "device_unavailable" if availability is False else "device_availability_unknown"
                self.assertTrue(any(reason in item.get("reasons", []) for item in result["diagnostics"]))
        del entries[0]["isAvailable"]
        self.assertEqual(discover_runtime_selection(self.root, runner=self.runner())["devices"], [])

    def test_runtime_and_minimum_version_limits(self):
        for minimum, selected in [("26.5", True), ("26.5.0", True), ("26.5.1", False), ("27", False), (None, False), ("broken", False)]:
            with self.subTest(minimum=minimum):
                result = discover_runtime_selection(self.root, runner=self.runner(settings={(str(self.project.resolve()), "Fixture"): dict(APP_SETTINGS, IPHONEOS_DEPLOYMENT_TARGET=minimum)}))
                self.assertEqual(result["status"] == "selected", selected)
                if minimum in (None, "broken"):
                    self.assertTrue(any("minimum_runtime_unknown" in item.get("reasons", []) for item in result["diagnostics"]))

    def test_other_platforms_and_malformed_runtimes_do_not_select(self):
        entries = self.device_json["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-5"]
        for runtime in ["com.apple.CoreSimulator.SimRuntime.tvOS-26-5", "com.apple.CoreSimulator.SimRuntime.watchOS-26-5", "com.apple.CoreSimulator.SimRuntime.iOS-invalid"]:
            with self.subTest(runtime=runtime):
                result = discover_runtime_selection(self.root, runner=self.runner(devices={"devices": {runtime: entries}}))
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["devices"], [])
                self.assertTrue(any("runtime_unknown" in item.get("reasons", []) for item in result["diagnostics"]))

    def test_platform_substring_is_not_simulator_support(self):
        runner = self.runner(settings={(str(self.project.resolve()), "Fixture"): dict(APP_SETTINGS, SUPPORTED_PLATFORMS="notiphonesimulator")})
        self.assertEqual(discover_runtime_selection(self.root, runner=runner)["targets"], [])

    def test_configuration_choices_use_only_observed_settings_without_fanout(self):
        runner = self.runner()
        result = discover_runtime_selection(self.root, runner=runner)
        self.assertEqual(result["targets"][0]["configurations"], ["Debug"])
        self.assertEqual(result["targets"][0]["configuration"], "Debug")
        self.assertEqual(sum("-showBuildSettings" in argv for argv in runner.calls), 1)

    def test_remembered_and_explicit_requested_configuration_family_drift(self):
        path = str(self.project.resolve())
        runner = self.runner(settings={
            (path, "Fixture", "Debug"): dict(APP_SETTINGS),
            (path, "Fixture", "Release"): dict(APP_SETTINGS, CONFIGURATION="Release", TARGETED_DEVICE_FAMILY="2"),
        })
        result = discover_runtime_selection(self.root, runner=runner, remembered=self.remembered(configuration="Release"))
        self.assertEqual(result["remembered"]["status"], "stale")
        self.assertIn("device_family_not_targeted", result["remembered"]["reasons"])
        self.assertIsNone(result["selected"])
        self.assertEqual(result["targets"][0]["configurations"], ["Release"])
        self.assertEqual(result["targets"][0]["compatible_udids"], [IPAD])
        self.assertFalse(validate_target_selection(self.remembered(configuration="Release"), runner=runner)["valid"])
        ipad = validate_target_selection(self.remembered(configuration="Release", udid=IPAD), runner=runner)
        self.assertTrue(ipad["valid"], ipad)
        for argv in runner.calls:
            if "-showBuildSettings" in argv:
                self.assertEqual(argv[argv.index("-configuration") + 1], "Release")

    def test_missing_observed_configuration_is_unknown(self):
        settings = dict(APP_SETTINGS)
        del settings["CONFIGURATION"]
        result = discover_runtime_selection(self.root, runner=self.runner(settings={(str(self.project.resolve()), "Fixture"): settings}))
        self.assertEqual(result["targets"], [])
        self.assertTrue(any("configuration_unknown" in item.get("reasons", []) for item in result["diagnostics"]))

    def test_catalog_is_read_once_per_operation_without_cross_request_cache(self):
        runner = self.runner()
        catalog_command = ("xcrun", "simctl", "list", "devicetypes", "--json")
        self.assertEqual(discover_runtime_selection(self.root, runner=runner)["status"], "selected")
        self.assertEqual(runner.calls.count(catalog_command), 1)
        runner.calls.clear()
        self.assertTrue(validate_target_selection(self.remembered(), runner=runner)["valid"])
        self.assertEqual(runner.calls.count(catalog_command), 1)
        self.assertEqual(sum("-showBuildSettings" in argv for argv in runner.calls), 2)
        runner.catalog = {"devicetypes": []}
        runner.calls.clear()
        self.assertFalse(validate_target_selection(self.remembered(), runner=runner)["valid"])
        self.assertEqual(runner.calls.count(catalog_command), 1)

    def test_diagnostic_metadata_is_rejected_as_target_input(self):
        for key, value in [("families", [1, 2]), ("family", 1), ("minimum_runtime", "17.0"),
                           ("device_type_identifier", PHONE_TYPE), ("assessment", {"status": "native"})]:
            with self.subTest(key=key):
                runner = self.runner()
                result = validate_target_selection(dict(self.remembered(), **{key: value}), runner=runner)
                self.assertFalse(result["valid"])
                self.assertEqual(result["reasons"], ["runway_target_malformed"])
                self.assertEqual(runner.calls, [])

    def test_build_fingerprint_retains_identity_and_trusted_project_sensitivity(self):
        fields = dict(profile="isolated", worktree=self.root, project=self.project, scheme="Fixture",
                      configuration="Debug", runtime="iOS 26.5", udid=UDID)
        first = trusted_build_input_fingerprint(build_runtime_identity(**fields))
        for changes in [{"scheme": "Other"}, {"configuration": "Release"}, {"runtime": "iOS 27.0"}, {"udid": IPAD}]:
            with self.subTest(changes=changes):
                self.assertNotEqual(trusted_build_input_fingerprint(build_runtime_identity(**dict(fields, **changes))), first)
        runner = self.family_runner("1,2")
        discover_runtime_selection(self.root, runner=runner)
        self.assertEqual(trusted_build_input_fingerprint(build_runtime_identity(**fields)), first)
        runner.catalog = {"devicetypes": []}
        discover_runtime_selection(self.root, runner=runner)
        self.assertEqual(trusted_build_input_fingerprint(build_runtime_identity(**fields)), first)
        (self.project / "project.pbxproj").write_text("TARGETED_DEVICE_FAMILY = 2")
        self.assertNotEqual(trusted_build_input_fingerprint(build_runtime_identity(**fields)), first)

    def test_build_fingerprint_tracks_trusted_inputs_not_swift_sources(self) -> None:
        script = self.root / "scripts" / "build.sh"
        script.parent.mkdir()
        script.write_text("exit 0")
        config = self.root / "Local.xcconfig"
        config.write_text("FLAG=1")
        swift = self.root / "App.swift"
        swift.write_text("let value = 1")
        identity = build_runtime_identity(profile="isolated", worktree=self.root, project=self.project, scheme="Fixture", configuration="Debug", runtime="iOS 26.5", udid=UDID)
        first = trusted_build_input_fingerprint(identity)
        swift.write_text("let value = 2")
        self.assertEqual(trusted_build_input_fingerprint(identity), first)
        script.write_text("exit 1")
        second = trusted_build_input_fingerprint(identity)
        self.assertNotEqual(second, first)
        config.write_text("FLAG=2")
        self.assertNotEqual(trusted_build_input_fingerprint(identity), second)

if __name__ == "__main__":
    unittest.main()
