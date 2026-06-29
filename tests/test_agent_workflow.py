import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_PATH = REPO_ROOT / "AGENTS.md"


class TestAgentWorkflowDocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agents_text = AGENTS_PATH.read_text(encoding="utf-8")

    def test_agents_file_exists_at_repo_root(self):
        self.assertTrue(AGENTS_PATH.is_file())

    def test_no_removed_helper_script_is_referenced(self):
        self.assertNotIn("scripts/agent-workflow.sh", self.agents_text)

    def test_manual_branch_protocol_is_documented(self):
        self.assertIn("git fetch origin", self.agents_text)
        self.assertIn("git switch main && git pull --ff-only origin main && git switch -c docs/example-change", self.agents_text)
        self.assertIn("git switch -c docs/example-change", self.agents_text)
        self.assertIn("Allowed branch prefixes are `feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `test/`, and `perf/`.", self.agents_text)

    def test_pr_protocol_targets_main(self):
        self.assertIn("Every PR opened by the agent must target `main`.", self.agents_text)
        self.assertIn("gh pr create --base main --head <branch> --fill", self.agents_text)

    def test_agents_loading_notes_are_documented(self):
        self.assertIn("Compatible coding agents automatically read the repository-root `AGENTS.md`", self.agents_text)
        self.assertIn("Git, GitHub, and Python tooling do not execute `AGENTS.md`.", self.agents_text)


if __name__ == "__main__":
    unittest.main()
