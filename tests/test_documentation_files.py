import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
DECLARED_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
ARTIFACT_NAME = re.compile(r"wsbuilder-(\d+\.\d+\.\d+[^-\s/]*)")


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


class TestPackageVersion(unittest.TestCase):
    """The version lives in two files that the release workflow patches
    independently; nothing else would notice if one of them drifted."""

    def setUp(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            self.project_version = tomllib.load(handle)["project"]["version"]
        init_text = (ROOT / "src" / "wsbuilder" / "__init__.py").read_text(encoding="utf-8")
        match = DECLARED_VERSION.search(init_text)
        self.assertIsNotNone(match, "src/wsbuilder/__init__.py declares no __version__")
        self.module_version = match.group(1)

    def test_pyproject_and_module_versions_agree(self):
        self.assertEqual(self.project_version, self.module_version)

    def test_main_declares_an_unreleased_development_version(self):
        # release-from-main.yml strips the suffix on the release/vX.Y.Z branch
        # it cuts, so a plain version here means the tree claims to *be* a
        # published release it is not.
        self.assertRegex(
            self.project_version,
            r"^\d+\.\d+\.\d+\.dev\d+$",
            "the version on main must carry a .devN suffix",
        )

    def test_docs_do_not_pin_a_stale_artifact_version(self):
        stale = []
        for path in sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]:
            for found in ARTIFACT_NAME.findall(path.read_text(encoding="utf-8")):
                if found != self.project_version:
                    stale.append(f"{path.relative_to(ROOT)}: wsbuilder-{found}")

        self.assertEqual(stale, [])


if __name__ == "__main__":
    unittest.main()
