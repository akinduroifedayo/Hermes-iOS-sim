import { expect, test } from 'bun:test';
const root = new URL('../..', import.meta.url);
test('runtime package never reads personal host configuration or owns feedback attachments', async () => {
  const files = ['plugin/__init__.py', 'plugin/agent/ios_iteration.py', 'plugin/dashboard/plugin_api.py', 'scripts/package-release.ts'];
  for (const file of files) {
    const source = await Bun.file(new URL(file, root)).text();
    expect(source).not.toMatch(/auth\.json|state\.db|browser_storage|copy.*credentials|Path\.home\(\).*config/);
  }
  const bundle = await Bun.file(new URL('dist/plugin.js', root)).text();
  for (const forbidden of ['composer.middleware', 'composer.attachments', 'insertText', 'submitMessage', 'placement_reference']) expect(bundle).not.toContain(forbidden);
});
test('profile and request authority run through active backend regression tests', () => {
  const child = Bun.spawnSync({ cmd: ['bun', 'scripts/python-tests.ts', '--pattern', 'test_runway_authority_api.py'], cwd: root.pathname, env: process.env, stdout: 'pipe', stderr: 'pipe' });
  if (child.exitCode !== 0) console.error(child.stdout.toString(), child.stderr.toString());
  expect(child.exitCode).toBe(0);
}, 30_000); // real-SDK backend subprocess observed at ~5.6s, past bun's 5s default
