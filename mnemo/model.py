import datetime
import re
from dataclasses import dataclass

MAX_TEXT = 20000

CJK_RUN = re.compile(
    r"[⺀-⻿㐀-䶿⼀-㈀-鿿＀-￯\U0001F200-\U0001FAFF]+"
)


@dataclass
class Msg:
    ts: str
    role: str  # user / assistant / tool
    kind: str  # text / reasoning / tool_call / tool_result / summary
    text: str


def norm_ts(value):
    """Normalize an ISO timestamp to sortable UTC 'YYYY-MM-DDTHH:MM:SSZ'."""
    if not value:
        return ""
    t = value.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clip(text):
    if not isinstance(text, str):
        text = str(text)
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + " …[truncated]"
    return text


def cjk_grams(text):
    """Unigrams + bigrams of every CJK run for substring-ish FTS matching."""
    grams = []
    for m in CJK_RUN.finditer(text):
        run = m.group(0)
        grams.extend(list(run))
        grams.extend(run[i : i + 2] for i in range(len(run) - 1))
    return " ".join(grams)


def has_cjk(text):
    return bool(CJK_RUN.search(text))


def block_text(content):
    """Extract plain text from an Anthropic-style content field (str or blocks)."""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif isinstance(b.get("content"), str):
                    parts.append(b["content"])
    return "\n".join(p for p in parts if p)
