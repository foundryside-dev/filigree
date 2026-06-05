"""Classic-generation shape adapters.

Empty in Phase B. The classic generation returns internal shapes
directly today (ScanIngestResult, bare arrays for lists, the historical
``{updated, errors}`` container for batch mutations), so there is no
adaptation layer to register. Adapters land here if and only if Phase D
introduces internal-vocabulary renames that have to be reversed on the
classic wire (e.g. internal ``issue_id`` renamed from ``SlimIssue.id``
would require ``slim_issue_to_classic`` to map ``issue_id → id``).

See ``filigree.generations.weft.adapters`` for the exemplar pattern and
``docs/plans/2026-04-24-2.0-federation-work-package.md`` §D for the
Phase D rename work that will cause classic adapters to become non-empty.
"""
