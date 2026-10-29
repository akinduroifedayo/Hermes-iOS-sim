import { createHash } from 'node:crypto';
import { copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';

export interface ReleaseFile { path: string; sha256: string; size: number }
export interface ReleaseManifest { schema: 'hermes-ios-unified-release/v1'; plugin: string; version: string; files: ReleaseFile[] }
const REPO = resolve(import.meta.dir, '..');
export function sha256(path: string): string { return createHash('sha256').update(readFileSync(path)).digest('hex'); }

// Inventory current bytes, never git ls-files: newly implemented code must ship.
// Only the runtime package is distributed; research/evidence and private state
// are not release inputs. Writable dependencies are provisioned in plugin data.
export function inventory(root: string, current = root): ReleaseFile[] {
  const files: ReleaseFile[] = [];
  for (const name of readdirSync(current).sort()) {
    if (['__pycache__', '.DS_Store', '.git', 'node_modules'].includes(name)) continue;
    const path = join(current, name), stat = lstatSync(path);
    if (stat.isSymbolicLink()) throw new Error(`release_symlink_forbidden:${path}`);
    if (stat.isDirectory()) files.push(...inventory(root, path));
    else if (stat.isFile()) {
      const rel = relative(root, path).split(sep).join('/');
      if (!/\.(py|js|json|yaml|md)$/.test(rel) || /(?:^|\/)(?:\.env|auth|config|evidence|research|vendor)(?:\.|\/|$)/.test(rel)) throw new Error(`release_unexpected_file:${rel}`);
      files.push({ path: rel, sha256: sha256(path), size: stat.size });
    } else throw new Error(`release_file_type_invalid:${path}`);
  }
  return files.sort((a, b) => a.path.localeCompare(b.path));
}

export function verifyRelease(candidate: string, manifest: ReleaseManifest): void {
  if (manifest.schema !== 'hermes-ios-unified-release/v1' || manifest.plugin !== 'hermes-ios') throw new Error('release_manifest_invalid');
  if (JSON.stringify(inventory(candidate)) !== JSON.stringify(manifest.files)) throw new Error('release_inventory_mismatch');
  const required = ['plugin.yaml', '__init__.py', 'agent/ios_iteration.py', 'desktop/plugin.js', 'dashboard/plugin_api.py', 'skills/ios-development/SKILL.md', 'dependencies/package.json', 'dependencies/package-lock.json'];
  for (const path of required) if (!manifest.files.some(file => file.path === path)) throw new Error(`release_member_missing:${path}`);
  for (const file of manifest.files) if (/frame.helper|sidecar|stream_|control_proxy|placement|runway_artifacts/.test(file.path)) throw new Error(`obsolete_release_member:${file.path}`);
  const imports = [...readFileSync(join(candidate, 'desktop/plugin.js'), 'utf8').matchAll(/(?:from\s+|import\s*\(\s*)["']([^"']+)["']/g)].map(m => m[1]);
  if (imports.some(name => !['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'].includes(name))) throw new Error('desktop_import_not_public');
  const lock = JSON.parse(readFileSync(join(candidate, 'dependencies/package-lock.json'), 'utf8'));
  if (lock.packages['node_modules/serve-sim']?.version !== '0.1.49-beta.139.1') throw new Error('stock_pin_mismatch');
}

export function packageRelease(options: { output: string }): { output: string; candidatePath: string; manifestPath: string; manifestSha256: string; files: number; archivePath: string; archiveSha256: string } {
  const output = resolve(options.output);
  // Never recursively delete a caller-supplied destination.
  if (existsSync(output)) throw new Error(`release_output_exists:${output}`);
  const source = join(REPO, 'plugin'), bundle = join(REPO, 'dist/plugin.js');
  if (!existsSync(bundle)) throw new Error('desktop_bundle_missing');
  mkdirSync(join(source, 'desktop'), { recursive: true });
  copyFileSync(bundle, join(source, 'desktop/plugin.js'));
  const candidatePath = join(output, 'hermes-ios');
  for (const file of inventory(source)) {
    const destination = join(candidatePath, file.path);
    mkdirSync(dirname(destination), { recursive: true });
    copyFileSync(join(source, file.path), destination);
  }
  const manifest: ReleaseManifest = { schema: 'hermes-ios-unified-release/v1', plugin: 'hermes-ios', version: JSON.parse(readFileSync(join(REPO, 'package.json'), 'utf8')).version, files: inventory(candidatePath) };
  verifyRelease(candidatePath, manifest);
  const manifestPath = join(output, 'manifest.json');
  writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  const archivePath = join(output, 'hermes-ios.tar.gz');
  const archive = Bun.spawnSync({ cmd: ['tar', '--no-xattrs', '--no-acls', '--no-fflags', '--uid=0', '--gid=0', '--uname=root', '--gname=root', '-czf', archivePath, '-C', output, 'hermes-ios'], env: { ...process.env, COPYFILE_DISABLE: '1' }, stdout: 'pipe', stderr: 'pipe' });
  if (archive.exitCode !== 0) throw new Error(`release_archive_failed:${archive.stderr.toString()}`);
  return { output, candidatePath, manifestPath, manifestSha256: sha256(manifestPath), files: manifest.files.length, archivePath, archiveSha256: sha256(archivePath) };
}
if (import.meta.main) {
  const i = process.argv.indexOf('--output');
  console.log(JSON.stringify(packageRelease({ output: i >= 0 ? process.argv[i + 1] : join(REPO, 'dist/hermes-ios-release') }), null, 2));
}
