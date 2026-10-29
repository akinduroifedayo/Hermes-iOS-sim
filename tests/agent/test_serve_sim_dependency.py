import tempfile
import unittest
import asyncio
import base64
import hashlib
import io
import json
import os
import platform
import sys
import tarfile
from unittest.mock import patch
from pathlib import Path
import host_isolation
from plugin.serve_sim_dependency import StockDependency, digest, ENTRY, NATIVE, _native_arch
import struct


def thin_native(cpu, subtype=0):
    # Structurally valid synthetic MH_BUNDLE; never loaded.
    return struct.pack('<8I', 0xfeedfacf, cpu, subtype, 6, 0, 0, 0, 0)


def universal_native(cpus=(0x1000007, 0x100000c)):
    offset = 8 + 20 * len(cpus)
    entries, slices = [], []
    for cpu in cpus:
        data = thin_native(cpu)
        entries.append(struct.pack('>5I', cpu, 0, offset, len(data), 0))
        slices.append(data)
        offset += len(data)
    return struct.pack('>2I', 0xcafebabe, len(cpus)) + b''.join(entries + slices)


class NativeArchitectureTests(unittest.TestCase):
    def parse(self, data, host='arm64', system='Darwin'):
        with patch('plugin.serve_sim_dependency.platform.machine', return_value=host), patch('plugin.serve_sim_dependency.platform.system', return_value=system):
            return _native_arch(data)

    def test_universal_addon_selects_real_host_slice_not_first_slice(self):
        data = universal_native()
        for host, cpu in [('arm64', 0x100000c), ('aarch64', 0x100000c), ('x86_64', 0x1000007)]:
            with self.subTest(host=host):
                result = self.parse(data, host)
                self.assertEqual(result['format'], 'Mach-O universal')
                self.assertEqual(result['cpu_type'], cpu)
                self.assertEqual(result['host'], host)
                self.assertEqual(result['sha256'], digest(data))

    def test_thin_header_and_host_compatibility_remain_required(self):
        self.assertEqual(self.parse(thin_native(0x100000c))['format'], 'Mach-O 64')
        for data, host, system in [(thin_native(0x1000007), 'arm64', 'Darwin'), (universal_native((0x1000007,)), 'arm64', 'Darwin'), (universal_native(), 'arm64', 'Linux'), (universal_native(), 'riscv64', 'Darwin')]:
            with self.subTest(host=host, system=system, header=data[:8]):
                with self.assertRaisesRegex(ValueError, 'architecture:'):
                    self.parse(data, host, system)

    def test_load_command_lengths_are_bounded_by_the_slice(self):
        data = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 6, 1, 8, 0, 0) + struct.pack('<2I', 1, 8)
        self.assertEqual(self.parse(data)['cpu_type'], 0x100000c)
        for length in (0, 4, 9, 16, 0xffffffff):
            changed = bytearray(data)
            struct.pack_into('<I', changed, 36, length)
            with self.subTest(length=length), self.assertRaisesRegex(ValueError, 'architecture:'):
                self.parse(bytes(changed))
        changed = bytearray(data)
        struct.pack_into('<I', changed, 16, 2)
        with self.assertRaisesRegex(ValueError, 'architecture:'):
            self.parse(bytes(changed))

    def test_malformed_or_truncated_native_headers_are_rejected(self):
        data = universal_native()
        malformed = [b'', b'not Mach-O', thin_native(0x100000c)[:8], data[:7], data[:47], data[:-1], struct.pack('>2I', 0xcafebabe, 0), struct.pack('>2I', 0xcafebabe, 0xffffffff), bytes.fromhex('cafebabf') + data[4:]]
        # Tamper the second entry, even when the first matches the host.
        for position, value in [(28, 0x1000007), (32, 1), (36, 0), (36, 49), (36, 0xffffffff), (40, 0), (40, 0xffffffff), (44, 32), (44, 5)]:
            changed = bytearray(data)
            struct.pack_into('>I', changed, position, value)
            malformed.append(bytes(changed))
        # Slice identity must agree with the fat table; magic and command bounds too.
        for position, value in [(48, 0), (84, 0x1000007), (96, 1), (100, 8)]:
            changed = bytearray(data)
            struct.pack_into('<I', changed, position, value)
            malformed.append(bytes(changed))
        malformed.append(universal_native((0x100000c, 0x100000c)))
        overlap = bytearray(data)
        struct.pack_into('>I', overlap, 20, 64)  # first slice extends into the second
        malformed.append(bytes(overlap))
        for index, changed in enumerate(malformed):
            with self.subTest(case=index):
                with self.assertRaisesRegex(ValueError, 'architecture:'):
                    self.parse(changed)


