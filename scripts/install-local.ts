import { existsSync, lstatSync, readFileSync, realpathSync, renameSync } from 'node:fs';
import { basename, dirname, join, resolve, sep } from 'node:path';
import { homedir } from 'node:os';
import { sha256, verifyRelease, type ReleaseManifest } from './package-release.ts';

export interface LegacyConflict { legacyPath: string; candidatePath: string; sha256: string; reason: string }
export function legacyConflict(home: string, candidate: string): LegacyConflict | null {
  const legacyPath = join(home, 'desktop-plugins/hermes-ios'), file = join(legacyPath, 'plugin.js');
  if (!existsSync(legacyPath)) return null;
  if (lstatSync(legacyPath).isSymbolicLink()) throw new Error(`legacy_symlink_conflict:${legacyPath}`);
  if (!existsSync(file)) return null; // Host handles interrupted, empty copies.
  const marker = join(legacyPath, '.hermes-package.json');
  if (existsSync(marker)) {
    const data = JSON.parse(readFileSync(marker, 'utf8'));
    if (data.package === 'hermes-ios' && typeof data.source === 'string' && resolve(data.source) === resolve(home, 'plugins/hermes-ios/desktop')) return null;
  }
  return { legacyPath, candidatePath: join(candidate, 'desktop/plugin.js'), sha256: sha256(file), reason: 'Standalone or differently marked same-ID Desktop plugin. Disable it, then explicitly preserve/move this directory outside desktop-plugins before enabling the unified candidate.' };
}
export function preserveLegacy(conflict: LegacyConflict, backup: string): void {
  backup = resolve(backup);
  const discoveryRoot = realpathSync(dirname(conflict.legacyPath));
  backup = join(realpathSync(dirname(backup)), basename(backup));
  if (backup === discoveryRoot || backup.startsWith(discoveryRoot + sep)) throw new Error('legacy_backup_would_duplicate_ui');
  if (existsSync(backup)) throw new Error(`legacy_backup_exists:${backup}`);
  if (sha256(join(conflict.legacyPath, 'plugin.js')) !== conflict.sha256) throw new Error('legacy_changed_since_disclosure');
  renameSync(conflict.legacyPath, backup); // Exact bytes, no migration/deletion.
}
export function rollbackLegacy(conflict: LegacyConflict, backup: string): void {
  if (existsSync(conflict.legacyPath)) throw new Error('legacy_rollback_destination_occupied');
  if (sha256(join(backup, 'plugin.js')) !== conflict.sha256) throw new Error('legacy_backup_changed');
  renameSync(backup, conflict.legacyPath);
}
export function isolatedHome(home: string): string {
  const absolute = resolve(home);
  const daily = resolve(homedir(), '.hermes');
  if (absolute === daily || absolute.startsWith(join(daily, 'profiles') + sep)) throw new Error('active_home_or_profile_forbidden');
  if (lstatSync(absolute).isSymbolicLink() || realpathSync(absolute) !== absolute) throw new Error('isolated_home_symlink');
  if (!existsSync(join(absolute, '.hermes-ios-isolated-home'))) throw new Error('isolated_home_marker_required');
  for (const child of ['plugins', 'desktop-plugins']) if (existsSync(join(absolute, child)) && lstatSync(join(absolute, child)).isSymbolicLink()) throw new Error(`isolated_child_symlink:${child}`);
  return absolute;
}
export function installPlan(options: { home: string; candidate: string; manifest: string; identifier?: string; cli?: string; ref?: string }) {
  const home = isolatedHome(options.home), candidate = realpathSync(options.candidate);
  const manifest = JSON.parse(readFileSync(options.manifest, 'utf8')) as ReleaseManifest;
  verifyRelease(candidate, manifest);
  const conflict = legacyConflict(home, candidate);
  const identifier = options.identifier ?? `file://${candidate}`;
  if (options.ref !== undefined && !/^[A-Fa-f0-9]{40}$/.test(options.ref)) throw new Error('install_ref_must_be_exact_40_character_commit');
  // This host's install accepts Git URLs, not raw directory/ZIP uploads.
  // Never manufacture approval or silently fall back to a two-root copy.
  return { home, candidate, manifestSha256: sha256(options.manifest), conflict,
    command: [options.cli ?? 'hermes', 'plugins', 'install', identifier, ...(options.ref ? ['--ref', options.ref] : []), '--no-deps', '--no-enable'],
    effects: 'Public CLI admission into the isolated home, disabled. No stock provision, native run, Desktop copy or activation. A local file:// install requires a Git source; doctor/validate accept the unified directory directly.' };
}
if (import.meta.main) {
  const arg = (name: string) => { const i = process.argv.indexOf(name); return i >= 0 ? process.argv[i + 1] : undefined; };
  const plan = installPlan({ home: arg('--home') ?? '', candidate: arg('--candidate') ?? '', manifest: arg('--manifest') ?? '', identifier: arg('--identifier'), cli: arg('--cli'), ref: arg('--ref') });
  if (plan.conflict) { console.log(JSON.stringify(plan, null, 2)); process.exit(2); }
  if (process.argv.includes('--apply')) {
    const child = Bun.spawnSync({ cmd: plan.command, env: { ...process.env, HERMES_HOME: plan.home }, stdin: 'inherit', stdout: 'inherit', stderr: 'inherit' });
    if (child.exitCode !== 0) process.exit(child.exitCode);
    const manifest = JSON.parse(readFileSync(arg('--manifest')!, 'utf8')) as ReleaseManifest;
    const installed = join(plan.home, 'plugins/hermes-ios');
    for (const file of manifest.files) {
      if (sha256(join(installed, file.path)) !== file.sha256) throw new Error(`installed_artifact_mismatch:${file.path}`);
    }
    console.log(JSON.stringify({ status: 'installed_disabled_bytes_verified', home: plan.home, manifestSha256: plan.manifestSha256, candidatePath: installed, files: manifest.files.length, desktopLoaded: 'unproven' }, null, 2));
  }
  console.log(JSON.stringify(plan, null, 2));
}
