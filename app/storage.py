from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List


@dataclass
class FileRecord:
    id: str
    filename: str
    feature_count: int
    crs: str
    status: str
    uploaded_at: str
    features: List[dict] = field(default_factory=list)


class InMemoryFileStore:
    def __init__(self) -> None:
        self._records: Dict[str, FileRecord] = {}

    def add(self, record: FileRecord) -> FileRecord:
        self._records[record.id] = record
        return record

    def get(self, record_id: str) -> FileRecord | None:
        return self._records.get(record_id)

    def list(self) -> List[FileRecord]:
        return list(self._records.values())

    @staticmethod
    def utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()
