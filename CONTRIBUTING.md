# Contributing

Thanks for your interest in `wsbuilder`.

## Repository identity

The only authorized human account for this repository is `jorgelsc-dev`.
Before committing, configure the identity locally for this repository:

```sh
git config --local user.name jorgelsc-dev
git config --local user.email 62909911+jorgelsc-dev@users.noreply.github.com
git config --local user.useConfigOnly true
git config --local credential.https://github.com.username jorgelsc-dev
gh api user --jq .login
git var GIT_AUTHOR_IDENT
git var GIT_COMMITTER_IDENT
```

The GitHub account must be `jorgelsc-dev`, and both Git identities must match
the name and email configured above. Check for environment overrides if they
do not match. Keep these settings local to this repository.
The required `enforce-source` check rejects other human accounts and commit
identities. Existing Dependabot and release automation retain their bot identities.

## Branching and pull requests

1. Start from `main` updated with `origin/main`.
2. Create a focused topic branch:
   `git switch main && git pull --ff-only origin main && git switch -c <type>/<slug>`.
3. Use one of these branch prefixes: `feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `test/`, `perf/`.
4. Keep each branch focused on one logical change.
5. Open the pull request against `main`:
   `git push -u origin <branch> && gh pr create --base main --head <branch> --fill`.
6. Include validation notes for the changes you made.

## Local validation

Before opening a PR, run the checks that apply to the area you touched.

## Security

Do not report vulnerabilities in public issues. Use a private channel and keep the report minimal until triage.

## Optional financial support

If you want to support project maintenance, see `SUPPORT.md`.
