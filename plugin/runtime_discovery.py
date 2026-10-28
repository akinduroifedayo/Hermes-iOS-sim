from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, TypedDict

from .agent.native_plan import RuntimeIdentity

EXCLUDED_DIRECTORIES = {".git", ".build", "build", "DerivedData", "node_modules", "Pods"}
TRUSTED_NAMES = {
    "project.pbxproj", "contents.xcworkspacedata", "Package.swift", "Package.resolved",
    "Podfile", "Podfile.lock", "Cartfile", "Cartfile.resolved", "Gemfile", "Gemfile.lock",
    "Mintfile", "project.yml", "Project.swift", "Tuist.swift", "Makefile",
}
TRUSTED_SUFFIXES = {".xcconfig", ".xcscheme", ".sh", ".bash", ".zsh"}
SCRIPT_DIRECTORIES = {"scripts", "script", "build-scripts", "build_scripts"}

@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

Runner = Callable[[tuple[str, ...], Path], CommandResult]


class ProductFacts(TypedDict):
    configuration: str | None
    families: list[int] | None
    minimum_runtime: str | None
    issues: list[str]


class DeviceFacts(TypedDict):
    name: str
    udid: str
    runtime: str
    runtime_identifier: str
    state: str
    device_type_identifier: str | None
    product_family: str | None
    family: int | None
    is_available: bool | None
    issues: list[str]
    rationale: str


class DestinationAssessment(TypedDict):
    status: Literal["native", "compatibility_unverified", "incompatible", "unknown"]
    reasons: list[str]


def _version(value: object) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value) if isinstance(value, str) else None
    return tuple(int(part or 0) for part in match.groups()) if match else None


def product_facts(settings: dict, *, configuration: str | None = None) -> ProductFacts:
    issues = []
    observed_configuration = settings.get("CONFIGURATION")
    if not isinstance(observed_configuration, str) or not observed_configuration.strip():
        observed_configuration = None
        issues.append("configuration_unknown")
    elif configuration is not None and observed_configuration != configuration:
        issues.append("configuration_mismatch")
    raw_families = settings.get("TARGETED_DEVICE_FAMILY")
    families = None
    if isinstance(raw_families, str) and re.fullmatch(r"\s*[1267](?:\s*,\s*[1267])*\s*", raw_families):
        ios_families = {int(value.strip()) for value in raw_families.split(",")} & {1, 2}
        families = sorted(ios_families) if ios_families else None
    if families is None:
        issues.append("device_families_unknown")
    minimum = settings.get("IPHONEOS_DEPLOYMENT_TARGET")
    if _version(minimum) is None:
        minimum = None
        issues.append("minimum_runtime_unknown")
    return {"configuration": observed_configuration, "families": families, "minimum_runtime": minimum, "issues": issues}


def assess_destination(product: ProductFacts, device: DeviceFacts) -> DestinationAssessment:
    reasons = product["issues"] + device["issues"]
    runtime = _version(device["runtime"][4:]) if device["runtime"].startswith("iOS ") else None
    minimum = _version(product["minimum_runtime"])
    if runtime is None:
        reasons.append("runtime_unknown")
    if device["is_available"] is not True:
        reasons.append("device_unavailable" if device["is_available"] is False else "device_availability_unknown")
    if runtime is not None and minimum is not None and runtime < minimum:
        reasons.append("runtime_below_minimum")
    if product["issues"] or device["issues"] or runtime is None or device["is_available"] is None:
        return {"status": "unknown", "reasons": sorted(set(reasons))}
    if reasons:
        return {"status": "incompatible", "reasons": sorted(set(reasons))}
    if device["family"] in product["families"]:
        return {"status": "native", "reasons": []}
    if product["families"] == [1] and device["family"] == 2:
        return {"status": "compatibility_unverified", "reasons": ["phone_app_on_ipad_compatibility_unverified"]}
    return {"status": "incompatible", "reasons": ["device_family_not_targeted"]}

def run_command(argv: tuple[str, ...], cwd: Path) -> CommandResult:
    try:
        completed = subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(127, "", f"{type(exc).__name__}: {exc}")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)

def discover_projects(worktree: Path) -> list[dict]:
    root = worktree.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("runtime_worktree_unavailable")
    projects: list[dict] = []
    for current, directories, _files in os.walk(root):
        current_path = Path(current)
        directories[:] = sorted(directory for directory in directories if directory not in EXCLUDED_DIRECTORIES)
        retained: list[str] = []
        for directory in directories:
            candidate = current_path / directory
            if candidate.suffix in {".xcworkspace", ".xcodeproj"}:
                if not any(parent.suffix == ".xcodeproj" for parent in candidate.parents):
                    projects.append({"project": str(candidate.resolve()), "kind": "workspace" if candidate.suffix == ".xcworkspace" else "project"})
            else:
                retained.append(directory)
        directories[:] = retained
    return sorted(projects, key=lambda item: (item["kind"] != "workspace", item["project"]))

