import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "agent-workflow.sh"


class TestAgentWorkflowScript(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="wsbuilder-agent-workflow-")
        self.root = Path(self.temp_dir.name)
        self.remote = self.root / "remote.git"
        self.work = self.root / "work"
        self._git("init", "--bare", "--initial-branch=main", str(self.remote), cwd=REPO_ROOT)
        self._git("clone", str(self.remote), str(self.work), cwd=REPO_ROOT)
        self._git("config", "user.name", "tester", cwd=self.work)
        self._git("config", "user.email", "tester@example.com", cwd=self.work)
        (self.work / "README.md").write_text("root\n", encoding="utf-8")
        self._git("add", "README.md", cwd=self.work)
        self._git("commit", "-m", "init", cwd=self.work)
        self._git("push", "-u", "origin", "main", cwd=self.work)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _git(self, *args, cwd):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        )

    def _run_script(self, *args, cwd=None, env=None, check=True):
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run(
            [str(SCRIPT_PATH), *args],
            cwd=cwd or self.work,
            check=check,
            text=True,
            capture_output=True,
            env=merged_env,
        )

    def test_prepare_creates_branch_from_dirty_main_without_sync(self):
        (self.work / "README.md").write_text("root\ndirty\n", encoding="utf-8")

        result = self._run_script("prepare", "docs", "dirty-main-case")

        self.assertIn("main has local changes", result.stdout)
        branch = self._git("branch", "--show-current", cwd=self.work).stdout.strip()
        self.assertEqual(branch, "docs/dirty-main-case")
        merge_base = self._git(
            "config",
            "--get",
            "branch.docs/dirty-main-case.gh-merge-base",
            cwd=self.work,
        ).stdout.strip()
        self.assertEqual(merge_base, "main")

    def test_prepare_switches_back_to_main_before_new_topic_branch(self):
        self._run_script("prepare", "docs", "first-topic")

        result = self._run_script("prepare", "fix", "second-topic")

        self.assertIn("switching back to main", result.stdout)
        branch = self._git("branch", "--show-current", cwd=self.work).stdout.strip()
        self.assertEqual(branch, "fix/second-topic")
        branches = self._git("branch", "--format=%(refname:short)", cwd=self.work).stdout.splitlines()
        self.assertIn("docs/first-topic", branches)
        self.assertIn("fix/second-topic", branches)

    def test_pr_pushes_branch_before_reporting_missing_gh_auth(self):
        self._run_script("prepare", "docs", "push-before-pr")
        (self.work / "README.md").write_text("root\nchange\n", encoding="utf-8")
        self._git("add", "README.md", cwd=self.work)
        self._git("commit", "-m", "docs: test pr fallback", cwd=self.work)

        fake_gh = self.root / "fake-gh"
        fake_gh.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)

        result = self._run_script(
            "pr",
            env={"GH_BIN": str(fake_gh)},
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gh CLI is not authenticated", result.stderr)
        remote_refs = self._git("ls-remote", "--heads", "origin", "docs/push-before-pr", cwd=self.work).stdout
        self.assertIn("refs/heads/docs/push-before-pr", remote_refs)


if __name__ == "__main__":
    unittest.main()
