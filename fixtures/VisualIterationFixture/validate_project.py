#!/usr/bin/env python3
"""Validate the fixture target graph after plutil converts project.pbxproj to JSON."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn, cast


def fail(message: str) -> NoReturn:
    raise SystemExit(f"fixture project validation failed: {message}")


def main() -> None:
    if len(sys.argv) != 3:
        fail("expected <project-json> <source-relative-path>")
    project_json = Path(sys.argv[1])
    expected_source = sys.argv[2]
    document = json.loads(project_json.read_text())
    objects = cast(dict[str, dict[str, Any]], document.get("objects"))
    root_ref = document.get("rootObject")
    if not isinstance(objects, dict) or root_ref not in objects:
        fail("missing object graph or root project")
    project = objects[root_ref]
    target_refs = project.get("targets", [])
    targets = [objects.get(ref) for ref in target_refs]
    target = next((candidate for candidate in targets if isinstance(candidate, dict) and candidate.get("name") == "VisualIterationFixture"), None)
    if target is None or target.get("productType") != "com.apple.product-type.application":
        fail("missing iOS application target")
    source_phases = [objects.get(ref) for ref in target.get("buildPhases", [])]
    source_phase = next((phase for phase in source_phases if isinstance(phase, dict) and phase.get("isa") == "PBXSourcesBuildPhase"), None)
    if source_phase is None:
        fail("target has no Sources build phase")
    source_paths: set[str] = set()
    for build_file_ref in source_phase.get("files", []):
        build_file = objects.get(build_file_ref)
        if not isinstance(build_file, dict) or build_file.get("isa") != "PBXBuildFile":
            fail("Sources phase contains an invalid build file")
        file_ref = build_file.get("fileRef")
        if not isinstance(file_ref, str):
            fail("build file has no file-reference identity")
        file_reference = objects.get(file_ref)
        if not isinstance(file_reference, dict) or file_reference.get("isa") != "PBXFileReference":
            fail("build file has no valid file reference")
        path = file_reference.get("path")
        if isinstance(path, str):
            source_paths.add(path)
    if expected_source not in source_paths:
        fail(f"target Sources phase does not contain {expected_source}")
    print(f"Validated Xcode target source membership: {expected_source}")


if __name__ == "__main__":
    main()
