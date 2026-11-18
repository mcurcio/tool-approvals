# Contributing

Thanks for helping. This project is pre-release (`0.x`): the [design pages](docs/design/system.md) describe
where it is going, and the [roadmap](README.md#roadmap) says what exists today.

## Set up

- Python 3.13 under [uv](https://docs.astral.sh/uv/) (`.python-version`). Run `uv sync` to install.
- Node 24 (`.nvmrc`) only for the release tooling; you do not need it to develop.

## Checks

Run these before you push; CI runs the same on every pull request and is the full gate.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports                               # the layering contracts in pyproject.toml
uv run python scripts/check_migrations.py         # migrations only expand
uv run python scripts/generate_contract.py --check  # the wire stubs match proto/ and vendor/
uv run pytest -n auto
actionlint                                        # the workflows (https://github.com/rhysd/actionlint)
```

The store tests start Postgres through Docker (testcontainers). The end-to-end and conformance tests run against
an image built from your tree, with both gateways, and are skipped without one:

```bash
docker build -t tool-approvals:e2e .
TOOL_APPROVALS_E2E_IMAGE=tool-approvals:e2e uv run pytest -n auto tests/e2e tests/conformance
```

A change to behaviour that the [reference pages](docs/reference/configuration.md) describe updates them in the
same pull request.

The import contracts are part of the design, not style: they keep the three services independent, the shared
kernel free of I/O, and the code that spends or answers an approval behind one door each
([module design](docs/design/module.md#the-module-map)). A change that needs a contract loosened needs a design
discussion first, in an issue.

Tests prove behaviour through the real thing where it matters: real Postgres for the store, real gateways for
the conformance suites, a real MCP client for the wait server.

## Commits and pull requests

Commits and pull request titles follow [Conventional Commits](https://www.conventionalcommits.org). Pull requests
are **squash-merged with the pull request's title as the commit message**, so the title decides the release:

| Title type | Release |
|---|---|
| `feat:` | Minor |
| `fix:`, `perf:`, `build(deps):` | Patch |
| `feat!:`, `fix!:` or a `BREAKING CHANGE:` footer | Minor |
| `docs:`, `test:`, `refactor:`, `ci:`, `chore:`, `style:` | None |

A breaking change raises the minor version only while the project is on `0.x`: that rule
(`{"breaking": true, "release": "minor"}` in `.releaserc.json`) is deleted when the project goes 1.0.
`tests/release_rules.test.mjs` runs these rules through the pinned commit analyzer in CI.

Use a scope where it helps (`feat(gate): …`). Dependabot's pull requests use the prefixes `.github/dependabot.yml`
sets: `build(deps)` for a runtime Python dependency (a patch release), `build(deps-dev)` for a development
dependency or the release tooling, and `ci(deps)` for GitHub Actions (no release).

Releases are automatic: on every merge to `main` that passes CI, [semantic-release](https://semantic-release.gitbook.io)
works out the version from the commits, tags it and publishes the GitHub release with its notes; nothing is
committed back, so the tag is the only place the version lives, and the image is built from the tagged commit.
Never tag or edit a version by hand. The `0.x` line starts from a seed tag, `v0.1.0`: with no `v*` tag
semantic-release would cut 1.0.0, so when none exists the release job tags the root commit `v0.1.0` before
semantic-release runs.
