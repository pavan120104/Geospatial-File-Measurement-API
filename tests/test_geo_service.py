import zipfile

import geopandas as gpd
import numpy as np
import pytest
from pyproj import CRS, Transformer
from shapely import transform as shapely_transform
from shapely.geometry import GeometryCollection, LineString, MultiPoint, MultiPolygon, Point, Polygon

from app.services import geo_service
from app.services.geo_service import (
    _get_projected_crs,
    _measurement_for_feature,
    _project_geometry,
    normalize_crs,
)


def test_geographic_polygon_is_projected_before_area_calculation():
    polygon = Polygon(
        [
            (77.5, 12.9),
            (77.51, 12.9),
            (77.51, 12.91),
            (77.5, 12.91),
            (77.5, 12.9),
        ]
    )

    result = _measurement_for_feature(polygon, "EPSG:4326")

    assert result["measurement_status"] == "computed"
    assert abs(result["area_m2"] - 1_201_853) / 1_201_853 < 0.01
    assert result["area_m2"] > 100_000


def test_web_mercator_polygon_uses_local_utm_area():
    geographic = Polygon(
        [
            (77.5, 12.9),
            (77.51, 12.9),
            (77.51, 12.91),
            (77.5, 12.91),
            (77.5, 12.9),
        ]
    )
    to_web_mercator = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    web_mercator = shapely_transform(
        geographic,
        lambda coordinates: np.column_stack(
            to_web_mercator.transform(coordinates[:, 0], coordinates[:, 1])
        ),
    )

    result = _measurement_for_feature(web_mercator, "EPSG:3857")

    assert result["measurement_status"] == "computed"
    assert abs(result["area_m2"] - 1_201_853) / 1_201_853 < 0.01


def test_geographic_linestring_is_projected_before_length_calculation():
    line = LineString([(77.5, 12.9), (77.51, 12.91)])

    result = _measurement_for_feature(line, "EPSG:4326")

    assert result["measurement_status"] == "computed"
    assert 1_500 < result["length_m"] < 1_600


@pytest.mark.parametrize("geometry", [Point(77.5, 12.9), MultiPoint([(77.5, 12.9)])])
def test_points_are_not_applicable(geometry):
    result = _measurement_for_feature(geometry, "EPSG:4326")

    assert result["measurement_status"] == "not_applicable"
    assert result["note"]


def test_unknown_crs_and_other_geometry_types_are_unsupported():
    polygon = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])

    unknown_crs = _measurement_for_feature(polygon, "Unknown")
    unsupported_geometry = _measurement_for_feature(GeometryCollection([Point(0, 0)]), "EPSG:4326")

    assert unknown_crs["measurement_status"] == "unsupported"
    assert unknown_crs["note"] == "CRS unknown, cannot compute measurements"
    assert unsupported_geometry["measurement_status"] == "unsupported"


def test_empty_geometry_is_unsupported():
    result = _measurement_for_feature(Polygon(), "EPSG:4326")

    assert result == {"measurement_status": "unsupported", "note": "Empty geometry"}


def test_projection_failure_is_reported_without_returning_raw_measurement(monkeypatch):
    def fail_projection(*args, **kwargs):
        raise RuntimeError("transform unavailable")

    monkeypatch.setattr(geo_service, "_project_geometry", fail_projection)
    result = _measurement_for_feature(
        Polygon([(77.5, 12.9), (77.51, 12.9), (77.51, 12.91), (77.5, 12.9)]),
        "EPSG:4326",
    )

    assert result["measurement_status"] == "failed"
    assert result["note"] == "Could not project geometry to a metric CRS; no measurement was calculated."
    assert "area_m2" not in result


def test_projected_crs_in_feet_is_converted_to_meters():
    polygon = Polygon(
        [
            (1_000_000, 200_000),
            (1_000_100, 200_000),
            (1_000_100, 200_100),
            (1_000_000, 200_100),
        ]
    )

    result = _measurement_for_feature(polygon, "EPSG:2263")

    assert result["measurement_status"] == "computed"
    assert result["area_m2"] == pytest.approx(929.034, rel=2e-2)


def test_invalid_bow_tie_polygon_is_not_measured():
    bow_tie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])

    result = _measurement_for_feature(bow_tie, "EPSG:4326")

    assert result["measurement_status"] == "unsupported"
    assert result["note"].startswith("Invalid geometry:")


