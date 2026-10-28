import { expect, test } from 'bun:test';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { installPlan, isolatedHome, legacyConflict, preserveLegacy, rollbackLegacy } from '../../scripts/install-local.ts';
import { packageRelease, verifyRelease } from '../../scripts/package-release.ts';

// mkdtempSync under tmpdir() can resolve through a symlinked ancestor (e.g. macOS
// /var -> /private/var); canonicalize so the fixture itself is not mistaken for the
// symlinked-home case isolatedHome() is guarding against below.
const isolatedRoot = () => realpathSync(mkdtempSync(join(tmpdir(), 'u7-install-')));

test('normal admission preview is inert, single-package and rejects tampered bytes', () => {
  const root = isolatedRoot();
  try {
    const home = join(root, 'home'); mkdirSync(home); writeFileSync(join(home, '.hermes-ios-isolated-home'), 'synthetic');
    const release = packageRelease({ output: join(root, 'release') });
    const plan = installPlan({ home, candidate: release.candidatePath, manifest: release.manifestPath });
    expect(plan.command).toEqual(['hermes', 'plugins', 'install', `file://${release.candidatePath}`, '--no-deps', '--no-enable']);
    expect(plan.conflict).toBeNull();
    expect(existsSync(join(home, 'plugins'))).toBe(false);
    expect(existsSync(join(home, 'desktop-plugins'))).toBe(false);
    const manifest = JSON.parse(readFileSync(release.manifestPath, 'utf8'));
    writeFileSync(join(release.candidatePath, 'plugin.yaml'), 'tampered');
    expect(() => verifyRelease(release.candidatePath, manifest)).toThrow('release_inventory_mismatch');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('a symlinked home is rejected even when it carries the isolated-home marker', () => {
  const root = isolatedRoot();
  try {
    const real = join(root, 'real-home'); mkdirSync(real); writeFileSync(join(real, '.hermes-ios-isolated-home'), 'synthetic');
    const home = join(root, 'home-link'); symlinkSync(real, home);
    expect(() => isolatedHome(home)).toThrow('isolated_home_symlink');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('legacy backup alias cannot leave preserved code inside Desktop discovery', () => {
  const root = mkdtempSync(join(tmpdir(), 'u7-backup-alias-'));
  try {
    const discovery = join(root, 'desktop-plugins');
    const old = join(discovery, 'hermes-ios'); mkdirSync(old, { recursive: true });
    writeFileSync(join(old, 'plugin.js'), 'legacy fixture code');
    writeFileSync(join(old, 'historical.json'), 'historical bytes');
    const alias = join(root, 'backup-alias'); symlinkSync(discovery, alias);
    const conflict = legacyConflict(root, join(root, 'candidate'))!;
    expect(() => preserveLegacy(conflict, join(alias, 'legacy-preserved'))).toThrow('legacy_backup_would_duplicate_ui');
    expect(readFileSync(join(old, 'historical.json'), 'utf8')).toBe('historical bytes');
    expect(legacyConflict(root, join(root, 'candidate'))?.sha256).toBe(conflict.sha256);
    expect(existsSync(join(discovery, 'legacy-preserved'))).toBe(false);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('legacy same-ID conflict names exact paths; explicit resolution and rollback preserve all fixture bytes', () => {
  const root = mkdtempSync(join(tmpdir(), 'u7-conflict-'));
  try {
    const old = join(root, 'desktop-plugins/hermes-ios'); mkdirSync(old, { recursive: true });
    writeFileSync(join(old, 'plugin.js'), 'legacy fixture code\n'); writeFileSync(join(old, 'historical.json'), 'historical bytes');
    const candidate = join(root, 'candidate');
    const conflict = legacyConflict(root, candidate)!;
    expect(conflict.legacyPath).toBe(old);
    expect(conflict.candidatePath).toBe(join(candidate, 'desktop/plugin.js'));
    expect(readFileSync(join(old, 'plugin.js'), 'utf8')).toBe('legacy fixture code\n');
    const backup = join(root, 'legacy-preserved'); preserveLegacy(conflict, backup);
    expect(legacyConflict(root, candidate)).toBeNull();
    expect(readFileSync(join(backup, 'historical.json'), 'utf8')).toBe('historical bytes');
    rollbackLegacy(conflict, backup);
    expect(readFileSync(join(old, 'plugin.js'), 'utf8')).toBe('legacy fixture code\n');
    expect(readFileSync(join(old, 'historical.json'), 'utf8')).toBe('historical bytes');
    expect(legacyConflict(root, candidate)?.sha256).toBe(conflict.sha256);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
