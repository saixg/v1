// Typed client for services/api. Shapes mirror the pydantic models in
// app/models/schemas.py and the response models in app/main.py — nothing
// here is invented on the client side.

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

// This module has no React hook of its own, so `ClerkTokenBridge` (mounted
// once in layout.tsx) feeds it Clerk's `useAuth().getToken` here.
type TokenGetter = () => Promise<string | null>;
let tokenGetter: TokenGetter | null = null;

export function setTokenGetter(fn: TokenGetter): void {
  tokenGetter = fn;
}

async function authHeader(): Promise<Record<string, string>> {
  const token = tokenGetter ? await tokenGetter() : null;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export type AnalysisStatus = "verified" | "inferred" | "unknown" | "unanalyzed";

export type NodeKind =
  | "repository"
  | "directory"
  | "file"
  | "symbol"
  | "route"
  | "model"
  | "test"
  | "external_service"
  | "config_value"
  | "cluster";

/** codeaxis_contracts.Node */
export interface GraphNode {
  id: number | null;
  repo_id: string;
  commit_sha: string;
  kind: NodeKind;
  name: string;
  path: string | null;
  line_start: number | null;
  line_end: number | null;
  col_start: number | null;
  col_end: number | null;
  attrs: Record<string, unknown>;
  embedding: number[] | null;
  analyzer: string;
  analyzer_version: string;
  status: AnalysisStatus;
  source_file: string | null;
  confidence: number | null;
  reason: string | null;
}

/** services/api PreviewEntryOut */
export interface PreviewEntry {
  path: string;
  kind: string;
  language: string | null;
  size_bytes: number | null;
  included: boolean;
  reason: string | null;
}

/** services/api StageRepoResponse */
export interface StageRepoResponse {
  id: string;
  commit_sha: string;
  local_path: string;
  preview: PreviewEntry[];
}

/** codeaxis_contracts.PassRun */
export interface PassRun {
  id: number | null;
  repo_id: string;
  commit_sha: string;
  analyzer: string;
  analyzer_version: string;
  status: "running" | "succeeded" | "failed";
  error: string | null;
}

export interface FileContent {
  path: string;
  content: string;
  content_hash: string;
}

/** codeaxis_contracts.ResolutionStatus — architectural certainty of one
 * edge's destination, distinct from the node/edge AnalysisStatus above. */
export type ResolutionStatus = "resolved" | "ambiguous" | "unresolved" | "external";

export type RelationshipEdgeKind = "imports" | "calls" | "extends" | "implements" | "tests";

/** The connected node's identity on one relationship entry — enough to
 * render and link to it without a second round-trip. */
export interface RelationshipNode {
  id: number | null;
  name: string;
  path: string | null;
  kind: NodeKind;
}

/** One edge of a `/symbols/{id}` relationships list: `resolution_status` is
 * null for edge kinds that predate the concept (there are none among
 * RelationshipEdgeKind today, but the server type allows it), and `node` is
 * null when the edge's destination never resolved (unresolved/external). */
export interface RelationshipEntry {
  kind: RelationshipEdgeKind;
  resolution_status: ResolutionStatus | null;
  attrs: Record<string, unknown>;
  node: RelationshipNode | null;
}

/** services/api symbol_mechanics response: the raw node plus its depth-1
 * relationships in both directions. */
export interface SymbolDetail extends GraphNode {
  relationships: {
    outgoing: RelationshipEntry[];
    incoming: RelationshipEntry[];
  };
}

/** codeaxis_contracts.Coverage */
export interface Coverage {
  repo_id: string;
  commit_sha: string;
  area: string;
  metric: string;
  resolved: number;
  total: number;
  blockers: Record<string, number>;
}

export interface UnresolvedSummaryRow {
  kind: string;
  resolution_status: ResolutionStatus | null;
  count: number;
}

export interface CoverageResponse {
  coverage: Coverage[];
  unresolved: UnresolvedSummaryRow[];
}

/** codeaxis_contracts.ProjectRole */
export type ProjectRole = "owner" | "member";

/** services/api ProjectWithRole */
export interface Project {
  id: string;
  name: string;
  owner_id: string;
  created_at: string;
  role: ProjectRole;
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

/** codeaxis_contracts.Job */
export interface Job {
  id: string;
  project_id: string;
  repo_id: string | null;
  /** "scan" for ingestion; blueprint jobs add their own kinds, so this is
   * not narrowed to a literal. */
  kind: string;
  status: JobStatus;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export type IngestSource =
  | { kind: "github"; remote_url: string; ref?: string | null }
  | { kind: "local"; local_path: string; commit_sha: string };

/** An HTTP error from the API, carrying its `detail` text verbatim. Some
 * routes answer with a structured `detail` object instead of a string — the
 * chat route's citation-check failure lists every rejected citation that way
 * — so the parsed body is kept alongside the flattened message. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** One authenticated fetch with the shared error handling. Callers decide
 * how to read a success body (JSON, text, or nothing for a 204). */
async function send(path: string, init?: RequestInit): Promise<Response> {
  let resp: Response;
  try {
    const auth = await authHeader();
    resp = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { ...auth, ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, `Could not reach the CodeAxis API at ${API_URL}. Check that it is running.`);
  }
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    let payload: unknown;
    try {
      const body = await resp.json();
      payload = body?.detail;
      if (typeof body?.detail === "string") detail = body.detail;
      else if (body?.detail) detail = JSON.stringify(body.detail);
    } catch {
      // Non-JSON error body: keep the status line.
    }
    throw new ApiError(resp.status, detail, payload);
  }
  return resp;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return (await (await send(path, init)).json()) as T;
}

/** For routes that answer 204 No Content. */
async function requestEmpty(path: string, init?: RequestInit): Promise<void> {
  await send(path, init);
}

function json(body: unknown, method: "POST" | "PATCH" | "PUT" = "POST"): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function qs(params: Record<string, string>): string {
  return new URLSearchParams(params).toString();
}

function encodePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

export function getProjects(): Promise<{ projects: Project[] }> {
  return request("/projects");
}

export function createProject(name: string): Promise<Project> {
  return request("/projects", json({ name }));
}

export function stageRepo(source: IngestSource, projectId: string): Promise<StageRepoResponse> {
  return request(`/repos/stage?${qs({ project_id: projectId })}`, json(source));
}

export function uploadZip(file: File, projectId: string): Promise<StageRepoResponse> {
  const form = new FormData();
  form.append("file", file);
  return request(`/repos/upload?${qs({ project_id: projectId })}`, {
    method: "POST",
    body: form,
  });
}

export function confirmScan(
  repoId: string,
  commitSha: string,
  excludedPaths: string[],
): Promise<{ status: string; job_id: string }> {
  return request(
    `/repos/${encodeURIComponent(repoId)}/confirm`,
    json({ commit_sha: commitSha, excluded_paths: excludedPaths }),
  );
}

export function getStatus(repoId: string): Promise<{ runs: PassRun[] }> {
  return request(`/repos/${encodeURIComponent(repoId)}/status`);
}

export function getJob(jobId: string): Promise<Job> {
  return request(`/jobs/${encodeURIComponent(jobId)}`);
}

export function getTreeRoot(repoId: string, commitSha: string): Promise<GraphNode> {
  return request(`/repos/${encodeURIComponent(repoId)}/tree-root?${qs({ commit_sha: commitSha })}`);
}

export async function getChildren(
  repoId: string,
  nodeId: number,
  commitSha: string,
): Promise<GraphNode[]> {
  const body = await request<{ children: GraphNode[] }>(
    `/repos/${encodeURIComponent(repoId)}/nodes/${nodeId}/children?${qs({ commit_sha: commitSha })}`,
  );
  return body.children;
}

export function getSymbol(
  repoId: string,
  nodeId: number,
  commitSha: string,
): Promise<SymbolDetail> {
  return request(
    `/repos/${encodeURIComponent(repoId)}/symbols/${nodeId}?${qs({ commit_sha: commitSha })}`,
  );
}

export function getCoverage(repoId: string, commitSha: string): Promise<CoverageResponse> {
  return request(
    `/repos/${encodeURIComponent(repoId)}/coverage?${qs({ commit_sha: commitSha })}`,
  );
}

export function getFile(repoId: string, path: string, commitSha: string): Promise<FileContent> {
  return request(
    `/repos/${encodeURIComponent(repoId)}/files/${encodePath(path)}?${qs({ commit_sha: commitSha })}`,
  );
}

// ---------------------------------------------------------------- semantic
// Phase 3 surfaces: hybrid retrieval, grounded chat, stored explanations.
// Shapes mirror app/main.py's `_result_payload`/`_budget_payload`,
// `StageOutcome.as_dict`, `GroundedAnswer.citation_payload` and
// `_explanation_payload` — again, nothing invented client-side.

/** codeaxis Citation: a file+line reference behind one claim. */
export interface Citation {
  path: string;
  line_start: number;
  line_end: number;
  symbol?: string | null;
  node_id?: number | null;
  chunk_id?: number | null;
}

/** A chat citation also reports whether its path was in the evidence bundle
 * the model was shown. `false` is informative, not a failure — a path can be
 * real without having been retrieved. */
export interface ChatCitation extends Citation {
  in_evidence?: boolean;
}

/** `app.analysis.semantic.retrieve.StageOutcome` — one retrieval stage, with
 * "skipped" as a first-class result rather than an absence. */
export interface StageOutcome {
  name: string;
  status: string;
  hits: number;
  reason: string | null;
}

export interface RetrievalBudget {
  token_budget: number;
  tokens_used: number;
  dropped_for_budget: number;
  fused_candidates: number;
}

/** One retrieved span. `content` is present on /search and omitted on /chat,
 * where the evidence list is metadata only. */
export interface RetrievedSpan {
  chunk_id: number | null;
  node_id: number | null;
  path: string;
  symbol: string | null;
  kind: string | null;
  line_start: number;
  line_end: number;
  token_count: number;
  score: number;
  stages: string[];
  content?: string;
}

export interface SearchResponse {
  commit_sha: string;
  query: string;
  results: RetrievedSpan[];
  citations: Citation[];
  stages: StageOutcome[];
  budget: RetrievalBudget;
}

export interface ChatClaim {
  text: string;
  citations: Citation[];
}

/** POST /repos/{id}/chat. `status: "no_evidence"` is a real outcome: nothing
 * was retrieved, so no model call was made and `answer` is null. */
export interface ChatResponse {
  thread_id: string;
  commit_sha: string;
  status: "ok" | "no_evidence";
  message_id: number | null;
  answer: string | null;
  claims: ChatClaim[];
  citations: ChatCitation[];
  unsupported: string | null;
  citation_check?: { checked: number; rejected: number };
  stages: StageOutcome[];
  budget: RetrievalBudget;
  evidence: RetrievedSpan[];
  streaming: string;
}

/** The 422 body when the validator rejects the model's citations: the answer
 * was discarded whole, and every rejection says why. */
export interface CitationCheckFailure {
  error: "citation_check_failed";
  thread_id: string;
  checked: number;
  unsupported_claims: number;
  rejected: Record<string, unknown>[];
  uncited_claim_indices: number[];
  detail: string;
}

export function isCitationCheckFailure(detail: unknown): detail is CitationCheckFailure {
  return (
    typeof detail === "object" &&
    detail !== null &&
    (detail as { error?: unknown }).error === "citation_check_failed"
  );
}

export interface ChatMessage {
  id: number | null;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  model: string | null;
  created_at: string | null;
}

export interface ChatThread {
  thread_id: string;
  commit_sha: string;
  title: string | null;
  messages: ChatMessage[];
}

/** GET /repos/{id}/nodes/{nid}/explanation — a *stored* summary. `origin` is
 * always "inferred" for these: model prose about analyzer-established facts,
 * and the UI must label it as such. 404 means none was written; explanations
 * are never generated on request. */
export interface Explanation {
  level: string;
  scope_key: string;
  node_id: number | null;
  summary: string;
  origin: string;
  model: string | null;
  analyzer: string;
  analyzer_version: string;
  created_at: string | null;
  citations: Citation[];
  evidence: {
    input: string | null;
    chunk_ids: number[];
    node_ids: number[];
    children: string[];
    truncated: boolean;
  };
}

export interface RetrievalOptions {
  limit?: number;
  tokenBudget?: number;
  hops?: number;
  vector?: boolean;
  pathPrefix?: string[];
  kind?: string[];
}

export function searchRepo(
  repoId: string,
  commitSha: string,
  q: string,
  opts: RetrievalOptions = {},
): Promise<SearchResponse> {
  const params = new URLSearchParams({ commit_sha: commitSha, q });
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.tokenBudget !== undefined) params.set("token_budget", String(opts.tokenBudget));
  if (opts.hops !== undefined) params.set("hops", String(opts.hops));
  if (opts.vector !== undefined) params.set("vector", String(opts.vector));
  for (const prefix of opts.pathPrefix ?? []) params.append("path_prefix", prefix);
  for (const kind of opts.kind ?? []) params.append("kind", kind);
  return request(`/repos/${encodeURIComponent(repoId)}/search?${params.toString()}`);
}

export function askRepo(
  repoId: string,
  commitSha: string,
  message: string,
  threadId: string | null,
  opts: RetrievalOptions = {},
): Promise<ChatResponse> {
  const body: Record<string, unknown> = { message };
  if (threadId) body.thread_id = threadId;
  if (opts.limit !== undefined) body.limit = opts.limit;
  if (opts.tokenBudget !== undefined) body.token_budget = opts.tokenBudget;
  if (opts.hops !== undefined) body.hops = opts.hops;
  if (opts.vector !== undefined) body.vector = opts.vector;
  if (opts.pathPrefix) body.path_prefix = opts.pathPrefix;
  if (opts.kind) body.kind = opts.kind;
  return request(
    `/repos/${encodeURIComponent(repoId)}/chat?${qs({ commit_sha: commitSha })}`,
    json(body),
  );
}

export function getChatThread(
  repoId: string,
  threadId: string,
  commitSha: string,
): Promise<ChatThread> {
  return request(
    `/repos/${encodeURIComponent(repoId)}/chat/${encodeURIComponent(threadId)}?${qs({
      commit_sha: commitSha,
    })}`,
  );
}

export function getNodeExplanation(
  repoId: string,
  nodeId: number,
  commitSha: string,
): Promise<Explanation> {
  return request(
    `/repos/${encodeURIComponent(repoId)}/nodes/${nodeId}/explanation?${qs({
      commit_sha: commitSha,
    })}`,
  );
}

export function getScopeExplanation(
  repoId: string,
  commitSha: string,
  level: "symbol" | "file" | "directory" | "component" | "repo",
  scopeKey = "",
): Promise<Explanation> {
  return request(
    `/repos/${encodeURIComponent(repoId)}/explanation?${qs({
      commit_sha: commitSha,
      level,
      scope_key: scopeKey,
    })}`,
  );
}

// The exclusion preview comes back from stage/upload only; there is no GET
// for it. It is handed to the preview screen through sessionStorage.
const PREVIEW_KEY = (repoId: string) => `codeaxis:stage:${repoId}`;

export function rememberStaged(staged: StageRepoResponse): void {
  try {
    sessionStorage.setItem(PREVIEW_KEY(staged.id), JSON.stringify(staged));
  } catch {
    // Storage unavailable: the preview screen reports it can't find the list.
  }
}

/** The stored stage response as raw JSON, or null if this tab has none. */
export function recallStagedRaw(repoId: string): string | null {
  try {
    return sessionStorage.getItem(PREVIEW_KEY(repoId));
  } catch {
    return null;
  }
}

// ------------------------------------------------------------ context engine
// Phase 4 Agent Context Engine. Shapes mirror services/api/app/context_api.py
// and the pydantic records in app/models/schemas.py. Everything the server
// derives is static analysis; none of it was observed by running the program.

/** How a stored record came to exist. `inferred` is a model's observation,
 * never a parsed fact. */
export type OriginTag = "parsed" | "configured" | "inferred";

/** app.models.ContextEvidence */
export interface ContextEvidence {
  spans: Citation[];
  node_ids: number[];
  chunk_ids: number[];
  documents: Record<string, string>[];
  notes: string | null;
  truncated: boolean;
}

export type TaskStatus = "draft" | "extracting" | "ready" | "blocked" | "archived";

export interface Task {
  id: string | null;
  project_id: string;
  repo_id: string;
  commit_sha: string;
  title: string;
  intent: string | null;
  status: TaskStatus;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export type RequirementKind =
  | "objective"
  | "functional"
  | "technical"
  | "architectural"
  | "existing_behavior"
  | "acceptance_criteria"
  | "open_question";

export type RequirementStatus = "open" | "ambiguous" | "unresolved" | "satisfied" | "rejected";

export interface Requirement {
  id: string | null;
  task_id: string;
  repo_id: string;
  commit_sha: string;
  kind: RequirementKind;
  statement: string;
  priority: "must" | "should" | "could" | null;
  status: RequirementStatus;
  question: string | null;
  origin: OriginTag;
  source_ref: Record<string, unknown>;
  evidence: ContextEvidence;
  ordinal: number;
  confidence: number | null;
  model: string | null;
  analyzer: string;
  analyzer_version: string;
  created_at: string | null;
}

export type ConstraintKind =
  | "public_api"
  | "db_schema"
  | "auth_boundary"
  | "ci_lint"
  | "do_not_touch"
  | "dependency"
  | "performance"
  | "other";

export const CONSTRAINT_KINDS: readonly ConstraintKind[] = [
  "public_api",
  "db_schema",
  "auth_boundary",
  "ci_lint",
  "do_not_touch",
  "dependency",
  "performance",
  "other",
];

export interface Constraint {
  id: string | null;
  task_id: string | null;
  repo_id: string;
  commit_sha: string;
  kind: ConstraintKind;
  scope: string;
  statement: string;
  severity: "hard" | "soft";
  source: "user_stated" | "auto_detected";
  origin: OriginTag;
  confirmed: boolean;
  confirmed_by: string | null;
  confirmed_at: string | null;
  rationale: string | null;
  evidence: ContextEvidence;
  analyzer: string | null;
  analyzer_version: string | null;
  created_at: string | null;
}

export interface ConstraintInput {
  kind: ConstraintKind;
  scope: string;
  statement: string;
  severity: "hard" | "soft";
  rationale?: string | null;
}

export interface Decision {
  id: string | null;
  task_id: string;
  commit_sha: string;
  question: string;
  decision: string | null;
  status: "open" | "decided" | "superseded";
  locked: boolean;
  decided_by: string | null;
  decided_at: string | null;
  rationale: string | null;
  origin: OriginTag;
  evidence: ContextEvidence;
  analyzer: string;
  analyzer_version: string;
}

export interface TaskSnapshot {
  id: number | null;
  task_id: string;
  commit_sha: string;
  status: "active" | "stale" | "superseded";
  reason: string | null;
  created_at: string | null;
}

export type ContextItemKind =
  | "task_intent"
  | "constraint"
  | "requirement"
  | "architecture"
  | "code"
  | "convention"
  | "test"
  | "limitation"
  | "open_question";

export interface ContextItem {
  id: string | null;
  task_id: string;
  package_id: string | null;
  commit_sha: string;
  kind: ContextItemKind;
  content: string;
  origin: OriginTag;
  evidence: ContextEvidence;
  token_count: number;
  ordinal: number;
  analyzer: string;
  analyzer_version: string;
}

export type ContextPackageStatus = "assembled" | "stale" | "invalid";

/** One section row of `package.manifest.sections`. */
export interface ManifestSection {
  kind: ContextItemKind;
  truncatable: boolean;
  allocated: number | null;
  used: number;
  included: number;
  dropped: number;
  dropped_items: { ref: string; tokens: number }[];
  dropped_by_retrieval?: number;
}

export interface PackageManifest {
  order?: ContextItemKind[];
  budget?: {
    token_budget: number;
    fixed_tokens: number;
    tail_budget: number;
    tail_used: number;
  };
  sections?: ManifestSection[];
  dropped_total?: number;
}

/** `package.retrieval`: the retrieval stages behind the code section. */
export interface PackageRetrieval {
  pipeline?: string;
  seed_symbols?: number;
  grounded_files?: number;
  stages?: StageOutcome[];
  token_budget?: number;
  tokens_used?: number;
  fused_candidates?: number;
  dropped_for_budget?: number;
}

/** One `package.unresolved` entry. `kind` is open-ended (for example
 * `constraints_exceed_budget`, `open_decision`, `unmatched_path`), so the
 * remaining keys stay readable but untyped. */
export interface UnresolvedEntry {
  kind: string;
  detail?: string;
  ref?: string;
  [key: string]: unknown;
}

export interface ContextPackage {
  id: string | null;
  task_id: string;
  commit_sha: string;
  version: number;
  status: ContextPackageStatus;
  token_budget: number;
  token_count: number;
  manifest: PackageManifest;
  retrieval: PackageRetrieval;
  unresolved: UnresolvedEntry[];
  analyzer: string;
  analyzer_version: string;
  created_at: string | null;
}

export interface PackageFreshnessSummary {
  package_id: string | null;
  version: number;
  commit_sha: string;
  status: ContextPackageStatus;
  state: "current" | "stale";
  reason: string | null;
  created_at: string | null;
}

export interface PinnedRecord {
  kind: "requirement" | "constraint" | "task_component";
  ref: string;
  commit_sha: string;
}

export interface TaskState {
  analysis: "static";
  notice: string;
  task: Task;
  active_snapshot: TaskSnapshot | null;
  snapshots: TaskSnapshot[];
  requirements: Requirement[];
  constraints: Constraint[];
  decisions: Decision[];
  packages: PackageFreshnessSummary[];
  latest_package: PackageFreshnessSummary | null;
  current_package_id: string | null;
  pinned_elsewhere: PinnedRecord[];
  problems: string[];
}

export interface DocumentPreview {
  task_id: string;
  /** Always false: the preview persists nothing. */
  stored: boolean;
  documents: { name: string; chunks: number; secrets_redacted: number }[];
  images: { chunk_id: string | number; name: string }[];
  secrets_redacted: number;
  unreadable: unknown[];
}

export interface DroppedOutput {
  key: string | null;
  kind: string;
  reason: string;
  detail: string;
}

export interface ExtractionResult {
  task_id: string;
  task_status: TaskStatus;
  requirements: Requirement[];
  dropped: DroppedOutput[];
  constraint_conflicts: {
    requirement_key: string;
    constraint_id: string | null;
    constraint_kind: string;
    scope: string;
    path: string;
    confirmed: boolean;
    severity: string;
  }[];
  contradictions: { first: string; second: string; explanation: string }[];
  /** "ran" | "skipped_by_caller" | "not_run" */
  contradiction_check: string;
  secrets_redacted: number;
  unreadable: unknown[];
}

export interface ContextPackageResponse {
  analysis: "static";
  notice: string;
  package: ContextPackage;
  freshness: { state: "current" | "stale"; current: boolean; reason: string | null };
  items: ContextItem[];
}

export type ExportFormat = "claude_md" | "agents_md" | "context_md";

export interface ExportedFile {
  filename: string;
  markdown: string;
  packageStatus: string | null;
  snapshot: string | null;
}

/** The 409 body when an export is refused because the package is stale or
 * invalid. The caller may opt in with `allow_stale`. */
export interface PackageNotCurrent {
  error: "package_not_current";
  package_status: string;
  reason: string;
  detail: string;
}

export function isPackageNotCurrent(detail: unknown): detail is PackageNotCurrent {
  return (
    typeof detail === "object" &&
    detail !== null &&
    (detail as { error?: unknown }).error === "package_not_current"
  );
}

/** 502 bodies from extraction: the previous requirements were left as they were. */
export interface ModelFailure {
  error: "model_output_invalid" | "model_call_failed";
  detail: string;
  task_id: string;
}

export function isModelFailure(detail: unknown): detail is ModelFailure {
  if (typeof detail !== "object" || detail === null) return false;
  const e = (detail as { error?: unknown }).error;
  return e === "model_output_invalid" || e === "model_call_failed";
}

export interface RegroundReport {
  previous_commit_sha: string;
  snapshot: TaskSnapshot;
  stale_package_ids: string[];
  needs_rederivation: PinnedRecord[];
}

export interface ApiTokenRecord {
  id: string | null;
  repo_id: string;
  name: string;
  token_prefix: string;
  scope: "read";
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

export interface IssuedToken {
  token: string;
  shown_once: boolean;
  record: ApiTokenRecord;
  mcp_url_path: string;
}

const taskPath = (taskId: string) => `/tasks/${encodeURIComponent(taskId)}`;
const repoPath = (repoId: string) => `/repos/${encodeURIComponent(repoId)}`;

export function createTask(
  repoId: string,
  title: string,
  intent: string,
  commitSha?: string,
): Promise<Task> {
  const body: Record<string, unknown> = { title, intent };
  if (commitSha) body.commit_sha = commitSha;
  return request(`${repoPath(repoId)}/tasks`, json(body));
}

export function getTaskState(taskId: string): Promise<TaskState> {
  return request(taskPath(taskId));
}

export function previewDocuments(
  taskId: string,
  files: File[],
  includeImages: boolean,
): Promise<DocumentPreview> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  form.append("include_images", String(includeImages));
  return request(`${taskPath(taskId)}/documents`, { method: "POST", body: form });
}

export function extractRequirements(
  taskId: string,
  files: File[],
  opts: { includeImages: boolean; checkContradictions: boolean },
): Promise<ExtractionResult> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  form.append("include_images", String(opts.includeImages));
  form.append("check_contradictions", String(opts.checkContradictions));
  return request(`${taskPath(taskId)}/requirements/extract`, { method: "POST", body: form });
}

export function listTaskConstraints(
  taskId: string,
): Promise<{ commit_sha: string; constraints: Constraint[] }> {
  return request(`${taskPath(taskId)}/constraints`);
}

export function createConstraint(taskId: string, input: ConstraintInput): Promise<Constraint> {
  return request(`${taskPath(taskId)}/constraints`, json(input));
}

export function patchConstraint(
  taskId: string,
  constraintId: string,
  patch: Partial<ConstraintInput>,
): Promise<Constraint> {
  return request(
    `${taskPath(taskId)}/constraints/${encodeURIComponent(constraintId)}`,
    json(patch, "PATCH"),
  );
}

export function deleteConstraint(taskId: string, constraintId: string): Promise<void> {
  return requestEmpty(`${taskPath(taskId)}/constraints/${encodeURIComponent(constraintId)}`, {
    method: "DELETE",
  });
}

export function confirmConstraint(
  taskId: string,
  constraintId: string,
  confirmed = true,
): Promise<Constraint> {
  return request(
    `${taskPath(taskId)}/constraints/${encodeURIComponent(constraintId)}/confirm`,
    json({ confirmed }),
  );
}

/** Repo-wide ledger: auto-detected and repo-level entries only. */
export function getRepoLedger(
  repoId: string,
  commitSha?: string,
): Promise<{ analysis: "static"; commit_sha: string; constraints: Constraint[] }> {
  const q = commitSha ? `?${qs({ commit_sha: commitSha })}` : "";
  return request(`${repoPath(repoId)}/constraints${q}`);
}

export function resolveDecision(
  taskId: string,
  decisionId: string,
  decision: string,
  rationale?: string,
): Promise<Decision> {
  const body: Record<string, unknown> = { decision };
  if (rationale) body.rationale = rationale;
  return request(
    `${taskPath(taskId)}/decisions/${encodeURIComponent(decisionId)}/resolve`,
    json(body),
  );
}

export function assembleContext(
  taskId: string,
  tokenBudget: number,
): Promise<ContextPackageResponse> {
  return request(`${taskPath(taskId)}/context`, json({ token_budget: tokenBudget }));
}

export function getContextPackage(taskId: string): Promise<ContextPackageResponse> {
  return request(`${taskPath(taskId)}/context`);
}

/** Fetches the rendered markdown. A stale or invalid package is refused with
 * a 409 whose `detail` satisfies `isPackageNotCurrent`; retry with
 * `allowStale` only when the person has chosen to. The file name is the
 * server's, from Content-Disposition. */
export async function exportContext(
  taskId: string,
  format: ExportFormat,
  allowStale = false,
): Promise<ExportedFile> {
  const resp = await send(
    `${taskPath(taskId)}/export?${qs({ format, allow_stale: String(allowStale) })}`,
  );
  const type = resp.headers.get("Content-Type") ?? "";
  if (!type.startsWith("text/markdown")) {
    throw new ApiError(resp.status, `Unexpected export content type: ${type || "none"}`);
  }
  const disposition = resp.headers.get("Content-Disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  return {
    filename: match ? match[1] : "context.md",
    markdown: await resp.text(),
    packageStatus: resp.headers.get("X-CodeAxis-Package-Status"),
    snapshot: resp.headers.get("X-CodeAxis-Snapshot"),
  };
}

export function regroundTask(
  taskId: string,
  commitSha: string,
  reason: string,
): Promise<RegroundReport> {
  return request(`${taskPath(taskId)}/reground`, json({ commit_sha: commitSha, reason }));
}

export function createApiToken(
  repoId: string,
  name: string,
  expiresInDays?: number,
): Promise<IssuedToken> {
  const body: Record<string, unknown> = { name };
  if (expiresInDays !== undefined) body.expires_in_days = expiresInDays;
  return request(`${repoPath(repoId)}/api-tokens`, json(body));
}

export function listApiTokens(
  repoId: string,
  includeRevoked = false,
): Promise<{ tokens: ApiTokenRecord[] }> {
  return request(`${repoPath(repoId)}/api-tokens?${qs({ include_revoked: String(includeRevoked) })}`);
}

export function revokeApiToken(repoId: string, tokenId: string): Promise<void> {
  return requestEmpty(`${repoPath(repoId)}/api-tokens/${encodeURIComponent(tokenId)}`, {
    method: "DELETE",
  });
}

// ------------------------------------------------------- virtual dev environment
// Phase 5 blueprints. Shapes mirror packages/schemas/blueprint.v1.json (the
// assembled document) and the /blueprints routes. A blueprint is a design for
// code that does not exist yet; none of it was observed by running anything.

export type BlueprintStatus = "draft" | "validated" | "invalid" | "approved";
export type ComponentKind = "existing" | "proposed" | "modified";
export type ComponentMapping = "reuse" | "extend" | "new";
export type GenerationStatus = "generated" | "edited" | "locked";
export type ConnectionKind = "control_flow" | "data_flow" | "dependency" | "event";
export type ProposedFileStatus = "new" | "modified" | "unchanged";

/** The only legal (kind, mapping) pairings. The database, the server model and
 * the validator all refuse any other combination. */
export const KIND_MAPPING: Record<ComponentKind, ComponentMapping> = {
  existing: "reuse",
  modified: "extend",
  proposed: "new",
};

export interface Blueprint {
  id: string;
  project_id: string;
  repo_id: string;
  commit_sha: string;
  task_id: string;
  title: string;
  current_version_id?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface BlueprintValidationError {
  code: string;
  message: string;
  entity_kind?: "component" | "connection" | "proposed_file" | "blueprint" | null;
  entity_key?: string | null;
}

export interface BlueprintVersion {
  id?: string;
  blueprint_id?: string;
  version: number;
  schema_version?: string;
  status: BlueprintStatus;
  validation_errors?: BlueprintValidationError[];
  origin_reason?: "generated" | "regenerated" | "edited" | "validated";
  analyzer: string;
  analyzer_version: string;
  created_by?: string | null;
  created_at?: string | null;
}

export interface ComponentInterface {
  name: string;
  kind: "api" | "event" | "function";
  signature?: string | null;
}

/** Evidence on a blueprint row; every collection may be absent. */
export interface BlueprintEvidence {
  spans?: Citation[];
  node_ids?: number[];
  chunk_ids?: number[];
  documents?: Record<string, string>[];
  notes?: string | null;
  truncated?: boolean;
}

export interface BlueprintComponent {
  id?: string;
  blueprint_version_id?: string | null;
  key: string;
  name: string;
  kind: ComponentKind;
  mapping: ComponentMapping;
  linked_component_id?: number | null;
  repo_id: string;
  commit_sha: string;
  module_key?: string | null;
  responsibility?: string | null;
  interfaces?: ComponentInterface[];
  confidence?: number | null;
  evidence?: BlueprintEvidence;
  generation_status?: GenerationStatus;
  locked?: boolean;
  created_at?: string | null;
}

export interface BlueprintConnection {
  id?: string;
  blueprint_version_id?: string | null;
  key: string;
  kind: ConnectionKind;
  source_key: string;
  target_key: string;
  semantics?: {
    protocol?: string | null;
    trigger?: string | null;
    description?: string | null;
  };
  data_shape?: Record<string, unknown> | null;
  locked?: boolean;
  created_at?: string | null;
}

export interface BlueprintModule {
  key: string;
  name?: string | null;
  components: string[];
}

export interface ProposedFile {
  id?: string;
  blueprint_version_id?: string | null;
  path: string;
  status: ProposedFileStatus;
  purpose: string;
  implements_component_key?: string | null;
  conventions_refs?: number[];
  locked?: boolean;
  created_at?: string | null;
}

/** Positions only, keyed by component/connection key. Never semantics. */
export interface BlueprintLayout {
  nodes?: { key: string; x: number; y: number; w?: number | null; h?: number | null }[];
  edges?: { key: string; points?: { x: number; y: number }[] }[];
  viewport?: { x: number; y: number; zoom: number };
}

export interface BlueprintUnresolved {
  decision_id?: string | null;
  question: string;
  status?: "open" | "decided" | "deferred";
  affects?: string[];
}

/** The assembled document: blueprint.v1.json. */
export interface BlueprintDocument {
  schema_version: string;
  blueprint: Blueprint;
  version: BlueprintVersion;
  components: BlueprintComponent[];
  connections?: BlueprintConnection[];
  data_flows?: BlueprintConnection[];
  modules?: BlueprintModule[];
  proposed_files?: ProposedFile[];
  layout?: BlueprintLayout;
  requirement_ids?: string[];
  constraint_ids?: string[];
  unresolved?: BlueprintUnresolved[];
}

export interface BlueprintComponentPatch {
  name?: string;
  kind?: ComponentKind;
  mapping?: ComponentMapping;
  linked_component_id?: number | null;
  module_key?: string | null;
  responsibility?: string | null;
  generation_status?: GenerationStatus;
  locked?: boolean;
}

export interface BlueprintExport {
  filename: string;
  markdown: string;
}

const blueprintPath = (id: string) => `/blueprints/${encodeURIComponent(id)}`;

export function listBlueprints(
  repoId: string,
  filter: { commitSha?: string; taskId?: string } = {},
): Promise<Blueprint[]> {
  const params: Record<string, string> = {};
  if (filter.commitSha) params.commit_sha = filter.commitSha;
  if (filter.taskId) params.task_id = filter.taskId;
  const q = Object.keys(params).length > 0 ? `?${qs(params)}` : "";
  return request(`${repoPath(repoId)}/blueprints${q}`);
}

export function createBlueprint(
  repoId: string,
  taskId: string,
  title: string,
  commitSha: string,
): Promise<Blueprint> {
  return request(
    `${repoPath(repoId)}/blueprints`,
    json({ task_id: taskId, title, commit_sha: commitSha }),
  );
}

export function getBlueprint(blueprintId: string): Promise<BlueprintDocument> {
  return request(blueprintPath(blueprintId));
}

export function listBlueprintVersions(blueprintId: string): Promise<BlueprintVersion[]> {
  return request(`${blueprintPath(blueprintId)}/versions`);
}

export function getBlueprintVersion(
  blueprintId: string,
  version: number,
): Promise<BlueprintDocument> {
  return request(`${blueprintPath(blueprintId)}/versions/${version}`);
}

export function generateBlueprint(blueprintId: string): Promise<{ job_id: string }> {
  return request(`${blueprintPath(blueprintId)}/generate`, json({}));
}

export function validateBlueprint(blueprintId: string): Promise<BlueprintVersion> {
  return request(`${blueprintPath(blueprintId)}/validate`, json({}));
}

/** A 422 here means the regeneration would have altered a locked entity. The
 * server's `detail` shape for that is not pinned down, so callers read it with
 * `describeRefusal` rather than assuming fields. */
export function regenerateBlueprint(
  blueprintId: string,
  componentKeys?: string[],
): Promise<{ job_id: string }> {
  const body: Record<string, unknown> = {};
  if (componentKeys && componentKeys.length > 0) body.component_keys = componentKeys;
  return request(`${blueprintPath(blueprintId)}/regenerate`, json(body));
}

export function patchBlueprintComponent(
  blueprintId: string,
  componentId: string,
  patch: BlueprintComponentPatch,
): Promise<BlueprintComponent> {
  return request(
    `${blueprintPath(blueprintId)}/components/${encodeURIComponent(componentId)}`,
    json(patch, "PATCH"),
  );
}

export function getBlueprintFileStructure(blueprintId: string): Promise<ProposedFile[]> {
  return request(`${blueprintPath(blueprintId)}/file-structure`);
}

export function exportBlueprint(blueprintId: string): Promise<BlueprintExport> {
  return request(`${blueprintPath(blueprintId)}/export`, json({}));
}

export async function getBlueprintLayout(blueprintId: string): Promise<BlueprintLayout> {
  const body = await request<{ layout: BlueprintLayout | null }>(
    `${blueprintPath(blueprintId)}/layout`,
  );
  return body.layout ?? {};
}

/** Persists positions only. This never creates a blueprint version. */
export function putBlueprintLayout(blueprintId: string, layout: BlueprintLayout): Promise<void> {
  return requestEmpty(`${blueprintPath(blueprintId)}/layout`, json({ layout }, "PUT"));
}

/** What a 422 refusal says, flattened for display. The detail may be a
 * string, a structured object, or FastAPI's validation list; whatever it is,
 * it is shown rather than swallowed. */
export interface Refusal {
  summary: string;
  reasons: string[];
}

function reasonText(item: unknown): string {
  if (typeof item === "string") return item;
  if (typeof item === "object" && item !== null) {
    const o = item as Record<string, unknown>;
    const what = [o.entity_kind, o.entity_key ?? o.key]
      .filter((x) => typeof x === "string")
      .join(" ");
    const why = [o.reason, o.message, o.msg, o.detail].find((x) => typeof x === "string");
    if (what || why) return [what, why].filter(Boolean).join(": ");
    return JSON.stringify(item);
  }
  return String(item);
}

export function describeRefusal(detail: unknown, fallback: string): Refusal {
  if (typeof detail === "string") return { summary: detail, reasons: [] };
  if (Array.isArray(detail)) return { summary: fallback, reasons: detail.map(reasonText) };
  if (typeof detail === "object" && detail !== null) {
    const o = detail as Record<string, unknown>;
    const summary = [o.detail, o.message, o.reason].find((x) => typeof x === "string");
    const list = ["reasons", "locked_entities", "locked", "entities", "conflicts", "violations"]
      .map((k) => o[k])
      .find((v) => Array.isArray(v));
    return {
      summary: (summary as string | undefined) ?? fallback,
      reasons: Array.isArray(list) ? list.map(reasonText) : [],
    };
  }
  return { summary: fallback, reasons: [] };
}
