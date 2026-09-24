import os

from ..model import Msg, clip, norm_ts
from .base import Source, decode_cwd_dir


class PiSource(Source):
    name = "pi"

    def root(self):
        return os.path.join(self.home, ".pi", "agent", "sessions")

    def files(self):
        root = self.root()
        if not os.path.isdir(root):
            return
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                if n.endswith(".jsonl"):
                    yield os.path.join(dirpath, n)

    def parse(self, path):
        # filename: 2026-09-01T09-46-41-101Z_<uuid>.jsonl
        sid = os.path.splitext(os.path.basename(path))[0].split("_", 1)[-1]
        cwd = decode_cwd_dir(os.path.basename(os.path.dirname(path)))
        msgs = []
        for lineno, d in self.read_jsonl(path):
            t = d.get("type")
            if t == "session":
                sid = d.get("id") or sid
                cwd = d.get("cwd") or cwd
            elif t == "message":
                m = d.get("message")
                if not isinstance(m, dict):
                    continue
                role = {"toolResult": "tool", "bashExecution": "tool"}.get(
                    m.get("role"), m.get("role")
                )
                if role not in ("user", "assistant", "tool"):
                    continue
                ts = norm_ts(d.get("timestamp"))
                for kind, text in self._content(m.get("content")):
                    if role == "tool" and kind == "text":
                        kind = "tool_result"
                    if text:
                        msgs.append((lineno, Msg(ts, role, kind, clip(text))))
        return sid, cwd, msgs

    @staticmethod
    def _content(content):
        out = []
        if isinstance(content, list):
            blocks = content
        elif isinstance(content, str):
            return [("text", content)]
        else:
            return out
        for b in blocks:
            if not isinstance(b, dict):
                if isinstance(b, str):
                    out.append(("text", b))
                continue
            ty = b.get("type")
            if ty == "text" and b.get("text"):
                out.append(("text", b["text"]))
            elif ty == "thinking" and b.get("thinking"):
                out.append(("reasoning", b["thinking"]))
            elif ty == "toolCall":
                import json

                args = b.get("arguments", "")
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                out.append(("tool_call", "%s(%s)" % (b.get("name", "tool"), args)))
            # image and other blocks are skipped
        return out
