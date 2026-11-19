"""Checks that the UI inventory tests run in `make e2e` and in CI.

Run with: python3 -m unittest discover -s .github/scripts
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return (ROOT / path).read_text()


class UiInventoryProcess(unittest.TestCase):
    def test_inventory_tests_run_in_make_e2e_and_ci(self):
        # make e2e runs every spec in frontend/e2e, and CI runs make e2e, then the same
        # specs against the image.
        config = read("frontend/playwright.config.ts")
        self.assertIn('testDir: "./e2e"', config)
        self.assertRegex(read("Makefile"), re.compile(r"^e2e:.*\n(\t.*\n)*\tcd frontend && npx playwright test$", re.MULTILINE))
        ci = read(".github/workflows/ci.yml")
        self.assertIn("run: make e2e", ci)
        self.assertIn("E2E_BASE_URL=http://127.0.0.1:3000", ci)
        for spec in ("ui-inventory.spec.ts", "ui-inventory-fixtures.spec.ts"):
            self.assertTrue((ROOT / "frontend/e2e" / spec).is_file(), spec)
        self.assertTrue((ROOT / "frontend/e2e/ui-inventory.yaml").is_file())

    def test_entries_name_no_tracker_issue(self):
        # Entries carry no issue number; a known bug is `bug: {fails, note}` without one.
        fixtures = ROOT / "frontend/e2e/fixtures/ui-inventory"
        for path in [ROOT / "frontend/e2e/ui-inventory.yaml", *fixtures.glob("*.yaml")]:
            text = path.read_text()
            issue_key = re.compile(r"^\s+issue:", re.MULTILINE)
            self.assertNotRegex(text, issue_key, path.name)
            self.assertNotRegex(text, re.compile(r"bug:\s*\{[^}]*\bissue\b"), path.name)

if __name__ == "__main__":
    unittest.main()
