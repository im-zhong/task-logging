# Publishing a new version

This document is the canonical recipe for cutting a release of
`task-logging` to PyPI. It exists because release-time mistakes are
disproportionately painful (you can't unpublish a version, you can
only yank it), so getting it right by following a list is better than
remembering.

## How releases are wired (one-time context)

The release pipeline is **tag-driven** and uses
[PyPI Trusted Publishers (OIDC)](https://docs.pypi.org/trusted-publishers/) —
no API tokens stored in GitHub.

```
git tag vX.Y.Z         ──▶  GitHub Actions: .github/workflows/publish.yml
                                 ├── ruff
                                 ├── mypy --strict
                                 ├── pytest
                                 ├── uv build  →  dist/*.whl + *.tar.gz
                                 └── uv publish
                                       │
                                       ▼
                                     PyPI
```

You don't run `uv build` or `uv publish` locally. You don't need
PyPI credentials on your machine. The only thing you do is:

1. Bump the version in `pyproject.toml`
2. Push a `vX.Y.Z` tag

Everything else is the workflow.

## One-time setup (already done — for reference only)

If you ever need to re-bootstrap on a fresh repo or PyPI account, here's
what was configured. Skip if you're just cutting a release.

1. **PyPI Trusted Publisher.** On
   <https://pypi.org/manage/project/task-logging/settings/publishing/>,
   add a publisher with:
   - Owner: `im-zhong`
   - Repository: `task-logging`
   - Workflow: `publish.yml`
   - Environment: `pypi`

2. **GitHub environment.** In the GitHub repo settings, create an
   environment called `pypi`. The workflow gates on it. Optionally
   restrict it to protected branches (`main`) and require a manual
   review before deployment.

3. **Workflow file.** Already at `.github/workflows/publish.yml`.
   Triggered on `push:tags: ["v*"]`. Uses
   `permissions: id-token: write` for OIDC.

## Versioning policy

We follow [SemVer](https://semver.org/) loosely, with the practical rule
that anything that would break a user's import or output goes through a
major bump.

| Change | Version bump |
|---|---|
| Breaking API change (renamed symbol, removed kwarg, JSON key rename) | major |
| New public symbol, new optional kwarg | minor |
| Bug fix, doc-only change, internal refactor | patch |

While the project is `0.x`, we don't make hard SemVer promises — `0.x.y`
breaking changes are flagged with `!` in commit subjects (`feat!: ...`,
`refactor!: ...`) but only bump the patch or minor. We'll switch to
strict SemVer at `1.0.0`.

Look at the recent `feat!:` / `refactor!:` commits in `git log` to
decide if a release is breaking. If you're unsure, treat it as breaking
— it's cheaper to over-bump than to under-bump.

## Pre-flight (before you tag)

These checks run again in CI, but failing locally first saves the
release-pipeline round-trip.

```bash
# Working tree clean and on main, with everything you want released merged in
git status
git checkout main
git pull --rebase

# All gates green
uv run ruff check .
uv run mypy task_logging
uv run pytest
```

If any of those fail, fix and merge to `main` before continuing.

Quickly confirm the **README quick-start still copy-pastes and runs**.
This is the example users will try first; if it doesn't work, the
release is broken even though every test passed:

```bash
uv run python -c "
import logging, sys
from task_logging import JsonFormatter, TaskLogFilter, task_log_context

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
handler.addFilter(TaskLogFilter(global_log_attrs={'service': 'check'}))
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)

with task_log_context({'task_id': 't-1'}):
    logging.getLogger('biz').info('release smoke test')
"
```

The output should be one JSON line containing `service`, `task_id`, and
`message`. If it doesn't, stop and investigate.

## Cutting a release

### 1. Pick the new version

Inspect commits since the last tag and decide major / minor / patch:

```bash
git log $(git describe --tags --abbrev=0)..HEAD --oneline
```

For this example assume the new version is `0.0.3`.

### 2. Bump `pyproject.toml`

Edit one line:

```toml
[project]
name = "task-logging"
version = "0.0.3"   # ← was 0.0.2
```

Then sync the lockfile so `uv.lock` records the new version:

```bash
uv lock
```

Both files should now show the bumped version. Verify:

```bash
grep '^version' pyproject.toml
grep -A1 '"task-logging"' uv.lock | head -3
```

### 3. Commit the bump

Use a deterministic commit message — past releases used the form
`bump version to X.Y.Z`. Match it:

```bash
git add pyproject.toml uv.lock
git commit -m "bump version to 0.0.3"
git push origin main
```

### 4. Tag and push

The tag is what triggers the publish workflow. **Tag the bump commit
itself**, not anything later — you want the tag, the version in
`pyproject.toml`, and the published artifact to all line up exactly.

```bash
git tag -a v0.0.3 -m "v0.0.3"
git push origin v0.0.3
```

### 5. Watch the workflow

Open <https://github.com/im-zhong/task-logging/actions> and find the
`Publish` run for tag `v0.0.3`. It will:

1. Run the full test matrix (ruff, mypy, pytest)
2. `uv build` to produce a wheel + sdist under `dist/`
3. `uv publish` to upload to PyPI via OIDC (no token exchange, no
   secret reveal)

Total runtime ~2 minutes. If a step fails, the upload doesn't happen —
you get a failure email and can fix forward (see "Recovering from a
broken release" below).

### 6. Verify it landed

Once the workflow shows green:

- <https://pypi.org/project/task-logging/> should list the new version
- The PyPI page's metadata (description, keywords, classifiers) should
  match `pyproject.toml`
- A clean install pulls the right thing:

```bash
# In a throwaway venv, NOT the dev environment:
mkdir /tmp/release-check && cd /tmp/release-check
uv venv && source .venv/bin/activate
uv pip install task-logging==0.0.3
uv run python -c "import task_logging; print(task_logging.__file__)"
```

PyPI's index can lag for ~30 seconds after upload; if `pip` says no
matching distribution, wait and retry.

### 7. Cut a GitHub Release (optional but recommended)

The PyPI page is the source of truth for "what's installable", but the
GitHub release page is the source of truth for "what changed and why."

```bash
gh release create v0.0.3 \
    --title "v0.0.3" \
    --notes "$(git log $(git describe --tags --abbrev=0 v0.0.3^)..v0.0.3 --pretty=format:'- %s')"
```

This generates release notes from the commit subjects between the
previous tag and this one. Edit before publishing if anything reads
oddly.

## Recovering from a broken release

You have three different options depending on how broken it is.

### The workflow failed — nothing was published

Just fix the bug, push to `main`, and re-tag. PyPI never saw the
broken artifact.

```bash
# Fix the bug, commit, push.
git push origin main

# Delete the failed tag locally and remotely:
git tag -d v0.0.3
git push origin :refs/tags/v0.0.3

# Bump again if needed and re-tag the new commit:
git tag -a v0.0.3 -m "v0.0.3"
git push origin v0.0.3
```

### The artifact uploaded but is broken

**You cannot replace a PyPI version.** Once `task-logging==0.0.3` is on
PyPI, that file hash is permanent. Two paths:

1. **Yank the broken release.** PyPI will still serve it to anyone who
   pinned exactly `==0.0.3`, but `pip install task-logging` will skip
   over it for new installs. Yank from
   <https://pypi.org/project/task-logging/0.0.3/>.

2. **Cut a fix-up release.** Bump to `0.0.4`, tag, and publish a fixed
   build. This is what you want for anything most users would notice.

Yank doesn't free up the version number — `0.0.3` is still consumed.
The next release is `0.0.4` regardless of whether you yanked or not.

### Wrong commit was tagged

(e.g. you tagged before the fix was merged.)

```bash
git tag -d v0.0.3
git push origin :refs/tags/v0.0.3
# ...merge the right thing, then re-tag the right commit:
git tag -a v0.0.3 <correct-sha> -m "v0.0.3"
git push origin v0.0.3
```

This only works if PyPI didn't already publish from the wrong tag. If
it did, treat it as the "broken artifact" case above.

## Things that should never happen

- **Don't publish from local.** The workflow is the single publish
  path. If you find yourself reaching for `uv publish` on your laptop,
  stop and figure out why the workflow isn't doing it.
- **Don't store a PyPI API token in the repo or in GitHub Secrets.**
  Trusted Publishers / OIDC make this unnecessary. If you find an old
  `PYPI_API_TOKEN` secret, delete it.
- **Don't reuse a version number.** PyPI rejects re-uploads of the
  same name+version, by design. If your workflow upload silently
  "succeeds" but PyPI shows the old artifact, something's wrong —
  read the workflow log.
- **Don't tag without bumping `pyproject.toml`.** The workflow will
  build a wheel whose internal version doesn't match the tag, and
  PyPI will either reject it or accept a confusingly-named one.

## TL;DR

```bash
# 1. Pre-flight
git checkout main && git pull --rebase
uv run ruff check . && uv run mypy task_logging && uv run pytest

# 2. Bump
$EDITOR pyproject.toml      # version = "X.Y.Z"
uv lock
git commit -am "bump version to X.Y.Z"
git push

# 3. Tag → triggers publish workflow
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z

# 4. Watch https://github.com/im-zhong/task-logging/actions
# 5. Verify https://pypi.org/project/task-logging/
```
