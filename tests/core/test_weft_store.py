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

import logging
import shutil
import sqlite3
from pathlib import Path

import pytest

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
    StoreMigrationBusyError,
    StoreMigrationConfUnreadableError,
    WeftConfigUnreadableError,
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

    def test_weft_toml_store_dir_override_absolute_is_ignored(self, tmp_path: Path) -> None:
        # An absolute store_dir cannot be represented in the conf's
        # project-relative db field, so it is ignored (warn + default), never
        # honoured half-way (would split db vs metadata).
        elsewhere = tmp_path / "elsewhere"
        (tmp_path / "weft.toml").write_text(f'[filigree]\nstore_dir = "{elsewhere}"\n')
        assert resolve_store_dir(tmp_path) == _weft_store(tmp_path)

    def test_weft_toml_store_dir_override_escaping_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / FILIGREE_DIR_NAME).mkdir()
        (tmp_path / "weft.toml").write_text('[filigree]\nstore_dir = "../outside"\n')
        # Escaping the project root is rejected; falls back to the present layout.
        assert resolve_store_dir(tmp_path) == tmp_path / FILIGREE_DIR_NAME

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

    def test_empty_weft_dir_does_not_shadow_legacy_holding_the_db(self, tmp_path: Path) -> None:
        # Data-loss guard: an *empty* .weft/filigree/ (no DB) — e.g. left behind by
        # a busy- or copy-aborted migration that pre-created the dir before the
        # copy — must NOT shadow a legacy .filigree/ that still holds the real DB.
        # Resolution keys on DB presence, not bare directory existence; otherwise
        # a confless open would stamp a fresh empty DB over live legacy data.
        legacy = tmp_path / FILIGREE_DIR_NAME
        legacy.mkdir()
        (legacy / DB_FILENAME).write_bytes(b"SQLite format 3\x00")  # the canonical DB lives here
        _weft_store(tmp_path).mkdir(parents=True)  # empty weft dir, no DB inside
        assert resolve_store_dir(tmp_path) == legacy

    def test_committed_conf_weft_db_wins_over_legacy_husk(self, tmp_path: Path) -> None:
        # COMMITTED state for a CONF install: a .filigree.conf marker exists, so
        # the conf-presence discriminator collapses the guard to the original
        # DB-presence tie-break — once weft holds the DB it is canonical and wins
        # even with a lingering legacy husk that also has a DB. The guard keys on
        # the conf MARKER's presence, never on the conf's ``db`` contents.
        legacy = tmp_path / FILIGREE_DIR_NAME
        legacy.mkdir()
        (legacy / DB_FILENAME).write_bytes(b"SQLite format 3\x00")
        store = _weft_store(tmp_path)
        store.mkdir(parents=True)
        (store / DB_FILENAME).write_bytes(b"SQLite format 3\x00")
        # The conf marker that distinguishes a committed install from a confless
        # mid-migration window. Its db field points at weft for realism, but the
        # guard never reads it — presence alone collapses to the old tie-break.
        write_conf(
            tmp_path / CONF_FILENAME,
            {
                "version": 1,
                "project_name": "proj",
                "prefix": "proj",
                "db": f"{WEFT_DIR_NAME}/{WEFT_MEMBER_SUBDIR}/{DB_FILENAME}",
            },
        )
        assert resolve_store_dir(tmp_path) == store

    def test_confless_both_dbs_window_keeps_legacy_canonical(self, tmp_path: Path) -> None:
        # MID-MIGRATION window for a CONFLESS install: both DBs present, NO
        # .filigree.conf marker. Legacy stays canonical until migrate deletes it
        # (the de-facto commit point for confless), so a confless writer routes to
        # legacy and is never clobbered. This is the discriminator case for the
        # data-loss bug (filigree-6f4b6dcd78).
        legacy = tmp_path / FILIGREE_DIR_NAME
        legacy.mkdir()
        (legacy / DB_FILENAME).write_bytes(b"SQLite format 3\x00")
        store = _weft_store(tmp_path)
        store.mkdir(parents=True)
        (store / DB_FILENAME).write_bytes(b"SQLite format 3\x00")
        assert resolve_store_dir(tmp_path) == legacy


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

    def test_metadata_dir_copy_is_atomic_no_torn_partial(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A crash mid-copy of a metadata sub-tree (scanners/, templates/) must not
        leave a PARTIAL directory at the destination that the copy-once existence
        guard then mistakes for a finished copy and skips — the torn-then-skip
        data-loss defect (filigree-197be8b501). The atomic copytree-to-temp +
        ``os.replace`` means ``dest`` only ever appears complete; a crash leaves
        only the temp (cleaned up), so a re-run RE-COPIES rather than publishing
        the partial.
        """
        # Keep the assertion on the copytree crash deterministic (no live daemon).
        monkeypatch.setattr("filigree.core._refuse_if_daemon_serving", lambda _root: None)
        legacy = _make_legacy_install(tmp_path)
        scanners = legacy / "scanners"
        scanners.mkdir()
        (scanners / "a.json").write_text('{"a": 1}')
        (scanners / "b.json").write_text('{"b": 2}')

        real_copytree = shutil.copytree
        crashed = {"done": False}

        def boom(src: str, dst: str, *args: object, **kwargs: object) -> object:
            # Simulate a crash mid-copytree on the first metadata-dir copy.
            if "scanners" in str(src) and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("simulated crash mid-copytree")
            return real_copytree(src, dst, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("filigree.core.shutil.copytree", boom)
        with pytest.raises(RuntimeError, match="simulated crash"):
            migrate_store_to_weft(tmp_path)

        # No torn partial published at the destination: the existence guard would
        # otherwise skip it on re-run and ship the partial as if complete.
        weft_scanners = _weft_store(tmp_path) / "scanners"
        assert not weft_scanners.exists()

        # Re-run (copytree now succeeds) completes and publishes the COMPLETE dir.
        monkeypatch.setattr("filigree.core.shutil.copytree", real_copytree)
        store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        assert (store / "scanners" / "a.json").read_text() == '{"a": 1}'
        assert (store / "scanners" / "b.json").read_text() == '{"b": 2}'

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
        # The secret is carried forward, NOT left behind in the auditable husk —
        # a tracked-husk project must not retain a (now-dead) federation_token.
        assert not (legacy / "federation_token").exists()

    def test_husk_token_unlink_failure_warns_but_does_not_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-FileNotFound OSError unlinking the dead husk token (e.g. a
        PermissionError) must not fail the migration — the live token was already
        forwarded — but must WARN so the now-dead secret is not left behind in
        silence (previously a blanket ``contextlib.suppress(OSError)``)."""
        legacy = _make_legacy_install(tmp_path)
        (legacy / "federation_token").write_text("tok-12345\n")

        real_unlink = Path.unlink

        def _unlink(self: Path, *args: object, **kwargs: object) -> None:
            if self.name == "federation_token":
                raise PermissionError("husk token is read-only")
            return real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "unlink", _unlink)
        with caplog.at_level(logging.WARNING, logger="filigree.core"):
            store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        # The forwarded copy still landed; only the husk cleanup failed.
        assert (store / "federation_token").read_text() == "tok-12345\n"
        assert any("federation_token" in rec.message and "husk" in rec.message for rec in caplog.records if rec.levelno == logging.WARNING)

    def test_resumed_confless_migration_re_copies_changed_metadata(self, tmp_path: Path) -> None:
        """M1: a resumed migration must ship CURRENT metadata, not a copy frozen at
        the first (interrupted) run. For a CONFLESS install legacy stays canonical
        until step 4, so a federation_token rotated on legacy between the interrupt
        and the resume must win. Copy-once froze the stale weft copy; the symmetric
        re-copy (mirroring the DB) refreshes it.
        """
        import shutil

        # Confless legacy install with an initial token.
        legacy = tmp_path / FILIGREE_DIR_NAME
        legacy.mkdir()
        write_config(legacy, {"prefix": "proj", "version": 1, "enabled_packs": ["core"]})
        ldb = FiligreeDB(legacy / DB_FILENAME, prefix="proj")
        ldb.initialize()
        ldb.close()
        (legacy / "federation_token").write_text("tok-OLD\n")
        assert not (tmp_path / CONF_FILENAME).exists()  # confless

        # Interrupted run: weft DB + weft token already staged (stale), legacy present.
        store = _weft_store(tmp_path)
        store.mkdir(parents=True)
        shutil.copy2(str(legacy / DB_FILENAME), str(store / DB_FILENAME))
        (store / "federation_token").write_text("tok-OLD\n")

        # Token rotated on the still-canonical legacy store after the interrupt.
        (legacy / "federation_token").write_text("tok-NEW\n")

        _store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        # The resumed migration refreshed the token from canonical legacy.
        assert (store / "federation_token").read_text() == "tok-NEW\n"

    def test_resumed_conf_migration_does_not_clobber_fresher_weft_metadata(self, tmp_path: Path) -> None:
        """M1 boundary: the symmetric re-copy must NOT introduce a clobber. For a
        CONF install, weft becomes canonical the moment the weft DB lands (step 1) —
        before the conf commits. A metadata write that resolved to the now-canonical
        weft must survive a re-run; re-copying legacy→weft unconditionally would
        destroy it. The fix re-copies ONLY while legacy is canonical, so here it
        leaves the fresher weft copy intact.
        """
        import shutil

        legacy = _make_legacy_install(tmp_path)
        (legacy / "federation_token").write_text("tok-OLD\n")

        # Interrupted run: weft DB staged (so weft is now canonical for a conf
        # install), conf still points at legacy.
        store = _weft_store(tmp_path)
        store.mkdir(parents=True)
        shutil.copy2(str(legacy / DB_FILENAME), str(store / DB_FILENAME))
        assert read_conf(tmp_path / CONF_FILENAME)["db"] == f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"
        # A token write that resolved to the now-canonical weft store.
        (store / "federation_token").write_text("tok-WEFT\n")

        _store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        # The fresher weft token was preserved, NOT clobbered by stale legacy.
        assert (store / "federation_token").read_text() == "tok-WEFT\n"

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

    def test_completed_confless_migration_rerun_is_idempotent(self, tmp_path: Path) -> None:
        """A confless project (no .filigree.conf) has no conf to point at the weft
        DB, so the confful idempotency short-circuit can never fire for it. After
        its migration completes (legacy DB gone, weft DB present), a re-run must be
        an idempotent no-op — NOT a needless re-copy reporting migrated=True.
        """
        # Confless legacy install: .filigree/ with config + db, but NO .filigree.conf.
        legacy = tmp_path / FILIGREE_DIR_NAME
        legacy.mkdir()
        write_config(legacy, {"prefix": "proj", "version": 1, "enabled_packs": ["core"]})
        db = FiligreeDB(legacy / DB_FILENAME, prefix="proj")
        db.initialize()
        db.close()
        assert not (tmp_path / CONF_FILENAME).exists()  # confless

        store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        assert (store / DB_FILENAME).is_file()
        assert not (legacy / DB_FILENAME).exists()  # completed: legacy DB carried forward + removed
        assert not (tmp_path / CONF_FILENAME).exists()  # still confless — no conf created

        # Re-run a completed confless migration: idempotent no-op.
        store2, migrated2 = migrate_store_to_weft(tmp_path)
        assert migrated2 is False, "completed confless migration must re-run as a no-op"
        assert store2 == store

    def test_completed_confless_rerun_does_not_false_refuse_on_live_daemon(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The confless-completion short-circuit must return BEFORE the daemon
        liveness probe — a completed confless migration has nothing left to carry
        forward, so a live daemon can't orphan a write and must not be refused.
        """
        import filigree.core as core_mod

        legacy = tmp_path / FILIGREE_DIR_NAME
        legacy.mkdir()
        write_config(legacy, {"prefix": "proj", "version": 1, "enabled_packs": ["core"]})
        db = FiligreeDB(legacy / DB_FILENAME, prefix="proj")
        db.initialize()
        db.close()
        migrate_store_to_weft(tmp_path)  # complete it

        # Simulate a live daemon: the probe would refuse if reached.
        def _boom(_root: Path) -> None:
            raise StoreMigrationBusyError("daemon live")

        monkeypatch.setattr(core_mod, "_refuse_if_daemon_serving", _boom)
        # Must NOT raise — the completion short-circuit returns before the probe.
        _store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is False

    def test_crash_during_copy_leaves_no_partial_and_re_run_recovers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hard crash mid-copy must NOT leave a partial DB at the final path.

        The copy stages to a temp file in the dest dir and publishes with an
        atomic ``os.replace``, so the destination only ever appears as a
        complete file. A re-run then completes the migration with data intact.
        """
        legacy = _make_legacy_install(tmp_path)
        db = FiligreeDB.from_filigree_dir(legacy)
        try:
            created = db.create_issue("seed", type="task")
        finally:
            db.close()

        def _boom(src: str, dst: str, *a: object, **k: object) -> None:
            # Simulate power loss mid-copy: a partial file then a crash.
            Path(dst).write_bytes(b"\x00" * 50)
            raise OSError("simulated crash mid-copy")

        monkeypatch.setattr("filigree.core.shutil.copy2", _boom)
        with pytest.raises(OSError, match="simulated crash mid-copy"):
            migrate_store_to_weft(tmp_path)

        # No partial published at the final path; legacy DB + conf untouched.
        assert not (_weft_store(tmp_path) / DB_FILENAME).exists()
        assert (legacy / DB_FILENAME).is_file()
        assert read_conf(tmp_path / CONF_FILENAME)["db"] == f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"

        # Re-run with copy restored → migration completes, seeded data survives.
        monkeypatch.undo()
        _store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        db2 = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            assert db2.get_issue(created.id) is not None
        finally:
            db2.close()

    def test_migration_re_copies_a_corrupt_partial_weft_db(self, tmp_path: Path) -> None:
        """While legacy is canonical (conf not yet committed to weft), the
        re-run re-copies it forward unconditionally — so a truncated weft DB
        already at the destination (left by an interrupted copy) is overwritten
        from the still-valid legacy DB, never published as-is with the legacy
        original then deleted (total data loss).
        """
        legacy = _make_legacy_install(tmp_path)
        db = FiligreeDB.from_filigree_dir(legacy)
        try:
            created = db.create_issue("seed", type="task")
        finally:
            db.close()
        store = _weft_store(tmp_path)
        store.mkdir(parents=True)
        # A truncated, unreadable DB already at the destination path.
        legacy_bytes = (legacy / DB_FILENAME).read_bytes()
        (store / DB_FILENAME).write_bytes(legacy_bytes[: len(legacy_bytes) // 2])
        assert read_conf(tmp_path / CONF_FILENAME)["db"] == f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"

        _store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        # The published weft DB is intact and the seeded issue survived.
        db2 = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            assert db2.get_issue(created.id) is not None
        finally:
            db2.close()

    def test_migration_re_copies_stale_weft_preserving_post_interrupt_legacy_writes(self, tmp_path: Path) -> None:
        """C1 regression: an interrupted copy can leave an *intact-but-stale* weft
        DB. Because the conf still points at legacy, the live install keeps
        writing there, so legacy — not the stale weft snapshot — is canonical.
        The re-run must re-copy legacy forward; publishing the stale weft copy
        (then deleting legacy) would silently lose every write that landed after
        the interrupt. Guarding re-copy on weft *validity* missed this: a
        valid-but-stale copy is not a committed migration.
        """
        import shutil

        legacy = _make_legacy_install(tmp_path)
        db = FiligreeDB.from_filigree_dir(legacy)
        try:
            before = db.create_issue("before interrupt", type="task")
        finally:
            db.close()

        # Simulate the interrupt: an intact weft snapshot exists, but the conf
        # was never rewritten (still points at legacy). Copy pristine bytes —
        # never open this staged DB, so it stays a faithful stale snapshot.
        store = _weft_store(tmp_path)
        store.mkdir(parents=True)
        shutil.copy2(str(legacy / DB_FILENAME), str(store / DB_FILENAME))
        assert read_conf(tmp_path / CONF_FILENAME)["db"] == f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"

        # The live install keeps writing to the conf-pointed legacy DB after the
        # interrupt. Write explicitly to legacy (presence-driven resolution would
        # otherwise be perturbed by the staged weft dir), and close the handle so
        # the migration's TRUNCATE checkpoint isn't blocked by a live writer.
        db = FiligreeDB.from_filigree_dir(legacy)
        try:
            after = db.create_issue("after interrupt", type="task")
        finally:
            db.close()

        _store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True

        # Both issues survive in the published weft DB — the post-interrupt write
        # was NOT lost to a stale-copy publish.
        db2 = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            assert db2.get_issue(before.id) is not None
            assert db2.get_issue(after.id) is not None
        finally:
            db2.close()

    def test_committed_migration_does_not_reclobber_weft_from_lingering_legacy(self, tmp_path: Path) -> None:
        """Boundary: once the conf commits to weft, weft is canonical. A legacy
        DB that lingers (or reappears) with divergent data must NOT overwrite the
        committed weft DB — the top guard short-circuits before the re-copy. This
        fences the other side of the unconditional re-copy: pre-commit legacy
        wins, post-commit weft wins.
        """
        _make_legacy_install(tmp_path)
        _store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True

        # Post-commit, weft is the live DB. Add an issue that exists ONLY in weft.
        db = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            weft_only = db.create_issue("weft-only post-commit", type="task")
        finally:
            db.close()

        # A divergent legacy DB reappears at the old path (e.g. a restored backup).
        # The conf already points at weft, so this must be ignored, not re-copied.
        legacy = tmp_path / FILIGREE_DIR_NAME
        stale = FiligreeDB(legacy / DB_FILENAME, prefix="proj")
        stale.initialize()
        try:
            stale.create_issue("stale legacy resurrection", type="task")
        finally:
            stale.close()

        _store, migrated2 = migrate_store_to_weft(tmp_path)
        assert migrated2 is False
        # The committed weft DB is untouched: its post-commit issue still resolves.
        db2 = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            assert db2.get_issue(weft_only.id) is not None
        finally:
            db2.close()

    def test_confless_both_dbs_window_preserves_writer_data(self, tmp_path: Path) -> None:
        """Data-loss regression (filigree-6f4b6dcd78): a CONFLESS install in the
        both-DBs window must not lose a writer's data through migration.

        Stage the post-crash state directly (no crash injection): a legacy
        ``.filigree/`` DB AND a stale ``.weft/filigree/`` DB both present, with
        NO ``.filigree.conf`` marker. For a confless install the legacy delete in
        :func:`migrate_store_to_weft` is the de-facto commit point, so legacy
        stays canonical until then — :func:`resolve_store_dir` must route a
        confless writer to LEGACY during this window.

        A writer opens whatever ``resolve_store_dir`` picks (the function under
        test) and records a sentinel. Pre-fix, resolve returns weft, the sentinel
        lands in weft, and migrate's unconditional re-copy clobbers weft from the
        stale legacy DB (sentinel lost). Post-fix, resolve returns legacy, the
        sentinel lands in legacy, and migrate copies it forward — it survives.
        """
        # Confless legacy install: config (for the prefix) + DB, but NO conf.
        legacy = tmp_path / FILIGREE_DIR_NAME
        legacy.mkdir()
        write_config(legacy, {"prefix": "proj", "version": 1, "enabled_packs": ["core"]})
        legacy_db = FiligreeDB(legacy / DB_FILENAME, prefix="proj")
        legacy_db.initialize()
        legacy_db.close()
        # A stale weft DB also present (left by an interrupted migration). Its
        # mere presence is what makes the buggy resolver pick weft.
        store = _weft_store(tmp_path)
        store.mkdir(parents=True)
        write_config(store, {"prefix": "proj", "version": 1, "enabled_packs": ["core"]})
        stale_weft_db = FiligreeDB(store / DB_FILENAME, prefix="proj")
        stale_weft_db.initialize()
        stale_weft_db.close()

        # A confless writer opens WHATEVER resolve_store_dir picks — the bug's
        # actual open-time path. Correct resolution routes it to legacy.
        db = FiligreeDB.from_filigree_dir(resolve_store_dir(tmp_path))
        try:
            sentinel = db.create_issue("confless window sentinel", type="task")
        finally:
            db.close()

        migrate_store_to_weft(tmp_path)

        # Post-migration the legacy DB is gone, so resolve returns weft. The
        # sentinel must have survived the migration into the canonical store.
        db2 = FiligreeDB.from_filigree_dir(resolve_store_dir(tmp_path))
        try:
            assert db2.get_issue(sentinel.id) is not None
        finally:
            db2.close()

    def test_malformed_weft_toml_refuses_migration_leaving_legacy_intact(self, tmp_path: Path) -> None:
        """A present-but-unparseable weft.toml on the mutating init/migrate path
        must NOT be conflated with 'absent'. Conflation skips the operator-pinned
        ``store_dir`` guard (the pin could be hiding in the unreadable bytes) and
        silently relocates the store. Distinguish broken from absent and refuse:
        raise rather than auto-migrate over a config we cannot read. Passive
        discovery (``resolve_store_dir``) still boots on built-in defaults (C-9c)
        — this stricter rule applies only to the write path.
        """
        _make_legacy_install(tmp_path)
        (tmp_path / "weft.toml").write_text("this is not [valid toml")
        with pytest.raises(WeftConfigUnreadableError):
            migrate_store_to_weft(tmp_path)
        # Refused before any fs mutation: legacy fully intact, no weft store created.
        assert (tmp_path / FILIGREE_DIR_NAME / DB_FILENAME).is_file()
        assert not _weft_store(tmp_path).exists()
        assert read_conf(tmp_path / CONF_FILENAME)["db"] == f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"

    def test_non_utf8_weft_toml_refuses_migration(self, tmp_path: Path) -> None:
        # tomllib decodes UTF-8 internally; non-UTF-8 bytes raise UnicodeDecodeError
        # — also "unreadable", so the write path must refuse, not boot on defaults.
        _make_legacy_install(tmp_path)
        (tmp_path / "weft.toml").write_bytes(b'[filigree]\nstore_dir = "\xff\xfe bad"\n')
        with pytest.raises(WeftConfigUnreadableError):
            migrate_store_to_weft(tmp_path)
        assert (tmp_path / FILIGREE_DIR_NAME / DB_FILENAME).is_file()
        assert not _weft_store(tmp_path).exists()

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


class TestMigrationWalAndBusy:
    """I4: the riskiest copy-time invariants — committed-but-in-WAL pages must be
    folded forward (not orphaned by a main-file-only copy), and a held writer must
    abort the migration leaving the legacy store fully intact.
    """

    def test_committed_wal_pages_are_not_orphaned_by_copy(self, tmp_path: Path) -> None:
        """Committed pages can live in the ``-wal`` sidecar (a normal ``close()``
        does not truncate it), and ``shutil.copy2`` copies only the main ``.db``.
        The migration must therefore fold the WAL into the main file (it runs
        ``wal_checkpoint(TRUNCATE)`` through a real connection) BEFORE copying. The
        data-loss regression this guards is a refactor that copies the main file
        without going through SQLite's checkpoint at all (a "just copy the file"
        simplification) — that orphans WAL-resident commits: green suite, lost
        data. Force a populated ``-wal`` at copy time and assert the row survives.
        (Note: dropping only the explicit pragma is masked by SQLite's last-close
        checkpoint, so the meaningful guard is against bypassing the connection.)
        """
        legacy = _make_legacy_install(tmp_path)
        legacy_db = legacy / DB_FILENAME

        raw = sqlite3.connect(str(legacy_db), isolation_level=None)
        try:
            raw.execute("PRAGMA journal_mode=WAL")
            raw.execute("CREATE TABLE wal_probe (id INTEGER PRIMARY KEY, marker TEXT)")
            # Fold the (empty) table into the main file so only the ROW is stranded.
            raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            raw.execute("PRAGMA wal_autocheckpoint=0")
            raw.execute("INSERT INTO wal_probe (marker) VALUES ('in-wal-only')")
            raw.commit()  # committed — but only into the -wal, not the main file
            # Snapshot all three files WHILE the row is still only in the -wal.
            snapshot = {suffix: (legacy / (DB_FILENAME + suffix)).read_bytes() for suffix in ("", "-wal", "-shm")}
        finally:
            raw.close()  # close may checkpoint; we restore the stranded-WAL state below
        assert len(snapshot["-wal"]) > 0  # the -wal genuinely carried committed frames
        # Restore: main WITHOUT the row, -wal HOLDING it, and no open connection
        # (so the migration's TRUNCATE checkpoint can complete and fold it forward).
        for suffix, data in snapshot.items():
            (legacy / (DB_FILENAME + suffix)).write_bytes(data)

        store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True

        conn = sqlite3.connect(str(store / DB_FILENAME))
        try:
            markers = [r[0] for r in conn.execute("SELECT marker FROM wal_probe").fetchall()]
        finally:
            conn.close()
        assert markers == ["in-wal-only"]  # WAL-resident commit folded forward, not orphaned

    def test_held_writer_aborts_migration_leaving_legacy_intact(self, tmp_path: Path) -> None:
        """A live writer holding the legacy DB blocks the TRUNCATE checkpoint, so
        the migration must abort with ``StoreMigrationBusyError`` BEFORE mutating
        anything — the legacy store and conf stay canonical and a later unblocked
        run still succeeds with data intact.
        """
        legacy = _make_legacy_install(tmp_path)
        db = FiligreeDB.from_filigree_dir(legacy)
        try:
            created = db.create_issue("seed before busy", type="task")
        finally:
            db.close()

        blocker = sqlite3.connect(str(legacy / DB_FILENAME), isolation_level=None)
        try:
            blocker.execute("PRAGMA journal_mode=WAL")
            blocker.execute("BEGIN IMMEDIATE")  # hold the write lock
            with pytest.raises(StoreMigrationBusyError):
                migrate_store_to_weft(tmp_path)
        finally:
            blocker.rollback()
            blocker.close()

        # Aborted before any mutation: legacy DB present, conf untouched, and no
        # weft store left behind at all — the eager mkdir is deferred until after
        # the busy check passes, so a busy abort never litters an empty
        # .weft/filigree/ husk that a confless open could mistake for canonical.
        assert (legacy / DB_FILENAME).is_file()
        assert read_conf(tmp_path / CONF_FILENAME)["db"] == f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"
        assert not _weft_store(tmp_path).exists()
        assert not (legacy / LEGACY_MOVED_BREADCRUMB).exists()  # no breadcrumb on abort

        # With the writer gone, a re-run completes and the seeded data survives.
        _store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        db2 = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            assert db2.get_issue(created.id) is not None
        finally:
            db2.close()

    def test_confless_busy_abort_does_not_orphan_legacy_data(self, tmp_path: Path) -> None:
        """Data-loss regression (confless installs).

        The conf-install variant above is safe because the conf still pins the
        legacy DB, so discovery opens the right database regardless of any empty
        weft dir. A *confless* install has no conf: discovery resolves purely on
        store layout. A busy-aborted migration pre-creates an empty
        ``.weft/filigree/`` (the eager ``mkdir`` runs before the abortable
        checkpoint), and an empty weft dir must not win over a legacy dir that
        holds the DB — otherwise the next confless open stamps a fresh empty DB
        there and orphans the real data in legacy. Reachable on the deploy
        recipe: a live daemon holds the legacy DB while ``filigree init`` runs.
        """
        # Confless legacy install: .filigree/ + DB + config.json, NO .filigree.conf.
        legacy = tmp_path / FILIGREE_DIR_NAME
        legacy.mkdir()
        write_config(legacy, {"prefix": "proj", "version": 1, "enabled_packs": ["core"]})
        db = FiligreeDB(legacy / DB_FILENAME, prefix="proj")
        db.initialize()
        try:
            created = db.create_issue("seed before busy", type="task")
        finally:
            db.close()
        assert not (tmp_path / CONF_FILENAME).exists()  # genuinely confless

        blocker = sqlite3.connect(str(legacy / DB_FILENAME), isolation_level=None)
        try:
            blocker.execute("PRAGMA journal_mode=WAL")
            blocker.execute("BEGIN IMMEDIATE")  # hold the write lock → checkpoint busy
            with pytest.raises(StoreMigrationBusyError):
                migrate_store_to_weft(tmp_path)
        finally:
            blocker.rollback()
            blocker.close()

        # Reopen through the real confless entry point (what a CLI / daemon uses).
        # The seeded issue MUST still be retrievable — not orphaned behind a
        # freshly-stamped empty weft DB.
        reopened = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            assert reopened.get_issue(created.id) is not None
        finally:
            reopened.close()

    def test_corrupt_conf_refuses_migration_before_any_mutation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A present-but-unreadable ``.filigree.conf`` must REFUSE before any
        filesystem mutation, not crash mid-migration with a half-published weft
        husk (filigree-obs-85b37a7cdc).

        Steps 1-2 of the migration publish the weft DB + copy metadata, and step
        3 rewrites the conf's ``db`` field (preserving ``prefix`` / registry
        settings) — a corrupt conf can be neither rewritten nor trusted to reveal
        a relocated custom layout, so we mirror the strict ``weft.toml`` read (I1)
        and refuse up front. The legacy store and conf are left byte-identical; a
        re-run converges once the conf is readable.
        """
        # The conf gate runs AFTER the daemon-liveness probe, whose deterministic
        # per-path port can false-refuse with StoreMigrationBusyError when an
        # unrelated daemon happens to hold that port (obs-fcbeb1718d). This test
        # does not intend a live daemon, so no-op the probe to keep the assertion
        # on StoreMigrationConfUnreadableError deterministic.
        monkeypatch.setattr("filigree.core._refuse_if_daemon_serving", lambda _root: None)
        legacy = _make_legacy_install(tmp_path)
        db = FiligreeDB.from_filigree_dir(legacy)
        try:
            created = db.create_issue("seed before corrupt conf", type="task")
        finally:
            db.close()

        conf_path = tmp_path / CONF_FILENAME
        valid_conf_bytes = conf_path.read_bytes()  # kept to restore for the re-run
        corrupt_conf_bytes = b"{ this is not valid json"
        conf_path.write_bytes(corrupt_conf_bytes)  # corrupt it

        with pytest.raises(StoreMigrationConfUnreadableError):
            migrate_store_to_weft(tmp_path)

        # Refused BEFORE mutation: no weft store, no breadcrumb, legacy DB present,
        # and the migration left the conf byte-identical to what it found (it never
        # half-wrote a rewrite — the corrupt bytes are exactly as we left them).
        assert not _weft_store(tmp_path).exists()
        assert not (legacy / LEGACY_MOVED_BREADCRUMB).exists()
        assert (legacy / DB_FILENAME).is_file()
        assert conf_path.read_bytes() == corrupt_conf_bytes

        # Fix the conf → a re-run converges and the seeded data survives intact.
        conf_path.write_bytes(valid_conf_bytes)
        store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        assert store == _weft_store(tmp_path)
        db2 = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            assert db2.get_issue(created.id) is not None
        finally:
            db2.close()

    def test_completed_migration_with_corrupt_conf_is_noop_not_refusal(self, tmp_path: Path) -> None:
        """Regression: the conf-readability gate sits AFTER the idempotency no-op
        checks, not at the top.

        A migration that already completed (legacy DB carried forward + removed)
        has nothing left to mutate, so a conf that gets corrupted *afterwards*
        must still no-op — the confless-completion short-circuit (conf_db is None,
        weft present, legacy DB gone) fires before the gate. Refusing here would
        block an unrelated re-run on a problem the caller's own post-migrate guard
        already reports.
        """
        _make_legacy_install(tmp_path)
        store, migrated = migrate_store_to_weft(tmp_path)  # complete it
        assert migrated is True

        # Corrupt the conf AFTER completion — legacy DB is already gone.
        (tmp_path / CONF_FILENAME).write_text("{ corrupt after the fact")
        assert not (tmp_path / FILIGREE_DIR_NAME / DB_FILENAME).exists()

        store2, migrated2 = migrate_store_to_weft(tmp_path)  # must NOT raise
        assert migrated2 is False
        assert store2 == store


class TestDeepStoreDirOverrideProjectRoot:
    """I2: project_root must be correct for an arbitrary-depth store_dir override.

    Reverse-deriving the project root from the store dir by stripping a fixed
    number of segments (the deleted ``store_dir_to_project_root`` / ``.parent``)
    is wrong for a multi-segment override; the canonical anchor walk (which reads
    weft.toml) recovers it. The recovered root feeds safe_path containment, the
    scanner path boundary, and confless DB opens — getting it wrong was a
    cross-surface split-brain.
    """

    def _make_deep_override_install(self, root: Path, rel: str = "data/store/fg") -> Path:
        (root / "weft.toml").write_text(f'[filigree]\nstore_dir = "{rel}"\n')
        store = root / Path(rel)
        store.mkdir(parents=True)
        write_config(store, {"prefix": "p", "version": 1})
        write_conf(
            root / CONF_FILENAME,
            {"version": 1, "project_name": "p", "prefix": "p", "db": f"{rel}/{DB_FILENAME}"},
        )
        return store

    def test_anchor_from_root_recovers_deep_override(self, tmp_path: Path) -> None:
        store = self._make_deep_override_install(tmp_path)
        anchor = find_filigree_anchor(tmp_path)
        assert anchor.store_dir == store
        assert anchor.project_root == tmp_path

    def test_anchor_from_store_dir_recovers_project_root(self, tmp_path: Path) -> None:
        # The recovery the dashboard registry / CLI / MCP confless fallback use:
        # walking up from the resolved store dir must land on the project root,
        # NOT on the store dir's parent (data/store).
        store = self._make_deep_override_install(tmp_path)
        anchor = find_filigree_anchor(store)
        assert anchor.project_root == tmp_path
        assert store.parent != tmp_path  # the reverse-derivation would have been wrong

    def test_from_anchor_sets_correct_project_root_for_deep_override(self, tmp_path: Path) -> None:
        store = self._make_deep_override_install(tmp_path)
        db = FiligreeDB.from_anchor(find_filigree_anchor(tmp_path))
        try:
            # project_root is the real root, not store.parent — so safe_path()
            # containment and scanner-relative paths resolve against the right tree.
            assert db.project_root == tmp_path
            assert db.meta_dir == store
        finally:
            db.close()


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


class TestMigrationDaemonQuiesce:
    """Detect-and-refuse: a live filigree daemon holding the legacy DB open must
    block migration before any mutation (filigree-031f9a413f). The daemon can be
    idle through the copy->unlink window and then commit to the orphaned inode, so
    detection is registry-based (server.json + deterministic port), not lock-based.
    """

    def test_live_daemon_refuses_before_any_mutation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_legacy_install(tmp_path)
        monkeypatch.setattr("filigree.core._live_filigree_daemon_for_project", lambda _root: 8749)
        with pytest.raises(StoreMigrationBusyError, match="port 8749"):
            migrate_store_to_weft(tmp_path)
        # No mutation: no weft husk, legacy DB + conf left intact.
        assert not _weft_store(tmp_path).exists()
        assert (tmp_path / FILIGREE_DIR_NAME / DB_FILENAME).is_file()
        assert read_conf(tmp_path / CONF_FILENAME)["db"] == f"{FILIGREE_DIR_NAME}/{DB_FILENAME}"

    def test_no_daemon_proceeds_normally(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_legacy_install(tmp_path)
        monkeypatch.setattr("filigree.core._live_filigree_daemon_for_project", lambda _root: None)
        store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        assert (store / DB_FILENAME).is_file()

    def test_detection_failure_never_blocks_migration(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _make_legacy_install(tmp_path)

        def _boom(_root: Path) -> int | None:
            raise RuntimeError("registry exploded")

        monkeypatch.setattr("filigree.core._live_filigree_daemon_for_project", _boom)
        # Best-effort: a detection bug must not crash migration.
        store, migrated = migrate_store_to_weft(tmp_path)
        assert migrated is True
        assert (store / DB_FILENAME).is_file()

    def test_server_registry_match_for_this_project_returns_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree import server
        from filigree.core import _live_filigree_daemon_for_project

        _make_legacy_install(tmp_path)
        # Ephemeral probe out of the way — exercise only the server-registry tier.
        monkeypatch.setattr("filigree.core._ephemeral_dashboard_port_if_live", lambda _root: None)
        monkeypatch.setattr(server, "daemon_status", lambda: server.DaemonStatus(running=True, pid=4321, port=8749, project_count=1))
        monkeypatch.setattr(
            server,
            "read_server_config",
            lambda: server.ServerConfig(port=8749, projects={str(tmp_path / FILIGREE_DIR_NAME): {"prefix": "proj"}}),
        )
        assert _live_filigree_daemon_for_project(tmp_path) == 8749

    def test_server_registry_no_match_for_unrelated_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree import server
        from filigree.core import _live_filigree_daemon_for_project

        _make_legacy_install(tmp_path)
        monkeypatch.setattr("filigree.core._ephemeral_dashboard_port_if_live", lambda _root: None)
        monkeypatch.setattr(server, "daemon_status", lambda: server.DaemonStatus(running=True, pid=4321, port=8749, project_count=1))
        monkeypatch.setattr(
            server,
            "read_server_config",
            lambda: server.ServerConfig(port=8749, projects={"/some/other/project/.filigree": {"prefix": "other"}}),
        )
        # No registry match and ephemeral probe stubbed to None → no live daemon.
        assert _live_filigree_daemon_for_project(tmp_path) is None

    def test_daemon_not_running_skips_registry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from filigree import server
        from filigree.core import _live_filigree_daemon_for_project

        monkeypatch.setattr("filigree.core._ephemeral_dashboard_port_if_live", lambda _root: None)
        monkeypatch.setattr(server, "daemon_status", lambda: server.DaemonStatus(running=False))
        assert _live_filigree_daemon_for_project(tmp_path) is None
