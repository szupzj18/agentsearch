import json
import os
import re

from ..model import Msg, block_text, clip, norm_ts
from .base import Source, decode_cwd_dir

CAVEAT = re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.S)
COMMAND_BLOCK = re.compile(
    r"<command-(?:name|message|args|stdout|stderr)(?:\s[^>]*)?>.*?</command-(?:name|message|args|stdout|stderr)>",
    re.S,
)


def clean_user_text(text):
    text = CAVEAT.sub("", text)
    text = COMMAND_BLOCK.sub("", text)
    return text.strip()


class ClaudeSource(Source):
    name = "claude"

    def root(self):
        return os.path.join(self.home, ".claude", "projects")

    def files(self):
        root = self.root()
        if not os.path.isdir(root):
            return
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                if n.endswith(".jsonl"):
                    yield os.path.join(dirpath, n)

    def parse(self, path, clip_text=True):
        sid = os.path.splitext(os.path.basename(path))[0]
        cwd = decode_cwd_dir(os.path.basename(os.path.dirname(path)))
        clipf = clip if clip_text else (lambda t: t)
        msgs = []
        for lineno, d in self.read_jsonl(path):
            t = d.get("type")
            if t in ("user", "assistant"):
                m = d.get("message")
                if not isinstance(m, dict):
                    continue
                sid = d.get("sessionId") or sid
                cwd = d.get("cwd") or cwd
                role = m.get("role") or t
                if role not in ("user", "assistant"):
                    continue
                ts = norm_ts(d.get("timestamp"))
                for kind, text in self._content(m.get("content")):
                    if role == "user" and kind == "text":
                        text = clean_user_text(text)
                    if text:
                        msgs.append((lineno, Msg(ts, role, kind, clipf(text))))
            elif t == "summary":
                text = d.get("summary")
                if text:
                    msgs.append(
                        (lineno, Msg(norm_ts(d.get("timestamp")), "user", "summary", clipf(text)))
                    )
        return sid, cwd, msgs

    @staticmethod
    def _content(content):
        if isinstance(content, str):
            return [("text", content)]
        out = []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                ty = b.get("type")
                if ty == "text":
                    if b.get("text"):
                        out.append(("text", b["text"]))
                elif ty == "thinking":
                    if b.get("thinking"):
                        out.append(("reasoning", b["thinking"]))
                elif ty == "tool_use":
                    args = b.get("input", "")
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    out.append(("tool_call", "%s(%s)" % (b.get("name", "tool"), args)))
                elif ty == "tool_result":
                    text = block_text(b.get("content"))
                    if text:
                        out.append(("tool_result", text))
        return out