def _json_result(runner: Runner, argv: tuple[str, ...], cwd: Path):
    result = runner(argv, cwd)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None

def _credible_settings(settings: dict) -> bool:
    product_type = str(settings.get("PRODUCT_TYPE", ""))
    wrapper = str(settings.get("WRAPPER_EXTENSION", ""))
    platforms = str(settings.get("SUPPORTED_PLATFORMS", ""))
    sdkroot = str(settings.get("SDKROOT", ""))
    is_app = product_type == "com.apple.product-type.application"
    supports_simulator = "iphonesimulator" in platforms.split() or sdkroot == "iphonesimulator"
    return is_app and supports_simulator

def discover_targets(worktree: Path, *, runner: Runner = run_command, configuration: str | None = None) -> list[dict]:
    root = worktree.expanduser().resolve()
    targets: list[dict] = []
    for project in discover_projects(root):
        flag = "-workspace" if project["kind"] == "workspace" else "-project"
        listed = _json_result(runner, ("xcodebuild", "-list", "-json", flag, project["project"]), root)
        container = listed.get(project["kind"], {}) if isinstance(listed, dict) else {}
        schemes = container.get("schemes", []) if isinstance(container, dict) else []
        for scheme in sorted({item for item in schemes if isinstance(item, str) and item.strip()}):
            if scheme.lower().endswith(("tests", "uitests")):
                continue
            payload = _json_result(
                runner,
                ("xcodebuild", "-showBuildSettings", "-json", flag, project["project"], "-scheme", scheme, "-sdk", "iphonesimulator") + (("-configuration", configuration) if configuration else ()),
                root,
            )
            settings = None
            applications = []
            if isinstance(payload, list):
                for item in payload:
                    candidate = item.get("buildSettings") if isinstance(item, dict) else None
                    if isinstance(candidate, dict) and _credible_settings(candidate):
                        applications.append(candidate)
            if len(applications) == 1:
                settings = applications[0]
            if settings is None:
                continue
            facts = product_facts(settings, configuration=configuration)
            if 'configuration_mismatch' in facts['issues']:
                continue
            targets.append({
                **project,
                "scheme": scheme,
                "bundle_identifier": settings.get("PRODUCT_BUNDLE_IDENTIFIER"),
                "product_name": settings.get("PRODUCT_NAME"),
                "configurations": [facts["configuration"]] if facts["configuration"] else [],
                **facts,
                "rationale": "shared scheme resolves to an iOS Simulator application product",
            })
    return sorted(targets, key=lambda item: (item["kind"] != "workspace", item["project"], item["scheme"]))

def _runtime_name(identifier: str) -> str:
    match = re.search(r"\.iOS-(\d+)-(\d+)(?:-(\d+))?$", identifier)
    if not match:
        return identifier
    parts = [part for part in match.groups() if part is not None]
    return "iOS " + ".".join(parts)

def read_simulator_inventory(worktree: Path, *, runner: Runner) -> dict:
    root = worktree.expanduser().resolve()
    catalog_payload = _json_result(runner, ("xcrun", "simctl", "list", "devicetypes", "--json"), root)
    payload = _json_result(runner, ("xcrun", "simctl", "list", "devices", "available", "--json"), root)
    diagnostics = []
    catalog: dict[str, str | None] = {}
    entries = catalog_payload.get("devicetypes") if isinstance(catalog_payload, dict) else None
    if not isinstance(entries, list):
        diagnostics.append({"stage": "device_inventory", "reason": "device_type_catalog_unavailable"})
    else:
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("identifier"), str):
                continue
            identifier = entry["identifier"]
            family = entry.get("productFamily")
            family = family if isinstance(family, str) else None
            catalog[identifier] = family if identifier not in catalog or catalog[identifier] == family else None
    devices: list[DeviceFacts] = []
    runtimes = payload.get("devices") if isinstance(payload, dict) else None
    if not isinstance(runtimes, dict):
        diagnostics.append({"stage": "device_inventory", "reason": "device_inventory_unavailable"})
        runtimes = {}
    for runtime_id, entries in runtimes.items():
        if not isinstance(entries, list):
            diagnostics.append({"stage": "device_inventory", "runtime_identifier": runtime_id, "reason": "runtime_devices_malformed"})
            continue
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("udid"), str) or not item["udid"]:
                continue
            identifier = item.get("deviceTypeIdentifier")
            identifier = identifier if isinstance(identifier, str) and identifier else None
            product_family = catalog.get(identifier)
            family = {"iPhone": 1, "iPad": 2}.get(product_family)
            issues = [] if family is not None else ["device_family_unknown"]
            if family is None:
                diagnostics.append({"stage": "device_inventory", "udid": item["udid"], "device_type_identifier": identifier, "reason": "device_family_unknown"})
            devices.append({
                "name": str(item.get("name", "")), "udid": item["udid"],
                "runtime": _runtime_name(runtime_id), "runtime_identifier": runtime_id,
                "state": str(item.get("state", "Unknown")), "device_type_identifier": identifier,
                "product_family": product_family, "family": family,
                "is_available": item.get("isAvailable") if isinstance(item.get("isAvailable"), bool) else None,
                "issues": issues,
                "rationale": "iOS Simulator device inventory",
            })
    return {"devices": sorted(devices, key=lambda item: (item["state"] != "Booted", item["runtime"], item["name"], item["udid"])), "diagnostics": diagnostics}


