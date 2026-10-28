import { expect, test } from 'bun:test';
const root = new URL('../..', import.meta.url);
for (const pattern of ['test_authority.py', 'test_u8_fault_injection.py', 'test_runtime_controller.py']) {
  test(`active cancellation, atomic state and owned process failures: ${pattern}`, () => {
    const child = Bun.spawnSync({ cmd: ['bun', 'scripts/python-tests.ts', '--pattern', pattern], cwd: root.pathname, env: process.env, stdout: 'pipe', stderr: 'pipe' });
    expect(child.exitCode, child.stderr.toString()).toBe(0);
  }, 30_000);
}
