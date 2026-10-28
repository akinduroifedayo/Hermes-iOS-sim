import { existsSync } from "node:fs";

function pythonCandidates(): string[] {
  const configured = process.env.PYTHON?.trim();
  const candidates = configured ? [configured] : ["python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"];
  return [...new Set(candidates)];
}

export function selectPython(): string {
  for (const candidate of pythonCandidates()) {
    if (candidate.includes("/") && !existsSync(candidate)) continue;
    const probe = Bun.spawnSync({ cmd: [candidate, "-c", "import sys, fastapi; assert sys.version_info >= (3, 11)"], stdout: "ignore", stderr: "ignore" });
    if (probe.exitCode === 0) return candidate;
  }
  throw new Error("No Python 3.11+ interpreter with the active backend's FastAPI dependency is available. Set PYTHON to the qualified interpreter.");
}

export function pythonTestCommand(args = process.argv.slice(2)): string[] {
  const patternIndex = args.indexOf("--pattern");
  const pattern = patternIndex >= 0 ? args[patternIndex + 1] : undefined;
  if (patternIndex >= 0 && (!pattern || pattern.startsWith("--"))) throw new Error("--pattern requires a unittest discovery pattern");
  return [selectPython(), "-m", "unittest", "discover", "-s", "tests/agent", ...(pattern ? ["-p", pattern] : [])];
}

if (import.meta.main) {
  const command = pythonTestCommand();
  const child = Bun.spawnSync({ cmd: command, stdin: "inherit", stdout: "inherit", stderr: "inherit", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  process.exit(child.exitCode);
}
