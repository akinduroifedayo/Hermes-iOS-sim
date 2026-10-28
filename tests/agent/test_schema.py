from __future__ import annotations

import re
import unittest

from plugin.schemas import IOS_ITERATION_SCHEMA, IOS_SETUP_SCHEMA


class IOSIterationSchemaTests(unittest.TestCase):
    def test_setup_has_no_model_approval_argument(self):
        parameters = IOS_SETUP_SCHEMA['parameters']
        self.assertFalse(parameters['additionalProperties'])
        self.assertEqual(set(parameters['properties']), {'action', 'worktree'})
    def test_runtime_schema_accepts_exact_versioned_simulator_runtime(self) -> None:
        runtime_schema = IOS_ITERATION_SCHEMA["parameters"]["properties"]["runtime"]
        pattern = runtime_schema["pattern"]

        self.assertIsNotNone(re.fullmatch(pattern, "iOS 26.5"))
        self.assertIsNotNone(re.fullmatch(pattern, "iOS Simulator"))
        self.assertIsNone(re.fullmatch(pattern, "iOS Device"))
        self.assertNotIn("enum", runtime_schema)
    def test_actions_match_native_boundary_without_claim_or_model_authority(self) -> None:
        parameters = IOS_ITERATION_SCHEMA['parameters']
        self.assertFalse(parameters['additionalProperties'])
        self.assertEqual(parameters['properties']['action']['enum'],
            ['discover', 'preflight', 'run', 'preview', 'status', 'cancel', 'stop', 'handoff', 'card'])
        self.assertEqual(parameters['required'], ['action', 'worktree'])
        rule = parameters['allOf'][0]
        self.assertEqual(rule['if']['properties']['action']['enum'], ['run', 'preview', 'status', 'cancel', 'stop', 'handoff', 'card'])
        self.assertEqual(rule['then']['required'], ['project', 'scheme', 'configuration', 'runtime', 'udid'])
        self.assertTrue({'profile', 'argv', 'ownership_token', 'locality_proof', 'approved',
                         'placement_reference', 'review_mark_id', 'iteration'}.isdisjoint(parameters['properties']))
        self.assertEqual(parameters['allOf'][1]['then']['required'], ['expected_claim_generation'])
        self.assertEqual(parameters['allOf'][2]['then']['required'], ['recipient_session'])


if __name__ == "__main__":
    unittest.main()
