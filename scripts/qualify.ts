import { existsSync, lstatSync, readFileSync, realpathSync } from 'node:fs';
import { join, resolve, sep } from 'node:path';
import { sha256, type ReleaseManifest } from './package-release.ts';
export const installedGates = ['clean_setup', 'first_automatic_open', 'repeat_run', 'feedback_loop', 'app_data', 'compile_failure', 'install_launch_failure', 'occupied_port', 'crashed_server', 'preview_close', 'cancellation_reload_disable', 'legacy_conflict', 'isolation_teardown'] as const;

function regular(path: string) {
  if (!existsSync(path) || !lstatSync(path).isFile() || lstatSync(path).isSymbolicLink()) throw new Error(`evidence_file_missing_or_unsafe:${path}`);
  return realpathSync(path);
}
export function verifyInstalledReceipts(receipt: any, options: { home: string; manifestPath: string; evidenceRoot: string }) {
  const home = realpathSync(options.home), manifestPath = regular(options.manifestPath), evidenceRoot = realpathSync(options.evidenceRoot);
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as ReleaseManifest;
  const artifact = sha256(manifestPath);
  if (manifest.schema !== 'hermes-ios-unified-release/v1' || manifest.plugin !== 'hermes-ios') throw new Error('release_manifest_invalid');
  if (receipt?.schema !== 'hermes-ios-installed-qualification/v1' || receipt.scope !== 'isolated_installed_candidate' || receipt.manifestSha256 !== artifact || realpathSync(receipt.home) !== home) throw new Error('installed_receipt_identity_mismatch');
  if (!Array.isArray(manifest.files) || manifest.files.length === 0 || new Set(manifest.files.map(file => file.path)).size !== manifest.files.length) throw new Error('release_inventory_invalid');
  for (const file of manifest.files) if (!/^[a-zA-Z0-9_./-]+$/.test(file.path) || file.path.startsWith('/') || file.path.split('/').includes('..') || !/^[a-f0-9]{64}$/.test(file.sha256)) throw new Error('release_member_invalid');
  const installed = join(home, 'plugins/hermes-ios');
  for (const file of manifest.files) if (sha256(regular(join(installed, file.path))) !== file.sha256) throw new Error(`installed_artifact_mismatch:${file.path}`);
  for (const [name, file] of Object.entries({ backend: '__init__.py', desktop: 'desktop/plugin.js', skill: 'skills/ios-development/SKILL.md' })) {
    const loaded = receipt.loaded?.[name];
    const expected = manifest.files.find(entry => entry.path === file);
    // Desktop really loads the host-managed copy, not the package source.
    const target = name === 'desktop' ? join(home, 'desktop-plugins/hermes-ios/plugin.js') : join(installed, file);
    if (!expected || !loaded || realpathSync(loaded.path) !== regular(target) || loaded.sha256 !== expected.sha256 || sha256(target) !== expected.sha256) throw new Error(`loaded_artifact_unproven:${name}`);
  }
  if (!receipt.host?.sourceCommit || !receipt.host?.loadedDesktopIdentity || !receipt.host?.loadedBackendIdentity) throw new Error('loaded_host_identity_missing');
  for (const gate of installedGates) {
    const row = receipt.gates?.[gate];
    if (row?.status !== 'passed' || row.manifestSha256 !== artifact || !Array.isArray(row.evidence) || row.evidence.length === 0) throw new Error(`installed_gate_unproven:${gate}`);
    for (const evidence of row.evidence) {
      const path = regular(resolve(evidenceRoot, evidence.path));
      if (!path.startsWith(evidenceRoot + sep) || sha256(path) !== evidence.sha256) throw new Error(`installed_evidence_binding_invalid:${gate}`);
    }
  }
  const loop = receipt.loop;
  const digest = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
  for (const run of [loop?.initial, loop?.rebuilt]) {
    if (!run || typeof run.udid !== 'string' || !/^[A-Fa-f0-9-]{36}$/.test(run.udid) || typeof run.serverGeneration !== 'string' || !run.serverGeneration || !/^http:\/\/127\.0\.0\.1:\d+\/?$/.test(run.url) || !digest(run.pixelSha256)) throw new Error('loop_target_server_pixels_missing');
  }

  if (!loop || loop.initial.udid !== loop.rebuilt.udid || loop.initial.url !== loop.rebuilt.url || loop.initial.serverGeneration !== loop.rebuilt.serverGeneration || loop.initial.tabId !== loop.rebuilt.tabId || !loop.initial.tabId || loop.initial.pixelSha256 === loop.rebuilt.pixelSha256 || !loop.rebuilt.pixelSha256 || loop.appDataBefore !== loop.appDataAfter || !loop.appDataBefore) throw new Error('same_preview_app_data_loop_unproven');
  if (loop.previewTool !== 'desktop_preview' || loop.previewAction !== 'open' || !loop.sameOriginatingTurn || loop.inspectionTool !== 'vision_analyze' || loop.submittedImageSha256 !== loop.inspectedImageSha256 || !loop.submittedImageSha256 || !loop.inspectedBeforeEdit) throw new Error('public_open_image_inspection_unproven');
  if (!digest(loop.sourceBeforeSha256) || !digest(loop.sourceAfterSha256) || loop.sourceBeforeSha256 === loop.sourceAfterSha256) throw new Error('source_change_unproven');
  for (const [name, expected] of Object.entries({ initial: loop.initial.pixelSha256, rebuilt: loop.rebuilt.pixelSha256, submitted: loop.submittedImageSha256 })) {
    const path = regular(resolve(evidenceRoot, loop.images?.[name] ?? ''));
    const bytes = readFileSync(path);
    const image = bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) || (bytes[0] === 255 && bytes[1] === 216);
    if (!path.startsWith(evidenceRoot + sep) || !image || sha256(path) !== expected) throw new Error(`native_image_evidence_invalid:${name}`);
  }
  return { status: 'passed', scope: 'isolated_installed_candidate', manifestSha256: artifact, gates: installedGates.length };
}
export function qualify() {
  const required = ['HERMES_IOS_QUALIFICATION_HOME', 'HERMES_IOS_RELEASE_MANIFEST', 'HERMES_IOS_INSTALLED_RECEIPTS', 'HERMES_IOS_EVIDENCE_ROOT'];
  const missing = required.filter(key => !process.env[key]);
  if (missing.length) throw new Error(`installed_qualification_unproven:missing:${missing.join(',')}`);
  return verifyInstalledReceipts(JSON.parse(readFileSync(regular(process.env.HERMES_IOS_INSTALLED_RECEIPTS!), 'utf8')), {
    home: process.env.HERMES_IOS_QUALIFICATION_HOME!, manifestPath: process.env.HERMES_IOS_RELEASE_MANIFEST!, evidenceRoot: process.env.HERMES_IOS_EVIDENCE_ROOT!,
  });
}
if (import.meta.main) {
  try { console.log(JSON.stringify(qualify(), null, 2)); }
  catch (error) { console.error(JSON.stringify({ status: 'unproven', scope: 'isolated_installed_candidate', error: String(error) }, null, 2)); process.exit(2); }
}
