import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRIBUTING_PATH = REPO_ROOT / "CONTRIBUTING.md"


class TestContributingWorkflowDocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contributing_text = CONTRIBUTING_PATH.read_text(encoding="utf-8")

    def test_branch_protocol_is_documented(self):
        self.assertIn("git switch main && git pull --ff-only origin main && git switch -c <type>/<slug>", self.contributing_text)
        self.assertIn("`feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `test/`, `perf/`", self.contributing_text)

    def test_pr_protocol_targets_main(self):
        self.assertIn("gh pr create --base main --head <branch> --fill", self.contributing_text)

    def test_repository_identity_is_documented(self):
        text = self.contributing_text
        self.assertIn("The only authorized human account for this repository is `jorgelsc-dev`.", text)
        self.assertIn("git config --local user.name jorgelsc-dev", text)
        self.assertIn("git config --local user.email 62909911+jorgelsc-dev@users.noreply.github.com", text)
        self.assertIn("git var GIT_AUTHOR_IDENT", text)
        self.assertIn("git var GIT_COMMITTER_IDENT", text)


if __name__ == "__main__":
    unittest.main()
