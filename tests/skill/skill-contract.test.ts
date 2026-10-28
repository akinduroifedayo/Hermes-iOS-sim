import { describe, expect, test } from "bun:test";
import { readFile } from "node:fs/promises";

const path = new URL("../../plugin/skills/ios-development/SKILL.md", import.meta.url);

describe("bundled host-native iOS workflow policy (text, not agent execution)", () => {
  test("supports ordinary native tooling and optional coordinator with shared preview lifecycle", async () => {
    const skill = await readFile(path, "utf8");
    expect(skill).toMatch(/^---\nname: ios-development\ndescription: Use for iOS app iteration in Hermes Desktop with the hermes-ios plugin enabled\.\n---\n/);
    for (const text of ['ios_iteration(action="discover")', 'ios_iteration(action="preflight")', 'ios_setup(action="provision")', 'ios_iteration(action="run")', 'ios_iteration(action="preview")', 'Xcode/simctl', 'optional', 'action="stop"', 'resume', 'desktop_preview', 'same originating agent turn', 'preview_action', 'receipt', 'outcomes', 'preview_url', 'Never return only a URL', 'not an atomic callback', 'ordinary Hermes source-reading and editing tools']) expect(skill).toContain(text);
    for (const historical of ['@hermes-ios-review', 'action="claim"', 'Review Mark', 'placement_reference', 'Runway', 'ctx.dispatch_tool']) expect(skill).not.toContain(historical);
  });
  test("requires automatic card handoff after the originating public open", async () => {
    const skill = await readFile(path, "utf8");
    for (const text of ['app_card.status', 'app_card.view.directive', 'app_card.code', 'automatically prepares', 'previous version', 'Ready to open', 'Last Preview operation']) expect(skill).toContain(text);
    expect(skill).not.toContain('Cards are optional');
  });
  test("documents preparation failure isolation and historical Preview evidence", async () => {
    const docs = await readFile(new URL("../../docs/contextual-cards.md", import.meta.url), "utf8");
    for (const text of ['app_card_preparation_failed', 'card_capacity_reached', 'Cancellation propagates', 'conversation admission', 'never call either fingerprint function', 'concurrent matching insertion wins', 'closed Details', 'Last Preview operation', 'previous version']) expect(docs).toContain(text);
    const schema = await readFile(new URL("../../plugin/schemas.py", import.meta.url), "utf8");
    for (const text of ['automatically prepares app_card', 'app_card.view.directive', 'app_card.code', 'same originating agent turn']) expect(schema).toContain(text);
  });
  test("keeps annotation/Send host-owned and requires actual image inspection before edits", async () => {
    const skill = await readFile(path, "utf8");
    for (const text of ['ordinary Send', 'vision_analyze', 'image_url', 'question', 'before editing', 'unreadable', 'do not guess', 'full image first', 'same URL', 'Never close', 'polling', 'unavailable', 'visible pixels', 'unknown', 'authorized worktree']) expect(skill).toContain(text);
    expect(skill).not.toMatch(/latest.feedback|composer middleware|attachment provider/i);
  });
});