def discover_devices(worktree: Path, *, runner: Runner = run_command) -> list[dict]:
    return read_simulator_inventory(worktree, runner=runner)["devices"]

def validate_remembered_target(worktree: Path, remembered: dict | None, targets: list[dict], devices: list[dict]) -> dict:
    if not isinstance(remembered, dict):
        return {"status": "absent", "reasons": []}
    root = worktree.expanduser().resolve()
    reasons: list[str] = []
    try:
        if Path(str(remembered.get("worktree", ""))).expanduser().resolve() != root:
            reasons.append("worktree_changed")
        project = str(Path(str(remembered.get("project", ""))).expanduser().resolve())
    except (OSError, RuntimeError):
        project = ""
        reasons.append("project_invalid")
    scheme = remembered.get("scheme")
    target = next((item for item in targets if item["project"] == project and item["scheme"] == scheme), None)
    if target is None:
        reasons.append("target_unavailable")
    device = next((item for item in devices if item["udid"] == remembered.get("udid") and item["runtime"] == remembered.get("runtime")), None)
    if device is None:
        reasons.append("device_unavailable")
    configuration = remembered.get("configuration")
    if not isinstance(configuration, str) or not configuration.strip():
        reasons.append("configuration_invalid")
    elif target is None or configuration not in target["configurations"]:
        reasons.append("configuration_unavailable")
    if target is not None and device is not None:
        assessment = assess_destination(target, device)
        if assessment["status"] != "native":
            reasons.extend(["destination_" + assessment["status"], *assessment["reasons"]])
    return {"status": "valid" if not reasons else "stale", "reasons": sorted(set(reasons))}

def _selection(root: Path, target: dict, device: dict, configuration: str = "Debug") -> dict:
    return {
        "worktree": str(root), "project": target["project"], "scheme": target["scheme"],
        "configuration": configuration, "runtime": device["runtime"], "udid": device["udid"],
        "device_name": device["name"], "project_kind": target["kind"],
    }

def compatible_destination(target: dict, device: dict) -> bool:
    return assess_destination(target, device)["status"] == "native"


