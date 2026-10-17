import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github/workflows/main-only-from-develop.yml"
)
OWNER = {
    "name": "jorgelsc-dev",
    "email": "62909911+jorgelsc-dev@users.noreply.github.com",
}


def workflow_script(step_name):
    step = WORKFLOW.read_text(encoding="utf-8").split(
        f"      - name: {step_name}\n", 1
    )[1].split("      - name:", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "requires bash and jq")
class TestPullRequestIdentityPolicy(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        gh = Path(self.temp_dir.name) / "gh"
        gh.write_text(
            '#!/bin/bash\n'
            'set -euo pipefail\n'
            'if [ "${MOCK_API_FAILURE:-0}" = "1" ]; then exit 1; fi\n'
            'while [ "$1" != "--jq" ]; do shift; done\n'
            'shift\n'
            'printf "%s" "$MOCK_COMMITS" | jq -r "$1"\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": self.temp_dir.name + os.pathsep + os.environ.get("PATH", ""),
            "BASE_REF": "main",
            "HEAD_REF": "fix/example",
            "PR_AUTHOR": "jorgelsc-dev",
            "PR_NUMBER": "1",
            "GITHUB_REPOSITORY": "jorgelsc-dev/wsbuilder",
        }
        self.commit = {
            "sha": "example-sha",
            "author": {"login": "jorgelsc-dev"},
            "committer": {"login": "jorgelsc-dev"},
            "commit": {"author": dict(OWNER), "committer": dict(OWNER)},
        }

    def run_step(self, name, **env):
        return subprocess.run(
            ["bash", "-c", workflow_script(name)],
            env={**self.env, **env},
            text=True,
            capture_output=True,
            timeout=10,
        )

    def assert_commit_status(self, commit, valid):
        result = self.run_step(
            "Validate commit identities", MOCK_COMMITS=json.dumps([commit])
        )
        self.assertEqual(result.returncode == 0, valid, result.stdout + result.stderr)

    def test_owner_can_submit_a_topic_branch(self):
        result = self.run_step("Validate PR source branch policy")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_other_humans_cannot_submit_a_topic_branch(self):
        result = self.run_step(
            "Validate PR source branch policy", PR_AUTHOR="another-user"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_bots_are_limited_to_their_branch_patterns(self):
        cases = (
            ("dependabot[bot]", "dependabot/pip/example", True),
            ("dependabot[bot]", "fix/example", False),
            ("github-actions[bot]", "chore/open-1.2.3.dev0", True),
            ("github-actions[bot]", "fix/example", False),
            ("another-user", "dependabot/pip/example", False),
        )
        for author, branch, valid in cases:
            with self.subTest(author=author, branch=branch):
                result = self.run_step(
                    "Validate PR source branch policy",
                    PR_AUTHOR=author,
                    HEAD_REF=branch,
                )
                self.assertEqual(result.returncode == 0, valid, result.stdout)

    def test_owner_still_requires_a_valid_topic_branch(self):
        for branch in ("main", "feature/example", ""):
            with self.subTest(branch=branch):
                result = self.run_step(
                    "Validate PR source branch policy", HEAD_REF=branch
                )
                self.assertNotEqual(result.returncode, 0)

    def test_canonical_owner_commit_is_accepted(self):
        self.assert_commit_status(self.commit, True)

    def test_other_names_emails_and_accounts_are_rejected(self):
        for role in ("author", "committer"):
            for field, value in (
                ("name", "another-user"),
                ("email", "another-user@example.invalid"),
                ("login", "another-user"),
            ):
                with self.subTest(role=role, field=field):
                    commit = copy.deepcopy(self.commit)
                    if field == "login":
                        commit[role][field] = value
                    else:
                        commit["commit"][role][field] = value
                    self.assert_commit_status(commit, False)

    def test_unlinked_human_author_is_rejected(self):
        self.commit["author"] = None
        self.assert_commit_status(self.commit, False)

    def test_known_automation_identities_are_accepted(self):
        for name, email, login in (
            ("release-bot", "release-bot@users.noreply.github.com", None),
            (
                "dependabot[bot]",
                "49699333+dependabot[bot]@users.noreply.github.com",
                "dependabot[bot]",
            ),
        ):
            with self.subTest(name=name):
                commit = copy.deepcopy(self.commit)
                for role in ("author", "committer"):
                    commit[role] = {"login": login}
                    commit["commit"][role] = {"name": name, "email": email}
                self.assert_commit_status(commit, True)

    def test_github_merge_committer_is_accepted(self):
        self.commit["committer"] = {"login": "web-flow"}
        self.commit["commit"]["committer"] = {
            "name": "GitHub", "email": "noreply@github.com"
        }
        self.assert_commit_status(self.commit, True)

    def test_later_api_pages_are_also_checked(self):
        bad_commit = copy.deepcopy(self.commit)
        bad_commit["commit"]["author"]["name"] = "another-user"
        result = self.run_step(
            "Validate commit identities",
            MOCK_COMMITS=json.dumps([self.commit]) + "\n" + json.dumps([bad_commit]),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("example-sha", result.stdout)

    def test_api_errors_fail_the_check(self):
        result = self.run_step("Validate commit identities", MOCK_API_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
