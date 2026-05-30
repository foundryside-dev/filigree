"""Type-level guards for entity association IDs and content hashes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_entity_association_newtypes_reject_positional_swaps(tmp_path: Path) -> None:
    """Mypy must reject swapped NewTypes at the DB-layer call boundary."""
    specimen = tmp_path / "entity_assoc_typecheck.py"
    specimen.write_text(
        """
from filigree.db_entity_associations import EntityAssociationsMixin
from filigree.types.core import ClarionEntityId, ContentHash, IssueId


def exercise(db: EntityAssociationsMixin) -> None:
    issue_id = IssueId("test-1234567890")
    entity_id = ClarionEntityId("py:func:target")
    content_hash = ContentHash("hash")
    db.add_entity_association(issue_id, entity_id, content_hash)
    db.add_entity_association(entity_id, issue_id, content_hash)
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            "pyproject.toml",
            str(specimen),
        ],
        check=False,
        cwd=Path(__file__).parents[2],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "incompatible type" in output
    assert "EntityId" in output
    assert "IssueId" in output
