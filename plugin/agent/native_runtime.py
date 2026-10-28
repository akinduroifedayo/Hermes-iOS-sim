from __future__ import annotations

import asyncio
import hashlib
import json
import os
import plistlib
import re
import secrets
import signal
import time
from pathlib import Path

from .ios_iteration import NativeDispatchResult, NativeOperation


_MAX_OUTPUT = 64 * 1024
_PID = re.compile(r":\s*(\d+)\s*$")
_XCODEBUILD_TIMEOUT_SECONDS = 300
_SIMCTL_QUERY_TIMEOUT_SECONDS = 60
_SIMCTL_MUTATION_TIMEOUT_SECONDS = 120
_SIMCTL_BOOT_STATUS_TIMEOUT_SECONDS = 180
_SIMCTL_INSTALL_TIMEOUT_SECONDS = 180


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with child.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _parse_host_process_observation(output: str, pid: int) -> dict[str, str | int] | None:
    for line in output.splitlines():
        fields = line.strip().split(None, 6)
        if len(fields) < 7:
            continue
        try:
            observed_pid = int(fields[0])
        except ValueError:
            continue
        if observed_pid != pid:
            continue
        return {
            "pid": observed_pid,
            "process_generation": " ".join(fields[1:6]),
            "command": fields[6],
        }
    return None


async def _run(argv: tuple[str, ...], cancel_event: asyncio.Event, *, timeout: float = 180.0) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    communicate = asyncio.create_task(process.communicate())
    cancelled = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({communicate, cancelled}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if communicate in done:
            stdout, stderr = communicate.result()
            return process.returncode or 0, stdout[-_MAX_OUTPUT:].decode("utf-8", "replace"), stderr[-_MAX_OUTPUT:].decode("utf-8", "replace")
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(communicate, timeout=2.0)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await communicate
        return 130, "", "cancelled" if cancel_event.is_set() else "deadline_exceeded"
    finally:
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)


async def _checked(argv: tuple[str, ...], cancel_event: asyncio.Event, *, timeout: float = 180.0) -> str:
    if cancel_event.is_set():
        raise RuntimeError("native_cancelled")
    code, stdout, stderr = await _run(argv, cancel_event, timeout=timeout)
    if code != 0:
        detail = (stderr or stdout).strip().splitlines()[-1:] or ["native command failed"]
        raise RuntimeError(f"native_command_failed:{argv[0]}:{detail[0][:500]}")
    return stdout.strip()


def _derived_data(operation: NativeOperation) -> Path:
    try:
        index = operation.argv.index("-derivedDataPath")
        return Path(operation.argv[index + 1]).expanduser().resolve()
    except (ValueError, IndexError):
        raise RuntimeError("derived_data_path_missing") from None


def _single_app(derived_data: Path, settings: str) -> Path:
    """Resolve the application product from the selected invocation, never a stale glob."""
    try:
        entries = json.loads(settings)
        candidates = {
            (Path(entry['buildSettings']['TARGET_BUILD_DIR']) / entry['buildSettings']['FULL_PRODUCT_NAME']).resolve(strict=True)
            for entry in entries
            if entry.get('buildSettings', {}).get('PRODUCT_TYPE') == 'com.apple.product-type.application'
        }
        if len(candidates) != 1:
            raise ValueError('ambiguous')
        app = candidates.pop()
        app.relative_to(derived_data.resolve())
        if app.suffix != '.app' or not app.is_dir():
            raise ValueError('not app')
        return app
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise RuntimeError('built_app_settings_unavailable_or_ambiguous') from None


def _bundle_metadata(app: Path) -> tuple[str, str, Path, bool]:
    try:
        payload = plistlib.loads((app / "Info.plist").read_bytes())
        bundle_id = payload["CFBundleIdentifier"]
        executable_name = payload["CFBundleExecutable"]
        fixture_challenge_required = payload.get("HermesFixtureChallengeRequired", False)
    except (OSError, plistlib.InvalidFileException, KeyError, TypeError):
        raise RuntimeError("built_app_metadata_invalid") from None
    executable = (app / executable_name).resolve(strict=True)
    if not isinstance(bundle_id, str) or not bundle_id or not isinstance(fixture_challenge_required, bool):
        raise RuntimeError("built_app_metadata_invalid")
    return bundle_id, executable_name, executable, fixture_challenge_required


