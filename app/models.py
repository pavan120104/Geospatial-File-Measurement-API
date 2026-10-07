from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class FileMetadata(BaseModel):
    id: str
    filename: str
    feature_count: int
    crs: str
    status: str
    uploaded_at: str


class FeatureMeasurement(BaseModel):
    feature_id: int
    geometry_type: str
    crs: str
    geometry: Optional[Dict[str, Any]]
    properties: Dict[str, Any]
    measurement_status: str
    area_m2: Optional[float] = None
    length_m: Optional[float] = None
    note: Optional[str] = None


class MeasurementSummary(BaseModel):
    file_id: str
    filename: str
    feature_count: int
    crs: str
    measurements: List[FeatureMeasurement] = Field(default_factory=list)
