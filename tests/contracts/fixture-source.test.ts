import { describe, expect, test } from "bun:test";
import { readFile } from "node:fs/promises";

describe("VisualIterationFixture source contract", () => {
  test("exposes deterministic version, navigation, crash, and challenge identifiers", async () => {
    const source = await readFile(new URL("../../fixtures/VisualIterationFixture/VisualIterationFixture/VisualIterationFixtureApp.swift", import.meta.url), "utf8");
    for (const token of ["HERMES-IOS-U1-v1", "fixture.home.open-details", "fixture.settings.title", "fixture.crash.trigger", "HERMES-IOS-U1-DETERMINISTIC-CRASH", "fixture.challenge.trigger", "fixture.challenge.response", "--hermes-challenge", "challenge-response:\\(challengeToken):HERMES-IOS-U1-OK"]) expect(source).toContain(token);
    const buildScript = await readFile(new URL("../../fixtures/VisualIterationFixture/build.sh", import.meta.url), "utf8");
    expect(buildScript).toContain("validate_project.py");
    expect(buildScript).toContain("-showBuildSettings");
    const project = await readFile(new URL("../../fixtures/VisualIterationFixture/VisualIterationFixture.xcodeproj/project.pbxproj", import.meta.url), "utf8");
    expect(project.match(/INFOPLIST_KEY_HermesFixtureChallengeRequired = YES/g)).toHaveLength(2);
  });
});
