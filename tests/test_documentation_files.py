import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


class TestDocumentationFiles(unittest.TestCase):
    def test_readme_local_links_exist(self):
        readme = ROOT / "README.md"
        text = readme.read_text(encoding="utf-8")

        missing = []
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if not (ROOT / target).exists():
                missing.append(raw_target)

        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