def test_multipolygon_area_is_measured():
    first = Polygon([(77.5, 12.9), (77.501, 12.9), (77.501, 12.901), (77.5, 12.901)])
    second = Polygon([(77.51, 12.9), (77.511, 12.9), (77.511, 12.901), (77.51, 12.901)])
    result = _measurement_for_feature(MultiPolygon([first, second]), "EPSG:4326")

    assert result["measurement_status"] == "computed"
    assert result["area_m2"] > 20_000


def test_utm_zone_and_polar_selection():
    lon_180_geometry = Point(180, 10)
    southern_geometry = Polygon([(30, -10), (30.1, -10), (30.1, -9.9), (30, -10)])
    north_polar_geometry = Polygon([(0, 85), (1, 85), (1, 85.1), (0, 85)])
    south_polar_geometry = Polygon([(0, -81), (1, -81), (1, -81.1), (0, -81)])

    assert _get_projected_crs("EPSG:4326", lon_180_geometry).to_epsg() == 32660
    assert _get_projected_crs("EPSG:4326", southern_geometry).to_epsg() == 32736
    assert _get_projected_crs("EPSG:4326", north_polar_geometry).to_epsg() == 3413
    assert _get_projected_crs("EPSG:4326", south_polar_geometry).to_epsg() == 3031


def test_normalize_crs_avoids_wkt_string():
    crs = CRS.from_epsg(4326)
    assert normalize_crs(crs) == "EPSG:4326"
    assert normalize_crs(None) == "Unknown"


def test_zip_limits_and_shapefile_sidecars(tmp_path, monkeypatch):
    archive_path = tmp_path / "oversized.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("large.dat", b"12345")
    monkeypatch.setattr(geo_service, "MAX_UNCOMPRESSED_BYTES", 4)

    with pytest.raises(ValueError, match="uncompressed size"):
        geo_service._extract_shapefile_zip(archive_path)

    shapefile_dir = tmp_path / "shape"
    shapefile_dir.mkdir()
    (shapefile_dir / "roads.shp").write_bytes(b"shp")
    with pytest.raises(ValueError, match="missing required sidecar"):
        geo_service._find_shapefiles(shapefile_dir)


def test_processes_every_kml_layer_with_stable_unique_ids(tmp_path, monkeypatch):
    source_path = tmp_path / "two-layers.kml"
    source_path.write_text("<kml />", encoding="utf-8")
    layers = {
        "first": gpd.GeoDataFrame({"name": ["one"]}, geometry=[Point(0, 0)], crs="EPSG:4326"),
        "second": gpd.GeoDataFrame({"name": ["two"]}, geometry=[Point(1, 1)], crs="EPSG:4326"),
    }
    calls = []
    monkeypatch.setattr(geo_service.pyogrio, "list_layers", lambda path: [["first", "Point"], ["second", "Point"]])

    def read_layer(path, layer):
        calls.append(layer)
        return layers[layer]

    monkeypatch.setattr(geo_service.pyogrio, "read_dataframe", read_layer)
    result = geo_service.process_uploaded_file(source_path, "two-layers.kml")

    assert calls == ["first", "second"]
    assert result["feature_count"] == 2
    assert [feature["feature_id"] for feature in result["features"]] == [0, 1]


def test_mixed_shapefile_crs_is_reported_at_file_level(tmp_path):
    archive_path = tmp_path / "mixed-crs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, crs, point in (
            ("geographic", "EPSG:4326", Point(77.5, 12.9)),
            ("webmercator", "EPSG:3857", Point(8_628_000, 1_450_000)),
        ):
            directory = tmp_path / name
            directory.mkdir()
            path = directory / f"{name}.shp"
            gpd.GeoDataFrame({"name": [name]}, geometry=[point], crs=crs).to_file(path)
            for sidecar in directory.iterdir():
                archive.write(sidecar, f"{name}/{sidecar.name}")

    result = geo_service.process_uploaded_file(archive_path, "mixed-crs.zip")

    assert result["crs"] == "Mixed"
    assert [feature["crs"] for feature in result["features"]] == ["EPSG:4326", "EPSG:3857"]
