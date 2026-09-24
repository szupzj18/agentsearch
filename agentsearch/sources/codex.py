import json
import os
import re

from ..model import Msg, clip, norm_ts
from .base import Source

SKIP_ROLES = {"developer", "system"}

ENV_CONTEXT = re.compile(r"^\s*<environment_context>.*?</environment_context>", re.S)


class CodexSource(Source):
    name = "codex"

    def roots(self):
        base = os.path.join(self.home, ".codex")
        return [
            os.path.join(base, "sessions"),
            os.path.join(base, "archived_sessions"),
        ]

    def files(self):
        for root in self.roots():
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, names in os.walk(root):
                for n in names:
                    if n.endswith(".jsonl"):
                        yield os.path.join(dirpath, n)

    def parse(self, path, clip_text=True):
        sid = ""
        cwd = ""
        clipf = clip if clip_text else (lambda t: t)
        msgs = []
        for lineno, d in self.read_jsonl(path):
            t = d.get("type")
            ts = norm_ts(d.get("timestamp"))
            if t == "session_meta":
                p = d.get("payload") or {}
                sid = p.get("session_id") or p.get("id") or sid
                cwd = p.get("cwd") or cwd
                continue
            if t != "response_item":
                continue
            p = d.get("payload")
            if not isinstance(p, dict):
                continue
            pt = p.get("type")
            if pt == "message":
                role = p.get("role")
                if role in SKIP_ROLES:
                    continue
                if role not in ("user", "assistant"):
                    continue
                text = self._message_text(p.get("content"))
                text = ENV_CONTEXT.sub("", text).strip()
                if text:
                    msgs.append((lineno, Msg(ts, role, "text", clipf(text))))
            elif pt == "reasoning":
                text = self._reasoning_text(p)
                if text:
                    msgs.append((lineno, Msg(ts, "assistant", "reasoning", clipf(text))))
            elif pt in ("function_call", "custom_tool_call"):
                args = p.get("arguments", "")
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                name = p.get("name") or pt
                msgs.append((lineno, Msg(ts, "assistant", "tool_call", clipf("%s(%s)" % (name, args)))))
            elif pt in ("function_call_output", "custom_tool_call_output"):
                out = p.get("output", "")
                if not isinstance(out, str):
                    out = json.dumps(out, ensure_ascii=False)
                if out:
                    msgs.append((lineno, Msg(ts, "tool", "tool_result", clipf(out))))
        return sid, cwd, msgs

    @staticmethod
    def _message_text(content):
        parts = []
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif isinstance(b, str):
                    parts.append(b)
        elif isinstance(content, str):
            parts.append(content)
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _reasoning_text(p):
        if p.get("encrypted_content") and not p.get("summary"):
            return ""
        summary = p.get("summary")
        if isinstance(summary, str):
            return summary
        if isinstance(summary, list):
            parts = []
            for item in summary:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        if isinstance(p.get("text"), str):
            return p["text"]
        return ""
