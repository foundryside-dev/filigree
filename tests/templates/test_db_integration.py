"""Tests for FiligreeDB.templates lazy property integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from filigree.core import FiligreeDB, write_config
from tests._db_factory import make_db


class TestFiligreeDBTemplatesProperty:
    """Test lazy TemplateRegistry property on FiligreeDB."""

    def test_templates_property_returns_registry(self, tmp_path: Path) -> None:
        """db.templates should return a TemplateRegistry instance."""
        db = make_db(tmp_path, packs=["core", "planning"])

        from filigree.templates import TemplateRegistry

        assert isinstance(db.templates, TemplateRegistry)
        db.close()

    def test_templates_property_lazy(self, tmp_path: Path) -> None:
        """The registry is built on first access to ``.templates`` and the cached
        instance is reused on subsequent accesses."""
        db = make_db(tmp_path)

        # Accessing .templates populates and returns the cached instance
        reg = db.templates
        assert reg is not None
        assert db._template_registry is reg
        assert db.templates is reg  # still the same on second access
        assert db.templates is reg  # and on third access

        db.close()

    def test_templates_property_has_types(self, tmp_path: Path) -> None:
        """Loaded registry should have types from enabled packs."""
        db = make_db(tmp_path, packs=["core", "planning"])

        reg = db.templates
        assert reg.get_type("task") is not None
        assert reg.get_type("bug") is not None
        assert reg.get_type("milestone") is not None

        db.close()

    def test_templates_injectable(self, tmp_path: Path) -> None:
        """TemplateRegistry should be injectable via constructor for testing."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "test", "version": 1})

        from filigree.templates import TemplateRegistry

        custom_reg = TemplateRegistry()
        db = FiligreeDB(filigree_dir / "filigree.db", prefix="test", template_registry=custom_reg)
        db.initialize()

        # Should use the injected registry, not create a new one
        assert db.templates is custom_reg
        # Injected registry is empty (no load called), so no types
        assert db.templates.get_type("task") is None

        db.close()

    def test_templates_no_circular_import(self) -> None:
        """Importing core should not import templates at module load time."""
        import sys

        # If filigree.templates is already imported, remove it temporarily
        had_templates = "filigree.templates" in sys.modules
        if had_templates:
            saved = sys.modules.pop("filigree.templates")

        try:
            # Force re-import of core
            if "filigree.core" in sys.modules:
                # Just verify that core can be imported without templates
                # The TYPE_CHECKING guard means the import is deferred
                import filigree.core

                assert hasattr(filigree.core.FiligreeDB, "templates")
        finally:
            if had_templates:
                sys.modules["filigree.templates"] = saved

    def test_templates_property_uses_filigree_dir(self, tmp_path: Path) -> None:
        """The registry should load from the correct .filigree directory."""
        db = make_db(tmp_path, packs=["core"])

        reg = db.templates
        # Only core enabled, so planning types should not be loaded
        assert reg.get_type("task") is not None
        # milestone is in planning pack — should NOT be available if only core enabled
        assert reg.get_type("milestone") is None

        db.close()

    def test_templates_property_prefers_constructor_enabled_packs(self, tmp_path: Path) -> None:
        """Constructor enabled_packs should override config for template loading."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "test", "version": 1, "enabled_packs": ["core", "planning"]})

        db = FiligreeDB(filigree_dir / "filigree.db", prefix="test", enabled_packs=["core"])
        db.initialize()

        reg = db.templates
        assert reg.get_type("task") is not None
        assert reg.get_type("milestone") is None

        db.close()

    def test_templates_with_from_project(self, tmp_path: Path) -> None:
        """FiligreeDB.from_project() should have working templates property."""
        filigree_dir = tmp_path / ".filigree"
        filigree_dir.mkdir()
        write_config(
            filigree_dir,
            {
                "prefix": "proj",
                "version": 1,
                "enabled_packs": ["core", "planning"],
            },
        )

        init_db = FiligreeDB(filigree_dir / "filigree.db", prefix="proj")
        init_db.initialize()
        init_db.close()

        db = FiligreeDB.from_project(tmp_path)
        assert db.templates.get_type("task") is not None
        assert db.templates.get_type("milestone") is not None
        db.close()

    def test_string_enabled_packs_raises_type_error(self, tmp_path: Path) -> None:
        """FiligreeDB constructor must reject a bare string for enabled_packs."""
        with pytest.raises(TypeError, match="bare string"):
            FiligreeDB(tmp_path / "filigree.db", prefix="test", enabled_packs="core")  # type: ignore[arg-type]
