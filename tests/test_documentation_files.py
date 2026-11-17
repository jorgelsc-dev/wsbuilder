import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
DECLARED_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
ARTIFACT_NAME = re.compile(r"wsbuilder-(\d+\.\d+\.\d+[^-\s/]*)")


DOCS_SITE = "https://wsbuilder.jorgelsc.dev/"
GITHUB_BLOB = "https://github.com/jorgelsc-dev/wsbuilder/blob/main/"


class TestDocumentationFiles(unittest.TestCase):
    def test_readme_uses_absolute_links(self):
        # README.md is the PyPI description, where relative links 404 (#82).
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        relative = [
            target
            for target in MARKDOWN_LINK.findall(text)
            if not target.startswith(("http://", "https://", "mailto:", "#"))
        ]
        self.assertEqual(relative, [])

    def test_readme_links_point_at_existing_pages(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        missing = []
        for target in MARKDOWN_LINK.findall(text):
            target = target.split("#", 1)[0]
            if target.startswith(DOCS_SITE):
                page = target[len(DOCS_SITE):].strip("/")
                candidates = [ROOT / "docs" / f"{page}.md", ROOT / "docs" / page / "index.md"]
                if page and not any(path.exists() for path in candidates):
                    missing.append(target)
            elif target.startswith(GITHUB_BLOB):
                if not (ROOT / target[len(GITHUB_BLOB):]).exists():
                    missing.append(target)
        self.assertEqual(missing, [])


class TestPackageVersion(unittest.TestCase):
    """The version comes from git tags through setuptools-scm (issue #69)."""

    def setUp(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            self.config = tomllib.load(handle)

    def test_the_version_is_dynamic_and_has_one_source(self):
        project = self.config["project"]
        self.assertNotIn("version", project, "pyproject.toml must not pin a version")
        self.assertIn("version", project.get("dynamic", []))
        self.assertIn("setuptools_scm", self.config["tool"])
        self.assertTrue(
            any(req.startswith("setuptools-scm") for req in self.config["build-system"]["requires"])
        )
        init_text = (ROOT / "src" / "wsbuilder" / "__init__.py").read_text(encoding="utf-8")
        self.assertIsNone(
            DECLARED_VERSION.search(init_text), "__init__.py must not hard-code __version__"
        )

    def test_the_module_reports_a_pep440_version(self):
        import wsbuilder

        self.assertRegex(wsbuilder.__version__, r"^\d+\.\d+\.\d+")

    def test_docs_do_not_pin_an_artifact_version(self):
        stale = []
        for path in sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]:
            for found in ARTIFACT_NAME.findall(path.read_text(encoding="utf-8")):
                stale.append(f"{path.relative_to(ROOT)}: wsbuilder-{found}")

        self.assertEqual(stale, [], "use wsbuilder-*.whl instead of a concrete version")


class TestPackageDiscovery(unittest.TestCase):
    def test_package_discovery_includes_subpackages(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            config = tomllib.load(handle)

        find_config = config["tool"]["setuptools"]["packages"]["find"]
        package_root = ROOT / find_config["where"][0]
        packages = {
            path.parent.relative_to(package_root).as_posix().replace("/", ".")
            for path in package_root.rglob("__init__.py")
            if path.parent.name != "__pycache__"
        }

        self.assertIn("wsbuilder", packages)
        self.assertIn("wsbuilder.quic", packages)


if __name__ == "__main__":
    unittest.main()
