from __future__ import annotations

from pathlib import Path
from typing import List


def tail_lines(path: str | Path, limit: int = 100) -> List[str]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    if limit <= 0:
        return []
    content = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return content[-limit:]
