from __future__ import annotations

import logging
import math
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyogrio
from pyproj import CRS, Transformer
from shapely import transform as shapely_transform
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity


logger = logging.getLogger(__name__)
SUPPORTED_GEOMETRY_TYPES = {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000


def normalize_crs(value: Any) -> str:
    if value is None:
        return "Unknown"
    try:
        crs = CRS.from_user_input(value)
        crs_string = crs.to_string()
        if crs_string.upper().startswith("EPSG:"):
            return crs_string
        epsg_code = crs.to_epsg()
        if epsg_code is not None:
            return f"EPSG:{epsg_code}"
        return crs.name or "Unknown"
    except Exception:
        return getattr(value, "name", None) or "Unknown"


def _to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, str) and value == "NaT":
        return None
    if str(value) == "NaT":
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return value


def _is_zip_junk(path: Path) -> bool:
    return any(part.upper() == "__MACOSX" for part in path.parts) or path.name.startswith("._")


def _find_shapefiles(base_dir: Path) -> list[Path]:
    candidates = sorted(
        (
            path
            for path in base_dir.rglob("*")
            if path.is_file() and path.suffix.lower() == ".shp" and not _is_zip_junk(path)
        ),
        key=lambda path: path.as_posix().casefold(),
    )
    valid_shapefiles = []
    missing_sidecars = []
    for shp_file in candidates:
        sibling_names = {path.name.casefold() for path in shp_file.parent.iterdir() if path.is_file()}
        stem = shp_file.stem.casefold()
        missing = [suffix for suffix in (".shx", ".dbf") if f"{stem}{suffix}" not in sibling_names]
        if missing:
            missing_sidecars.append(f"{shp_file.name} (missing {', '.join(missing)})")
        else:
            valid_shapefiles.append(shp_file)
    if not valid_shapefiles:
        if missing_sidecars:
            raise ValueError(f"Shapefile is missing required sidecar file(s): {'; '.join(missing_sidecars)}.")
        raise ValueError("The ZIP archive does not contain a valid shapefile (.shp with matching .shx and .dbf files).")
    return valid_shapefiles


