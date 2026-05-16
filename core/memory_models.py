from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class MemorySearchResult:
    memory_id: str
    source_type: str
    created_at: str
    summary: str
    preview: Optional[str] = None
    score: Optional[int] = None
    confidence: Optional[str] = None
    rank_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)
