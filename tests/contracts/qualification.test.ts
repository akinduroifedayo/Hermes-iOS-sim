import { expect, test } from 'bun:test';
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { installedGates, qualify, verifyInstalledReceipts } from '../../scripts/qualify.ts';
import { packageRelease } from '../../scripts/package-release.ts';

test('missing installed receipts are unproven, never PASS', () => {
  const prior = process.env.HERMES_IOS_INSTALLED_RECEIPTS;
  delete process.env.HERMES_IOS_INSTALLED_RECEIPTS;
  try { expect(() => qualify()).toThrow('installed_qualification_unproven:missing:'); }
  finally { if (prior !== undefined) process.env.HERMES_IOS_INSTALLED_RECEIPTS = prior; }
});

test('historical claim/restore/comparability acceptance cannot qualify stock v1', () => {
  const root = mkdtempSync(join(tmpdir(), 'u7-qualify-'));
  try {
    const release = packageRelease({ output: join(root, 'release') });
    const home = join(root, 'home'); mkdirSync(home);
    expect(() => verifyInstalledReceipts({ status: 'admitted', installed_journey: { stages: { claim: { status: 'passed' } } } }, { home, manifestPath: release.manifestPath, evidenceRoot: root })).toThrow('installed_receipt_identity_mismatch');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('v1 installed matrix retains every native, feedback, failure and cleanup gate', () => {
  expect(installedGates).toEqual(['clean_setup', 'first_automatic_open', 'repeat_run', 'feedback_loop', 'app_data', 'compile_failure', 'install_launch_failure', 'occupied_port', 'crashed_server', 'preview_close', 'cancellation_reload_disable', 'legacy_conflict', 'isolation_teardown']);
});
