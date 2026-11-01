# Contributing to memdex

Thanks for helping. memdex is small on purpose; most contributions are a new outline language, a sharper
regex, a bug fix with a test, or a clearer tool description.

## Setup

```bash
git clone https://github.com/ChoYugyeong/memdex && cd memdex
pip install -e .
python -m unittest discover tests
```

The suite takes about ten seconds. `tests/test_concurrency.py` spawns real `python -m memdex` processes
against a temporary database, so it needs `python` on PATH.

## Ground rules

- **No runtime dependencies.** Standard library only. The MCP protocol is implemented in `memdex/mcp.py`
  rather than through an SDK so that installs never break across SDK versions.
- **Tool descriptions are tokens.** Every word in `tools.py` schemas is sent on every request. Keep
  descriptions terse and responses plain text.
- **Outline regexes capture the symbol name in group 1.** `outline()` only reads `match.group(1)`.
  Negative lookaheads must group their alternation: `(?!(?:if|for)\b)`, not `(?!if|for\b)`.
- **Nothing is written inside a repository.** All state lives under `MEMDEX_HOME`.
- **Secrets are never read.** Extend `SECRET_GLOBS` in `scan.py` if you find a pattern that is missing.

## Adding a language

1. Map its extensions in `LANG_BY_EXT` and add a pattern list in `PATTERNS` (`memdex/scan.py`).
2. Add a test class in `tests/test_scan.py` with a realistic sample: definitions that must be found,
   and control-flow lines that must not be.
3. Run the suite on a real project in that language and check `memdex file <path>` output for noise.

## Pull requests

- One change per PR, with a test when behaviour changes.
- Add a line under `[Unreleased]` in `CHANGELOG.md`.
- CI runs on Linux, Windows and macOS with Python 3.9 and 3.13; path handling differs between them, so
  use `os.path` helpers and `to_rel()` rather than string operations.

## Releasing (maintainers)

Bump `version` in `pyproject.toml`, move the `[Unreleased]` section in `CHANGELOG.md` to the new version,
commit, then tag and push:

```bash
git tag -a v0.2.0 -m "memdex 0.2.0" && git push origin v0.2.0
```

`release.yml` checks that the tag matches the version, runs the tests, builds the sdist and wheel and
publishes to PyPI with Trusted Publishing. No token is stored anywhere.
