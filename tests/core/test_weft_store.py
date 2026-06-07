"""Tests for the .weft/filigree/ store layout, weft.toml [filigree] store_dir
overlay, and backward-compat with legacy .filigree/ installs.

WEFT config/store consolidation (filigree-37e3f26145). The machine-owned store
moves from .filigree/ to the federation convention .weft/filigree/. The root
.filigree.conf anchor stays. weft.toml [filigree].store_dir is an operator-authored,
read-only, enrich-only overlay that relocates the store subtree (legis parity);
a missing/malformed weft.toml or absent [filigree] table boots on defaults and
never hard-fails (C-9c).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from filigree.core import (
    CONF_FILENAME,
    CONFIG_FILENAME,
    DB_FILENAME,
    FILIGREE_APPLICATION_ID,
    FILIGREE_DIR_NAME,
    LEGACY_MOVED_BREADCRUMB,
    WEFT_DIR_NAME,
    WEFT_MEMBER_SUBDIR,
    FiligreeDB,
    find_filigree_anchor,
    migrate_store_to_weft,
    read_conf,
    read_weft_filigree_table,
    resolve_store_dir,
    write_conf,
    write_config,
)


def _weft_store(root: Path) -> Path:
    """Return the canonical .weft/filigree/ store dir under *root*."""
    return root / WEFT_DIR_NAME / WEFT_MEMBER_SUBDIR


class TestResolveStoreDir:
    def test_defaults_to_weft_store_when_neither_layout_present(self, tmp_path: Path) -> None:
        # No .weft/filigree/, no legacy .filigree/, no weft.toml: the canonical
        # default is .weft/filigree/ (where a fresh install would create it).
        assert resolve_store_dir(tmp_path) == _weft_store(tmp_path)

    def test_prefers_weft_store_dir_when_present(self, tmp_path: Path) -> None:
        _weft_store(tmp_path).mkdir(parents=True)
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        assert resolve_store_dir(tmp_path) == _weft_store(tmp_path)

    def test_falls_back_to_legacy_filigree_dir(self, tmp_path: Path) -> None:
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        assert resolve_store_dir(tmp_path) == tmp_path / FILIGREE_DIR_NAME

    def test_weft_toml_store_dir_override_relative(self, tmp_path: Path) -> None:
        (tmp_path / FILIGREE_DIR_NAME).mkdir()  # legacy present, but override wins
        (tmp_path / "weft.toml").write_text('[filigree]\nstore_dir = "var/state/fg"\n')
        assert resolve_store_dir(tmp_path) == tmp_path / "var" / "state" / "fg"

    def test_weft_toml_store_dir_override_absolute(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        (tmp_path / "weft.toml").write_text(f'[filigree]\nstore_dir = "{elsewhere}"\n')
        assert resolve_store_dir(tmp_path) == elsewhere

    def test_malformed_weft_toml_falls_back_to_default_never_raises(self, tmp_path: Path) -> None:
        (tmp_path / "weft.toml").write_text("this is not [valid toml")
        # Must not raise; boots on the built-in default (C-9c).
        assert resolve_store_dir(tmp_path) == _weft_store(tmp_path)

    def test_non_utf8_weft_toml_falls_back_to_default_never_raises(self, tmp_path: Path) -> None:
        # tomllib decodes UTF-8 internally; a non-UTF-8 weft.toml raises
        # UnicodeDecodeError, which must be caught (C-9c), not propagated.
        (tmp_path / "weft.toml").write_bytes(b'[filigree]\nstore_dir = "\xff\xfe bad"\n')
        assert resolve_store_dir(tmp_path) == _weft_store(tmp_path)
        assert read_weft_filigree_table(tmp_path) == {}

    def test_weft_toml_without_filigree_table_is_noop(self, tmp_path: Path) -> None:
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        (tmp_path / "weft.toml").write_text('[loomweave]\nstore_dir = "ignored"\n')
        assert resolve_store_dir(tmp_path) == tmp_path / FILIGREE_DIR_NAME


class TestReadWeftFiligreeTable:
    def test_absent_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_weft_filigree_table(tmp_path) == {}

    def test_no_filigree_table_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "weft.toml").write_text('[loomweave]\nstore_dir = "x"\n')
        assert read_weft_filigree_table(tmp_path) == {}

    def test_malformed_returns_empty_never_raises(self, tmp_path: Path) -> None:
        (tmp_path / "weft.toml").write_text("[filigree")
        assert read_weft_filigree_table(tmp_path) == {}

    def test_reads_filigree_table(self, tmp_path: Path) -> None:
        (tmp_path / "weft.toml").write_text('[filigree]\nstore_dir = "s"\n')
        assert read_weft_filigree_table(tmp_path) == {"store_dir": "s"}


class TestFindAnchorWeftStore:
    def test_confless_weft_store_returns_store_dir(self, tmp_path: Path) -> None:
        store = _weft_store(tmp_path)
        store.mkdir(parents=True)
        write_config(store, {"prefix": "p", "version": 1})
        anchor = find_filigree_anchor(tmp_path)
        assert anchor.project_root == tmp_path
        assert anchor.conf_path is None
        assert anchor.store_dir == store

    def test_conf_install_store_dir_is_presence_probe_not_hardcoded(self, tmp_path: Path) -> None:
        # Legacy conf + .filigree/ (no .weft/): store_dir must resolve to the
        # legacy dir, NOT a nonexistent .weft/filigree/ (which would silently
        # drop enabled_packs read from config.json).
        legacy = tmp_path / FILIGREE_DIR_NAME
        legacy.mkdir()
        conf = tmp_path / CONF_FILENAME
        write_conf(conf, {"version": 1, "project_name": "p", "prefix": "p", "db": f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"})
        anchor = find_filigree_anchor(tmp_path)
        assert anchor.conf_path == conf
        assert anchor.store_dir == legacy

    def test_weft_store_wins_over_legacy_same_dir(self, tmp_path: Path) -> None:
        _weft_store(tmp_path).mkdir(parents=True)
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        anchor = find_filigree_anchor(tmp_path)
        assert anchor.conf_path is None
        assert anchor.store_dir == _weft_store(tmp_path)


def _make_legacy_install(root: Path, *, prefix: str = "proj") -> Path:
    """Create a vanilla legacy ``.filigree/`` install (db + config + conf)."""
    legacy = root / FILIGREE_DIR_NAME
    legacy.mkdir()
    write_config(legacy, {"prefix": prefix, "version": 1, "enabled_packs": ["core"]})
    db = FiligreeDB(legacy / DB_FILENAME, prefix=prefix)
    db.initialize()
    db.close()
    write_conf(
        root / CONF_FILENAME,
        {"version": 1, "project_name": prefix, "prefix": prefix, "db": f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"},
    )
    return legacy


class TestMigrateStoreToWeft:
    def test_fresh_project_is_noop_returning_weft_default(self, tmp_path: Path) -> None:
        store, migrated = migrate_store_to_weft(tmp_path)
        assert store == _weft_store(tmp_path)
        assert migrated is False
        # No passive creation — the helper resolves; init creates the dir.
        assert not _weft_store(tmp_path).exists()

    def test_migrates_vanilla_legacy_install_forward(self, tmp_path: Path) -> None:
        _make_legacy_install(tmp_path)
        store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        assert store == _weft_store(tmp_path)
        # db + config moved into the federation store
        assert (store / DB_FILENAME).is_file()
        assert (store / CONFIG_FILENAME).is_file()
        # conf rewritten to point at the new location
        conf = read_conf(tmp_path / CONF_FILENAME)
        assert conf["db"] == f"{WEFT_DIR_NAME}/{WEFT_MEMBER_SUBDIR}/{DB_FILENAME}"
        # legacy dir left in place with a breadcrumb (no destructive delete)
        assert (tmp_path / FILIGREE_DIR_NAME / LEGACY_MOVED_BREADCRUMB).is_file()
        assert not (tmp_path / FILIGREE_DIR_NAME / DB_FILENAME).exists()

    def test_migration_preserves_app_id_and_data(self, tmp_path: Path) -> None:
        legacy = _make_legacy_install(tmp_path)
        # seed an issue in the legacy db
        db = FiligreeDB.from_filigree_dir(legacy)
        try:
            created = db.create_issue("seed issue", type="task")
        finally:
            db.close()

        store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True

        moved_db = store / DB_FILENAME
        conn = sqlite3.connect(str(moved_db))
        try:
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        finally:
            conn.close()
        assert app_id == FILIGREE_APPLICATION_ID

        db2 = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            assert db2.get_issue(created.id) is not None
        finally:
            db2.close()

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        _make_legacy_install(tmp_path)
        migrate_store_to_weft(tmp_path)
        store, migrated = migrate_store_to_weft(tmp_path)
        assert store == _weft_store(tmp_path)
        assert migrated is False

    def test_migration_carries_federation_token(self, tmp_path: Path) -> None:
        legacy = _make_legacy_install(tmp_path)
        (legacy / "federation_token").write_text("tok-12345\n")
        store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        assert (store / "federation_token").read_text() == "tok-12345\n"

    def test_store_dir_override_blocks_auto_migration(self, tmp_path: Path) -> None:
        _make_legacy_install(tmp_path)
        (tmp_path / "weft.toml").write_text('[filigree]\nstore_dir = "var/custom"\n')
        store, migrated = migrate_store_to_weft(tmp_path)
        # Operator pinned a custom store — never auto-migrate over it.
        assert migrated is False
        assert store == tmp_path / "var" / "custom"
        assert not _weft_store(tmp_path).exists()
        # legacy install left fully intact
        assert (tmp_path / FILIGREE_DIR_NAME / DB_FILENAME).is_file()

    def test_migration_resumes_from_partial_state(self, tmp_path: Path) -> None:
        """Crash-convergence: a half-finished migration (DB already at weft,
        conf still pointing at legacy) is completed by a re-run."""
        import shutil

        legacy = _make_legacy_install(tmp_path)
        store = _weft_store(tmp_path)
        store.mkdir(parents=True)
        # Simulate "DB copied, conf not yet rewritten, legacy DB still present".
        shutil.copy2(str(legacy / DB_FILENAME), str(store / DB_FILENAME))
        assert read_conf(tmp_path / CONF_FILENAME)["db"] == f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"

        _store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        # conf now points at the weft DB; legacy DB removed; weft DB intact.
        assert read_conf(tmp_path / CONF_FILENAME)["db"] == f"{WEFT_DIR_NAME}/{WEFT_MEMBER_SUBDIR}/{DB_FILENAME}"
        assert not (legacy / DB_FILENAME).exists()
        assert (store / DB_FILENAME).is_file()

    def test_does_not_migrate_operator_relocated_db(self, tmp_path: Path) -> None:
        # Operator put the db outside .filigree/ (fg-da8d50 custom layout) and
        # keeps metadata in .filigree/. Respect that — no forced move.
        legacy = tmp_path / FILIGREE_DIR_NAME
        legacy.mkdir()
        (tmp_path / "storage").mkdir()
        write_config(legacy, {"prefix": "p", "version": 1})
        db = FiligreeDB(tmp_path / "storage" / DB_FILENAME, prefix="p", project_root=tmp_path)
        db.initialize()
        db.close()
        write_conf(
            tmp_path / CONF_FILENAME,
            {"version": 1, "project_name": "p", "prefix": "p", "db": f"storage/{DB_FILENAME}"},
        )
        store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is False
        assert store == legacy
        assert not _weft_store(tmp_path).exists()


class TestFromAnchorConflessWeftStore:
    def test_from_anchor_opens_confless_weft_store_with_correct_project_root(self, tmp_path: Path) -> None:
        store = _weft_store(tmp_path)
        store.mkdir(parents=True)
        write_config(store, {"prefix": "proj", "version": 1})
        db = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            assert db.prefix == "proj"
            assert db.db_path == store / DB_FILENAME
            # project_root must be tmp_path, NOT .weft (the store dir's parent).
            assert db.project_root == tmp_path
            assert db.meta_dir == store
        finally:
            db.close()
