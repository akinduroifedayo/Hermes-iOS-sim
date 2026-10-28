import { expect, test } from 'bun:test';

const read = (path: string) => Bun.file(new URL(`../../${path}`, import.meta.url)).text();

test('display spelling changes without migrating package, tool, dashboard or skill identity', async () => {
  expect(JSON.parse(await read('package.json')).name).toBe('hermes-ios');
  const manifest = await read('plugin/plugin.yaml');
  expect(manifest).toContain('name: hermes-ios\n');
  expect(manifest).toContain('description: Hermes-iOS-sim provides');
  expect(manifest).toContain('  - ios_iteration\n  - ios_setup\n');
  const dashboard = JSON.parse(await read('plugin/dashboard/manifest.json'));
  expect(dashboard.name).toBe('hermes-ios');
  expect(dashboard.label).toBe('Hermes-iOS-sim');
  expect(await read('plugin/skills/ios-development/SKILL.md')).toContain('name: ios-development\n');
  expect(JSON.parse(await read('plugin/dependencies/package.json')).name).toBe('hermes-ios-stock-dependency');
  expect(await read('README.md')).toStartWith('# Hermes-iOS-sim\n');
});
