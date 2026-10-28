import { afterEach, describe, expect, test } from "bun:test";
import { mkdir, mkdtemp, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { canonicalizeTargetIdentity, runtimeIdentityMatches } from "../../src/shared/identity.ts";

const roots: string[] = [];
afterEach(async () => Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))));

describe("canonical target identity", () => {
  test("canonicalizes an in-worktree workspace and compares the full runtime tuple", async () => {
    const root = await mkdtemp(join(tmpdir(), "hermes-ios-worktree-")); roots.push(root);
    await mkdir(join(root, "App.xcworkspace"));
    const identity = await canonicalizeTargetIdentity({ profile: "default", worktreeRoot: root, projectPath: join(root, "App.xcworkspace"), target: { scheme: "App", configuration: "Debug", sdk: "iphonesimulator", destination: "platform=iOS Simulator" }, udid: "SIM-1" });
    expect(identity.worktreeRoot).toBe(await realpath(root));
    expect(runtimeIdentityMatches(identity, { ...identity })).toBe(true);
    expect(runtimeIdentityMatches(identity, { ...identity, udid: "SIM-2" })).toBe(false);
  });

  test("rejects lexical and symlink escapes after realpath resolution", async () => {
    const root = await mkdtemp(join(tmpdir(), "hermes-ios-worktree-")); roots.push(root);
    const outside = await mkdtemp(join(tmpdir(), "hermes-ios-outside-")); roots.push(outside);
    await writeFile(join(outside, "Escape.xcodeproj"), "fixture");
    await symlink(outside, join(root, "linked"));
    await expect(canonicalizeTargetIdentity({ profile: "default", worktreeRoot: root, projectPath: join(root, "..", "Escape.xcodeproj"), target: { scheme: "App", configuration: "Debug", sdk: "iphonesimulator", destination: "generic/platform=iOS Simulator" }, udid: "SIM-1" })).rejects.toThrow(/contained/i);
    await expect(canonicalizeTargetIdentity({ profile: "default", worktreeRoot: root, projectPath: join(root, "linked", "Escape.xcodeproj"), target: { scheme: "App", configuration: "Debug", sdk: "iphonesimulator", destination: "generic/platform=iOS Simulator" }, udid: "SIM-1" })).rejects.toThrow(/contained/i);
  });
});
