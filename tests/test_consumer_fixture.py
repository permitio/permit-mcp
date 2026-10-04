"""The consumer fixture (tests/consumer/consumer.py) uses every export of permit_mcp.

CI type-checks the fixture against the built wheel (.github/scripts/check-consumer-types.sh), so
an export the fixture leaves out would go unchecked.
"""

import ast
from pathlib import Path

import permit_mcp

FIXTURE = Path(__file__).parent / "consumer" / "consumer.py"


def test_the_consumer_fixture_imports_and_uses_every_export() -> None:
    tree = ast.parse(FIXTURE.read_text())
    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "permit_mcp"
        for alias in node.names
    }
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert sorted(imported) == sorted(permit_mcp.__all__)
    assert sorted(set(permit_mcp.__all__) - used) == []
