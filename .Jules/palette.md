## 2026-06-06 - Interactive badges as spans
**Learning:** Found interactive badges (like `healthBadge` and `staleBadge`) built as `<span>` elements with `onclick` handlers, breaking keyboard navigation and screen reader semantics. There are also inputs and selects with no `aria-label` or `<label>` tag.
**Action:** Always check if an `onclick` is on a non-interactive element like a `div` or `span`. Convert them to `<button>` or add appropriate `role="button"` and `tabindex="0"`. Add missing `aria-label` attributes to form elements not directly tied to visible labels.
