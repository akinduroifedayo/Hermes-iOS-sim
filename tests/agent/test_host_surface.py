"""Production code reaches the host only through public names (review 7 finding 2).

An AST scan of every module under ``plugin/`` (the parse child's source included): no
``from <host> import _name``, no ``<host module>._name`` attribute and no
``getattr(<host module>, '_name')``. A host module is any absolute import outside the standard
library and this package, including one bound through ``importlib.import_module('<name>')``.

One owner-approved exception (repair 8): ``plugin/host_compat.py`` may import
``worker_config_snapshot`` from the host's private ``agent.safe_worker_policy``, by that exact
statement and nothing else. Any other name from that module, any other way of reaching it, and
any mention of the module or the function in any other plugin file is a violation.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / 'plugin'
APPROVED_FILE = 'plugin/host_compat.py'
APPROVED_MODULE = 'agent.safe_worker_policy'
APPROVED_NAME = 'worker_config_snapshot'


def _private(name: str) -> bool:
    return name.startswith('_') and not (name.startswith('__') and name.endswith('__'))


def _foreign(module: str | None) -> bool:
    top = (module or '').split('.')[0]
    return bool(top) and top not in sys.stdlib_module_names and top not in ('__future__', 'plugin')


def _imported_module(node: ast.AST) -> str | None:
    """``importlib.import_module('<constant>')`` → its name."""
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and \
            isinstance(node.args[0].value, str):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == 'import_module') or \
                (isinstance(func, ast.Name) and func.id == 'import_module'):
            return node.args[0].value
    return None


def violations(source: str, filename: str = '<source>') -> tuple[list[str], set[str]]:
    """Private host names used in ``source``, and the host modules it imports (for a non-vacuity check)."""
    tree = ast.parse(source, filename)
    bound, modules, found = set(), set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _foreign(alias.name):
                    modules.add(alias.name)
                    bound.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and _foreign(node.module):
            modules.add(node.module)
            for alias in node.names:
                if _private(alias.name):
                    found.append(f'{filename}:{node.lineno} from {node.module} import {alias.name}')
                bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign) and _foreign(_imported_module(node.value)):
            modules.add(_imported_module(node.value))
            bound.update(target.id for target in node.targets if isinstance(target, ast.Name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            chain, base = [node.attr], node.value
            while isinstance(base, ast.Attribute):
                chain.append(base.attr)
                base = base.value
            if isinstance(base, ast.Name) and base.id in bound and any(_private(attr) for attr in chain):
                found.append(f'{filename}:{node.lineno} {base.id}.{".".join(reversed(chain))}')
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'getattr' and \
                len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in bound and \
                isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str) and \
                _private(node.args[1].value):
            found.append(f'{filename}:{node.lineno} getattr({node.args[0].id}, {node.args[1].value!r})')
    found += internal_uses(tree, source, filename)
    return sorted(set(found)), modules


def internal_uses(tree: ast.AST, source: str, filename: str) -> list[str]:
    """Uses of the approved private module beyond its one approved import in its one file. The
    seam file may reach the host's ``agent`` package by that one statement only."""
    if filename != APPROVED_FILE:
        return [f'{filename} names {word}' for word in ('safe_worker_policy', APPROVED_NAME) if word in source]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or '').split('.')[0] == 'agent':
            for alias in node.names:
                if (node.module, alias.name, alias.asname) != (APPROVED_MODULE, APPROVED_NAME, None):
                    found.append(f'{filename}:{node.lineno} from {node.module} import {alias.name}')
        elif isinstance(node, ast.Import):
            found += [f'{filename}:{node.lineno} import {alias.name}' for alias in node.names
                      if alias.name.split('.')[0] == 'agent']
        elif (_imported_module(node) or '').split('.')[0] == 'agent':
            found.append(f'{filename}:{node.lineno} import_module({_imported_module(node)!r})')
    return found


class HostSurfaceTests(unittest.TestCase):
    def test_the_scan_catches_each_private_shape(self):
        source = ('from hermes_cli.config import _deep_merge, load_config_readonly\n'
                  'from hermes_cli import config as host_config\n'
                  'import importlib\n'
                  'scope = importlib.import_module("hermes_cli.managed_scope")\n'
                  'host_config._canonicalize_config({})\n'
                  'scope._DEFAULT_MANAGED_DIR\n'
                  'getattr(host_config, "_normalize_root_model_keys")\n'
                  'host_config.load_config_readonly()\n'
                  'import json\n'
                  'json._default_decoder\n'
                  'from . import _local\n')
        found, modules = violations(source)
        self.assertEqual(sorted(item.split(' ', 1)[1] for item in found),
                         sorted(['from hermes_cli.config import _deep_merge', 'host_config._canonicalize_config',
                                 'scope._DEFAULT_MANAGED_DIR', "getattr(host_config, '_normalize_root_model_keys')"]))
        self.assertEqual(modules, {'hermes_cli.config', 'hermes_cli', 'hermes_cli.managed_scope'})

    def test_the_approved_internal_is_allowed_only_by_its_one_statement_in_its_one_file(self):
        approved = 'from agent.safe_worker_policy import worker_config_snapshot\n'
        self.assertEqual(violations(approved, APPROVED_FILE)[0], [])
        for source in ('from agent.safe_worker_policy import safe_worker_enabled\n',
                       'from agent.safe_worker_policy import _current_policy\n',
                       'from agent.safe_worker_policy import worker_config_snapshot as snapshot\n',
                       'from agent import safe_worker_policy\n',
                       'import agent.safe_worker_policy\n',
                       'import importlib\npolicy = importlib.import_module("agent.safe_worker_policy")\n',
                       'from agent.managed_worker import bind_worker_policy\n'):
            with self.subTest(source=source):
                self.assertNotEqual(violations(approved + source, APPROVED_FILE)[0], [])
        for source in (approved, 'getattr(policy, "worker_config_snapshot")\n',
                       '"""Reads agent.safe_worker_policy."""\n'):
            with self.subTest(elsewhere=source):
                self.assertNotEqual(violations(source, 'plugin/runtime_lifecycle.py')[0], [])

    def test_production_code_uses_no_private_host_names(self):
        found, modules = [], set()
        for path in sorted(PLUGIN_DIR.rglob('*.py')):
            more, imported = violations(path.read_text(encoding='utf-8'), str(path.relative_to(PLUGIN_DIR.parent)))
            found += more
            modules |= imported
        # Not vacuous: the scan sees the host modules the plugin does import, the approved one included.
        self.assertTrue({'hermes_constants', 'ruamel'} <= {name.split('.')[0] for name in modules}, modules)
        self.assertIn(APPROVED_MODULE, modules)
        self.assertEqual(found, [], 'private host names in production code')


if __name__ == '__main__':
    unittest.main()
