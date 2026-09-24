import json
import os

from ..model import Msg, clip, norm_ts


class Source:
    name = ""

    def __init__(self, home=None):
        self.home = os.path.expanduser(home or "~")

    def files(self):
        """Yield session file paths."""
        raise NotImplementedError

    def parse(self, path):
        """Return (session_id, cwd, [(lineno, Msg), ...])."""
        raise NotImplementedError

    @staticmethod
    def read_jsonl(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield lineno, json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue


def decode_cwd_dir(name):
    """'-Users-bytedance-foo' -> '/Users/bytedance/foo'."""
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return name.replace("-", "/")
