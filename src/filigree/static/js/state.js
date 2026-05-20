// ---------------------------------------------------------------------------
// Shared state and constants for the Filigree dashboard.
// All modules import from here — single source of truth.
// ---------------------------------------------------------------------------

// --- Constants ---

export const CATEGORY_COLORS = { open: "#64748B", wip: "#38BDF8", done: "#7B919C" };

export const THEME_COLORS = {
  textPrimary: "#E2EEF2",
  textSecondary: "#8FAAB8",
  graphOutline: "#0B1215",
  graphEdge: "#2A4454",
  graphEdgeHierarchy: "#3B5C6E",
  accent: "#38BDF8",
};

export const PRIORITY_COLORS = {
  0: "#EF4444",
  1: "#F97316",
  2: "#6B7280",
  3: "#D1D5DB",
  4: "#D1D5DB",
};

export const TYPE_ICONS = {
  bug: "\u{1F41B}",
  feature: "\u2728",
  task: "\u{1F4CB}",
  epic: "\u{1F4CA}",
  milestone: "\u{1F3AF}",
  step: "\u25B6",
};

export const TYPE_COLORS = {
  bug: "#EF4444",
  feature: "#8B5CF6",
  task: "#3B82F6",
  epic: "#F59E0B",
  milestone: "#10B981",
  step: "#6B7280",
};

export const SEVERITY_COLORS = {
  critical: { bg: "bg-red-900/50", text: "text-red-400", border: "border-red-800", hex: "#EF4444" },
  high: {
    bg: "bg-orange-900/50",
    text: "text-orange-400",
    border: "border-orange-800",
    hex: "#F97316",
  },
  medium: {
    bg: "bg-yellow-900/50",
    text: "text-yellow-400",
    border: "border-yellow-800",
    hex: "#EAB308",
  },
  low: { bg: "bg-blue-900/50", text: "text-blue-400", border: "border-blue-800", hex: "#3B82F6" },
  info: {
    bg: "bg-slate-800/50",
    text: "text-slate-400",
    border: "border-slate-700",
    hex: "#64748B",
  },
};

export const TOUR_STEPS = [
  {
    el: "#btnKanban",
    text: "The dashboard has 5 views: Kanban (default), Graph, Releases, Insights, and Files. Each shows your project differently.",
    pos: "bottom",
  },
  {
    el: "#btnReadyFilter",
    text: "Ready issues have no blockers and can be worked on immediately. Toggle this to sort them first.",
    pos: "bottom",
  },
  {
    el: "#filterSearch",
    text: 'Search issues by title or ID. Press "/" anywhere to focus this field instantly.',
    pos: "bottom",
  },
  {
    el: "#healthBadge",
    text: "Health score (0\u2013100) measures project flow. Click it for a detailed breakdown of what affects the score.",
    pos: "bottom",
  },
  {
    el: "#kanbanBoard",
    text: "Click any card to open its detail panel. Use j/k to navigate between cards. Switch between Board, Cluster, and List modes.",
    pos: "top",
  },
  {
    el: null,
    text: 'Press "?" anytime to see all keyboard shortcuts. Happy tracking!',
    pos: "center",
  },
];

export const REFRESH_INTERVAL = 15000;

// --- Mutable application state ---

export const state = {
  // Core data
  allIssues: [],
  allDeps: [],
  issueMap: {},
  stats: null,

  // View state
  currentView: "kanban",
  kanbanMode: "board",
  statusPills: { open: true, active: true, done: false },
  selectedIssue: null,
  selectedCards: new Set(),
  multiSelectMode: false,
  expandedEpics: new Set(),
  detailHistory: [],

  // Graph instances
  cy: null,
  workflowCy: null,
  graphConfig: {
    graph_v2_enabled: false,
    graph_api_mode: "legacy",
    graph_mode_configured: null,
  },
  graphConfigLoaded: false,
  graphData: null,
  graphMode: "legacy",
  graphQuery: {},
  graphQueryKey: "",
  graphTelemetry: null,
  graphFallbackNotice: "",
  graphSearchQuery: "",
  graphSearchIndex: 0,
  graphPathNodes: new Set(),
  graphPathEdges: new Set(),

  // Graph sidebar (scoped subtree explorer)
  graphSidebarSelections: new Map(), // Map<issueId, {state, causedBy}>
  graphSidebarTypeFilter: new Set(), // active type filters (empty = all)

  // Filters
  readyFilter: true,
  blockedFilter: false,
  searchResults: null,
  _searchTimeout: null,

  // Type-filtered kanban
  typeTemplate: null,
  _typeFilterSeq: 0,

  // Drag-and-drop
  _dragIssueId: null,
  _dragTransitions: [],
  _transitionsLoaded: false,

  // Multi-project
  API_BASE: "/api",
  currentProjectKey: "",
  allProjects: [],

  // Health & critical path
  criticalPathIds: new Set(),
  criticalPathActive: false,
  impactScores: {},
  healthScore: null,

  // Change tracking
  previousIssueState: {},
  changedIds: new Set(),

  // List mode sort
  _listSortCol: "priority",
  _listSortDir: "asc",

  // Cached view data (previously on window._)
  _staleIssues: [],
  _healthBreakdown: null,

  // File views
  filesData: null,
  filesPage: { offset: 0, limit: 25 },
  filesSort: "updated_at",
  filesSearch: "",
  filesCriticalOnly: false,
  filesScanSource: "",
  selectedFile: null,
  fileDetailData: null,
  fileDetailTab: "findings",
  timelineFilter: null,
  hotspots: null,
};
