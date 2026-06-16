# src/filigree/templates_data.py
"""Built-in workflow pack definitions.

This module contains the data definitions for all built-in packs.
Logic lives in templates.py; this file is pure data.

Each pack is a JSON-compatible dict matching the pack schema (design Section 4.2).
Types within packs match the type template schema (design Section 3.2).

Pack tiers (all complete):
  - Tier 1: core (4 types), planning (5 types)
  - Tier 2: risk (2 types), spike (2 types)
  - Tier 3: requirements (2 types), roadmap (3 types), incident (2 types), debt (2 types), release (2 types)
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Core Pack -- Foundational software development types
# ---------------------------------------------------------------------------

_CORE_PACK: dict[str, Any] = {
    "pack": "core",
    "version": "1.0",
    "display_name": "Core",
    "description": "Foundational software development types: tasks, bugs, features, and epics",
    "requires_packs": [],
    "types": {
        "task": {
            "type": "task",
            "display_name": "Task",
            "description": "General-purpose work item",
            "pack": "core",
            "states": [
                {"name": "open", "category": "open"},
                {"name": "in_progress", "category": "wip"},
                {"name": "closed", "category": "done"},
            ],
            "initial_state": "open",
            "transitions": [
                {"from": "open", "to": "in_progress", "enforcement": "soft"},
                {"from": "in_progress", "to": "closed", "enforcement": "soft"},
                {"from": "open", "to": "closed", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "context", "type": "text", "description": "Background context"},
                {"name": "done_definition", "type": "text", "description": "How to know this is complete"},
                {"name": "estimated_minutes", "type": "number", "description": "Rough time estimate"},
            ],
            "suggested_children": ["step"],
            "suggested_labels": ["chore", "cleanup", "setup"],
        },
        "bug": {
            "type": "bug",
            "display_name": "Bug Report",
            "description": "Defects, regressions, and unexpected behavior",
            "pack": "core",
            "states": [
                {"name": "triage", "category": "open"},
                {"name": "confirmed", "category": "open"},
                {"name": "fixing", "category": "wip"},
                {"name": "verifying", "category": "wip"},
                {"name": "closed", "category": "done"},
                {"name": "wont_fix", "category": "done"},
                {"name": "not_a_bug", "category": "done"},
            ],
            "initial_state": "triage",
            "transitions": [
                {"from": "triage", "to": "confirmed", "enforcement": "soft"},
                {"from": "triage", "to": "wont_fix", "enforcement": "soft"},
                {"from": "triage", "to": "not_a_bug", "enforcement": "soft"},
                {"from": "confirmed", "to": "fixing", "enforcement": "soft"},
                {"from": "confirmed", "to": "wont_fix", "enforcement": "soft"},
                {"from": "confirmed", "to": "not_a_bug", "enforcement": "soft"},
                {"from": "fixing", "to": "verifying", "enforcement": "soft", "requires_fields": ["fix_verification"]},
                {"from": "verifying", "to": "closed", "enforcement": "hard", "requires_fields": ["fix_verification"]},
                {"from": "verifying", "to": "fixing", "enforcement": "soft"},
            ],
            "fields_schema": [
                {
                    "name": "severity",
                    "type": "enum",
                    "options": ["critical", "major", "minor", "cosmetic"],
                    "default": "major",
                    "description": "Impact severity",
                    "required_at": ["confirmed"],
                },
                {"name": "component", "type": "text", "description": "Affected subsystem"},
                {"name": "steps_to_reproduce", "type": "text", "description": "Numbered steps to trigger the bug"},
                {
                    "name": "root_cause",
                    "type": "text",
                    "description": "Identified root cause",
                    "required_at": ["fixing"],
                },
                {
                    "name": "fix_verification",
                    "type": "text",
                    "description": "How to verify the fix works",
                    "required_at": ["verifying"],
                },
                {"name": "expected_behavior", "type": "text", "description": "What should happen"},
                {"name": "actual_behavior", "type": "text", "description": "What actually happens"},
                {"name": "environment", "type": "text", "description": "Python version, OS, relevant config"},
                {"name": "error_output", "type": "text", "description": "Stack trace or error message"},
            ],
            "suggested_children": ["task"],
            "suggested_labels": ["regression", "ux", "perf", "security"],
        },
        "feature": {
            "type": "feature",
            "display_name": "Feature",
            "description": "User-facing functionality",
            "pack": "core",
            "states": [
                {"name": "proposed", "category": "open"},
                {"name": "approved", "category": "open"},
                {"name": "building", "category": "wip"},
                {"name": "reviewing", "category": "wip"},
                {"name": "done", "category": "done"},
                {"name": "deferred", "category": "done"},
            ],
            "initial_state": "proposed",
            "transitions": [
                {"from": "proposed", "to": "approved", "enforcement": "soft"},
                {"from": "proposed", "to": "deferred", "enforcement": "soft"},
                {"from": "approved", "to": "building", "enforcement": "soft"},
                {"from": "approved", "to": "deferred", "enforcement": "soft"},
                {"from": "building", "to": "reviewing", "enforcement": "soft"},
                {"from": "building", "to": "deferred", "enforcement": "soft"},
                {"from": "reviewing", "to": "done", "enforcement": "soft"},
                {"from": "reviewing", "to": "building", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "user_story", "type": "text", "description": "As a [who], I want [what], so that [why]"},
                {
                    "name": "acceptance_criteria",
                    "type": "text",
                    "description": "Testable conditions for done",
                    "required_at": ["approved"],
                },
                {"name": "design_notes", "type": "text", "description": "Architecture / UX notes"},
                {"name": "test_strategy", "type": "text", "description": "How this will be tested"},
            ],
            "suggested_children": ["task", "bug"],
            "suggested_labels": ["mvp", "stretch", "v2"],
        },
        "epic": {
            "type": "epic",
            "display_name": "Epic",
            "description": "Large body of work spanning multiple features or tasks",
            "pack": "core",
            "container": True,
            "states": [
                {"name": "open", "category": "open"},
                {"name": "in_progress", "category": "wip"},
                {"name": "closed", "category": "done"},
            ],
            "initial_state": "open",
            "transitions": [
                {"from": "open", "to": "in_progress", "enforcement": "soft"},
                {"from": "in_progress", "to": "closed", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "scope", "type": "text", "description": "What is in and out of scope"},
                {"name": "success_metrics", "type": "text", "description": "How we measure success"},
            ],
            "suggested_children": ["feature", "task", "bug"],
            "suggested_labels": [],
        },
    },
    "relationships": [
        {
            "name": "epic_contains",
            "from_types": ["task", "bug", "feature"],
            "to_types": ["epic"],
            "mechanism": "parent_id",
            "description": "Work items belong to an epic",
        },
    ],
    "cross_pack_relationships": [],
    "guide": {
        "state_diagram": (
            "task:    open(O) --> in_progress(W) --> closed(D)\n"
            "                 \\-> closed(D)\n"
            "\n"
            "bug:     triage(O) --> confirmed(O) --> fixing(W) --> verifying(W) --> closed(D)\n"
            "                  \\-> wont_fix(D) \\-> wont_fix(D)  \\-> fixing(W)  [loop]\n"
            "         HARD: verifying-->closed requires fix_verification\n"
            "\n"
            "feature: proposed(O) --> approved(O) --> building(W) --> reviewing(W) --> done(D)\n"
            "                    \\-> deferred(D) \\-> deferred(D) \\-> deferred(D)\n"
            "                                                    \\-> building(W) [loop]\n"
            "\n"
            "epic:    open(O) --> in_progress(W) --> closed(D)"
        ),
        "overview": (
            "Core software development types for everyday work. "
            "Tasks for general work, bugs for defects, features for new functionality, and epics for large initiatives."
        ),
        "when_to_use": "Always enabled. Bread-and-butter types for any software project.",
        "tips": [
            "Use tasks for small, well-defined work items that one agent can complete in a session",
            "Bugs should always have steps_to_reproduce when possible -- without them, fixing is guesswork",
            "Features need acceptance_criteria before approval -- otherwise 'done' is ambiguous",
            "Use epics to group related features and tasks under a single objective",
            "Set severity on bugs during triage, not later -- it drives priority ordering",
        ],
        "common_mistakes": [
            "Skipping triage on bugs -- always assess severity first, even for obvious fixes",
            "Closing bugs without fix_verification -- the verifying->closed transition requires it for good reason",
            "Approving features without acceptance_criteria -- you need a definition of done before building",
            "Creating tasks when you mean steps -- tasks are standalone; steps belong to a phase in a plan",
        ],
    },
}

# ---------------------------------------------------------------------------
# Planning Pack -- PMBOK-lite project planning
# ---------------------------------------------------------------------------

_PLANNING_PACK: dict[str, Any] = {
    "pack": "planning",
    "version": "1.0",
    "display_name": "Planning",
    "description": "Hierarchical project planning: milestones, phases, steps, work packages, and deliverables",
    "requires_packs": ["core"],
    "types": {
        "milestone": {
            "type": "milestone",
            "display_name": "Milestone",
            "description": "Top-level delivery marker containing phases",
            "pack": "planning",
            "container": True,
            "states": [
                {"name": "planning", "category": "open"},
                {"name": "active", "category": "wip"},
                {"name": "closing", "category": "wip"},
                {"name": "completed", "category": "done"},
                {"name": "cancelled", "category": "done"},
            ],
            "initial_state": "planning",
            "transitions": [
                {"from": "planning", "to": "active", "enforcement": "soft"},
                {"from": "planning", "to": "cancelled", "enforcement": "soft"},
                {"from": "active", "to": "closing", "enforcement": "soft"},
                {"from": "active", "to": "cancelled", "enforcement": "soft"},
                {"from": "closing", "to": "completed", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "target_date", "type": "date", "description": "Target completion date"},
                {"name": "success_criteria", "type": "text", "description": "How we know this is achieved"},
                {"name": "deliverables", "type": "list", "description": "Concrete outputs"},
                {"name": "risks", "type": "text", "description": "Known risks"},
                {"name": "scope_summary", "type": "text", "description": "What is in and out of scope"},
            ],
            "suggested_children": ["phase"],
            "suggested_labels": [],
        },
        "phase": {
            "type": "phase",
            "display_name": "Phase",
            "description": "Logical grouping of steps within a milestone",
            "pack": "planning",
            "container": True,
            "states": [
                {"name": "pending", "category": "open"},
                {"name": "active", "category": "wip"},
                {"name": "completed", "category": "done"},
                {"name": "skipped", "category": "done"},
            ],
            "initial_state": "pending",
            "transitions": [
                {"from": "pending", "to": "active", "enforcement": "soft"},
                {"from": "pending", "to": "skipped", "enforcement": "soft"},
                {"from": "active", "to": "completed", "enforcement": "soft"},
                {"from": "active", "to": "skipped", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "sequence", "type": "number", "description": "Execution order within milestone"},
                {"name": "entry_criteria", "type": "text", "description": "What must be true before start"},
                {"name": "exit_criteria", "type": "text", "description": "What must be true for completion"},
                {"name": "estimated_effort", "type": "text", "description": "Rough effort estimate"},
            ],
            "suggested_children": ["step"],
            "suggested_labels": [],
        },
        "step": {
            "type": "step",
            "display_name": "Implementation Step",
            "description": "Atomic unit of work within a phase",
            "pack": "planning",
            "states": [
                {"name": "pending", "category": "open"},
                {"name": "in_progress", "category": "wip"},
                {"name": "completed", "category": "done"},
                {"name": "skipped", "category": "done"},
            ],
            "initial_state": "pending",
            "transitions": [
                {"from": "pending", "to": "in_progress", "enforcement": "soft"},
                {"from": "pending", "to": "skipped", "enforcement": "soft"},
                {"from": "in_progress", "to": "completed", "enforcement": "soft"},
                {"from": "in_progress", "to": "skipped", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "sequence", "type": "number", "description": "Execution order within phase"},
                {"name": "target_files", "type": "list", "description": "Files to create or modify"},
                {"name": "verification", "type": "text", "description": "How to verify completion"},
                {"name": "implementation_notes", "type": "text", "description": "Technical guidance"},
                {"name": "estimated_minutes", "type": "number", "description": "Rough time estimate"},
                {"name": "done_definition", "type": "text", "description": "Definition of done"},
            ],
            "suggested_children": [],
            "suggested_labels": [],
        },
        "work_package": {
            "type": "work_package",
            "display_name": "Work Package",
            "description": "Bundled unit of assignable work within a project",
            "pack": "planning",
            "states": [
                {"name": "defined", "category": "open"},
                {"name": "assigned", "category": "open"},
                {"name": "executing", "category": "wip"},
                {"name": "delivered", "category": "done"},
                {"name": "cancelled", "category": "done"},
            ],
            "initial_state": "defined",
            "transitions": [
                {"from": "defined", "to": "assigned", "enforcement": "soft"},
                {"from": "defined", "to": "cancelled", "enforcement": "soft"},
                {"from": "assigned", "to": "executing", "enforcement": "soft"},
                {"from": "assigned", "to": "cancelled", "enforcement": "soft"},
                {"from": "executing", "to": "delivered", "enforcement": "soft"},
                {"from": "executing", "to": "cancelled", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "effort_estimate", "type": "text", "description": "Estimated effort"},
                {"name": "assigned_team", "type": "text", "description": "Team or person responsible"},
                {"name": "acceptance_criteria", "type": "text", "description": "Conditions for delivery"},
            ],
            "suggested_children": ["task"],
            "suggested_labels": [],
        },
        "deliverable": {
            "type": "deliverable",
            "display_name": "Deliverable",
            "description": "Concrete output produced by a work package or phase",
            "pack": "planning",
            "states": [
                {"name": "planned", "category": "open"},
                {"name": "producing", "category": "wip"},
                {"name": "reviewing", "category": "wip"},
                {"name": "accepted", "category": "done"},
                {"name": "rejected", "category": "done"},
            ],
            "initial_state": "planned",
            "transitions": [
                {"from": "planned", "to": "producing", "enforcement": "soft"},
                {"from": "producing", "to": "reviewing", "enforcement": "soft"},
                {"from": "reviewing", "to": "accepted", "enforcement": "soft"},
                {"from": "reviewing", "to": "producing", "enforcement": "soft"},
                {"from": "reviewing", "to": "rejected", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "format", "type": "text", "description": "Expected format (document, code, artifact, etc.)"},
                {"name": "audience", "type": "text", "description": "Who receives this deliverable"},
                {"name": "quality_criteria", "type": "text", "description": "Quality standards to meet"},
            ],
            "suggested_children": [],
            "suggested_labels": [],
        },
    },
    "relationships": [
        {
            "name": "milestone_contains_phase",
            "from_types": ["phase"],
            "to_types": ["milestone"],
            "mechanism": "parent_id",
            "description": "Phases belong to milestones",
        },
        {
            "name": "phase_contains_step",
            "from_types": ["step"],
            "to_types": ["phase"],
            "mechanism": "parent_id",
            "description": "Steps belong to phases",
        },
        {
            "name": "work_package_in_milestone",
            "from_types": ["work_package"],
            "to_types": ["milestone"],
            "mechanism": "parent_id",
            "description": "Work packages belong to milestones",
        },
        {
            "name": "deliverable_for_package",
            "from_types": ["deliverable"],
            "to_types": ["work_package"],
            "mechanism": "dependency",
            "description": "Deliverables are produced by work packages",
        },
    ],
    "cross_pack_relationships": [],
    "guide": {
        "state_diagram": (
            "milestone: planning(O) --> active(W) --> closing(W) --> completed(D)\n"
            "                      \\-> cancelled(D) \\-> cancelled(D)\n"
            "\n"
            "phase:     pending(O) --> active(W) --> completed(D)\n"
            "                     \\-> skipped(D) \\-> skipped(D)\n"
            "\n"
            "step:      pending(O) --> in_progress(W) --> completed(D)\n"
            "                     \\-> skipped(D)       \\-> skipped(D)\n"
            "\n"
            "work_package: defined(O) --> assigned(O) --> executing(W) --> delivered(D)\n"
            "                        \\-> cancelled(D)  \\-> cancelled(D)  \\-> cancelled(D)\n"
            "\n"
            "deliverable:  planned(O) --> producing(W) --> reviewing(W) --> accepted(D)\n"
            "                                           \\-> producing(W) [rework loop]\n"
            "                                           \\-> rejected(D)"
        ),
        "overview": (
            "PMBOK-lite project planning. Structure work as milestones containing phases containing steps. "
            "Work packages for assignable bundles, deliverables for concrete outputs."
        ),
        "when_to_use": "Structured multi-phase projects needing clear hierarchy and progress tracking.",
        "tips": [
            "Start with a milestone, then break into phases, then into steps -- top-down decomposition",
            "Use phase dependencies to enforce ordering -- phase 2 should depend on phase 1",
            "Steps should be small enough to complete in a single session -- if it takes multiple days, it is a phase",
            "Work packages are useful for delegating to different agents or teams with clear acceptance criteria",
            "Set sequence fields on phases and steps to maintain intended execution order",
            "Use deliverables to track concrete outputs -- code, documents, test reports, artifacts",
        ],
        "common_mistakes": [
            "Creating steps without phases -- you lose the grouping benefit and cannot track phase-level progress",
            "Skipping entry/exit criteria on phases -- without them, you have no clear transition points",
            "Making steps too large -- each step should be an atomic, completable unit of work",
            "Forgetting to set sequence numbers -- without ordering, agents will not know which step comes next",
        ],
    },
}

# ---------------------------------------------------------------------------
# Extended packs
# ---------------------------------------------------------------------------

_REQUIREMENTS_PACK: dict[str, Any] = {
    "pack": "requirements",
    "version": "1.0",
    "display_name": "Requirements",
    "description": "Requirements lifecycle: draft, review, approve, implement, verify",
    "requires_packs": ["core"],
    "types": {
        "requirement": {
            "type": "requirement",
            "display_name": "Requirement",
            "description": "A functional or non-functional requirement to be reviewed, approved, and verified",
            "pack": "requirements",
            "states": [
                {"name": "drafted", "category": "open"},
                {"name": "reviewing", "category": "wip"},
                {"name": "approved", "category": "open"},
                {"name": "implementing", "category": "wip"},
                {"name": "verified", "category": "done"},
                {"name": "rejected", "category": "done"},
                {"name": "deferred", "category": "done"},
            ],
            "initial_state": "drafted",
            "transitions": [
                {"from": "drafted", "to": "reviewing", "enforcement": "soft"},
                {"from": "drafted", "to": "rejected", "enforcement": "soft"},
                {"from": "drafted", "to": "deferred", "enforcement": "soft"},
                {"from": "reviewing", "to": "approved", "enforcement": "soft"},
                {"from": "reviewing", "to": "rejected", "enforcement": "soft"},
                {"from": "reviewing", "to": "drafted", "enforcement": "soft"},
                {"from": "approved", "to": "implementing", "enforcement": "soft"},
                {"from": "approved", "to": "deferred", "enforcement": "soft"},
                {
                    "from": "implementing",
                    "to": "verified",
                    "enforcement": "hard",
                    "requires_fields": ["verification_method"],
                },
                {"from": "implementing", "to": "drafted", "enforcement": "soft"},
            ],
            "fields_schema": [
                {
                    "name": "req_type",
                    "type": "enum",
                    "options": ["functional", "non_functional", "constraint", "interface"],
                    "description": "Classification of requirement",
                },
                {"name": "stakeholder", "type": "text", "description": "Who needs this requirement"},
                {"name": "rationale", "type": "text", "description": "Why this requirement exists"},
                {
                    "name": "verification_method",
                    "type": "enum",
                    "options": ["test", "inspection", "analysis", "demonstration"],
                    "description": "How this requirement will be verified",
                    "required_at": ["verified"],
                },
                {"name": "acceptance_criteria", "type": "text", "description": "Conditions for acceptance"},
                {
                    "name": "priority_justification",
                    "type": "text",
                    "description": "Why this requirement has its current priority",
                },
            ],
            "suggested_children": ["acceptance_criterion", "task"],
            "suggested_labels": ["must_have", "should_have", "nice_to_have", "security", "performance"],
        },
        "acceptance_criterion": {
            "type": "acceptance_criterion",
            "display_name": "Acceptance Criterion",
            "description": "A testable condition that must be met to satisfy a requirement",
            "pack": "requirements",
            "states": [
                {"name": "draft", "category": "open"},
                {"name": "validated", "category": "done"},
            ],
            "initial_state": "draft",
            "transitions": [
                {"from": "draft", "to": "validated", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "given", "type": "text", "description": "Given (precondition)"},
                {"name": "when", "type": "text", "description": "When (action)"},
                {"name": "then", "type": "text", "description": "Then (expected outcome)"},
            ],
            "suggested_children": [],
            "suggested_labels": [],
        },
    },
    "relationships": [
        {
            "name": "criterion_for_requirement",
            "from_types": ["acceptance_criterion"],
            "to_types": ["requirement"],
            "mechanism": "parent_id",
            "description": "Acceptance criteria belong to a requirement",
        },
        {
            "name": "requirement_depends_on",
            "from_types": ["requirement"],
            "to_types": ["requirement"],
            "mechanism": "dependency",
            "description": "Requirement depends on another requirement",
        },
    ],
    "cross_pack_relationships": [
        {
            "name": "requirement_implemented_by",
            "from_types": ["task", "feature"],
            "to_types": ["requirement"],
            "mechanism": "dependency",
            "description": "Core work items implement requirements",
        },
    ],
    "guide": {
        "state_diagram": (
            "requirement: drafted(O) --> reviewing(W) --> approved(O) --> implementing(W) --> verified(D)\n"
            "                       \\-> rejected(D)   \\-> rejected(D)                    \\-> drafted(O) [rework]\n"
            "                       \\-> deferred(D)   \\-> drafted(O) [revision]  \\-> deferred(D)\n"
            "             HARD: implementing-->verified requires verification_method\n"
            "\n"
            "acceptance_criterion: draft(O) --> validated(D)"
        ),
        "overview": (
            "Requirements lifecycle management. Draft requirements, review with stakeholders, "
            "approve for implementation, and verify with documented methods."
        ),
        "when_to_use": "Projects needing traceable requirements from stakeholder need to verified delivery.",
        "states_explained": {
            "drafted": "Requirement has been written but not yet reviewed",
            "reviewing": "Requirement is under stakeholder review",
            "approved": "Requirement is accepted and ready for implementation",
            "implementing": "Work is underway to fulfill this requirement",
            "verified": "Requirement has been verified as met via documented method",
            "rejected": "Requirement was reviewed and deemed unnecessary or infeasible",
            "deferred": "Requirement is valid but postponed to a later phase",
            "draft": "Acceptance criterion captured but not yet validated",
            "validated": "Acceptance criterion has been confirmed as met",
        },
        "typical_flow": (
            "1. Create requirement in 'drafted' with stakeholder and rationale\n"
            "2. Move to 'reviewing' for stakeholder sign-off\n"
            "3. Transition to 'approved' once consensus is reached\n"
            "4. Link implementing tasks/features via dependencies\n"
            "5. Move to 'implementing' when work begins\n"
            "6. Verify with documented method (hard gate: verification_method required)"
        ),
        "tips": [
            "Write acceptance criteria in Given/When/Then format for testability",
            "Link requirements to implementing tasks via dependencies for traceability",
            "Use deferred instead of rejected for valid requirements that are just not now",
            "Set verification_method early so the team knows how to prove completion",
            "Group related requirements under a parent epic or feature for context",
        ],
        "common_mistakes": [
            "Skipping review -- unreviewed requirements lead to wasted implementation effort",
            "No acceptance criteria -- requirements without testable criteria cannot be verified",
            "Implementing before approval -- leads to rework when requirements change during review",
            "Missing verification method -- the hard gate exists because 'it works' is not verification",
            "Orphan requirements -- always link to implementing work items for traceability",
        ],
    },
}

_RISK_PACK: dict[str, Any] = {
    "pack": "risk",
    "version": "1.0",
    "display_name": "Risk Management",
    "description": "ISO 31000-lite: identify, assess, and manage project risks",
    "requires_packs": ["core"],
    "types": {
        "risk": {
            "type": "risk",
            "display_name": "Risk",
            "description": "A project risk to identify, assess, and manage",
            "pack": "risk",
            "states": [
                {"name": "identified", "category": "open"},
                {"name": "assessing", "category": "wip"},
                {"name": "assessed", "category": "open"},
                {"name": "mitigating", "category": "wip"},
                {"name": "mitigated", "category": "done"},
                {"name": "accepted", "category": "done"},
                {"name": "escalated", "category": "open"},
                {"name": "retired", "category": "done"},
            ],
            "initial_state": "identified",
            "transitions": [
                {"from": "identified", "to": "assessing", "enforcement": "soft"},
                {"from": "identified", "to": "retired", "enforcement": "soft"},
                {
                    "from": "assessing",
                    "to": "assessed",
                    "enforcement": "hard",
                    "requires_fields": ["risk_score", "impact"],
                },
                {"from": "assessed", "to": "mitigating", "enforcement": "soft"},
                {
                    "from": "assessed",
                    "to": "accepted",
                    "enforcement": "hard",
                    "requires_fields": ["risk_owner", "acceptance_rationale"],
                },
                {"from": "assessed", "to": "escalated", "enforcement": "soft"},
                {"from": "mitigating", "to": "mitigated", "enforcement": "soft"},
                {"from": "escalated", "to": "mitigating", "enforcement": "soft"},
                {
                    "from": "escalated",
                    "to": "accepted",
                    "enforcement": "hard",
                    "requires_fields": ["risk_owner", "acceptance_rationale"],
                },
                {"from": "escalated", "to": "retired", "enforcement": "soft"},
            ],
            "fields_schema": [
                {
                    "name": "risk_score",
                    "type": "number",
                    "description": "Numeric risk score (e.g., 1-25)",
                    "required_at": ["assessed"],
                },
                {
                    "name": "impact",
                    "type": "text",
                    "description": "Description of potential impact",
                    "required_at": ["assessed"],
                },
                {
                    "name": "likelihood",
                    "type": "enum",
                    "options": ["rare", "unlikely", "possible", "likely", "almost_certain"],
                    "description": "Probability of occurrence",
                },
                {
                    "name": "risk_owner",
                    "type": "text",
                    "description": "Person responsible for this risk",
                    "required_at": ["accepted"],
                },
                {
                    "name": "acceptance_rationale",
                    "type": "text",
                    "description": "Why this risk is accepted",
                    "required_at": ["accepted"],
                },
                {"name": "mitigation_strategy", "type": "text", "description": "Planned approach to reduce risk"},
                {"name": "residual_risk", "type": "text", "description": "Remaining risk after mitigation"},
            ],
            "suggested_children": ["mitigation"],
            "suggested_labels": ["technical", "schedule", "resource", "external"],
        },
        "mitigation": {
            "type": "mitigation",
            "display_name": "Mitigation",
            "description": "An action to reduce or eliminate a risk",
            "pack": "risk",
            "states": [
                {"name": "planned", "category": "open"},
                {"name": "in_progress", "category": "wip"},
                {"name": "completed", "category": "done"},
                {"name": "ineffective", "category": "open"},
                {"name": "cancelled", "category": "done"},
            ],
            "initial_state": "planned",
            "transitions": [
                {"from": "planned", "to": "in_progress", "enforcement": "soft"},
                {"from": "planned", "to": "cancelled", "enforcement": "soft"},
                {"from": "in_progress", "to": "completed", "enforcement": "soft"},
                {"from": "in_progress", "to": "ineffective", "enforcement": "soft"},
                {"from": "in_progress", "to": "cancelled", "enforcement": "soft"},
                {"from": "ineffective", "to": "planned", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "approach", "type": "text", "description": "How this mitigation will be executed"},
                {
                    "name": "outcome",
                    "type": "text",
                    "description": "Result of the mitigation",
                    "required_at": ["completed", "ineffective"],
                },
                {"name": "effort_estimate", "type": "text", "description": "Estimated effort to complete"},
                {"name": "target_date", "type": "date", "description": "Target completion date"},
            ],
            "suggested_children": [],
            "suggested_labels": [],
        },
    },
    "relationships": [
        {
            "name": "mitigation_for",
            "from_types": ["mitigation"],
            "to_types": ["risk"],
            "mechanism": "parent_id",
            "description": "Mitigations belong to a risk",
        },
        {
            "name": "risk_threatens",
            "from_types": ["risk"],
            "to_types": [],
            "mechanism": "dependency",
            "description": "Risk threatens other work items via dependency",
        },
    ],
    "cross_pack_relationships": [
        {
            "name": "spike_investigates_risk",
            "from_types": ["spike"],
            "to_types": ["risk"],
            "mechanism": "dependency",
            "description": "Spike investigates a risk",
        },
    ],
    "guide": {
        "state_diagram": (
            "risk:       identified(O) --> assessing(W) --> assessed(O) --> mitigating(W) --> mitigated(D)\n"
            "                          \\-> retired(D)                   \\-> accepted(D)\n"
            "                                                           \\-> escalated(O) --> mitigating(W)\n"
            "                                                                            \\-> accepted(D)\n"
            "                                                                            \\-> retired(D)\n"
            "            HARD: assessing-->assessed requires risk_score, impact\n"
            "            HARD: assessed-->accepted requires risk_owner, acceptance_rationale\n"
            "\n"
            "mitigation: planned(O) --> in_progress(W) --> completed(D)\n"
            "                      \\-> cancelled(D)    \\-> ineffective(O) --> planned(O) [replan]\n"
            "                                          \\-> cancelled(D)"
        ),
        "overview": (
            "ISO 31000-lite risk management. Identify risks, assess severity and likelihood, "
            "then mitigate or accept with documented rationale."
        ),
        "when_to_use": "Projects with uncertainty needing structured risk tracking.",
        "states_explained": {
            "identified": "Risk has been recognized but not yet analyzed",
            "assessing": "Risk is being analyzed for impact and likelihood",
            "assessed": "Risk has been scored and is awaiting a response decision",
            "mitigating": "Active work is underway to reduce the risk",
            "mitigated": "Risk has been successfully reduced to acceptable levels",
            "accepted": "Risk is acknowledged and accepted with documented rationale",
            "escalated": "Risk exceeds current authority and has been escalated",
            "retired": "Risk is no longer relevant (e.g., the threat has passed)",
            "planned": "Mitigation action has been defined but not started",
            "in_progress": "Mitigation action is actively being executed",
            "completed": "Mitigation action finished successfully",
            "ineffective": "Mitigation was tried but did not reduce the risk",
            "cancelled": "Mitigation was abandoned before or during execution",
        },
        "typical_flow": (
            "1. Create risk in 'identified' state\n"
            "2. Move to 'assessing' and fill in risk_score + impact\n"
            "3. Transition to 'assessed' (hard gate: risk_score + impact required)\n"
            "4. Decide: mitigate, accept, or escalate\n"
            "5. If mitigating, create mitigation children and track to completion\n"
            "6. If accepting, provide risk_owner + acceptance_rationale (hard gate)"
        ),
        "tips": [
            "Score risks consistently -- use a 1-5 impact x 1-5 likelihood matrix for risk_score",
            "Assign a risk_owner early, even before the acceptance decision -- someone needs to watch it",
            "Create mitigations as children of the risk so the hierarchy is clear",
            "Review escalated risks in each planning session -- they should not sit idle",
            "Use retired for risks that pass naturally (e.g., deadline-specific risks after the deadline)",
        ],
        "common_mistakes": [
            "Skipping assessment -- going straight from identified to mitigating loses the scoring data",
            "Accepting risks without rationale -- the hard gate exists because undocumented acceptance is invisible risk",
            "Creating mitigations without linking to a risk -- orphan mitigations have no context",
            "Never retiring old risks -- the risk list grows unbounded and becomes noise",
            "Treating ineffective mitigations as failures -- they are data; loop back to planned with a new approach",
        ],
    },
}

_ROADMAP_PACK: dict[str, Any] = {
    "pack": "roadmap",
    "version": "1.0",
    "display_name": "Roadmap",
    "description": "OKR-lite strategic planning: themes, objectives, and measurable key results",
    "requires_packs": ["core", "planning"],
    "types": {
        "theme": {
            "type": "theme",
            "display_name": "Theme",
            "description": "A strategic theme grouping related objectives",
            "pack": "roadmap",
            "states": [
                {"name": "proposed", "category": "open"},
                {"name": "active", "category": "wip"},
                {"name": "achieved", "category": "done"},
                {"name": "sunset", "category": "done"},
            ],
            "initial_state": "proposed",
            "transitions": [
                {"from": "proposed", "to": "active", "enforcement": "soft"},
                {"from": "proposed", "to": "sunset", "enforcement": "soft"},
                {"from": "active", "to": "achieved", "enforcement": "soft"},
                {"from": "active", "to": "sunset", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "time_horizon", "type": "text", "description": "Target timeframe (e.g., Q1 2026, H2 2026)"},
                {"name": "strategic_rationale", "type": "text", "description": "Why this theme matters strategically"},
            ],
            "suggested_children": ["objective"],
            "suggested_labels": ["growth", "stability", "innovation", "platform"],
        },
        "objective": {
            "type": "objective",
            "display_name": "Objective",
            "description": "A qualitative goal to be achieved, measured by key results",
            "pack": "roadmap",
            "states": [
                {"name": "defined", "category": "open"},
                {"name": "pursuing", "category": "wip"},
                {"name": "achieved", "category": "done"},
                {"name": "dropped", "category": "done"},
            ],
            "initial_state": "defined",
            "transitions": [
                {"from": "defined", "to": "pursuing", "enforcement": "soft"},
                {"from": "defined", "to": "dropped", "enforcement": "soft"},
                {"from": "pursuing", "to": "achieved", "enforcement": "soft"},
                {"from": "pursuing", "to": "dropped", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "owner", "type": "text", "description": "Person or team responsible"},
                {"name": "time_horizon", "type": "text", "description": "Target timeframe"},
                {"name": "success_criteria", "type": "text", "description": "How we know this objective is achieved"},
            ],
            "suggested_children": ["key_result"],
            "suggested_labels": [],
        },
        "key_result": {
            "type": "key_result",
            "display_name": "Key Result",
            "description": "A measurable outcome that indicates progress toward an objective",
            "pack": "roadmap",
            "states": [
                {"name": "defined", "category": "open"},
                {"name": "tracking", "category": "wip"},
                {"name": "met", "category": "done"},
                {"name": "missed", "category": "done"},
            ],
            "initial_state": "defined",
            "transitions": [
                {"from": "defined", "to": "tracking", "enforcement": "soft"},
                {
                    "from": "tracking",
                    "to": "met",
                    "enforcement": "hard",
                    "requires_fields": ["current_value"],
                },
                {
                    "from": "tracking",
                    "to": "missed",
                    "enforcement": "hard",
                    "requires_fields": ["current_value"],
                },
            ],
            "fields_schema": [
                {"name": "target_value", "type": "text", "description": "Target metric value to achieve"},
                {
                    "name": "current_value",
                    "type": "text",
                    "description": "Current metric value",
                    "required_at": ["met", "missed"],
                },
                {"name": "unit", "type": "text", "description": "Unit of measurement (e.g., %, count, ms)"},
                {"name": "baseline", "type": "text", "description": "Starting value when tracking began"},
            ],
            "suggested_children": [],
            "suggested_labels": [],
        },
    },
    "relationships": [
        {
            "name": "objective_under_theme",
            "from_types": ["objective"],
            "to_types": ["theme"],
            "mechanism": "parent_id",
            "description": "Objectives belong to a strategic theme",
        },
        {
            "name": "key_result_for_objective",
            "from_types": ["key_result"],
            "to_types": ["objective"],
            "mechanism": "parent_id",
            "description": "Key results measure an objective",
        },
    ],
    "cross_pack_relationships": [
        {
            "name": "milestone_delivers_objective",
            "from_types": ["milestone"],
            "to_types": ["objective"],
            "mechanism": "dependency",
            "description": "Planning milestones deliver roadmap objectives",
        },
        {
            "name": "epic_supports_theme",
            "from_types": ["epic"],
            "to_types": ["theme"],
            "mechanism": "dependency",
            "description": "Epics contribute to strategic themes",
        },
    ],
    "guide": {
        "state_diagram": (
            "theme:      proposed(O) --> active(W) --> achieved(D)\n"
            "                       \\-> sunset(D)  \\-> sunset(D)\n"
            "\n"
            "objective:  defined(O) --> pursuing(W) --> achieved(D)\n"
            "                      \\-> dropped(D)  \\-> dropped(D)\n"
            "\n"
            "key_result: defined(O) --> tracking(W) --> met(D)\n"
            "                                      \\-> missed(D)\n"
            "            HARD: tracking-->met/missed requires current_value"
        ),
        "overview": (
            "OKR-lite strategic planning. Group work under themes, define qualitative "
            "objectives, and track measurable key results to gauge progress."
        ),
        "when_to_use": "When you need to connect daily work to strategic goals.",
        "states_explained": {
            "proposed": "Theme has been suggested but not yet committed to",
            "active": "Theme is the current strategic focus",
            "achieved": "Theme or objective has been successfully completed",
            "sunset": "Theme was retired without full achievement",
            "defined": "Objective or key result has been articulated but work has not started",
            "pursuing": "Active work is underway toward this objective",
            "dropped": "Objective was abandoned as no longer relevant",
            "tracking": "Key result is being actively measured",
            "met": "Key result target value has been reached",
            "missed": "Key result was not achieved within the timeframe",
        },
        "typical_flow": (
            "1. Create theme with strategic_rationale and time_horizon\n"
            "2. Add objectives as children of the theme\n"
            "3. Add key results as children of each objective with target_value\n"
            "4. Move to tracking as work begins and update current_value periodically\n"
            "5. Close key results as met or missed (hard gate: current_value required)"
        ),
        "tips": [
            "Keep objectives qualitative and aspirational -- measurability belongs in key results",
            "Set 2-5 key results per objective -- fewer is better than more",
            "Update current_value regularly -- stale metrics defeat the purpose of tracking",
            "Link milestones to objectives so strategic progress is visible from planning",
            "Use sunset for themes that shift, not dropped -- dropped implies failure",
        ],
        "common_mistakes": [
            "Objectives that are actually tasks -- objectives should be outcomes, not activities",
            "Key results without target values -- unmeasurable results cannot be met or missed",
            "Too many objectives per theme -- focus is the point; 3-5 objectives is the sweet spot",
            "Never closing key results -- the hard gate requires current_value to force honest assessment",
            "Orphan key results -- always link to a parent objective for strategic context",
        ],
    },
}

_INCIDENT_PACK: dict[str, Any] = {
    "pack": "incident",
    "version": "1.0",
    "display_name": "Incident Management",
    "description": "ITIL-lite incident response with severity tracking and postmortems",
    "requires_packs": ["core"],
    "types": {
        "incident": {
            "type": "incident",
            "display_name": "Incident",
            "description": "A service disruption or degradation requiring urgent response",
            "pack": "incident",
            "states": [
                {"name": "reported", "category": "open"},
                {"name": "triaging", "category": "wip"},
                {"name": "investigating", "category": "wip"},
                {"name": "mitigating", "category": "wip"},
                {"name": "resolved", "category": "wip"},
                {"name": "closed", "category": "done"},
            ],
            "initial_state": "reported",
            "transitions": [
                {
                    "from": "reported",
                    "to": "triaging",
                    "enforcement": "hard",
                    "requires_fields": ["severity"],
                },
                {"from": "triaging", "to": "investigating", "enforcement": "soft"},
                {"from": "investigating", "to": "mitigating", "enforcement": "soft"},
                {"from": "mitigating", "to": "resolved", "enforcement": "soft"},
                {"from": "investigating", "to": "resolved", "enforcement": "soft"},
                {
                    "from": "resolved",
                    "to": "closed",
                    "enforcement": "hard",
                    "requires_fields": ["root_cause"],
                },
            ],
            "fields_schema": [
                {
                    "name": "severity",
                    "type": "enum",
                    "options": ["sev1", "sev2", "sev3", "sev4"],
                    "description": "Severity level (sev1=critical, sev4=minor)",
                    "required_at": ["triaging"],
                },
                {"name": "impact_scope", "type": "text", "description": "What users/systems are affected"},
                {
                    "name": "root_cause",
                    "type": "text",
                    "description": "Root cause of the incident",
                    "required_at": ["closed"],
                },
                {"name": "resolution", "type": "text", "description": "How the incident was resolved"},
                {"name": "detection_method", "type": "text", "description": "How the incident was detected"},
                {
                    "name": "timeline",
                    "type": "text",
                    "description": "Key timestamps: detected, acknowledged, mitigated, resolved",
                },
            ],
            "suggested_children": ["postmortem"],
            "suggested_labels": ["infrastructure", "application", "security", "data"],
        },
        "postmortem": {
            "type": "postmortem",
            "display_name": "Postmortem",
            "description": "A blameless retrospective analysis of an incident",
            "pack": "incident",
            "states": [
                {"name": "drafting", "category": "open"},
                {"name": "reviewing", "category": "wip"},
                {"name": "published", "category": "done"},
            ],
            "initial_state": "drafting",
            "transitions": [
                {"from": "drafting", "to": "reviewing", "enforcement": "soft"},
                {
                    "from": "reviewing",
                    "to": "published",
                    "enforcement": "hard",
                    "requires_fields": ["action_items"],
                },
                {"from": "reviewing", "to": "drafting", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "summary", "type": "text", "description": "Brief summary of what happened"},
                {
                    "name": "contributing_factors",
                    "type": "text",
                    "description": "What factors contributed to the incident",
                },
                {
                    "name": "action_items",
                    "type": "text",
                    "description": "Concrete follow-up actions to prevent recurrence",
                    "required_at": ["published"],
                },
                {"name": "lessons_learned", "type": "text", "description": "Key takeaways for the team"},
            ],
            "suggested_children": ["task"],
            "suggested_labels": [],
        },
    },
    "relationships": [
        {
            "name": "postmortem_for_incident",
            "from_types": ["postmortem"],
            "to_types": ["incident"],
            "mechanism": "parent_id",
            "description": "Postmortems belong to an incident",
        },
        {
            "name": "incident_related_to",
            "from_types": ["incident"],
            "to_types": ["incident"],
            "mechanism": "dependency",
            "description": "Related or cascading incidents",
        },
    ],
    "cross_pack_relationships": [
        {
            "name": "incident_caused_by_bug",
            "from_types": ["incident"],
            "to_types": ["bug"],
            "mechanism": "dependency",
            "description": "Incidents caused by known bugs",
        },
        {
            "name": "postmortem_spawns_tasks",
            "from_types": ["task"],
            "to_types": ["postmortem"],
            "mechanism": "dependency",
            "description": "Follow-up tasks from postmortem action items",
        },
    ],
    "guide": {
        "state_diagram": (
            "incident:   reported(O) --> triaging(W) --> investigating(W) --> mitigating(W) -->\n"
            "              resolved(W) --> closed(D)\n"
            "                                                             \\-> resolved(W)\n"
            "            HARD: reported-->triaging requires severity\n"
            "            HARD: resolved-->closed requires root_cause\n"
            "\n"
            "postmortem: drafting(O) --> reviewing(W) --> published(D)\n"
            "                                        \\-> drafting(O) [revision]\n"
            "            HARD: reviewing-->published requires action_items"
        ),
        "overview": (
            "ITIL-lite incident management. Report disruptions, triage by severity, "
            "investigate and mitigate, then close with root cause and postmortem."
        ),
        "when_to_use": "When services can fail and you need structured incident response.",
        "states_explained": {
            "reported": "Incident has been detected but not yet triaged",
            "triaging": "Severity is being assessed and responders assigned",
            "investigating": "Root cause investigation is underway",
            "mitigating": "A workaround or fix is being applied to restore service",
            "resolved": "Service is restored but incident is not yet formally closed",
            "closed": "Incident is complete with documented root cause",
            "drafting": "Postmortem is being written",
            "reviewing": "Postmortem is under team review",
            "published": "Postmortem is finalized with action items",
        },
        "typical_flow": (
            "1. Report incident and immediately set severity (hard gate)\n"
            "2. Triage: assign responders, assess impact_scope\n"
            "3. Investigate root cause while mitigating impact\n"
            "4. Resolve when service is restored\n"
            "5. Close with documented root_cause (hard gate)\n"
            "6. Create postmortem child with lessons and action items"
        ),
        "tips": [
            "Set severity immediately -- it determines response urgency and escalation",
            "Fill in timeline as you go -- reconstructing it later is unreliable",
            "Create the postmortem before closing -- it is easy to skip once the urgency passes",
            "Link postmortem action items to concrete tasks for follow-through",
            "Use detection_method to improve monitoring -- every incident is a monitoring gap",
        ],
        "common_mistakes": [
            "Closing without root cause -- the hard gate exists because 'fixed it' is not a root cause",
            "Skipping postmortems on sev3/sev4 -- small incidents reveal systemic patterns",
            "Postmortems without action items -- the hard gate requires them because insights without actions change nothing",
            "Not linking to the causing bug -- incident-to-bug traceability prevents repeat incidents",
            "Blame in postmortems -- keep them blameless; focus on contributing_factors, not people",
        ],
    },
}

_DEBT_PACK: dict[str, Any] = {
    "pack": "debt",
    "version": "1.0",
    "display_name": "Technical Debt",
    "description": "Catalog, assess, and systematically remediate technical debt",
    "requires_packs": ["core"],
    "types": {
        "debt_item": {
            "type": "debt_item",
            "display_name": "Debt Item",
            "description": "A piece of technical debt to be cataloged, assessed, and addressed",
            "pack": "debt",
            "states": [
                {"name": "identified", "category": "open"},
                {"name": "assessed", "category": "open"},
                {"name": "scheduled", "category": "open"},
                {"name": "remediating", "category": "wip"},
                {"name": "resolved", "category": "done"},
                {"name": "accepted", "category": "done"},
            ],
            "initial_state": "identified",
            "transitions": [
                {
                    "from": "identified",
                    "to": "assessed",
                    "enforcement": "hard",
                    "requires_fields": ["debt_category", "impact"],
                },
                {"from": "identified", "to": "accepted", "enforcement": "soft"},
                {"from": "assessed", "to": "scheduled", "enforcement": "soft"},
                {"from": "assessed", "to": "accepted", "enforcement": "soft"},
                {"from": "scheduled", "to": "remediating", "enforcement": "soft"},
                {"from": "remediating", "to": "resolved", "enforcement": "soft"},
                {"from": "remediating", "to": "assessed", "enforcement": "soft"},
            ],
            "fields_schema": [
                {
                    "name": "debt_category",
                    "type": "enum",
                    "options": ["code", "architecture", "test", "documentation", "dependency", "infrastructure"],
                    "description": "What kind of debt this is",
                    "required_at": ["assessed"],
                },
                {
                    "name": "impact",
                    "type": "enum",
                    "options": ["high", "medium", "low"],
                    "description": "Impact on velocity/quality if left unaddressed",
                    "required_at": ["assessed"],
                },
                {
                    "name": "effort_estimate",
                    "type": "text",
                    "description": "Estimated effort to remediate (e.g., 2d, 1w)",
                },
                {"name": "code_location", "type": "text", "description": "Where in the codebase this debt lives"},
                {
                    "name": "interest_description",
                    "type": "text",
                    "description": "Ongoing cost of not fixing this (the 'interest' on the debt)",
                },
                {"name": "incurred_reason", "type": "text", "description": "Why this debt was taken on originally"},
            ],
            "suggested_children": ["remediation"],
            "suggested_labels": ["legacy", "shortcut", "deprecation", "coupling", "complexity"],
        },
        "remediation": {
            "type": "remediation",
            "display_name": "Remediation",
            "description": "A concrete action to reduce or eliminate a debt item",
            "pack": "debt",
            "states": [
                {"name": "planned", "category": "open"},
                {"name": "in_progress", "category": "wip"},
                {"name": "completed", "category": "done"},
                {"name": "abandoned", "category": "done"},
            ],
            "initial_state": "planned",
            "transitions": [
                {"from": "planned", "to": "in_progress", "enforcement": "soft"},
                {"from": "planned", "to": "abandoned", "enforcement": "soft"},
                {"from": "in_progress", "to": "completed", "enforcement": "soft"},
                {"from": "in_progress", "to": "abandoned", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "approach", "type": "text", "description": "How the debt will be addressed"},
                {"name": "outcome", "type": "text", "description": "Result of the remediation"},
                {"name": "risk", "type": "text", "description": "Risks of performing this remediation"},
            ],
            "suggested_children": [],
            "suggested_labels": [],
        },
    },
    "relationships": [
        {
            "name": "remediation_for_debt",
            "from_types": ["remediation"],
            "to_types": ["debt_item"],
            "mechanism": "parent_id",
            "description": "Remediations belong to a debt item",
        },
        {
            "name": "debt_compounds",
            "from_types": ["debt_item"],
            "to_types": ["debt_item"],
            "mechanism": "dependency",
            "description": "Debt items that make other debt worse",
        },
    ],
    "cross_pack_relationships": [
        {
            "name": "debt_blocks_feature",
            "from_types": ["feature"],
            "to_types": ["debt_item"],
            "mechanism": "dependency",
            "description": "Features blocked until debt is resolved",
        },
        {
            "name": "spike_investigates_debt",
            "from_types": ["spike"],
            "to_types": ["debt_item"],
            "mechanism": "dependency",
            "description": "Spike investigation of debt remediation approach",
        },
    ],
    "guide": {
        "state_diagram": (
            "debt_item:   identified(O) --> assessed(O) --> scheduled(O) --> remediating(W) --> resolved(D)\n"
            "                          \\-> accepted(D)  \\-> accepted(D)  \\-> assessed(O) [rescope]\n"
            "             HARD: identified-->assessed requires debt_category, impact\n"
            "\n"
            "remediation: planned(O) --> in_progress(W) --> completed(D)\n"
            "                      \\-> abandoned(D)     \\-> abandoned(D)"
        ),
        "overview": (
            "Catalog and manage technical debt. Identify debt, assess its category and impact, "
            "schedule remediation, and track resolution or conscious acceptance."
        ),
        "when_to_use": "When technical debt is accumulating and needs visible tracking.",
        "states_explained": {
            "identified": "Debt has been recognized but not yet categorized or scored",
            "assessed": "Debt has been categorized and its impact evaluated",
            "scheduled": "Debt remediation has been planned into a milestone or sprint",
            "remediating": "Active work is underway to address this debt",
            "resolved": "Debt has been successfully eliminated",
            "accepted": "Debt is acknowledged and consciously left in place",
            "planned": "Remediation action is defined but not started",
            "in_progress": "Remediation is actively being worked on",
            "completed": "Remediation finished successfully",
            "abandoned": "Remediation was dropped (scope changed, approach invalid)",
        },
        "typical_flow": (
            "1. Identify debt with code_location and incurred_reason\n"
            "2. Assess with debt_category + impact (hard gate)\n"
            "3. Schedule into a sprint or milestone when prioritized\n"
            "4. Create remediation children with specific approaches\n"
            "5. Track remediations to completion\n"
            "6. Accept low-impact debt explicitly rather than leaving it untracked"
        ),
        "tips": [
            "Always fill code_location -- debt without a location is unfindable",
            "Describe the interest, not just the debt -- 'slows feature X by 2 days' is better than 'messy code'",
            "Accept debt consciously -- accepted debt with documented rationale is not neglect",
            "Link to blocking features so debt gets prioritized when it impacts delivery",
            "Use spikes to investigate complex remediations before committing to an approach",
        ],
        "common_mistakes": [
            "Identifying without assessing -- uncategorized debt piles up as an undifferentiated backlog",
            "Scheduling everything -- not all debt needs remediation; accept low-impact items explicitly",
            "Remediating without a plan -- create remediation children with specific approaches first",
            "Abandoning remediations silently -- update the debt_item back to assessed when rescoping",
            "Treating all debt as equal -- the hard gate on assessment forces category + impact for triage",
        ],
    },
}

_SPIKE_PACK: dict[str, Any] = {
    "pack": "spike",
    "version": "1.0",
    "display_name": "Spikes",
    "description": "Time-boxed investigation and research with documented findings",
    "requires_packs": ["core"],
    "types": {
        "spike": {
            "type": "spike",
            "display_name": "Spike",
            "description": "A time-boxed investigation to reduce uncertainty",
            "pack": "spike",
            "states": [
                {"name": "proposed", "category": "open"},
                {"name": "investigating", "category": "wip"},
                {"name": "concluded", "category": "wip"},
                {"name": "actioned", "category": "done"},
                {"name": "abandoned", "category": "done"},
            ],
            "initial_state": "proposed",
            "transitions": [
                {"from": "proposed", "to": "investigating", "enforcement": "soft"},
                {"from": "proposed", "to": "abandoned", "enforcement": "soft"},
                {
                    "from": "investigating",
                    "to": "concluded",
                    "enforcement": "hard",
                    "requires_fields": ["findings"],
                },
                {"from": "investigating", "to": "abandoned", "enforcement": "soft"},
                {"from": "concluded", "to": "actioned", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "hypothesis", "type": "text", "description": "What we believe and want to verify"},
                {"name": "time_box", "type": "text", "description": "Maximum time allocated for investigation"},
                {
                    "name": "findings",
                    "type": "text",
                    "description": "What was discovered during investigation",
                    "required_at": ["concluded"],
                },
                {
                    "name": "recommendation",
                    "type": "text",
                    "description": "Recommended next steps based on findings",
                    "required_at": ["actioned"],
                },
                {
                    "name": "decision",
                    "type": "enum",
                    "options": ["proceed", "pivot", "stop", "more_research"],
                    "description": "Decision made based on findings",
                },
            ],
            "suggested_children": ["finding", "task"],
            "suggested_labels": ["research", "prototype", "feasibility"],
        },
        "finding": {
            "type": "finding",
            "display_name": "Finding",
            "description": "A discrete discovery or insight from a spike investigation",
            "pack": "spike",
            "states": [
                {"name": "draft", "category": "open"},
                {"name": "published", "category": "done"},
            ],
            "initial_state": "draft",
            "transitions": [
                {"from": "draft", "to": "published", "enforcement": "soft"},
            ],
            "fields_schema": [
                {"name": "summary", "type": "text", "description": "Brief summary of the finding"},
                {"name": "evidence", "type": "text", "description": "Supporting evidence or data"},
                {"name": "implications", "type": "text", "description": "What this finding means for the project"},
            ],
            "suggested_children": [],
            "suggested_labels": [],
        },
    },
    "relationships": [
        {
            "name": "finding_from_spike",
            "from_types": ["finding"],
            "to_types": ["spike"],
            "mechanism": "parent_id",
            "description": "Findings belong to a spike investigation",
        },
        {
            "name": "spike_investigates",
            "from_types": ["spike"],
            "to_types": [],
            "mechanism": "dependency",
            "description": "Spike investigates other work items via dependency",
        },
    ],
    "cross_pack_relationships": [
        {
            "name": "spike_spawns_work",
            "from_types": ["task", "bug", "feature"],
            "to_types": ["spike"],
            "mechanism": "dependency",
            "description": "Spawned work items depend on source spike",
        },
        {
            "name": "spike_spawns_mitigation",
            "from_types": ["mitigation"],
            "to_types": ["spike"],
            "mechanism": "dependency",
            "description": "Spawned mitigations depend on source spike",
        },
    ],
    "guide": {
        "state_diagram": (
            "spike:   proposed(O) --> investigating(W) --> concluded(W) --> actioned(D)\n"
            "                    \\-> abandoned(D)      \\-> abandoned(D)\n"
            "         HARD: investigating-->concluded requires findings\n"
            "\n"
            "finding: draft(O) --> published(D)"
        ),
        "overview": (
            "Time-boxed investigation to reduce uncertainty. Propose a hypothesis, "
            "investigate within a time-box, document findings, then decide next steps."
        ),
        "when_to_use": "When facing technical uncertainty that needs structured investigation before committing.",
        "states_explained": {
            "proposed": "Spike has been suggested but investigation has not started",
            "investigating": "Active investigation is underway within the time-box",
            "concluded": "Investigation complete with documented findings",
            "actioned": "Findings have been acted upon (work items created)",
            "abandoned": "Spike was dropped before or during investigation",
            "draft": "Finding captured but not yet reviewed or finalized",
            "published": "Finding has been reviewed and shared with the team",
        },
        "typical_flow": (
            "1. Create spike in 'proposed' state with hypothesis and time_box\n"
            "2. Move to 'investigating' when work begins\n"
            "3. Create finding children as discoveries emerge\n"
            "4. Transition to 'concluded' with findings (hard gate)\n"
            "5. Set decision field and move to 'actioned' with recommendation"
        ),
        "tips": [
            "Always set a time_box before starting -- spikes without boundaries expand indefinitely",
            "Write the hypothesis before investigating -- it focuses the work and prevents scope creep",
            "Create findings as you go, not at the end -- draft findings capture insights while fresh",
            "Use the decision enum to make the outcome explicit -- 'more_research' is a valid decision",
            "Link spikes to the work items they investigate via dependencies for traceability",
        ],
        "common_mistakes": [
            "Skipping the hypothesis -- investigating without a clear question produces unfocused results",
            "Not setting a time_box -- the investigation expands to fill available time",
            "Concluding without findings -- the hard gate exists because undocumented spikes waste everyone's time",
            "Never moving to actioned -- concluded spikes without follow-up actions are wasted knowledge",
            "Creating spikes for known problems -- spikes are for uncertainty; use tasks for known work",
        ],
    },
}

_RELEASE_PACK: dict[str, Any] = {
    "pack": "release",
    "version": "1.0",
    "display_name": "Release Management",
    "description": "Release lifecycle: plan, freeze, test, ship, and roll back if needed",
    "requires_packs": ["core", "planning"],
    "types": {
        "release": {
            "type": "release",
            "display_name": "Release",
            "description": "A software release to be planned, tested, and shipped",
            "pack": "release",
            "container": True,
            "states": [
                {"name": "planning", "category": "open"},
                {"name": "development", "category": "wip"},
                {"name": "frozen", "category": "wip"},
                {"name": "testing", "category": "wip"},
                {"name": "staged", "category": "wip"},
                {"name": "released", "category": "done"},
                {"name": "rolled_back", "category": "wip"},
                {"name": "retired", "category": "done"},
                {"name": "cancelled", "category": "done"},
            ],
            "initial_state": "planning",
            "transitions": [
                {"from": "planning", "to": "development", "enforcement": "soft"},
                {"from": "planning", "to": "cancelled", "enforcement": "soft"},
                {
                    "from": "development",
                    "to": "frozen",
                    "enforcement": "hard",
                    "requires_fields": ["version"],
                },
                {"from": "development", "to": "cancelled", "enforcement": "soft"},
                {"from": "frozen", "to": "testing", "enforcement": "soft"},
                {"from": "frozen", "to": "development", "enforcement": "soft"},
                {"from": "testing", "to": "staged", "enforcement": "soft"},
                {"from": "testing", "to": "development", "enforcement": "soft"},
                {"from": "staged", "to": "released", "enforcement": "soft"},
                {"from": "staged", "to": "development", "enforcement": "soft"},
                {"from": "released", "to": "rolled_back", "enforcement": "soft"},
                {"from": "rolled_back", "to": "development", "enforcement": "soft"},
                {"from": "rolled_back", "to": "retired", "enforcement": "soft"},
            ],
            "fields_schema": [
                {
                    "name": "version",
                    "type": "text",
                    "description": "Version identifier. Must be 'Future' or semver vMAJOR.MINOR.PATCH (e.g., v2.1.0)",
                    "required_at": ["frozen"],
                    "pattern": r"^v\d+\.\d+\.\d+$|^Future$",
                    "unique": True,
                },
                {"name": "target_date", "type": "date", "description": "Planned release date"},
                {"name": "changelog", "type": "text", "description": "Summary of changes in this release"},
                {"name": "release_manager", "type": "text", "description": "Person coordinating this release"},
                {
                    "name": "rollback_plan",
                    "type": "text",
                    "description": "How to revert if the release fails",
                },
            ],
            "suggested_children": ["release_item", "epic", "milestone", "task", "bug", "feature"],
            "suggested_labels": ["major", "minor", "patch", "hotfix"],
        },
        "release_item": {
            "type": "release_item",
            "display_name": "Release Item",
            "description": "A work item included in a release, tracked through verification",
            "pack": "release",
            "states": [
                {"name": "queued", "category": "open"},
                {"name": "included", "category": "wip"},
                {"name": "verified", "category": "done"},
                {"name": "excluded", "category": "done"},
            ],
            "initial_state": "queued",
            "transitions": [
                {"from": "queued", "to": "included", "enforcement": "soft"},
                {"from": "queued", "to": "excluded", "enforcement": "soft"},
                {"from": "included", "to": "verified", "enforcement": "soft"},
                {"from": "included", "to": "excluded", "enforcement": "soft"},
            ],
            "fields_schema": [
                {
                    "name": "verification_status",
                    "type": "enum",
                    "options": ["untested", "passing", "failing", "blocked"],
                    "description": "Test/verification status for this item",
                },
                {"name": "release_notes", "type": "text", "description": "User-facing description of this change"},
            ],
            "suggested_children": [],
            "suggested_labels": [],
        },
    },
    "relationships": [
        {
            "name": "item_in_release",
            "from_types": ["release_item"],
            "to_types": ["release"],
            "mechanism": "parent_id",
            "description": "Release items belong to a release",
        },
    ],
    "cross_pack_relationships": [
        {
            "name": "release_delivers_milestone",
            "from_types": ["release"],
            "to_types": ["milestone"],
            "mechanism": "dependency",
            "description": "Releases deliver planning milestones",
        },
        {
            "name": "release_item_from_task",
            "from_types": ["release_item"],
            "to_types": ["task", "bug", "feature"],
            "mechanism": "dependency",
            "description": "Release items track core work items",
        },
    ],
    "guide": {
        "state_diagram": (
            "release:      planning(O) --> development(W) --> frozen(W) --> testing(W) --> staged(W) --> released(D)\n"
            "                          \\-> cancelled(D) \\-> cancelled(D)\n"
            "                             \\-> development(W) [unfreeze]       \\-> rolled_back(W)\n"
            "                                  \\-> development(W) [fix]            \\-> development(W) [re-release]\n"
            "                                       \\-> development(W) [fix]       \\-> retired(D) [close out]\n"
            "              HARD: development-->frozen requires version\n"
            "\n"
            "release_item: queued(O) --> included(W) --> verified(D)\n"
            "                      \\-> excluded(D)   \\-> excluded(D)"
        ),
        "overview": (
            "Release lifecycle management. Plan releases, freeze code, test, stage, "
            "ship, and roll back if needed. Track individual items through verification."
        ),
        "when_to_use": "When you ship versioned releases and need coordinated testing and deployment.",
        "states_explained": {
            "planning": "Release scope is being defined and items are being queued",
            "development": "Active development of items targeted for this release",
            "frozen": "Code freeze -- no new features, only bug fixes",
            "testing": "Release is under QA/integration testing",
            "staged": "Release is deployed to staging and awaiting go/no-go",
            "released": "Release has been shipped to production",
            "rolled_back": "Release was reverted after shipping",
            "retired": "Rolled-back release permanently closed (not re-released)",
            "cancelled": "Release was abandoned before shipping",
            "queued": "Item is proposed for inclusion in the release",
            "included": "Item is confirmed for this release",
            "verified": "Item has been tested and verified in the release",
            "excluded": "Item was removed from this release",
        },
        "typical_flow": (
            "1. Create release in 'planning' with target_date\n"
            "2. Queue release_items as children linked to tasks/bugs/features\n"
            "3. Move to 'development' as work progresses\n"
            "4. Freeze with version number (hard gate) when scope is locked\n"
            "5. Test, stage, then release\n"
            "6. Roll back if production issues are found"
        ),
        "tips": [
            "Set a rollback_plan before staging -- you will not have time to plan during a failure",
            "Freeze early and often -- long freezes indicate scope creep in development",
            "Exclude items rather than delaying the release -- smaller releases ship more reliably",
            "Link release_items to their source tasks/bugs for full traceability",
            "Use the version hard gate to enforce versioning discipline at freeze time",
        ],
        "common_mistakes": [
            "Skipping the freeze -- going straight from development to testing means scope is never locked",
            "No rollback plan -- the 'it will work' assumption fails when it matters most",
            "Including too many items -- large releases have exponentially more integration risk",
            "Not excluding failing items -- one failing item should not block an entire release",
            "Forgetting release notes on items -- write them while context is fresh, not at ship time",
        ],
    },
}

# ---------------------------------------------------------------------------
# Built-in reverse workflow edges
# ---------------------------------------------------------------------------

# These declared escape edges preserve the public cleanup/reopen/release
# behavior that previously used the internal transition-check bypass.
_BUILT_IN_REVERSE_TRANSITION_EDGES: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    "core": {
        "task": (
            ("open", "closed"),
            ("in_progress", "closed"),
            ("closed", "open"),
            ("closed", "in_progress"),
            ("in_progress", "open"),
        ),
        "bug": (
            ("triage", "closed"),
            ("triage", "wont_fix"),
            ("triage", "not_a_bug"),
            ("confirmed", "closed"),
            ("confirmed", "wont_fix"),
            ("confirmed", "not_a_bug"),
            ("fixing", "closed"),
            ("fixing", "wont_fix"),
            ("fixing", "not_a_bug"),
            ("verifying", "closed"),
            ("verifying", "wont_fix"),
            ("verifying", "not_a_bug"),
            ("closed", "triage"),
            ("closed", "confirmed"),
            ("closed", "fixing"),
            ("closed", "verifying"),
            ("wont_fix", "triage"),
            ("wont_fix", "confirmed"),
            ("wont_fix", "fixing"),
            ("wont_fix", "verifying"),
            ("not_a_bug", "triage"),
            ("not_a_bug", "confirmed"),
            ("not_a_bug", "fixing"),
            ("not_a_bug", "verifying"),
            ("fixing", "confirmed"),
            ("verifying", "triage"),
        ),
        "feature": (
            ("proposed", "done"),
            ("proposed", "deferred"),
            ("approved", "done"),
            ("approved", "deferred"),
            ("building", "done"),
            ("building", "deferred"),
            ("reviewing", "done"),
            ("reviewing", "deferred"),
            ("done", "proposed"),
            ("done", "approved"),
            ("done", "building"),
            ("done", "reviewing"),
            ("deferred", "proposed"),
            ("deferred", "approved"),
            ("deferred", "building"),
            ("deferred", "reviewing"),
            ("building", "approved"),
            ("reviewing", "proposed"),
        ),
        "epic": (
            ("open", "closed"),
            ("in_progress", "closed"),
            ("closed", "open"),
            ("closed", "in_progress"),
            ("in_progress", "open"),
        ),
    },
    "planning": {
        "milestone": (
            ("planning", "completed"),
            ("planning", "cancelled"),
            ("active", "completed"),
            ("active", "cancelled"),
            ("closing", "completed"),
            ("closing", "cancelled"),
            ("completed", "planning"),
            ("completed", "active"),
            ("completed", "closing"),
            ("cancelled", "planning"),
            ("cancelled", "active"),
            ("cancelled", "closing"),
            ("active", "planning"),
            ("closing", "planning"),
        ),
        "phase": (
            ("pending", "completed"),
            ("pending", "skipped"),
            ("active", "completed"),
            ("active", "skipped"),
            ("completed", "pending"),
            ("completed", "active"),
            ("skipped", "pending"),
            ("skipped", "active"),
            ("active", "pending"),
        ),
        "step": (
            ("pending", "completed"),
            ("pending", "skipped"),
            ("in_progress", "completed"),
            ("in_progress", "skipped"),
            ("completed", "pending"),
            ("completed", "in_progress"),
            ("skipped", "pending"),
            ("skipped", "in_progress"),
            ("in_progress", "pending"),
        ),
        "work_package": (
            ("defined", "delivered"),
            ("defined", "cancelled"),
            ("assigned", "delivered"),
            ("assigned", "cancelled"),
            ("executing", "delivered"),
            ("executing", "cancelled"),
            ("delivered", "defined"),
            ("delivered", "assigned"),
            ("delivered", "executing"),
            ("cancelled", "defined"),
            ("cancelled", "assigned"),
            ("cancelled", "executing"),
            ("executing", "assigned"),
        ),
        "deliverable": (
            ("planned", "accepted"),
            ("planned", "rejected"),
            ("producing", "accepted"),
            ("producing", "rejected"),
            ("reviewing", "accepted"),
            ("reviewing", "rejected"),
            ("accepted", "planned"),
            ("accepted", "producing"),
            ("accepted", "reviewing"),
            ("rejected", "planned"),
            ("rejected", "producing"),
            ("rejected", "reviewing"),
            ("producing", "planned"),
            ("reviewing", "planned"),
        ),
    },
    "requirements": {
        "requirement": (
            ("drafted", "verified"),
            ("drafted", "rejected"),
            ("drafted", "deferred"),
            ("reviewing", "verified"),
            ("reviewing", "rejected"),
            ("reviewing", "deferred"),
            ("approved", "verified"),
            ("approved", "rejected"),
            ("approved", "deferred"),
            ("implementing", "verified"),
            ("implementing", "rejected"),
            ("implementing", "deferred"),
            ("verified", "drafted"),
            ("verified", "reviewing"),
            ("verified", "approved"),
            ("verified", "implementing"),
            ("rejected", "drafted"),
            ("rejected", "reviewing"),
            ("rejected", "approved"),
            ("rejected", "implementing"),
            ("deferred", "drafted"),
            ("deferred", "reviewing"),
            ("deferred", "approved"),
            ("deferred", "implementing"),
            ("reviewing", "drafted"),
            ("implementing", "approved"),
        ),
        "acceptance_criterion": (
            ("draft", "validated"),
            ("validated", "draft"),
        ),
    },
    "risk": {
        "risk": (
            ("identified", "mitigated"),
            ("identified", "accepted"),
            ("identified", "retired"),
            ("assessing", "mitigated"),
            ("assessing", "accepted"),
            ("assessing", "retired"),
            ("assessed", "mitigated"),
            ("assessed", "accepted"),
            ("assessed", "retired"),
            ("mitigating", "mitigated"),
            ("mitigating", "accepted"),
            ("mitigating", "retired"),
            ("escalated", "mitigated"),
            ("escalated", "accepted"),
            ("escalated", "retired"),
            ("mitigated", "identified"),
            ("mitigated", "assessing"),
            ("mitigated", "assessed"),
            ("mitigated", "mitigating"),
            ("mitigated", "escalated"),
            ("accepted", "identified"),
            ("accepted", "assessing"),
            ("accepted", "assessed"),
            ("accepted", "mitigating"),
            ("accepted", "escalated"),
            ("retired", "identified"),
            ("retired", "assessing"),
            ("retired", "assessed"),
            ("retired", "mitigating"),
            ("retired", "escalated"),
            ("assessing", "identified"),
            ("mitigating", "assessed"),
        ),
        "mitigation": (
            ("planned", "completed"),
            ("planned", "cancelled"),
            ("in_progress", "completed"),
            ("in_progress", "cancelled"),
            ("ineffective", "completed"),
            ("ineffective", "cancelled"),
            ("completed", "planned"),
            ("completed", "in_progress"),
            ("completed", "ineffective"),
            ("cancelled", "planned"),
            ("cancelled", "in_progress"),
            ("cancelled", "ineffective"),
            ("in_progress", "planned"),
        ),
    },
    "roadmap": {
        "theme": (
            ("proposed", "achieved"),
            ("proposed", "sunset"),
            ("active", "achieved"),
            ("active", "sunset"),
            ("achieved", "proposed"),
            ("achieved", "active"),
            ("sunset", "proposed"),
            ("sunset", "active"),
            ("active", "proposed"),
        ),
        "objective": (
            ("defined", "achieved"),
            ("defined", "dropped"),
            ("pursuing", "achieved"),
            ("pursuing", "dropped"),
            ("achieved", "defined"),
            ("achieved", "pursuing"),
            ("dropped", "defined"),
            ("dropped", "pursuing"),
            ("pursuing", "defined"),
        ),
        "key_result": (
            ("defined", "met"),
            ("defined", "missed"),
            ("tracking", "met"),
            ("tracking", "missed"),
            ("met", "defined"),
            ("met", "tracking"),
            ("missed", "defined"),
            ("missed", "tracking"),
            ("tracking", "defined"),
        ),
    },
    "incident": {
        "incident": (
            ("reported", "closed"),
            ("triaging", "closed"),
            ("investigating", "closed"),
            ("mitigating", "closed"),
            ("resolved", "closed"),
            ("closed", "reported"),
            ("closed", "triaging"),
            ("closed", "investigating"),
            ("closed", "mitigating"),
            ("closed", "resolved"),
            ("triaging", "reported"),
            ("investigating", "reported"),
            ("mitigating", "reported"),
            ("resolved", "reported"),
        ),
        "postmortem": (
            ("drafting", "published"),
            ("reviewing", "published"),
            ("published", "drafting"),
            ("published", "reviewing"),
            ("reviewing", "drafting"),
        ),
    },
    "debt": {
        "debt_item": (
            ("identified", "resolved"),
            ("identified", "accepted"),
            ("assessed", "resolved"),
            ("assessed", "accepted"),
            ("scheduled", "resolved"),
            ("scheduled", "accepted"),
            ("remediating", "resolved"),
            ("remediating", "accepted"),
            ("resolved", "identified"),
            ("resolved", "assessed"),
            ("resolved", "scheduled"),
            ("resolved", "remediating"),
            ("accepted", "identified"),
            ("accepted", "assessed"),
            ("accepted", "scheduled"),
            ("accepted", "remediating"),
            ("remediating", "scheduled"),
        ),
        "remediation": (
            ("planned", "completed"),
            ("planned", "abandoned"),
            ("in_progress", "completed"),
            ("in_progress", "abandoned"),
            ("completed", "planned"),
            ("completed", "in_progress"),
            ("abandoned", "planned"),
            ("abandoned", "in_progress"),
            ("in_progress", "planned"),
        ),
    },
    "spike": {
        "spike": (
            ("proposed", "actioned"),
            ("proposed", "abandoned"),
            ("investigating", "actioned"),
            ("investigating", "abandoned"),
            ("concluded", "actioned"),
            ("concluded", "abandoned"),
            ("actioned", "proposed"),
            ("actioned", "investigating"),
            ("actioned", "concluded"),
            ("abandoned", "proposed"),
            ("abandoned", "investigating"),
            ("abandoned", "concluded"),
            ("investigating", "proposed"),
            ("concluded", "proposed"),
        ),
        "finding": (
            ("draft", "published"),
            ("published", "draft"),
        ),
    },
    "release": {
        "release": (
            ("planning", "released"),
            ("planning", "retired"),
            ("planning", "cancelled"),
            ("development", "released"),
            ("development", "retired"),
            ("development", "cancelled"),
            ("frozen", "released"),
            ("frozen", "retired"),
            ("frozen", "cancelled"),
            ("testing", "released"),
            ("testing", "retired"),
            ("testing", "cancelled"),
            ("staged", "released"),
            ("staged", "retired"),
            ("staged", "cancelled"),
            ("rolled_back", "released"),
            ("rolled_back", "retired"),
            ("rolled_back", "cancelled"),
            ("released", "planning"),
            ("released", "development"),
            ("released", "frozen"),
            ("released", "testing"),
            ("released", "staged"),
            ("released", "rolled_back"),
            ("retired", "planning"),
            ("retired", "development"),
            ("retired", "frozen"),
            ("retired", "testing"),
            ("retired", "staged"),
            ("retired", "rolled_back"),
            ("cancelled", "planning"),
            ("cancelled", "development"),
            ("cancelled", "frozen"),
            ("cancelled", "testing"),
            ("cancelled", "staged"),
            ("cancelled", "rolled_back"),
            ("development", "planning"),
            ("frozen", "planning"),
            ("testing", "planning"),
            ("staged", "planning"),
            ("rolled_back", "planning"),
        ),
        "release_item": (
            ("queued", "verified"),
            ("queued", "excluded"),
            ("included", "verified"),
            ("included", "excluded"),
            ("verified", "queued"),
            ("verified", "included"),
            ("excluded", "queued"),
            ("excluded", "included"),
            ("included", "queued"),
        ),
    },
}

# ---------------------------------------------------------------------------
# Public export
# ---------------------------------------------------------------------------

BUILT_IN_PACKS: dict[str, dict[str, Any]] = {
    "core": _CORE_PACK,
    "planning": _PLANNING_PACK,
    "requirements": _REQUIREMENTS_PACK,
    "risk": _RISK_PACK,
    "roadmap": _ROADMAP_PACK,
    "incident": _INCIDENT_PACK,
    "debt": _DEBT_PACK,
    "spike": _SPIKE_PACK,
    "release": _RELEASE_PACK,
}

for _pack_name, _types in _BUILT_IN_REVERSE_TRANSITION_EDGES.items():
    for _type_name, _edges in _types.items():
        BUILT_IN_PACKS[_pack_name]["types"][_type_name]["reverse_transitions"] = [
            {"from": _from, "to": _to, "enforcement": "soft"} for _from, _to in _edges
        ]
