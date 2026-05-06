"""Tests for list_labels and get_label_taxonomy."""

from __future__ import annotations

from filigree.core import FiligreeDB


class TestListLabels:
    def test_empty_project_includes_virtual_namespaces(self, db: FiligreeDB) -> None:
        result = db.list_labels()
        assert "age" in result["namespaces"]
        assert "has" in result["namespaces"]
        assert result["namespaces"]["age"]["type"] == "virtual"
        assert result["namespaces"]["age"]["writable"] is False

    def test_returns_manual_labels_grouped(self, db: FiligreeDB) -> None:
        db.create_issue("A", labels=["cluster:broad-except", "cluster:null-check"])
        db.create_issue("B", labels=["cluster:broad-except", "effort:m"])
        result = db.list_labels()
        cluster = result["namespaces"]["cluster"]
        assert cluster["type"] == "manual"
        assert cluster["writable"] is True
        labels = {entry["label"]: entry["count"] for entry in cluster["labels"]}
        assert labels["cluster:broad-except"] == 2
        assert labels["cluster:null-check"] == 1

    def test_sorted_alphabetically(self, db: FiligreeDB) -> None:
        db.create_issue("A", labels=["cluster:zebra", "cluster:alpha", "cluster:mid"])
        result = db.list_labels()
        cluster_labels = [entry["label"] for entry in result["namespaces"]["cluster"]["labels"]]
        assert cluster_labels == ["cluster:alpha", "cluster:mid", "cluster:zebra"]

    def test_top_n_limits_per_namespace(self, db: FiligreeDB) -> None:
        for i in range(15):
            db.create_issue(f"Issue {i}", labels=[f"cluster:type-{i:02d}"])
        result = db.list_labels(top=5)
        assert len(result["namespaces"]["cluster"]["labels"]) == 5

    def test_top_n_reports_truncation_metadata(self, db: FiligreeDB) -> None:
        for i in range(15):
            db.create_issue(f"Issue {i}", labels=[f"cluster:type-{i:02d}"])

        result = db.list_labels(top=5)
        cluster = result["namespaces"]["cluster"]

        assert cluster["total"] == 15
        assert cluster["truncated"] is True

    def test_top_zero_reports_untruncated_metadata(self, db: FiligreeDB) -> None:
        for i in range(15):
            db.create_issue(f"Issue {i}", labels=[f"cluster:type-{i:02d}"])

        result = db.list_labels(top=0)
        cluster = result["namespaces"]["cluster"]

        assert cluster["total"] == 15
        assert cluster["truncated"] is False

    def test_top_zero_returns_all(self, db: FiligreeDB) -> None:
        """top=0 means no truncation — all labels returned."""
        for i in range(15):
            db.create_issue(f"Issue {i}", labels=[f"cluster:type-{i:02d}"])
        result = db.list_labels(top=0)
        assert len(result["namespaces"]["cluster"]["labels"]) == 15

    def test_top_n_also_limits_virtual_namespaces(self, db: FiligreeDB) -> None:
        """filigree-b6abe0a2fb: --top is documented as 'max labels per
        namespace'. The age: virtual namespace has 5 buckets unconditionally;
        top=2 must truncate it like any other namespace.

        The has: virtual namespace is bounded by the predicates the workflow
        defines, but its count under top=2 must not exceed 2 either."""
        db.create_issue("Anchor", labels=["cluster:x"])
        result = db.list_labels(top=2)
        assert len(result["namespaces"]["age"]["labels"]) <= 2, (
            f"age: namespace must respect top=2; got {result['namespaces']['age']['labels']}"
        )
        assert len(result["namespaces"]["has"]["labels"]) <= 2, (
            f"has: namespace must respect top=2; got {result['namespaces']['has']['labels']}"
        )

    def test_top_zero_keeps_full_virtual_namespaces(self, db: FiligreeDB) -> None:
        """Reciprocal of the truncation fix: top=0 must NOT collapse age:/has:
        — the unlimited semantic must continue to return all virtual entries."""
        db.create_issue("Anchor", labels=["cluster:x"])
        result = db.list_labels(top=0)
        assert len(result["namespaces"]["age"]["labels"]) == 5

    def test_namespace_filter(self, db: FiligreeDB) -> None:
        db.create_issue("A", labels=["cluster:x", "effort:m"])
        result = db.list_labels(namespace="cluster")
        assert "cluster" in result["namespaces"]
        assert "effort" not in result["namespaces"]

    def test_bare_labels_grouped_under_bare_namespace(self, db: FiligreeDB) -> None:
        db.create_issue("A", labels=["tech-debt", "security"])
        result = db.list_labels()
        bare = result["namespaces"].get("_bare")
        assert bare is not None
        labels = {entry["label"] for entry in bare["labels"]}
        assert "tech-debt" in labels


class TestGetLabelTaxonomy:
    def test_returns_all_sections(self, db: FiligreeDB) -> None:
        result = db.get_label_taxonomy()
        assert "auto" in result
        assert "virtual" in result
        assert "manual_suggested" in result
        assert "bare_labels" in result

    def test_auto_namespaces_not_writable(self, db: FiligreeDB) -> None:
        result = db.get_label_taxonomy()
        for ns_data in result["auto"].values():
            assert ns_data["writable"] is False

    def test_manual_suggested_writable(self, db: FiligreeDB) -> None:
        result = db.get_label_taxonomy()
        for ns_data in result["manual_suggested"].values():
            assert ns_data["writable"] is True

    def test_review_namespace_lists_values(self, db: FiligreeDB) -> None:
        result = db.get_label_taxonomy()
        assert "review" in result["manual_suggested"]
        assert "needed" in result["manual_suggested"]["review"]["values"]

    def test_priority_text_labels_are_discouraged(self, db: FiligreeDB) -> None:
        result = db.get_label_taxonomy()
        discouraged = result["bare_labels"]["discouraged"]
        assert discouraged["pattern"] == "P[0-4]"
        assert "priority field" in discouraged["reason"]