def _simulator_state(payload: str, udid: str, *, runtime: str = "iOS Simulator") -> str:
    try:
        devices = json.loads(payload).get("devices", {})
    except (json.JSONDecodeError, AttributeError):
        raise RuntimeError("simulator_state_unavailable") from None
    matches = [
        (runtime_id, item.get("state"))
        for runtime_id, entries in devices.items()
        if isinstance(entries, list)
        for item in entries
        if isinstance(item, dict) and item.get("udid") == udid
    ]
    if len(matches) != 1 or matches[0][1] not in {"Booted", "Shutdown"}:
        raise RuntimeError("simulator_state_unavailable")
    if runtime != 'iOS Simulator':
        from ..runtime_discovery import _runtime_name
        if _runtime_name(str(matches[0][0])) != runtime:
            raise RuntimeError('simulator_runtime_mismatch')
    return str(matches[0][1])


async def _cleanup_partial_native(evidence: dict) -> dict:
    """Unwind only native resources whose complete tuple was already attested."""
    cleanup_event = asyncio.Event()
    result: dict = {"verified": True, "app": {"not_started": True}, "simulator": {"not_started": True}}
    launch = evidence.get("launch") if isinstance(evidence.get("launch"), dict) else None
    if launch is not None:
        app = {"app_stopped": False, "pid": launch.get("pid")}
        pid = launch.get("pid")
        program_path = launch.get("program_path")
        bundle_id = launch.get("bundle_id")
        udid = launch.get("udid")
        command_succeeded = launch.get("command_succeeded") is True

        async def terminate_exact_bundle() -> None:
            code, _, stderr = await _run(("/usr/bin/xcrun", "simctl", "terminate", udid, bundle_id), cleanup_event, timeout=_SIMCTL_MUTATION_TIMEOUT_SECONDS)
            if code == 0:
                app.update({"app_stopped": True, "cleanup_mode": "exact_bundle"})
            else:
                app["reason"] = f"terminate_failed:{stderr.strip()[-200:]}"
                result["verified"] = False

        if not isinstance(udid, str) or not isinstance(bundle_id, str) or not bundle_id.strip():
            app["reason"] = "launch_bundle_obligation_incomplete"
            result["verified"] = False
        elif command_succeeded and (not isinstance(pid, int) or pid < 1):
            await terminate_exact_bundle()
        elif not isinstance(pid, int) or pid < 1 or not isinstance(program_path, str) or not program_path:
            app["reason"] = "launch_tuple_incomplete"
            result["verified"] = False
        else:
            expected_generation = launch.get("process_generation")
            if isinstance(expected_generation, str) and expected_generation:
                host_code, host_stdout, _ = await _run(
                    ("/bin/ps", "-p", str(pid), "-o", "pid=,lstart=,command="),
                    cleanup_event,
                    timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS,
                )
                host_process = _parse_host_process_observation(host_stdout, pid) if host_code == 0 else None
                if host_process is None or host_process["process_generation"] != expected_generation:
                    app.update({"app_already_exited": True, "app_stopped": False, "process_generation_changed": True})
                else:
                    code, stdout, stderr = await _run(("/usr/bin/xcrun", "simctl", "spawn", udid, "launchctl", "procinfo", str(pid)), cleanup_event, timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS)
                    observed = next((line.removeprefix("program path = ") for line in stdout.splitlines() if line.startswith("program path = ")), "")
                    expected = Path(program_path).expanduser().resolve()
                    path_matches = code == 0 and Path(observed).expanduser().resolve() == expected
                    hash_matches = False
                    if path_matches:
                        try:
                            hash_matches = _sha256_file(expected) == launch.get("executable_hash")
                        except OSError:
                            hash_matches = False
                    after_code, after_stdout, _ = await _run(
                        ("/bin/ps", "-p", str(pid), "-o", "pid=,lstart=,command="),
                        cleanup_event,
                        timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS,
                    )
                    after_process = _parse_host_process_observation(after_stdout, pid) if after_code == 0 else None
                    if after_process is None or after_process["process_generation"] != expected_generation:
                        app.update({"app_already_exited": True, "app_stopped": False, "process_generation_changed": True})
                    elif path_matches and hash_matches:
                        await terminate_exact_bundle()
                    else:
                        app["reason"] = "launch_process_identity_mismatch"
                        result["verified"] = False
            else:
                code, stdout, stderr = await _run(("/usr/bin/xcrun", "simctl", "spawn", udid, "launchctl", "procinfo", str(pid)), cleanup_event, timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS)
                observed = next((line.removeprefix("program path = ") for line in stdout.splitlines() if line.startswith("program path = ")), "")
                exact_pid = code == 0 and Path(observed).expanduser().resolve() == Path(program_path).expanduser().resolve()
                if exact_pid:
                    await terminate_exact_bundle()
                elif command_succeeded:
                    app["observation"] = "ambiguous"
                    await terminate_exact_bundle()
                else:
                    process_error = f"{stdout}\n{stderr}".lower()
                    if any(marker in process_error for marker in ("no such process", "could not find", "not found", "does not exist")):
                        app.update({"app_already_exited": True, "app_stopped": False})
                    else:
                        app["reason"] = "process_observation_ambiguous"
                        result["verified"] = False
        result["app"] = app

    simulator = evidence.get("simulator") if isinstance(evidence.get("simulator"), dict) else None
    if simulator is not None:
        ownership = simulator.get("ownership")
        udid = simulator.get("udid")
        sim_result = {"simulator_ownership": ownership, "udid": udid}
        if ownership == "attached":
            sim_result["simulator_stopped"] = False
            sim_result["attached_simulator_preserved"] = True
        elif ownership == "owned" and isinstance(udid, str):
            state_payload = await _run(("/usr/bin/xcrun", "simctl", "list", "devices", "available", "--json"), cleanup_event, timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS)
            if state_payload[0] != 0:
                sim_result["reason"] = "simulator_state_unavailable"
                result["verified"] = False
            else:
                try:
                    state = _simulator_state(state_payload[1], udid)
                except RuntimeError:
                    state = "unknown"
                if state == "Shutdown":
                    sim_result["simulator_stopped"] = True
                elif state == "Booted":
                    code, _, stderr = await _run(("/usr/bin/xcrun", "simctl", "shutdown", udid), cleanup_event, timeout=_SIMCTL_MUTATION_TIMEOUT_SECONDS)
                    if code == 0:
                        sim_result["simulator_stopped"] = True
                    else:
                        sim_result["reason"] = f"simulator_shutdown_failed:{stderr.strip()[-200:]}"
                        result["verified"] = False
                else:
                    sim_result["reason"] = "simulator_state_unavailable"
                    result["verified"] = False
        else:
            sim_result["reason"] = "simulator_ownership_unavailable"
            result["verified"] = False
        result["simulator"] = sim_result
    return result


