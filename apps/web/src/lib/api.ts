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

/** An HTTP error from the API, carrying its `detail` text verbatim. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
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
    try {
      const body = await resp.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (body?.detail) detail = JSON.stringify(body.detail);
    } catch {
      // Non-JSON error body: keep the status line.
    }
    throw new ApiError(resp.status, detail);
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
