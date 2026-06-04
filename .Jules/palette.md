## 2024-06-04 - Adding ARIA labels to dynamically rendered elements
**Learning:** Found several icon-only (`&times;`) close/remove buttons generated dynamically via template literals in `app.js`, `detail.js`, `files.js`, `graph.js` and `workflow.js` that were missing `aria-label`s. Screen readers wouldn't know what these buttons do.
**Action:** Always scan JavaScript view files that render HTML strings for interactive elements like `<button>` and `<a>` to ensure they have accessible names, especially icon-only buttons.
