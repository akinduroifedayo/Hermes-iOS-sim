import { expect, test } from 'bun:test';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { packageRelease } from '../../scripts/package-release.ts';
import { selectPython } from '../../scripts/python-tests.ts';

test('unified release is self-contained, includes untracked implementation and excludes obsolete/private bytes', () => {
  const root = mkdtempSync(join(tmpdir(), 'u7-package-'));
  try {
    const result = packageRelease({ output: join(root, 'release') });
    const manifest = JSON.parse(readFileSync(result.manifestPath, 'utf8'));
    const paths = manifest.files.map((file: any) => file.path);
    for (const name of ['plugin.yaml', '__init__.py', 'agent/ios_iteration.py', 'desktop/plugin.js', 'skills/ios-development/SKILL.md', 'dependencies/package.json', 'dependencies/package-lock.json']) expect(paths).toContain(name);
    expect(paths.some((path: string) => /vendor|frame.helper|sidecar|stream_|control_proxy|placement|runway_artifacts|evidence|research|node_modules|__pycache__/.test(path))).toBe(false);
    expect(existsSync(join(result.candidatePath, 'desktop/plugin.js'))).toBe(true);
    expect(paths).toHaveLength(new Set(paths).size);
    const archive = Bun.spawnSync({ cmd: ['tar', '-tzf', result.archivePath], stdout: 'pipe', stderr: 'pipe' });
    expect(archive.exitCode).toBe(0);
    const archiveFiles = archive.stdout.toString().split('\n').filter(path => path && !path.endsWith('/')).map(path => path.replace(/^hermes-ios\//, ''));
    expect(archiveFiles.sort()).toEqual([...paths].sort());
    expect(archive.stdout.toString()).not.toContain('._');
    const metadata = Bun.spawnSync({ cmd: [selectPython(), '-c', 'import json,sys,tarfile; t=tarfile.open(sys.argv[1]); print(json.dumps(sorted(set((m.uid,m.gid,m.uname,m.gname) for m in t))))', result.archivePath], stdout: 'pipe', stderr: 'pipe' });
    expect(metadata.exitCode).toBe(0);
    expect(JSON.parse(metadata.stdout.toString())).toEqual([[0, 0, 'root', 'root']]);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
