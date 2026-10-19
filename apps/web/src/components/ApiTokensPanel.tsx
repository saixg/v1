"use client";

import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  createApiToken,
  listApiTokens,
  revokeApiToken,
  type ApiTokenRecord,
  type IssuedToken,
} from "@/lib/api";

import { ErrorLine, errMsg, Pill } from "./ContextBits";

/** Per-repo, read-only API tokens for the MCP server. Owner only: for anyone
 * else the server says so and this panel shows that message instead of a list. */
export function ApiTokensPanel({ repoId }: { repoId: string }) {
  const [tokens, setTokens] = useState<ApiTokenRecord[] | null>(null);
  const [forbidden, setForbidden] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [days, setDays] = useState("");
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState<IssuedToken | null>(null);
  const [copied, setCopied] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setTokens((await listApiTokens(repoId)).tokens);
      setForbidden(null);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setForbidden(err.message);
      else setError(errMsg(err));
    }
  }, [repoId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on mount
    void reload();
  }, [reload]);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const d = days.trim() === "" ? undefined : Number(days);
      setIssued(await createApiToken(repoId, name.trim(), d));
      setCopied(false);
      setName("");
      setDays("");
      await reload();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.token);
      setCopied(true);
    } catch {
      setError("Could not copy automatically. Select the token text and copy it by hand.");
    }
  }

  async function revoke(id: string) {
    try {
      await revokeApiToken(repoId, id);
      setRevoking(null);
      await reload();
    } catch (err) {
      setError(errMsg(err));
    }
  }

  return (
    <section aria-label="API tokens" className="space-y-3">
      <h3 className="text-sm font-semibold">API tokens (read-only MCP)</h3>
      <p className="text-[11px] text-muted">
        A token lets an agent read this repository&apos;s context through the read-only MCP
        server. It cannot change anything. Only a project owner can manage tokens.
      </p>

      {forbidden ? (
        <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-muted">
          {forbidden}
        </p>
      ) : (
        <>
          <form onSubmit={create} className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-muted">
              Name
              <input
                value={name}
                maxLength={200}
                onChange={(e) => setName(e.target.value)}
                className="mt-0.5 block w-48 rounded-md border border-line bg-transparent px-2 py-1.5 text-sm"
              />
            </label>
            <label className="text-xs text-muted">
              Expires in days (optional)
              <input
                type="number"
                min={1}
                max={365}
                value={days}
                onChange={(e) => setDays(e.target.value)}
                className="mt-0.5 block w-28 rounded-md border border-line bg-transparent px-2 py-1.5 text-sm"
              />
            </label>
            <button
              type="submit"
              disabled={busy || !name.trim()}
              className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
            >
              {busy ? "Creating…" : "Create token"}
            </button>
          </form>

          {issued && (
            <div className="space-y-2 rounded-md border border-amber-600/40 bg-amber-500/10 p-3 text-xs">
              <p className="font-medium text-amber-700 dark:text-amber-400">
                Copy this token now. It will not be shown again.
              </p>
              <code className="block break-all rounded bg-panel p-2 font-mono text-[11px]">
                {issued.token}
              </code>
              <p className="text-muted">
                MCP path on the API host: <span className="font-mono">{issued.mcp_url_path}</span>
              </p>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={copy}
                  className="rounded-md border border-line px-2.5 py-1 font-medium hover:bg-panel"
                >
                  {copied ? "Copied" : "Copy token"}
                </button>
                <button
                  type="button"
                  onClick={() => setIssued(null)}
                  className="rounded-md border border-line px-2.5 py-1 font-medium hover:bg-panel"
                >
                  I have copied it
                </button>
              </div>
            </div>
          )}

          {tokens === null ? (
            !error && <p className="text-xs text-muted">Loading tokens…</p>
          ) : tokens.length === 0 ? (
            <p className="text-xs text-muted">No active tokens for this repository.</p>
          ) : (
            <ul className="space-y-2">
              {tokens.map((t) => (
                <li key={t.id ?? t.token_prefix} className="space-y-1 rounded-md border border-line p-3 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{t.name}</span>
                    <span className="font-mono text-muted">{t.token_prefix}…</span>
                    <Pill tone="neutral">{t.scope}</Pill>
                    {t.revoked_at && <Pill tone="bad">revoked</Pill>}
                  </div>
                  <p className="text-muted">
                    {t.last_used_at ? `Last used ${t.last_used_at}` : "Never used"}
                    {t.expires_at ? ` · expires ${t.expires_at}` : " · does not expire"}
                  </p>
                  {t.id && !t.revoked_at &&
                    (revoking === t.id ? (
                      <span className="flex items-center gap-2">
                        Revoke {t.name}? Agents using it lose access.
                        <button
                          type="button"
                          onClick={() => revoke(t.id as string)}
                          className="rounded-md border border-rose-600/40 px-2.5 py-1 font-medium text-rose-700 hover:bg-rose-500/10 dark:text-rose-300"
                        >
                          Revoke token
                        </button>
                        <button
                          type="button"
                          onClick={() => setRevoking(null)}
                          className="rounded-md border border-line px-2.5 py-1 hover:bg-panel"
                        >
                          Keep
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setRevoking(t.id)}
                        className="rounded-md border border-line px-2.5 py-1 font-medium hover:bg-panel"
                      >
                        Revoke
                      </button>
                    ))}
                </li>
              ))}
            </ul>
          )}
          <ErrorLine message={error} />
        </>
      )}
    </section>
  );
}