def discover_runtime_selection(worktree: Path, *, runner: Runner = run_command, remembered: dict | None = None) -> dict:
    root = worktree.expanduser().resolve()
    diagnostics = []
    def observed(argv, cwd):
        result = runner(argv, cwd)
        try:
            parsed = json.loads(result.stdout)
            malformed = False
            if '-showBuildSettings' in argv and isinstance(parsed, list):
                applications = [item['buildSettings'] for item in parsed if isinstance(item, dict) and isinstance(item.get('buildSettings'), dict) and _credible_settings(item['buildSettings'])]
                if len(applications) > 1:
                    diagnostics.append({'stage': 'product_validation', 'command': list(argv), 'returncode': result.returncode, 'reason': 'ambiguous_application_product', 'recovery': 'Choose a scheme resolving to one iOS Simulator application product.'})
                elif len(applications) == 1:
                    configuration = argv[argv.index('-configuration') + 1] if '-configuration' in argv else None
                    facts = product_facts(applications[0], configuration=configuration)
                    if facts['issues']:
                        diagnostics.append({'stage': 'product_validation', 'command': list(argv), 'returncode': result.returncode, 'reasons': facts['issues']})
        except (ValueError, TypeError):
            malformed = True
        if result.returncode or malformed:
            diagnostics.append({"stage": "discovery", "command": list(argv), "returncode": result.returncode, "stderr": result.stderr[-4096:], "stdout": result.stdout[-4096:], "recovery": "Check Xcode selection/license and the exact project/scheme; rerun discovery.", "malformed_json": malformed})
        return result
    requested_configuration = remembered.get('configuration') if isinstance(remembered, dict) else None
    targets = discover_targets(root, runner=observed, configuration=requested_configuration if isinstance(requested_configuration, str) else None)
    inventory = read_simulator_inventory(root, runner=observed)
    devices = inventory["devices"]
    diagnostics.extend(inventory["diagnostics"])
    remembered_status = validate_remembered_target(root, remembered, targets, devices)
    for target in targets:
        target["compatible_udids"] = []
        for device in devices:
            assessment = assess_destination(target, device)
            if assessment["status"] == "native":
                target["compatible_udids"].append(device["udid"])
            else:
                diagnostics.append({"stage": "destination_assessment", "project": target["project"], "scheme": target["scheme"], "configuration": target["configuration"], "udid": device["udid"], "runtime": device["runtime"], **assessment})
    credible_targets = bool(targets)
    available_devices = any(device["is_available"] is True for device in devices)
    targets = [target for target in targets if target["compatible_udids"] and target["configurations"]]
    devices = [device for device in devices if any(device["udid"] in target["compatible_udids"] for target in targets)]
    base = {"worktree": str(root), "targets": targets, "devices": devices, "remembered": remembered_status, "diagnostics": diagnostics}
    if remembered_status["status"] == "stale":
        return {**base, "status": "requires_choice", "selection_source": None, "selected": None, "recommendation": None, "reasons": remembered_status["reasons"]}
    if remembered_status["status"] == "valid":
        target = next(item for item in targets if item["project"] == str(Path(remembered["project"]).expanduser().resolve()) and item["scheme"] == remembered["scheme"])
        device = next(item for item in devices if item["udid"] == remembered["udid"] and item["runtime"] == remembered["runtime"])
        return {**base, "status": "selected", "selection_source": "remembered_validated", "selected": _selection(root, target, device, remembered["configuration"]), "recommendation": None}
    if not targets or not devices:
        missing = []
        if not credible_targets:
            missing.append("no_credible_ios_application_target")
        if not available_devices:
            missing.append("no_available_ios_simulator")
        if credible_targets and available_devices:
            missing.append("no_native_compatible_destination")
        return {**base, "status": "unavailable", "selection_source": None, "selected": None, "recommendation": None, "reasons": missing}
    recommended_device = next(device for device in devices if device['udid'] in targets[0]['compatible_udids'])
    recommendation = {
        "selected": _selection(root, targets[0], recommended_device, targets[0]['configurations'][0]),
        "rationale": ("Prefer the shared workspace application scheme" if targets[0]["kind"] == "workspace" else "Use the lexically first credible application scheme") + (" and the already booted iOS Simulator device." if recommended_device["state"] == "Booted" else " and the first native-compatible iOS Simulator device."),
    }
    if len(targets) == 1 and len(devices) == 1:
        return {**base, "status": "selected", "selection_source": "only_credible_target", "selected": recommendation["selected"], "recommendation": recommendation}
    return {**base, "status": "requires_choice", "selection_source": None, "selected": None, "recommendation": recommendation, "reasons": ["multiple_credible_choices"]}

def _trusted_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
        return False
    if path.name in TRUSTED_NAMES or path.suffix in TRUSTED_SUFFIXES:
        return True
    return any(part.lower() in SCRIPT_DIRECTORIES for part in relative.parts) and path.suffix not in {".swift", ".m", ".mm", ".c", ".cpp"}

def trusted_build_input_fingerprint(identity: RuntimeIdentity) -> str:
    root = Path(identity.worktree).expanduser().resolve()
    project = Path(identity.project).expanduser().resolve()
    if not root.is_dir() or not project.exists() or root not in (project, *project.parents):
        raise ValueError("build_identity_unavailable")
    manifest: list[dict] = []
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        directories[:] = sorted(directory for directory in directories if directory not in EXCLUDED_DIRECTORIES)
        for name in sorted(files):
            path = current_path / name
            if not _trusted_file(path, root):
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                payload = ("SYMLINK\0" + os.readlink(path)).encode()
            else:
                try:
                    payload = path.read_bytes()
                except OSError as exc:
                    raise ValueError("build_input_unreadable") from exc
            manifest.append({"path": relative, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
    material = {
        "profile": identity.profile,
        "domain_key": identity.domain_key,
        "target_fingerprint": identity.target_fingerprint,
        "project": str(project),
        "scheme": identity.scheme,
        "configuration": identity.configuration,
        "inputs": manifest,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