async def _read_challenge(udid: str, bundle_id: str, expected: str, cancel_event: asyncio.Event) -> tuple[str, Path]:
    data_container = Path(await _checked(("/usr/bin/xcrun", "simctl", "get_app_container", udid, bundle_id, "data"), cancel_event, timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS)).resolve(strict=True)
    preferences = data_container / "Library" / "Preferences" / f"{bundle_id}.plist"
    for _ in range(150):
        if cancel_event.is_set():
            break
        try:
            payload = plistlib.loads(preferences.read_bytes())
            observed = payload.get("HermesChallengeResponse")
            if observed == expected:
                return observed, data_container
        except (OSError, plistlib.InvalidFileException, TypeError):
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("fixture_challenge_unverified")


async def native_runtime_dispatch(operation: NativeOperation, cancel_event: asyncio.Event) -> NativeDispatchResult:
    """Execute one exact validated build/install/launch operation and corroborate its result."""
    started_at_ms = int(time.time() * 1000)
    stage = "building"
    evidence: dict = {"native_started": False, "build_attempt_id": operation.build_attempt_id, "stage": stage}
    try:
        evidence["native_started"] = True
        operation.report_phase("building", {"tool": "xcodebuild"})
        await _checked(operation.argv, cancel_event, timeout=_XCODEBUILD_TIMEOUT_SECONDS)
        evidence['build'] = {'outcome': 'verified', 'argv': list(operation.argv)}
        stage = 'resolve_artifact'
        derived_data = _derived_data(operation)
        scheme = operation.argv[operation.argv.index("-scheme") + 1]
        configuration = operation.argv[operation.argv.index("-configuration") + 1]
        destination = operation.argv[operation.argv.index("-destination") + 1]
        udid = destination.rsplit("id=", 1)[-1]
        settings = await _checked(operation.argv[:-1] + ('-showBuildSettings', '-json'), cancel_event,
                                  timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS)
        app = _single_app(derived_data, settings)
        evidence['build'] = {'outcome': 'verified', 'argv': list(operation.argv), 'configuration': configuration}
        bundle_id, executable_name, executable, fixture_challenge_required = _bundle_metadata(app)
        app_hash = _sha256_tree(app)
        executable_hash = _sha256_file(executable)
        evidence["artifact"] = {"path": str(app), "app_hash": app_hash, "bundle_id": bundle_id, "executable_hash": executable_hash}

        stage = "booting"
        operation.report_phase("booting", {"udid": udid})
        state_before = _simulator_state(
            await _checked(("/usr/bin/xcrun", "simctl", "list", "devices", "available", "--json"), cancel_event, timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS),
            udid,
            runtime=operation.runtime,
        )
        simulator_ownership = "attached"
        if state_before == "Shutdown":
            evidence["simulator"] = {
                "udid": udid,
                "ownership": "attached",
                "state_before": state_before,
                "state_after": "boot-command-pending",
                "mutation_attempted": True,
                "outcome": "unknown",
            }
            await _checked(("/usr/bin/xcrun", "simctl", "boot", udid), cancel_event, timeout=_SIMCTL_MUTATION_TIMEOUT_SECONDS)
            simulator_ownership = "attached"
            evidence["simulator"].update({"state_after": "boot-command-succeeded", "outcome": "succeeded"})
        else:
            evidence["simulator"] = {"udid": udid, "ownership": simulator_ownership, "state_before": state_before, "state_after": "attached"}
        await _checked(("/usr/bin/xcrun", "simctl", "bootstatus", udid, "-b"), cancel_event, timeout=_SIMCTL_BOOT_STATUS_TIMEOUT_SECONDS)
        evidence["simulator"] = {
            "udid": udid,
            "ownership": simulator_ownership,
            "state_before": state_before,
            "state_after": "Booted",
            "mutation_attempted": simulator_ownership == "owned",
            "outcome": "verified",
        }
        stage = "installing"
        operation.report_phase("installing", {"udid": udid, "bundle_id": bundle_id})
        evidence["install"] = {
            "udid": udid,
            "bundle_id": bundle_id,
            "installed_app": None,
            "executable_hash": executable_hash,
            "observed_at_ms": int(time.time() * 1000),
            "mutation_attempted": True,
            "outcome": "unknown",
        }
        await _checked(("/usr/bin/xcrun", "simctl", "install", udid, str(app)), cancel_event, timeout=_SIMCTL_INSTALL_TIMEOUT_SECONDS)
        installed_at_ms = int(time.time() * 1000)
        evidence["install"] = {"udid": udid, "bundle_id": bundle_id, "installed_app": None, "executable_hash": executable_hash, "observed_at_ms": installed_at_ms, "provisional": True}
        stage = "verify_install"
        installed_app = Path(await _checked(("/usr/bin/xcrun", "simctl", "get_app_container", udid, bundle_id, "app"), cancel_event, timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS)).resolve(strict=True)
        installed_executable = (installed_app / executable_name).resolve(strict=True)
        evidence["install"] = {
            "udid": udid,
            "bundle_id": bundle_id,
            "installed_app": str(installed_app),
            "executable_hash": executable_hash,
            "observed_at_ms": installed_at_ms,
            "mutation_attempted": True,
            "outcome": "succeeded",
        }
        if _sha256_file(installed_executable) != executable_hash:
            raise RuntimeError("installed_executable_mismatch")
        evidence['install']['outcome'] = 'verified'

        stage = "launching"
        operation.report_phase("launching", {"udid": udid, "bundle_id": bundle_id})
        launch_argv = ("/usr/bin/xcrun", "simctl", "launch", "--terminate-running-process", udid, bundle_id)
        expected_challenge = None
        if fixture_challenge_required:
            challenge_token = secrets.token_urlsafe(18).replace("-", "_")
            expected_challenge = f"challenge-response:{challenge_token}:HERMES-IOS-U1-OK"
            launch_argv += ("--hermes-challenge", challenge_token)
        evidence["launch"] = {
            "udid": udid,
            "bundle_id": bundle_id,
            "program_path": str(installed_executable),
            "executable_hash": executable_hash,
            "pid": None,
            "launched_at_ms": int(time.time() * 1000),
            "mutation_attempted": True,
            "command_succeeded": False,
            "outcome": "unknown",
        }
        launch_output = await _checked(
            launch_argv,
            cancel_event,
            timeout=_SIMCTL_MUTATION_TIMEOUT_SECONDS,
        )
        launched_at_ms = int(time.time() * 1000)
        evidence["launch"].update({"launched_at_ms": launched_at_ms, "command_succeeded": True, "outcome": "succeeded"})
        match = _PID.search(launch_output)
        if match is None or int(match.group(1)) < 1:
            raise RuntimeError("launch_pid_unavailable")
        pid = int(match.group(1))
        evidence["launch"]["pid"] = pid
        stage = 'verify_pid'
        host_process_output = await _checked(
            ("/bin/ps", "-p", str(pid), "-o", "pid=,lstart=,command="),
            cancel_event,
            timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS,
        )
        host_process = _parse_host_process_observation(host_process_output, pid)
        if host_process is None:
            raise RuntimeError("launch_pid_unverified")
        command = str(host_process['command'])
        if Path(command).expanduser().resolve() != installed_executable and not command.startswith(str(installed_executable) + ' '):
            raise RuntimeError('launch_pid_unverified')
        host_program = installed_executable
        evidence["launch"].update({
            "process_generation": host_process["process_generation"],
            "host_program_path": str(host_program),
            "process_generation_source": "host_ps_lstart",
        })
        stage = "verify_pid"
        process_output = await _checked(("/usr/bin/xcrun", "simctl", "spawn", udid, "launchctl", "procinfo", str(pid)), cancel_event, timeout=_SIMCTL_QUERY_TIMEOUT_SECONDS)
        program_line = next((line for line in process_output.splitlines() if line.startswith("program path = ")), "")
        observed_program = Path(program_line.removeprefix("program path = ")).resolve() if program_line else None
        if observed_program != installed_executable:
            raise RuntimeError("launch_pid_unverified")
        evidence["launch"].update({"program_path": str(installed_executable), "provisional": False, "outcome": "verified"})

        if fixture_challenge_required:
            stage = "verify_challenge"
            assert expected_challenge is not None
            observed_challenge, _ = await _read_challenge(udid, bundle_id, expected_challenge, cancel_event)
            challenge_at_ms = int(time.time() * 1000)
            challenge = {"required": True, "expected": expected_challenge, "observed": observed_challenge, "observed_at_ms": challenge_at_ms}
        else:
            challenge_at_ms = launched_at_ms
            challenge = {"required": False, "expected": None, "observed": None, "observed_at_ms": challenge_at_ms}
        completed_at_ms = int(time.time() * 1000)
        evidence = {
            "native_started": True,
            "build_attempt_id": operation.build_attempt_id,
            "configuration": configuration,
            "native_dispatch": {"kind": "direct_apple_tool", "tool": "xcodebuild", "args_hash": operation.args_hash, "started_at_ms": started_at_ms, "completed_at_ms": completed_at_ms},
            "artifact": {"path": str(app), "app_hash": app_hash, "bundle_id": bundle_id, "executable_hash": executable_hash},
            "install": {"udid": udid, "bundle_id": bundle_id, "installed_app": str(installed_app), "executable_hash": executable_hash, "observed_at_ms": installed_at_ms, "outcome": "verified"},
            "launch": {
                "udid": udid,
                "bundle_id": bundle_id,
                "program_path": str(installed_executable),
                "executable_hash": executable_hash,
                "pid": pid,
                "launched_at_ms": launched_at_ms,
                "process_generation": host_process["process_generation"],
                "host_program_path": str(host_program),
                "process_generation_source": "host_ps_lstart",
                "outcome": "verified",
            },
            "challenge": challenge,
            "visual_provenance": "unknown",
            "build": evidence["build"],
            "simulator": {"udid": udid, "ownership": simulator_ownership, "state_before": state_before, "state_after": "Booted"},
            "attached_simulator_preserved": simulator_ownership == "attached",
        }
        return NativeDispatchResult(ok=True, code="completed", evidence=evidence)
    except (RuntimeError, OSError, ValueError) as error:
        evidence["stage"] = stage
        evidence["detail"] = str(error)[:1000]
        evidence["cleanup"] = await _cleanup_partial_native(evidence)
        return NativeDispatchResult(
            ok=False,
            code=str(error).split(":", 1)[0],
            evidence=evidence,
        )
