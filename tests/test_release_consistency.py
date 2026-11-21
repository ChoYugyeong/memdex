"""Release metadata that has to agree, checked on every CI run instead of on release day.

A tag publishes pyproject.toml to PyPI and server.json to the MCP Registry; the registry accepts the entry only
if the PyPI description (README.md) carries its `mcp-name`. A version bumped in one file but not another, or a
README that still prints last release's tool count, is caught here before it reaches a tag.
"""

import json
import re
import unittest
from pathlib import Path

import memdex
from memdex.tools import CORE_TOOLS

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def pyproject_field(field: str) -> str:
    # tomllib is 3.11+; CI also runs 3.9. The [project] table's first `field = "..."` is the one.
    project = read("pyproject.toml").split("[project]", 1)[1].split("\n[", 1)[0]
    return re.search(rf'^{field}\s*=\s*"([^"]+)"', project, re.M)[1]


class VersionTests(unittest.TestCase):
    def test_every_version_field_matches(self):
        version = pyproject_field("version")
        server = json.loads(read("server.json"))
        self.assertEqual(memdex.__version__, version, "memdex/__init__.py")
        self.assertEqual(server["version"], version, "server.json version")
        package = server["packages"][0]
        self.assertEqual(package["version"], version, "server.json packages[0].version")
        self.assertEqual(package["identifier"], pyproject_field("name"), "server.json package identifier")

    def test_the_changelog_has_a_section_for_the_current_version(self):
        version = pyproject_field("version")
        self.assertRegex(read("CHANGELOG.md"), rf"(?m)^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$",
                         "the release workflow uses this section as the GitHub release notes")


class RegistryTests(unittest.TestCase):
    def test_the_pypi_description_names_the_registry_entry(self):
        name = json.loads(read("server.json"))["name"]
        self.assertIn(f"mcp-name: {name}", read("README.md"),
                      "the MCP Registry verifies PyPI ownership through this line in the package description")


class ReadmeTests(unittest.TestCase):
    def test_serve_check_examples_print_the_default_tool_count(self):
        for name in ("README.md", "README.ko.md"):
            counts = {int(n) for n in re.findall(r"memdex answered: (\d+) tools", read(name))}
            self.assertEqual(counts, {len(CORE_TOOLS)}, f"{name}: serve-check examples vs CORE_TOOLS")


if __name__ == "__main__":
    unittest.main()
