"""Pinned dependency setup. Only the host-dispatched ios_setup tool provisions.

Registry byte integrity is not SLSA/native source provenance or security admission.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import platform
import re
import signal
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

MANIFEST_DIR = Path(__file__).parent / 'dependencies'
ENTRY = 'node_modules/serve-sim/dist/serve-sim.js'
NATIVE = 'node_modules/serve-sim/dist/native/serve-sim-native.node'
DISCLOSURE = (
    'Download serve-sim 0.1.49-beta.139.1 and the checked-in locked transitive npm tree from registry.npmjs.org '
    'into this profile\'s plugin-data cache (including a fresh private npm cache). '
    'No global installation or upgrade. Package lifecycle scripts are disabled. '
    'Includes executable JavaScript, a native .node addon, camera dylib/helpers and HID/render helpers. '
    'The preview requires a loopback Host; shell execution is disabled (no --allow-exec), and its token is not in the page. '
    'Local simulator stream/HID capabilities remain unauthenticated; '
    'loopback is not an authorization boundary. Registry integrity does not establish native source/SLSA provenance; '
    'this setup checks pinned bytes, not provenance. Even separately verified provenance is not runtime authorization or sandboxing. '
    'Xcode build scripts are executable code and require separate trusted-input approval.'
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _tree(root):
    result = {}
    for path in sorted((root / 'node_modules').rglob('*')):
        if path.is_symlink():
            resolved = path.resolve()
            if not resolved.is_relative_to(root.resolve()):
                raise ValueError('integrity: external symlink')
            result[path.relative_to(root).as_posix()] = {'link': os.readlink(path)}
        elif path.is_file():
            result[path.relative_to(root).as_posix()] = {'sha256': digest(path.read_bytes()), 'mode': path.stat().st_mode & 0o777}
    return result


def _native_arch(data):
    # Parse bytes only: the pinned addon is a universal Mach-O, not always thin.
    def thin_header(start, size):
        if size < 32 or data[start:start + 4] != b'\xcf\xfa\xed\xfe':
            raise ValueError('architecture: malformed Mach-O slice')
        def word(offset):
            return int.from_bytes(data[start + offset:start + offset + 4], 'little')
        cpu, subtype, commands, command_size = word(4), word(8), word(16), word(20)
        if command_size > size - 32 or commands > command_size // 8:
            raise ValueError('architecture: truncated Mach-O load commands')
        cursor, end = start + 32, start + 32 + command_size
        for _ in range(commands):
            if cursor + 8 > end:
                raise ValueError('architecture: truncated Mach-O load command')
            length = int.from_bytes(data[cursor + 4:cursor + 8], 'little')
            if length < 8 or length % 8 or length > end - cursor:
                raise ValueError('architecture: malformed Mach-O load command')
            cursor += length
        if cursor != end:
            raise ValueError('architecture: inconsistent Mach-O load commands')
        return cpu, subtype

    magic = data[:4]
    if magic == b'\xcf\xfa\xed\xfe':
        cpus = [thin_header(0, len(data))[0]]
        native_format = 'Mach-O 64'
    elif magic == b'\xca\xfe\xba\xbe':
        # FAT_MAGIC uses big-endian 32-bit fat_arch offsets and sizes.
        count = int.from_bytes(data[4:8], 'big')
        if len(data) < 8 or not count or count > (len(data) - 8) // 20:
            raise ValueError('architecture: truncated or empty Mach-O fat table')
        table_end = 8 + count * 20
        cpus, identities, ranges = [], set(), []
        for index in range(count):
            entry = 8 + index * 20
            cpu, subtype, offset, size, align = (
                int.from_bytes(data[pos:pos + 4], 'big')
                for pos in range(entry, entry + 20, 4)
            )
            if (offset < table_end or size < 32 or offset > len(data)
                    or size > len(data) - offset or align > 31
                    or offset % (1 << align)):
                raise ValueError('architecture: invalid Mach-O slice bounds or alignment')
            if (cpu, subtype) in identities:
                raise ValueError('architecture: duplicate Mach-O slice')
            identities.add((cpu, subtype))
            ranges.append((offset, offset + size))
            if thin_header(offset, size) != (cpu, subtype):
                raise ValueError('architecture: Mach-O slice identity mismatch')
            cpus.append(cpu)
        ranges.sort()
        if any(right[0] < left[1] for left, right in zip(ranges, ranges[1:])):
            raise ValueError('architecture: overlapping Mach-O slices')
        native_format = 'Mach-O universal'
    else:
        raise ValueError('architecture: unsupported native format')
    host = platform.machine()
    expected = {'arm64': 0x100000c, 'aarch64': 0x100000c, 'x86_64': 0x1000007}.get(host)
    if platform.system() != 'Darwin' or expected is None or expected not in cpus:
        raise ValueError('architecture: native addon incompatible with this host')
    return {'format': native_format, 'cpu_type': expected, 'host': host, 'sha256': digest(data)}


def _verify_archive(root, location, package, archive):
    if base64.b64encode(hashlib.sha512(archive).digest()).decode() != package['integrity'][7:]:
        raise ValueError('integrity: registry archive mismatch')
    expected = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for member in tar:
            relative = PurePosixPath(member.name)
            if not relative.parts or relative.parts[0] != 'package' or '..' in relative.parts or member.issym() or member.islnk():
                raise ValueError('integrity: unsafe archive member')
            if member.isfile():
                destination = root / location / Path(*relative.parts[1:])
                source = tar.extractfile(member)
                if source is None or destination.is_symlink() or destination.read_bytes() != source.read():
                    raise ValueError('integrity: installed bytes mismatch')
                if member.mode & 0o111 and not destination.stat().st_mode & 0o111:
                    raise ValueError('integrity: packaged executable permission missing')
                expected.add(destination.relative_to(root).as_posix())
    return expected


class StockDependency:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        if self.data_dir.is_relative_to(Path(__file__).parent.resolve()):
            raise ValueError('dependency cache must be outside the installed plugin tree')
        self.lock_bytes = (MANIFEST_DIR / 'package-lock.json').read_bytes()
        self.lock_digest = digest(self.lock_bytes)
        self.packages = json.loads(self.lock_bytes)['packages']
        self.cache = self.data_dir / ('serve-sim-0.1.49-beta.139.1-' + self.lock_digest[:16])
        self._lock = asyncio.Lock()

    def status(self):
        if self.cache.is_symlink():
            return {'status': 'integrity_or_incomplete', 'ready': False, 'recovery': 'Remove the candidate cache symlink; explicitly retry setup.'}
        if not self.cache.exists():
            return {'status': 'missing', 'ready': False, 'version': '0.1.49-beta.139.1', 'recovery': "Call ios_setup action='provision' for disclosed host approval."}
        try:
            receipt = json.loads((self.cache / 'receipt.json').read_text())
            if receipt['lock_sha256'] != self.lock_digest or receipt['files'] != _tree(self.cache):
                raise ValueError('integrity mismatch')
            if not receipt['files'] or not (self.cache / ENTRY).is_file():
                raise ValueError('entry missing')
            expected = set()
            for location, package in self.packages.items():
                if location:
                    expected.update(_verify_archive(self.cache, location, package, (self.cache / 'archives' / (digest(location.encode()) + '.tgz')).read_bytes()))
                    for name, entry in package.get('bin', {}).items():
                        link = self.cache / 'node_modules' / '.bin' / name
                        if not link.is_symlink() or link.resolve() != (self.cache / location / entry).resolve():
                            raise ValueError('integrity: executable link mismatch')
                        expected.add(link.relative_to(self.cache).as_posix())
            if set(_tree(self.cache)) != expected:
                raise ValueError('integrity: unexpected installed files')
            native = _native_arch((self.cache / NATIVE).read_bytes())
            if native != receipt['native']:
                raise ValueError('native mismatch')
            return {'status': 'ready', 'ready': True, 'version': '0.1.49-beta.139.1', 'entry': str(self.cache / ENTRY), 'native': native, 'lock_sha256': self.lock_digest, 'provenance': 'not_verified_by_runtime', 'security_admission': 'not_claimed'}
        except (OSError, ValueError, KeyError, TypeError):
            return {'status': 'integrity_or_incomplete', 'ready': False, 'recovery': 'Remove only this candidate dependency cache and explicitly retry setup.'}

    def approval_hook(self, tool_name='', args=None, **_kwargs):
        if tool_name == 'ios_setup' and isinstance(args, dict) and args.get('action') == 'provision':
            return {'action': 'approve', 'message': DISCLOSURE, 'rule_key': 'hermes-ios:stock-setup:' + self.lock_digest}
        return None

    async def provision(self):
        """Internal handler boundary; consent belongs to pre_tool_call, never tool args."""
        async with self._lock:
            cached = self.status()
            if cached['ready']:
                return cached
            if self.cache.is_symlink() or self.cache.exists():
                return cached  # do not overwrite suspicious/incomplete bytes
            from .runtime_discovery import run_command
            node = run_command(('node', '--version'), Path.cwd())
            npm = run_command(('npm', '--version'), Path.cwd())
            version = re.fullmatch(r'v(\d+)\.\d+\.\d+\s*', node.stdout)
            if node.returncode or npm.returncode or version is None or int(version.group(1)) < 20:
                return {'status': 'toolchain', 'ready': False, 'stage': 'dependency_preflight', 'recovery': 'Select Node >=20 and npm before explicitly retrying setup.'}
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            staging = Path(tempfile.mkdtemp(prefix='.stock-setup-', dir=self.data_dir))
            process = None
            try:
                lock = json.loads(self.lock_bytes)
                for name in ('package.json', 'package-lock.json'):
                    shutil.copyfile(MANIFEST_DIR / name, staging / name)
                # A private HOME + npm config/cache avoids personal npm state and credentials.
                home = staging / 'home'
                home.mkdir()
                env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'TMPDIR': str(staging), 'npm_config_userconfig': str(home / '.npmrc'), 'npm_config_globalconfig': str(home / 'global.npmrc'), 'npm_config_update_notifier': 'false'}
                argv = ('npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund', '--registry=https://registry.npmjs.org', '--cache=' + str(staging / 'npm-cache'))
                process = await asyncio.create_subprocess_exec(*argv, cwd=staging, env=env, start_new_session=True, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(), 180)
                if process.returncode:
                    text = stderr.decode(errors='replace')[-4096:]
                    category = 'permission' if 'EACCES' in text or 'EPERM' in text else 'integrity' if 'EINTEGRITY' in text else 'network_or_install'
                    return {'status': category, 'ready': False, 'stage': 'npm_ci', 'command': list(argv), 'returncode': process.returncode, 'stderr': text, 'recovery': 'Check registry connectivity/permissions and explicitly retry.'}
                # npm ci generates this metadata even with lifecycle scripts disabled.
                # It is not a packaged runtime file; never follow a metadata symlink.
                metadata = staging / 'node_modules' / '.package-lock.json'
                if metadata.is_symlink():
                    raise ValueError('integrity: npm metadata symlink')
                if metadata.exists():
                    metadata.unlink()
                # Compare installed bytes to each integrity-checked registry archive, not npm output.
                (staging / 'archives').mkdir()
                for location, package in lock['packages'].items():
                    if not location:
                        continue
                    url = package['resolved']
                    if not url.startswith('https://registry.npmjs.org/') or not package['integrity'].startswith('sha512-'):
                        raise ValueError('integrity: unsupported registry or digest')
                    def fetch():
                        with urllib.request.urlopen(url, timeout=30) as response:
                            return response.read()
                    archive = await asyncio.to_thread(fetch)
                    _verify_archive(staging, location, package, archive)
                    (staging / 'archives' / (digest(location.encode()) + '.tgz')).write_bytes(archive)
                native = _native_arch((staging / NATIVE).read_bytes())
                if not (staging / ENTRY).is_file():
                    raise ValueError('incomplete: entry missing')
                receipt = {'lock_sha256': self.lock_digest, 'files': _tree(staging), 'native': native}
                (staging / 'receipt.json').write_text(json.dumps(receipt, sort_keys=True))
                # Never publish npm cache, logs, or temporary home as runtime dependencies.
                shutil.rmtree(home)
                shutil.rmtree(staging / 'npm-cache', ignore_errors=True)
                if self.cache.is_symlink() or self.cache.exists():
                    return self.status()  # preserve a candidate appearing during setup
                os.rename(staging, self.cache)
                return self.status()
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, KeyError, asyncio.TimeoutError, tarfile.TarError) as exc:
                return {'status': str(exc).split(':')[0] if ':' in str(exc) else 'network_or_incomplete', 'ready': False, 'stage': 'dependency_setup', 'error': str(exc)[-4096:], 'recovery': 'Check the reported dependency failure and explicitly retry setup.'}
            finally:
                if process is not None and process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.communicate()
                if staging.exists():
                    shutil.rmtree(staging)
