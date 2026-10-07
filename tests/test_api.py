from __future__ import annotations

import io
import zipfile

import geopandas as gpd
from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from app import main
from app.main import app

client = TestClient(app)


def _kml(contents: str) -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>{contents}</Document>
</kml>
'''.encode()


def _placemark(name: str, geometry: str) -> str:
    return f"<Placemark><name>{name}</name>{geometry}</Placemark>"


def _polygon(coords: str) -> str:
    return f"""<Polygon><outerBoundaryIs><LinearRing>
      <coordinates>{coords}</coordinates>
    </LinearRing></outerBoundaryIs></Polygon>"""


def _make_shapefile_zip(tmp_path, *, include_prj: bool = True) -> bytes:
    shapefile_dir = tmp_path / "shape"
    shapefile_dir.mkdir()
    frame = gpd.GeoDataFrame(
        {"name": ["parcel"]},
        geometry=[Polygon([(77.5, 12.9), (77.51, 12.9), (77.51, 12.91), (77.5, 12.91)])],
        crs="EPSG:4326" if include_prj else None,
    )
    shapefile_path = shapefile_dir / "survey.shp"
    frame.to_file(shapefile_path, driver="ESRI Shapefile")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path in shapefile_dir.iterdir():
            if include_prj or path.suffix.lower() != ".prj":
                archive.write(path, path.name)
    return output.getvalue()


def _upload(filename: str, contents: bytes):
    return client.post("/api/files/", files={"file": (filename, contents, "application/octet-stream")})


def test_kml_upload_detail_measurements_and_unknown_ids():
    polygon = _placemark(
        "Parcel",
        _polygon("77.5,12.9,0 77.51,12.9,0 77.51,12.91,0 77.5,12.91,0 77.5,12.9,0"),
    )
    line = _placemark("Road", "<LineString><coordinates>77.5,12.9 77.51,12.91</coordinates></LineString>")
    response = _upload("survey.kml", _kml(polygon + line))

    assert response.status_code == 200, response.text
    metadata = response.json()
    assert metadata["id"]
    assert metadata["filename"] == "survey.kml"
    assert metadata["feature_count"] == 2
    assert metadata["crs"] == "EPSG:4326"
    assert metadata["status"] == "COMPLETED"

    details = client.get(f"/api/files/{metadata['id']}/")
    measurements_response = client.get(f"/api/files/{metadata['id']}/measurements/")
    assert details.status_code == 200
    assert details.json()["filename"] == "survey.kml"
    assert measurements_response.status_code == 200
    measurements = measurements_response.json()["measurements"]
    assert len(measurements) == 2
    assert measurements[0]["area_m2"] is not None
    assert measurements[1]["length_m"] is not None

    assert client.get("/api/files/not-a-real-id/").status_code == 404
    assert client.get("/api/files/not-a-real-id/measurements/").status_code == 404


def test_zipped_shapefile_upload_and_mac_junk_is_ignored(tmp_path):
    contents = _make_shapefile_zip(tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(contents)) as source, zipfile.ZipFile(output, "w") as target:
        for member in source.infolist():
            target.writestr(member.filename, source.read(member.filename))
        target.writestr("__MACOSX/._x.shp", b"junk")
        target.writestr("._x.shp", b"junk")

    response = _upload("survey.zip", output.getvalue())
    assert response.status_code == 200, response.text
    metadata = response.json()
    assert metadata["feature_count"] == 1
    assert metadata["crs"] == "EPSG:4326"
    measurements = client.get(f"/api/files/{metadata['id']}/measurements/").json()["measurements"]
    assert measurements[0]["measurement_status"] == "computed"
    assert measurements[0]["area_m2"] > 1_000_000


def test_unknown_crs_shapefile_does_not_measure(tmp_path):
    response = _upload("unknown-crs.zip", _make_shapefile_zip(tmp_path, include_prj=False))
    assert response.status_code == 200, response.text
    payload = client.get(f"/api/files/{response.json()['id']}/measurements/").json()
    measurement = payload["measurements"][0]
    assert payload["crs"] == "Unknown"
    assert measurement["measurement_status"] == "unsupported"
    assert measurement["note"] == "CRS unknown, cannot compute measurements"


def test_mixed_kml_keeps_point_and_empty_features():
    polygon = _placemark(
        "Polygon",
        _polygon("77.5,12.9 77.51,12.9 77.51,12.91 77.5,12.91 77.5,12.9"),
    )
    line = _placemark("Line", "<LineString><coordinates>77.5,12.9 77.51,12.91</coordinates></LineString>")
    point = _placemark("Point", "<Point><coordinates>77.5,12.9</coordinates></Point>")
    empty = "<Placemark><name>Empty</name></Placemark>"
    response = _upload("mixed.kml", _kml(polygon + line + point + empty))

    assert response.status_code == 200, response.text
    measurements = client.get(f"/api/files/{response.json()['id']}/measurements/").json()["measurements"]
    assert len(measurements) == 4
    statuses = {item["geometry_type"]: item["measurement_status"] for item in measurements}
    assert statuses["Polygon"] == "computed"
    assert statuses["LineString"] == "computed"
    assert statuses["Point"] == "not_applicable"
    empty_feature = next(item for item in measurements if item["geometry_type"] == "None")
    assert empty_feature["measurement_status"] == "unsupported"
    assert empty_feature["geometry"] is None


def test_kml_features_from_multiple_folders_are_returned():
    first = _placemark("First", "<Point><coordinates>77.5,12.9</coordinates></Point>")
    second = _placemark("Second", "<Point><coordinates>77.6,12.9</coordinates></Point>")
    folders = f"<Folder><name>One</name>{first}</Folder><Folder><name>Two</name>{second}</Folder>"
    response = _upload("folders.kml", _kml(folders))

    assert response.status_code == 200, response.text
    assert response.json()["feature_count"] == 2


def test_kml_timestamp_property_is_json_serializable():
    placemark = """<Placemark>
      <name>Timed point</name>
      <TimeStamp><when>2024-06-15T12:30:00Z</when></TimeStamp>
      <Point><coordinates>77.5,12.9</coordinates></Point>
    </Placemark>"""
    response = _upload("timestamp.kml", _kml(placemark))

    assert response.status_code == 200, response.text
    measurements = client.get(f"/api/files/{response.json()['id']}/measurements/").json()["measurements"]
    timestamp_value = measurements[0]["properties"]["timestamp"]
    assert timestamp_value is not None
    assert "2024-06-15" in timestamp_value


def test_invalid_uploads_return_bad_request():
    assert _upload("notes.txt", b"not geospatial").status_code == 400
    assert _upload("empty.kml", b"").status_code == 400
    assert _upload("broken.zip", b"not a zip archive").status_code == 400
    unreadable_kml = _upload("broken.kml", b"not KML data")
    assert unreadable_kml.status_code == 400
    assert "Unable to read" in unreadable_kml.json()["detail"]

    empty_zip = io.BytesIO()
    with zipfile.ZipFile(empty_zip, "w") as archive:
        archive.writestr("readme.txt", "no shapefile")
    missing_shp = _upload("missing.zip", empty_zip.getvalue())
    assert missing_shp.status_code == 400


def test_upload_limit_returns_413(monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 8)
    response = _upload("large.kml", b"123456789")
    assert response.status_code == 413
