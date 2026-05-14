"""Bundled scanner prompt packs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScannerPromptPack:
    name: str
    description: str
    instructions: str
    components: tuple[str, ...] = ()
    when_to_use: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "components": list(self.components),
            "when_to_use": self.when_to_use or self.description,
        }


PROMPT_PACKS: dict[str, ScannerPromptPack] = {
    "bug-hunt": ScannerPromptPack(
        name="bug-hunt",
        description="General concrete bug audit",
        instructions="Use the default Filigree bug-hunt criteria. Prioritize concrete defects with clear evidence and an actionable fix.",
    ),
    "security": ScannerPromptPack(
        name="security",
        description="Security-focused review",
        instructions=(
            "Focus on authentication, authorization, injection, path traversal, unsafe deserialization, SSRF, "
            "secret handling, crypto misuse, privilege boundaries, and user-controlled data reaching sensitive sinks."
        ),
    ),
    "pytorch": ScannerPromptPack(
        name="pytorch",
        description="PyTorch and ML-training review",
        instructions=(
            "Focus on tensor shapes, dtype/device drift, autograd breaks, train/eval mode mistakes, gradient leakage, "
            "loss scaling, optimizer/scheduler misuse, dataloader edge cases, memory pressure, and reproducibility hazards."
        ),
    ),
    "quality-engineering": ScannerPromptPack(
        name="quality-engineering",
        description="Quality engineering and testability review",
        instructions=(
            "Focus on missing regression boundaries, flaky timing, weak assertions, fixture leakage, CI-only failure modes, "
            "unobservable errors, brittle mocks, and behaviours that need mechanical verification."
        ),
    ),
    "solution-architecture": ScannerPromptPack(
        name="solution-architecture",
        description="Solution architecture review",
        instructions=(
            "Focus on ownership boundaries, coupling, lifecycle mismatches, integration contracts, data flow, migration risk, "
            "operability, and whether the local change preserves the larger system shape."
        ),
    ),
    "systems-thinking": ScannerPromptPack(
        name="systems-thinking",
        description="Systems-thinking review for cross-component and emergent failure modes",
        instructions=(
            "Focus on stocks and flows, feedback loops, delayed effects, incentives, leverage points, resource contention, "
            "control surfaces, and system behaviours that emerge over time rather than from one interface boundary."
        ),
        when_to_use="Use when the risk is an emergent system behaviour, feedback loop, or delayed operational failure.",
    ),
    "system-interactions": ScannerPromptPack(
        name="system-interactions",
        description="Cross-component interaction review",
        instructions=(
            "Focus on cross-component interface boundaries, integration contract drift, ordering assumptions, retries, "
            "idempotency, handoff state, protocol mismatches, and failures caused by two components interpreting the same "
            "contract differently."
        ),
        when_to_use="Use when the target depends on another component, API, queue, protocol, schema, or lifecycle boundary.",
    ),
    "python-engineering": ScannerPromptPack(
        name="python-engineering",
        description="Python engineering review",
        instructions=(
            "Focus on async correctness, context managers, exception boundaries, import/package behaviour, typing/runtime drift, "
            "mutable defaults, iterator exhaustion, subprocess handling, and filesystem portability."
        ),
    ),
    "css": ScannerPromptPack(
        name="css",
        description="CSS and visual styling review",
        instructions=(
            "Focus on cascade and specificity bugs, responsive layout breaks, overflow, stacking context, containment, "
            "theme-token drift, accessibility-affecting visual states, browser compatibility, and style rules that make UI text "
            "overlap or disappear."
        ),
    ),
    "javascript": ScannerPromptPack(
        name="javascript",
        description="JavaScript runtime review",
        instructions=(
            "Focus on event lifecycle bugs, stale closures, async race conditions, promise rejection handling, mutation side effects, "
            "DOM state drift, browser API misuse, serialization boundaries, and user-input edge cases."
        ),
    ),
    "typescript": ScannerPromptPack(
        name="typescript",
        description="TypeScript contract review",
        instructions=(
            "Focus on type erasure gaps, unsafe narrowing, any/unknown leaks, discriminated union exhaustiveness, generated-type drift, "
            "runtime validation mismatches, generic variance surprises, and API contracts that compile but can fail at runtime."
        ),
    ),
    "rust": ScannerPromptPack(
        name="rust",
        description="Rust systems engineering review",
        instructions=(
            "Focus on ownership and borrowing mistakes, lifetime assumptions, Send/Sync boundaries, unsafe blocks, error handling, "
            "panic paths, integer overflow, async cancellation, and FFI or serialization contracts."
        ),
    ),
    "go": ScannerPromptPack(
        name="go",
        description="Go concurrency and service review",
        instructions=(
            "Focus on goroutine leaks, context cancellation, channel ownership, data races, nil handling, error wrapping, "
            "defer ordering, resource cleanup, interface contracts, and HTTP/server lifecycle edge cases."
        ),
    ),
    "react": ScannerPromptPack(
        name="react",
        description="React UI state review",
        instructions=(
            "Focus on hook dependency drift, stale closures, render loops, controlled/uncontrolled state mismatches, hydration, "
            "event ordering, accessibility state, data fetching races, and UI states that can desynchronize from backend truth."
        ),
    ),
    "terraform": ScannerPromptPack(
        name="terraform",
        description="Terraform infrastructure review",
        instructions=(
            "Focus on state drift, lifecycle and replacement hazards, provider versioning, implicit dependencies, secrets in state, "
            "workspace/account mixups, module interface contracts, and destructive plan surprises."
        ),
    ),
    "sql": ScannerPromptPack(
        name="sql",
        description="SQL data integrity review",
        instructions=(
            "Focus on transaction boundaries, isolation anomalies, missing constraints, migration safety, query performance, "
            "NULL semantics, idempotency, lock contention, injection surfaces, and application/database contract drift."
        ),
    ),
}

PROMPT_PACKS["major-refactor"] = ScannerPromptPack(
    name="major-refactor",
    description="Four-pack review for major refactors",
    instructions="Apply the component prompt packs together and report only concrete defects or high-risk integration failures.",
    components=("solution-architecture", "systems-thinking", "python-engineering", "quality-engineering"),
    when_to_use="Use for risky refactors where architecture, emergent behaviour, Python mechanics, and verification all matter.",
)

PROMPT_PACKS["comprehensive"] = ScannerPromptPack(
    name="comprehensive",
    description="Broad multi-lens review",
    instructions="Apply the component prompt packs together and report only concrete defects or high-risk integration failures.",
    components=("security", "solution-architecture", "system-interactions", "python-engineering", "quality-engineering"),
    when_to_use="Use when you want a broad pass and are willing to pay for more review breadth than a targeted lens.",
)


def get_prompt_pack(name: str) -> ScannerPromptPack | None:
    return PROMPT_PACKS.get(name)


def list_prompt_packs() -> list[ScannerPromptPack]:
    return [PROMPT_PACKS[name] for name in sorted(PROMPT_PACKS)]


def expand_prompt_pack_names(name: str) -> list[str]:
    pack = get_prompt_pack(name)
    if pack is None:
        msg = f"Unknown prompt pack {name!r}. Available: {', '.join(sorted(PROMPT_PACKS))}"
        raise ValueError(msg)
    if pack.components:
        return list(pack.components)
    return [pack.name]
