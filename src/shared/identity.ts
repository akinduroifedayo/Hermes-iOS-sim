import { realpath } from "node:fs/promises";
import { relative, resolve } from "node:path";

export interface TargetSelection {
  scheme: string;
  configuration: string;
  sdk: string;
  destination: string;
}

export interface RuntimeIdentity {
  profile: string;
  worktreeRoot: string;
  projectPath: string;
  target: TargetSelection;
  udid: string;
}

function nonEmpty(label: string, value: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${label} must be non-empty`);
  return normalized;
}

export async function canonicalizeTargetIdentity(input: RuntimeIdentity): Promise<RuntimeIdentity> {
  const worktreeRoot = await realpath(resolve(input.worktreeRoot));
  let projectPath: string;
  try {
    projectPath = await realpath(resolve(input.projectPath));
  } catch {
    throw new Error("project path must exist and be contained in the canonical worktree");
  }
  const rel = relative(worktreeRoot, projectPath);
  if (rel === "" || rel === ".." || rel.startsWith(`..${String.raw`/`}`) || rel.startsWith("../") || rel.startsWith("..\\") || resolve(worktreeRoot, rel) !== projectPath) {
    if (rel !== "") throw new Error("project path must be contained in the canonical worktree");
  }
  return {
    profile: nonEmpty("profile", input.profile),
    worktreeRoot,
    projectPath,
    target: {
      scheme: nonEmpty("scheme", input.target.scheme),
      configuration: nonEmpty("configuration", input.target.configuration),
      sdk: nonEmpty("sdk", input.target.sdk),
      destination: nonEmpty("destination", input.target.destination),
    },
    udid: nonEmpty("UDID", input.udid),
  };
}

export function runtimeIdentityMatches(left: RuntimeIdentity, right: RuntimeIdentity): boolean {
  return left.profile === right.profile && left.worktreeRoot === right.worktreeRoot && left.projectPath === right.projectPath && left.udid === right.udid && left.target.scheme === right.target.scheme && left.target.configuration === right.target.configuration && left.target.sdk === right.target.sdk && left.target.destination === right.target.destination;
}