def _extract_shapefile_zip(zip_path: Path) -> Path:
    extract_dir = zip_path.parent / f"{zip_path.stem}-contents"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise ValueError(f"ZIP archive contains too many files (maximum {MAX_ZIP_MEMBERS}).")
        total_uncompressed_size = sum(member.file_size for member in members)
        if total_uncompressed_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(
                f"ZIP archive exceeds the maximum uncompressed size of {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB."
            )
        root = extract_dir.resolve()
        for member in members:
            target = (extract_dir / member.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError("ZIP archive contains an unsafe file path.")
        archive.extractall(extract_dir)
    return extract_dir


def _get_projected_crs(source_crs: str | None, geometry: BaseGeometry) -> CRS:
    if not source_crs or source_crs == "Unknown":
        raise ValueError("CRS unknown, cannot select a projected CRS")
    source = CRS.from_user_input(source_crs)
    centroid = geometry.centroid
    if source.is_geographic:
        lon, lat = centroid.x, centroid.y
    else:
        lon, lat = Transformer.from_crs(source, CRS.from_epsg(4326), always_xy=True).transform(
            centroid.x,
            centroid.y,
        )

    if lat > 84:
        return CRS.from_epsg(3413)
    if lat < -80:
        return CRS.from_epsg(3031)
    utm_zone = max(1, min(60, int((lon + 180) // 6) + 1))
    epsg = 32600 + utm_zone if lat >= 0 else 32700 + utm_zone
    return CRS.from_epsg(epsg)


def _project_geometry(geometry: BaseGeometry, source_crs: str | None) -> tuple[BaseGeometry, CRS]:
    if not source_crs or source_crs == "Unknown":
        raise ValueError("CRS unknown, cannot compute measurements")
    projected_crs = _get_projected_crs(source_crs, geometry)
    source = CRS.from_user_input(source_crs)
    if source == projected_crs:
        return geometry, projected_crs
    transformer = Transformer.from_crs(source, projected_crs, always_xy=True)

    def project_coordinates(coordinates: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coordinates[:, 0], coordinates[:, 1])
        projected = np.column_stack((x, y))
        if coordinates.shape[1] > 2:
            projected = np.column_stack((projected, coordinates[:, 2:]))
        return projected

    # Shapely's array transform keeps coordinate dimensions while transforming x/y.
    projected = shapely_transform(geometry, project_coordinates)
    return projected, projected_crs


def _measurement_for_feature(geometry: BaseGeometry, source_crs: str | None) -> dict:
    if geometry is None or geometry.is_empty:
        return {"measurement_status": "unsupported", "note": "Empty geometry"}

    geometry_type = geometry.geom_type
    if geometry_type in {"Point", "MultiPoint"}:
        return {"measurement_status": "not_applicable", "note": "No measurement is required for point geometries"}
    if geometry_type not in SUPPORTED_GEOMETRY_TYPES:
        return {"measurement_status": "unsupported", "note": f"Geometry type '{geometry_type}' is not supported for measurement"}
    if not geometry.is_valid:
        return {
            "measurement_status": "unsupported",
            "note": f"Invalid geometry: {explain_validity(geometry)}",
        }
    if not source_crs or source_crs == "Unknown":
        return {"measurement_status": "unsupported", "note": "CRS unknown, cannot compute measurements"}

    try:
        projected_geom, projected_crs = _project_geometry(geometry, source_crs)
    except Exception:
        logger.exception("Failed to project geometry for measurement")
        return {
            "measurement_status": "failed",
            "note": "Could not project geometry to a metric CRS; no measurement was calculated.",
        }
    unit_factor = projected_crs.axis_info[0].unit_conversion_factor
    if geometry_type in {"Polygon", "MultiPolygon"}:
        area = projected_geom.area * unit_factor**2
        return {
            "measurement_status": "computed",
            "area_m2": round(float(area), 3),
            "length_m": None,
            "projected_crs": projected_crs.to_string(),
        }

    if geometry_type in {"LineString", "MultiLineString"}:
        length = projected_geom.length * unit_factor
        return {
            "measurement_status": "computed",
            "area_m2": None,
            "length_m": round(float(length), 3),
            "projected_crs": projected_crs.to_string(),
        }

    return {"measurement_status": "unsupported", "note": f"Geometry type '{geometry_type}' is not supported for measurement"}


def process_uploaded_file(file_path: Path, original_filename: str) -> dict[str, Any]:
    filename = (original_filename or "").lower()
    if filename.endswith(".zip"):
        extracted_dir = _extract_shapefile_zip(file_path)
        shapefile_paths = _find_shapefiles(extracted_dir)
        try:
            datasets = [pyogrio.read_dataframe(str(path)) for path in shapefile_paths]
        except (OSError, ValueError, pyogrio.errors.DataSourceError) as exc:
            raise ValueError("Unable to read the Shapefile data; verify the archive contents.") from exc
    elif filename.endswith(".kml"):
        try:
            layers = pyogrio.list_layers(str(file_path))
            datasets = [pyogrio.read_dataframe(str(file_path), layer=str(layer[0])) for layer in layers]
        except (OSError, ValueError, pyogrio.errors.DataSourceError) as exc:
            raise ValueError("Unable to read the KML data; verify the file contents.") from exc
        if not datasets:
            raise ValueError("The KML file does not contain any readable layers.")
    else:
        raise ValueError("Unsupported file type. Please upload a .kml file or a .zip containing a shapefile.")

    features: list[dict[str, Any]] = []
    dataset_crss = [normalize_crs(dataset.crs) for dataset in datasets]
    result_crs = dataset_crss[0] if len(set(dataset_crss)) == 1 else "Mixed"

    for dataset in datasets:
        source_crs = normalize_crs(dataset.crs)
        for row in dataset.itertuples(index=False, name=None):
            properties = dict(zip(dataset.columns, row))
            geometry = properties.pop("geometry")
            properties = _to_jsonable(properties)
            geometry_type = geometry.geom_type if geometry is not None else "None"
            feature = {
                "feature_id": len(features),
                "geometry_type": geometry_type,
                "crs": source_crs,
                "geometry": _to_jsonable(geometry.__geo_interface__) if geometry is not None and not geometry.is_empty else None,
                "properties": properties,
            }
            if geometry is None or geometry.is_empty:
                measurement = {"measurement_status": "unsupported", "note": "Empty geometry"}
            else:
                measurement = _measurement_for_feature(geometry, source_crs)
            feature.update(measurement)
            features.append(feature)

    return {
        "filename": original_filename,
        "feature_count": len(features),
        "crs": result_crs,
        "status": "COMPLETED",
        "features": features,
    }
