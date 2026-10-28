
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import plugin


class DeclarationContext:
    state: SimpleNamespace
    profile_name = 'fixture'
    plugin_id = 'hermes-ios'

    def __init__(self):
        self.tools = {}
        self.hooks = []
        self.middleware = {}
        self.skills = []
        self.unloads = []

    def register_tool(self, *, name, handler, **kwargs):
        self.tools[name] = handler

    def register_hook(self, name, handler):
        self.hooks.append((name, handler))

    def register_middleware(self, name, handler):
        self.middleware[name] = handler

    def register_skill(self, *args):
        self.skills.append(args)

    def on_unload(self, callback):
        self.unloads.append(callback)


class DeclarationTests(unittest.TestCase):
    def setUp(self):
        self.context = DeclarationContext()
        self.addCleanup(self.unload)

    def unload(self):
        for callback in self.context.unloads:
            callback()

    def test_missing_state_declares_real_capabilities_without_constructing_runtime(self):
        with patch.object(plugin, 'StockDependency', side_effect=AssertionError('dependency constructed')), \
             patch.object(plugin, 'RuntimeStateStore', side_effect=AssertionError('store constructed')), \
             patch.object(plugin, 'StockRuntimeController', side_effect=AssertionError('controller constructed')):
            plugin.register(self.context)
            self.assertEqual(set(self.context.tools), {'ios_setup', 'ios_iteration'})
            self.assertEqual([name for name, _ in self.context.hooks].count('pre_tool_call'), 2)
            self.assertEqual([name for name, _ in self.context.hooks].count('post_tool_call'), 1)
            self.assertEqual(set(self.context.middleware), {'tool_execution'})
            self.assertEqual([skill[0] for skill in self.context.skills], ['ios-development'])
            self.assertEqual(len(self.context.unloads), 1)
            self.assertTrue(all(callable(fn) for fn in self.context.tools.values()))
            self.assertTrue(all(callable(fn) for _, fn in self.context.hooks))
            result = json.loads(asyncio.run(self.context.tools['ios_setup']({'action': 'provision'})))
            self.assertFalse(result.get('ready', result.get('ok', False)))
            self.assertNotEqual(result.get('status'), 'ready')
            result = json.loads(asyncio.run(self.context.tools['ios_iteration']({'action': 'run'})))
            self.assertFalse(result['ok'])

    def test_missing_state_hooks_and_middleware_fail_closed_without_effects(self):
        with patch.object(plugin, 'ServeSimProcessManager', side_effect=AssertionError('manager constructed')), \
             patch('subprocess.Popen', side_effect=AssertionError('process started')), \
             patch.object(plugin, '_registration', None):
            plugin.register(self.context)
            self.assertIsNone(plugin._registration)
            for name, hook in self.context.hooks:
                self.assertIsNone(hook(tool_name='unrelated', args={}))
                if name == 'pre_tool_call':
                    for tool, action in (('ios_setup', 'provision'), ('ios_iteration', 'run')):
                        result = hook(tool_name=tool, args={'action': action})
                        if result is not None:
                            self.assertEqual(result, {'action': 'block',
                                'message': 'public_stock_lifecycle_registration_required'})
            result = self.context.middleware['tool_execution']('ios_iteration', {'action': 'run'},
                lambda _: self.fail('unavailable middleware dispatched'))
            self.assertEqual(json.loads(result)['error'], 'public_stock_lifecycle_registration_required')

    def test_unload_before_first_use_is_terminal_even_if_state_appears(self):
        plugin.register(self.context)
        for callback in self.context.unloads:
            callback()
        self.context.state = SimpleNamespace(data_dir=Path('/must-not-be-created'))
        with patch.object(plugin, 'StockDependency', side_effect=AssertionError('revived dependency')), \
             patch.object(plugin, 'RuntimeStateStore', side_effect=AssertionError('revived store')):
            for action in ('status', 'provision'):
                result = json.loads(asyncio.run(self.context.tools['ios_setup']({'action': action})))
                self.assertFalse(result.get('ready', result.get('ok', False)))
                self.assertIn('retir', str(result).lower())


if __name__ == '__main__':
    unittest.main()
