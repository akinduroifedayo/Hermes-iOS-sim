import unittest


class FixtureDiscoveryTests(unittest.TestCase):
    def test_registration_fixture_does_not_repeat_unchanged_test_environments(self):
        import test_card_integration as cards
        import test_ownership_integration as ownership
        import test_reusable_preview_card as preview
        import test_runway_authority_api as fixtures

        loader = unittest.TestLoader()
        registration_tests = set(loader.getTestCaseNames(fixtures.RegistrationTests))
        self.assertTrue(registration_tests)
        for case in (cards.CardIntegrationTests, ownership.OwnershipIntegrationTests):
            self.assertFalse(registration_tests.intersection(loader.getTestCaseNames(case)), case.__name__)
        self.assertLessEqual(registration_tests, set(loader.getTestCaseNames(preview.ReusablePreviewTests)))
        self.assertIsNot(preview.ReusablePreviewTests.asyncSetUp, fixtures.RegistrationFixture.asyncSetUp)
        self.assertEqual(loader.getTestCaseNames(fixtures.RegistrationFixture), [])
        self.assertFalse(hasattr(fixtures.RegistrationFixture, 'runTest'))
