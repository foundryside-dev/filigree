# Contributing to Filigree

Thank you for considering a contribution to filigree. Whether it's a bug report, feature idea, documentation fix, or code change, your help is welcome.

## How to Report Bugs

Open a [bug report](https://github.com/foundryside-dev/filigree/issues/new?template=bug_report.yml) on GitHub. Include:

- Filigree version (`filigree --version`)
- Whether you hit the issue via CLI, MCP, or the dashboard
- Steps to reproduce
- Expected vs actual behavior
- Python version and OS

## How to Suggest Features

Open a [feature request](https://github.com/foundryside-dev/filigree/issues/new?template=feature_request.yml). Describe the problem you're solving and your proposed approach.

## Development Setup

```bash
git clone https://github.com/foundryside-dev/filigree.git
cd filigree
uv sync --group dev
```

This installs all runtime and dev dependencies (ruff, mypy, pytest, coverage, etc.) into a virtualenv managed by uv.

## Code Style

- **Linter/formatter**: [ruff](https://docs.astral.sh/ruff/) (config in `pyproject.toml`)
- **Type checker**: mypy in strict mode
- **Line length**: 120 characters

Before committing:

```bash
make format            # Auto-fix formatting and lint issues
make lint              # Check without modifying (same as CI)
make typecheck         # mypy strict
```

## Running Tests

```bash
make test              # Quick run (no coverage)
make test-cov          # With coverage report and 85% threshold
```

Tests live in `tests/` and use pytest fixtures with temporary databases. No test state leaks between runs.

## Commit Messages

This project uses [Conventional Commits](https://www.conventionalcommits.org/). Each commit message should have the form:

```
<type>: <short description>

<optional body>
```

Accepted types:

| Type | When to use |
|------|-------------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `test` | Adding or updating tests |
| `ci` | CI/CD pipeline changes |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `chore` | Maintenance (deps, config, etc.) |
| `build` | Build system or packaging changes |

Use `!` after the type for breaking changes: `refactor!: rename public API`.

## Pull Request Process

1. Fork the repository and create a branch from `main`
2. Make your changes
3. Run `make ci` to verify lint, types, and tests all pass
4. Open a pull request against `main`
5. Describe what the PR does, why, and link any related issues
6. Ensure the CI checks pass

Keep PRs focused. One logical change per PR is easier to review than a large omnibus.

## First-Time Contributors

Look for issues labeled [`good first issue`](https://github.com/foundryside-dev/filigree/labels/good%20first%20issue). Good starting points include:

- Documentation improvements
- Adding tests for uncovered code paths
- CLI help text improvements

## Architecture

All SQLite operations live in `src/filigree/core.py` (the `FiligreeDB` class). Both the CLI (`cli.py`) and MCP server (`mcp_server.py`) import from it. If you're adding a new operation, add the method to `FiligreeDB` first, then expose it through whichever interface makes sense.

The summary generator (`summary.py`) regenerates `.filigree/context.md` after every mutation. If you add a new write operation, make sure it triggers a summary refresh.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