class FixtureProvisionTests(unittest.IsolatedAsyncioTestCase):
    """Synthetic archive + executable npm stand-in; NOT real pinned-tree installation."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = self.root/'manifest'
        self.manifest.mkdir()
        cpu = 0x100000c if platform.machine() in ('arm64', 'aarch64') else 0x1000007
        self.files = {'dist/serve-sim.js': b'// synthetic fixture only', 'dist/native/serve-sim-native.node': thin_native(cpu)}
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name, data in self.files.items():
                member = tarfile.TarInfo('package/'+name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        self.archive = stream.getvalue()
        self.lock = {'packages': {'': {}, 'node_modules/serve-sim': {'version':'0.1.49-beta.139.1', 'resolved':'https://registry.npmjs.org/serve-sim/-/serve-sim-0.1.49-beta.139.1.tgz', 'integrity':'sha512-'+base64.b64encode(hashlib.sha512(self.archive).digest()).decode()}}}
        (self.manifest/'package-lock.json').write_text(json.dumps(self.lock))
        (self.manifest/'package.json').write_text('{"private":true}')
        self.bin = self.root/'bin'
        self.bin.mkdir()
        # A real short-lived process writes actual installed bytes; no success output is mocked.
        writes = '\n'.join('p=Path('+repr('node_modules/serve-sim/'+name)+'); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes('+repr(data)+')' for name,data in self.files.items())
        self.npm = self.bin/'npm'
        self.npm.write_text('#!'+sys.executable+'\nfrom pathlib import Path\nimport sys\nif "--version" in sys.argv: print("11.0.0"); sys.exit(0)\n'+writes+'\nPath("node_modules/.package-lock.json").write_text("{\\\"lockfileVersion\\\":3}")\n')
        self.npm.chmod(0o755)
        self.manifest_patch = patch('plugin.serve_sim_dependency.MANIFEST_DIR', self.manifest)
        self.manifest_patch.start()
        self.env_patch = patch.dict(os.environ, {'PATH':str(self.bin)+os.pathsep+os.environ['PATH']})
        self.env_patch.start()
        self.dependency = StockDependency(self.root/'data')

    def tearDown(self):
        self.env_patch.stop()
        self.manifest_patch.stop()
        self.tmp.cleanup()

    async def test_fixture_install_repeat_and_native_tamper(self):
        with patch('urllib.request.urlopen', side_effect=lambda *a,**k: io.BytesIO(self.archive)) as fetch:
            result = await self.dependency.provision()
            self.assertTrue(result['ready'], result)
            self.assertEqual(result['provenance'], 'not_verified_by_runtime')
            self.assertEqual(result['security_admission'], 'not_claimed')
            self.assertEqual(fetch.call_count, 1)
        self.assertFalse((self.dependency.cache/'node_modules/.package-lock.json').exists())
        with patch('urllib.request.urlopen', side_effect=AssertionError('network on repeat')):
            self.assertTrue((await self.dependency.provision())['ready'])
        (self.dependency.cache/NATIVE).write_bytes(b'tampered')
        # Even rewriting the mutable receipt cannot bless tampered registry bytes.
        from plugin.serve_sim_dependency import _tree
        receipt = json.loads((self.dependency.cache/'receipt.json').read_text())
        receipt['files'] = _tree(self.dependency.cache)
        (self.dependency.cache/'receipt.json').write_text(json.dumps(receipt))
        self.assertFalse(self.dependency.status()['ready'])

    async def test_npm_hidden_metadata_symlink_is_rejected_without_touching_target(self):
        target = self.root/'external-metadata'
        target.write_text('leave unchanged')
        with self.npm.open('a') as script:
            script.write('p=Path("node_modules/.package-lock.json"); p.unlink(); p.symlink_to('+repr(str(target))+')\n')
        with patch('urllib.request.urlopen', side_effect=lambda *a,**k: io.BytesIO(self.archive)):
            result = await self.dependency.provision()
        self.assertFalse(result['ready'])
        self.assertEqual(result['status'], 'integrity')
        self.assertIn('metadata symlink', result.get('error', ''))
        self.assertFalse(self.dependency.cache.exists())
        self.assertEqual(target.read_text(), 'leave unchanged')

    async def test_dangling_cache_symlink_refuses_before_npm_or_fetch(self):
        self.dependency.data_dir.mkdir()
        target = self.root/'absent-cache'
        self.dependency.cache.symlink_to(target, target_is_directory=True)
        before = self.dependency.status()
        with patch('plugin.runtime_discovery.run_command', side_effect=AssertionError('preflight entered for dangling cache symlink')) as command, patch('asyncio.create_subprocess_exec') as npm, patch('urllib.request.urlopen') as fetch:
            result = await self.dependency.provision()
        self.assertEqual(result, before)
        command.assert_not_called()
        npm.assert_not_called()
        fetch.assert_not_called()
        self.assertTrue(self.dependency.cache.is_symlink())
        self.assertEqual(self.dependency.cache.readlink(), target)
        self.assertFalse(target.exists())

    async def test_cache_symlink_appearing_before_publication_is_preserved(self):
        target = self.root/'absent-cache'
        def fetch_archive(*args, **kwargs):
            self.dependency.cache.symlink_to(target, target_is_directory=True)
            return io.BytesIO(self.archive)
        with patch('urllib.request.urlopen', side_effect=fetch_archive):
            result = await self.dependency.provision()
        self.assertEqual(result['status'], 'integrity_or_incomplete')
        self.assertFalse(result['ready'])
        self.assertTrue(self.dependency.cache.is_symlink())
        self.assertEqual(self.dependency.cache.readlink(), target)
        self.assertFalse(target.exists())

    async def test_ready_approval_snapshot_survives_cache_disappearance(self):
        with patch('plugin.serve_sim_dependency.StockDependency.status', return_value={'ready': True, 'status': 'ready'}):
            directive = self.dependency.approval_hook(tool_name='ios_setup', args={'action': 'provision'})
            self.assertIsNone(self.dependency.approval_hook(tool_name='ios_setup', args={'action': 'status'}))
        self.assertEqual(self.dependency.status()['status'], 'missing')
        self.assertIsNotNone(directive, 'ready snapshot must not bypass host approval')
        self.assertEqual(directive['action'], 'approve')

    async def test_wrong_archive_integrity_never_publishes(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'tampered')):
            result = await self.dependency.provision()
        self.assertFalse(result['ready'])
        self.assertEqual(result['status'], 'integrity')
        self.assertFalse(self.dependency.cache.exists())
        self.assertEqual(list(self.dependency.data_dir.iterdir()), [])

    async def test_offline_never_publishes(self):
        with patch('urllib.request.urlopen', side_effect=OSError('offline')):
            self.assertFalse((await self.dependency.provision())['ready'])
        self.assertFalse(self.dependency.cache.exists())

    async def test_cancel_drains_process_and_removes_staging(self):
        self.npm.write_text('#!'+sys.executable+'\nimport sys,time\nfrom pathlib import Path\nif "--version" in sys.argv: print("11.0.0"); sys.exit(0)\nPath("started").write_text("yes")\ntime.sleep(60)\n')
        task = asyncio.create_task(self.dependency.provision())
        for _ in range(100):
            await asyncio.sleep(.01)
            if list(self.dependency.data_dir.glob('.stock-setup-*/started')):
                break
        self.assertTrue(list(self.dependency.data_dir.glob('.stock-setup-*/started')))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.dependency.cache.exists())
        self.assertEqual(list(self.dependency.data_dir.iterdir()), [])

class PublicHostApprovalTests(unittest.TestCase):
    def test_real_sdk_noninteractive_gate_denies_without_setup(self):
        host = os.environ.get('HERMES_IOS_HOST_SOURCE')
        python = os.environ.get('HERMES_IOS_HOST_PYTHON')
        if not host or not python:
            self.skipTest('Set HERMES_IOS_HOST_SOURCE/PYTHON for real SDK boundary test')
        code = r'''
import asyncio,json,os,pathlib,tempfile
from unittest.mock import patch
with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home:
    os.environ['HERMES_HOME']=home
    from hermes_cli.plugins import PluginContext,PluginManager,PluginManifest,_dispatch_pre_tool_call_hooks
    from tools.registry import registry
    import plugin
    manager=PluginManager(scope_key=home)
    ctx=PluginContext(PluginManifest(name='u8-setup-probe', source='user', path=pathlib.Path.cwd()/'plugin'),manager)
    plugin.register(ctx)
    entry=registry.get_entry('ios_setup',scope=manager.scope_key)
    assert entry is not None
    data=ctx.state.data_dir
    before={str(p.relative_to(data)):p.read_bytes() for p in data.rglob('*') if p.is_file()} if data.exists() else {}
    # Bind delivery to this isolated real manager, not any daily profile manager.
    with patch('hermes_cli.plugins._delivery_manager',return_value=manager):
        blocked,_=_dispatch_pre_tool_call_hooks('ios_setup',{'action':'provision'})
    assert blocked and 'BLOCKED' in blocked,blocked
    # Model an offline ready snapshot at approval time, then a disappeared cache
    # at handler time. Only the dependency snapshot is mocked, never host consent.
    from plugin.serve_sim_dependency import StockDependency
    with patch('hermes_cli.plugins._delivery_manager',return_value=manager), patch.object(StockDependency,'status',return_value={'ready':True,'status':'ready'}):
        ready_blocked,_=_dispatch_pre_tool_call_hooks('ios_setup',{'action':'provision'})
    from plugin.runtime_discovery import CommandResult
    with patch('plugin.runtime_discovery.run_command',return_value=CommandResult(1,'','fixture preflight stop')) as command, patch('asyncio.create_subprocess_exec') as npm, patch('urllib.request.urlopen') as fetch:
        if not ready_blocked:
            asyncio.run(entry.handler({'action':'provision'}))
        assert ready_blocked and 'BLOCKED' in ready_blocked,ready_blocked
        command.assert_not_called()
        npm.assert_not_called()
        fetch.assert_not_called()
    with patch('hermes_cli.plugins._delivery_manager',return_value=manager):
        status_blocked,_=_dispatch_pre_tool_call_hooks('ios_setup',{'action':'status'})
    assert not status_blocked,status_blocked
    after={str(p.relative_to(data)):p.read_bytes() for p in data.rglob('*') if p.is_file()} if data.exists() else {}
    assert before==after
    assert not list(data.glob('serve-sim-*'))
    result=json.loads(asyncio.run(entry.handler({'action':'status'})))
    assert result['status']=='missing'
    print(json.dumps({'homes': resolved_homes(), 'boundary':'real-source-SDK-not-installed-host','blocked':blocked,'ready_snapshot_blocked':ready_blocked,'setup_side_effects':0,'status':result['status']}))
'''
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as fixture_root:
            root = Path(fixture_root).resolve()
            env = host_isolation.fixture_env(root, PYTHONPATH=os.pathsep.join([str(Path.cwd()), host]))
            result = host_isolation.run_host_child([python, '-c', host_isolation.HOMES + code],
                                                   root=root, env=env, cwd=root / 'cwd',
                                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('"setup_side_effects": 0',result.stdout)
            reply = json.loads(result.stdout.strip().splitlines()[-1])
            host_isolation.assert_homes_contained(reply.pop('homes'), root, env)
            print(result.stdout.strip())

class DependencyTests(unittest.TestCase):
    def test_fresh_cache_and_approval_directive_are_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'plugin-data'
            dependency = StockDependency(root)
            self.assertEqual(dependency.status()['status'], 'missing')
            directive = dependency.approval_hook(tool_name='ios_setup', args={'action':'provision'})
            self.assertEqual(directive['action'], 'approve')
            self.assertIn('0.1.49-beta.139.1', directive['message'])
            self.assertIn('native', directive['message'])
            self.assertFalse(root.exists())

    def test_interrupted_cache_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            dependency = StockDependency(Path(tmp))
            dependency.cache.mkdir()
            (dependency.cache/'receipt.json').write_text('{}')
            self.assertFalse(dependency.status()['ready'])
