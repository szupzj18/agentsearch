// agentsearch — search across claude / codex / pi sessions from inside pi.
// Thin wrapper over the agentsearch CLI.
// @ts-nocheck
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { Type } from "typebox";

function resolveBin(): string {
  const override = process.env.AGENTSEARCH_BIN;
  if (override) return override;
  const defaultBin = path.join(os.homedir(), "agentsearch", "bin", "agentsearch");
  return existsSync(defaultBin) ? defaultBin : "agentsearch";
}

const BIN = resolveBin();

function run(args: string[], timeoutMs = 30000): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      BIN,
      args,
      { timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          reject(new Error(stderr?.trim() || err.message));
          return;
        }
        resolve(stdout);
      }
    );
  });
}

export default function (pi) {
  pi.registerTool({
    name: "search_sessions",
    label: "Search agent sessions",
    description:
      "Full-text search across coding-agent sessions (claude, codex, pi) on this machine " +
      "and registered remote devboxes. " +
      "Finds user prompts, assistant replies, summaries, tool calls and tool results. " +
      "Each hit includes host, source, cwd, timestamp, snippet, and path+line for get_session_context " +
      "(pass the hit's host there). Supports English (prefix) and Chinese (substring).",
    parameters: Type.Object({
      query: Type.String({
        description: "keywords separated by whitespace; all must match",
      }),
      source: Type.Optional(
        Type.String({ description: "comma-separated subset of: claude,codex,pi" })
      ),
      cwd: Type.Optional(
        Type.String({ description: "only sessions whose working directory contains this" })
      ),
      since: Type.Optional(Type.String({ description: "YYYY-MM-DD" })),
      host: Type.Optional(
        Type.String({
          description:
            "comma-separated devices (local or a registered devbox name); default: all",
        })
      ),
      limit: Type.Optional(Type.Number({ description: "max hits, default 20" })),
    }),
    async execute(_toolCallId, params) {
      const args = ["search", params.query, "--json", "--limit", String(params.limit ?? 20)];
      if (params.source) args.push("--source", params.source);
      if (params.cwd) args.push("--cwd", params.cwd);
      if (params.since) args.push("--since", params.since);
      if (params.host) args.push("--host", params.host);
      const text = await run(args);
      return { content: [{ type: "text", text }], details: {} };
    },
  });

  pi.registerTool({
    name: "get_session_context",
    label: "Get session context",
    description:
      "Fetch surrounding messages of a search_sessions hit (path + line) so the hit can be read in context.",
    parameters: Type.Object({
      path: Type.String(),
      line: Type.Number(),
      host: Type.Optional(
        Type.String({ description: "device holding the hit, from the search result (default: local)" })
      ),
      before: Type.Optional(Type.Number()),
      after: Type.Optional(Type.Number()),
    }),
    async execute(_toolCallId, params) {
      const args = [
        "context",
        params.path,
        String(params.line),
        "--json",
        "--before",
        String(params.before ?? 4),
        "--after",
        String(params.after ?? 8),
      ];
      if (params.host) args.push("--host", params.host);
      const text = await run(args);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
}
