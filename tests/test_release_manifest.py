import unittest
from pathlib import Path

import yaml

from app import VERSION


ROOT = Path(__file__).parents[1]


class ReleaseManifestTests(unittest.TestCase):
    def test_versioned_ingress_entry_has_no_leading_slash(self):
        manifest = yaml.safe_load(
            (ROOT / "faceid-addon" / "config.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], VERSION)
        self.assertEqual(manifest["ingress_entry"], f"ui-{VERSION}")
        self.assertFalse(manifest["ingress_entry"].startswith("/"))


if __name__ == "__main__":
    unittest.main()
