from .claude import ClaudeSource
from .codex import CodexSource
from .pi import PiSource

SOURCES = {
    "claude": ClaudeSource,
    "codex": CodexSource,
    "pi": PiSource,
}


def get_sources(names=None, home=None):
    if names:
        return [SOURCES[n](home=home) for n in names]
    return [cls(home=home) for cls in SOURCES.values()]
