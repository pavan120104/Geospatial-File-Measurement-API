from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
import zipfile

from fastapi import FastAPI, File, HTTPException, UploadFile
import pyogrio

from app.models import FeatureMeasurement, FileMetadata, MeasurementSummary
from app.services.geo_service import process_uploaded_file
from app.storage import FileRecord, InMemoryFileStore

app = FastAPI(title="Geospatial File Measurement API", version="1.0.0")

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
FILE_STORE = InMemoryFileStore()


def _build_file_response(record: FileRecord) -> FileMetadata:
    return FileMetadata(
        id=record.id,
        filename=record.filename,
        feature_count=record.feature_count,
        crs=record.crs,
        status=record.status,
        uploaded_at=record.uploaded_at,
    )


def _build_measurements_response(record: FileRecord) -> MeasurementSummary:
    feature_measurements = [
        FeatureMeasurement(
            feature_id=item["feature_id"],
            geometry_type=item["geometry_type"],
            crs=item["crs"],
            geometry=item["geometry"],
            properties=item["properties"],
            measurement_status=item.get("measurement_status", "unsupported"),
            area_m2=item.get("area_m2"),
            length_m=item.get("length_m"),
            note=item.get("note"),
        )
        for item in record.features
    ]
    return MeasurementSummary(
        file_id=record.id,
        filename=record.filename,
        feature_count=record.feature_count,
        crs=record.crs,
        measurements=feature_measurements,
    )


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/files/", response_model=FileMetadata)
def upload_geospatial_file(file: UploadFile = File(...)) -> FileMetadata:
    if file.filename is None:
        raise HTTPException(status_code=400, detail="A file is required.")

    name_lower = file.filename.lower()
    if not (name_lower.endswith(".kml") or name_lower.endswith(".zip")):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Please upload a .kml file or a .zip containing a shapefile.",
        )

    record_id = uuid.uuid4().hex
    suffix = ".kml" if name_lower.endswith(".kml") else ".zip"
    with tempfile.TemporaryDirectory(prefix="geo-measure-") as temporary_directory:
        file_path = Path(temporary_directory) / f"{record_id}{suffix}"
        total_bytes = 0
        with file_path.open("wb") as destination:
            while True:
                remaining_bytes = MAX_UPLOAD_BYTES - total_bytes
                chunk = file.file.read(min(UPLOAD_CHUNK_BYTES, remaining_bytes + 1))
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Uploaded file exceeds the {MAX_UPLOAD_BYTES}-byte limit.",
                    )
                destination.write(chunk)

        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        try:
            parsed = process_uploaded_file(file_path, file.filename)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail="Invalid or corrupt ZIP file.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except pyogrio.errors.DataSourceError as exc:
            raise HTTPException(status_code=400, detail="Unable to read the KML or Shapefile data.") from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail="Unexpected file processing failure.") from exc

    record = FileRecord(
        id=record_id,
        filename=file.filename,
        feature_count=parsed["feature_count"],
        crs=parsed["crs"],
        status=parsed["status"],
        uploaded_at=FILE_STORE.utc_now(),
        features=parsed["features"],
    )
    FILE_STORE.add(record)
    return _build_file_response(record)


@app.get("/api/files/{record_id}/", response_model=FileMetadata)
def get_file_details(record_id: str) -> FileMetadata:
    record = FILE_STORE.get(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return _build_file_response(record)


@app.get("/api/files/{record_id}/measurements/", response_model=MeasurementSummary)
def get_measurements(record_id: str) -> MeasurementSummary:
    record = FILE_STORE.get(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return _build_measurements_response(record)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
