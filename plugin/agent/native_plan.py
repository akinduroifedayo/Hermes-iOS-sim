from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeIdentity:
    profile: str
    worktree: str
    project: str
    scheme: str
    configuration: str
    runtime: str
    udid: str
    target_fingerprint: str
    domain_key: str


def build_runtime_identity(*, profile: str, worktree: Path, project: Path, scheme: str, configuration: str, runtime: str, udid: str) -> RuntimeIdentity:
    material = {
        "profile": profile,
        "worktree": str(worktree),
        "project": str(project),
        "scheme": scheme,
        "configuration": configuration,
        "runtime": runtime,
        "udid": udid,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()
    domain_material = json.dumps({"profile": profile, "worktree": str(worktree), "target": fingerprint, "udid": udid}, sort_keys=True, separators=(",", ":")).encode()
    return RuntimeIdentity(**material, target_fingerprint=f"sha256:{fingerprint}", domain_key=f"domain:{hashlib.sha256(domain_material).hexdigest()}")


def build_native_plan(identity: RuntimeIdentity, *, data_dir: Path | None = None) -> tuple[tuple[str, ...], str]:
    """Construct the exact plugin-owned Xcode build argv deterministically."""
    project_flag = "-workspace" if identity.project.endswith(".xcworkspace") else "-project"
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
    derived_data = (data_dir or home / "hermes-ios") / "derived-data" / identity.target_fingerprint.removeprefix("sha256:")[:24]
    argv = (
        "/usr/bin/xcodebuild",
        project_flag,
        identity.project,
        "-scheme",
        identity.scheme,
        "-configuration",
        identity.configuration,
        "-destination",
        f"platform=iOS Simulator,id={identity.udid}",
        "-derivedDataPath",
        str(derived_data),
        "CODE_SIGNING_ALLOWED=NO",
        "build",
    )
    args_hash = hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode()).hexdigest()
    return argv, f"sha256:{args_hash}"
