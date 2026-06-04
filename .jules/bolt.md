## 2024-06-04 - Eliminate redundant batch issue queries
**Learning:** `_build_issues_batch` computed open_blockers counts doing a SQL query that identically mirrored the query used for `blocked_by_id`. The length of `blocked_by_id` arrays suffices to test if blockers equal 0.
**Action:** When constructing object graphs in batches using SQLite, avoid duplicate queries for "count > 0" and "get items"; the lengths of grouped result arrays can often be used directly to evaluate predicate lengths, eliminating N+1 and duplicate complex `GROUP BY`s entirely.
