import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'device_check', Path(__file__).resolve().parents[1] / 'verify-lite-device-check.py')
device_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(device_check)


class UpgradeCompatibilityTest(unittest.TestCase):
    baseline = ('com.example.sumire.lite', 822, '1.7.128-lite')

    def test_same_version_update_and_future_release_are_accepted(self):
        for code in (822, 823):
            device_check.check_upgrade(self.baseline,
                                       (self.baseline[0], code, self.baseline[2]), 'cert', 'cert')

    def test_downgrade_is_rejected(self):
        with self.assertRaises(ValueError):
            device_check.check_upgrade(self.baseline, (self.baseline[0], 821, 'old'), 'cert', 'cert')

    def test_different_signer_is_rejected(self):
        with self.assertRaises(ValueError):
            device_check.check_upgrade(self.baseline, self.baseline, 'published', 'new')

    def test_full_or_upstream_packages_are_rejected(self):
        for package in ('com.example.sumire', 'com.kazumaproject.markdownhelperkeyboard.lite'):
            wrong = (package, 822, '1.7.128-lite')
            with self.assertRaises(ValueError):
                device_check.check_upgrade(self.baseline, wrong, 'cert', 'cert')
            with self.assertRaises(ValueError):
                device_check.check_upgrade(wrong, wrong, 'cert', 'cert')
