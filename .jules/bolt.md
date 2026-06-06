## 2024-06-06 - N+1 Issue Query Optimization

**Learning:** `_build_issues_batch` in `db_issues.py` performs 6 separate batched queries to resolve an issue's labels, blocks, blocked_by, children, and open_blockers_by_id, but the open_blockers_by_id is just `len(blocked_by)`. We can remove the 6th batched query entirely to reduce round trips and DB load.

**Action:** Remove the `open_blockers_by_id` query from `_build_issues_batch` and replace it with `len(blocked_by_id.get(iid, []))` when constructing the Issue object.
