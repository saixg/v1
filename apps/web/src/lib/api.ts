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
  kind: "scan";
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
  return (await resp.json()) as T;
}

function json(body: unknown): RequestInit {
  return {
    method: "POST",
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
