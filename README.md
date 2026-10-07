# Geospatial File Measurement API

A FastAPI service for processing KML files and ZIP archives containing one or more Shapefiles. It returns file metadata and per-feature geometry, attributes, CRS, and metric area or length where supported.

## Setup

Python 3.14 was used to verify the pinned dependencies in `requirements.txt`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Run the API:

```powershell
python -m uvicorn app.main:app --reload
```

The API is served at `http://127.0.0.1:8000`. Interactive OpenAPI documentation is available at `/docs`.

Run all tests:

```powershell
python -m pytest -q
```

## API

Uploads are limited to 50 MiB. ZIP archives are limited to 10,000 members and 200 MiB total uncompressed content.

### Upload and process a file

`POST /api/files/` accepts multipart form data with a `file` field. Supported extensions are `.kml` and `.zip` (containing Shapefile `.shp`, `.shx`, and `.dbf` files).

```powershell
curl.exe -F "file=@survey.kml" http://127.0.0.1:8000/api/files/
curl.exe -F "file=@survey.zip" http://127.0.0.1:8000/api/files/
```

Example response:

```json
{
  "id": "bf35070b342e4c85a765c5b4",
  "filename": "survey.kml",
  "feature_count": 2,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "uploaded_at": "2026-10-07T16:00:00+00:00"
}
```

Invalid extensions, empty files, corrupt archives, and unreadable geospatial data return HTTP 400. Uploads over the limit return HTTP 413.

### Get file information

`GET /api/files/{id}/` returns upload metadata.

```powershell
curl.exe http://127.0.0.1:8000/api/files/bf35070b342e4c85a765c5b4/
```

The response uses the same metadata shape as the upload response. Unknown IDs return HTTP 404.

### Get per-feature measurements

`GET /api/files/{id}/measurements/` returns feature IDs, geometry types and geometries, input CRS, properties, and measurements.

```powershell
curl.exe http://127.0.0.1:8000/api/files/bf35070b342e4c85a765c5b4/measurements/
```

Example response:

```json
{
  "file_id": "bf35070b342e4c85a765c5b4",
  "filename": "survey.kml",
  "feature_count": 2,
  "crs": "EPSG:4326",
  "measurements": [
    {
      "feature_id": 0,
      "geometry_type": "Polygon",
      "crs": "EPSG:4326",
      "geometry": {"type": "Polygon", "coordinates": [[[77.5, 12.9], [77.51, 12.9], [77.51, 12.91], [77.5, 12.91], [77.5, 12.9]]]},
      "properties": {"Name": "Survey area"},
      "measurement_status": "computed",
      "area_m2": 1201853.0,
      "length_m": null,
      "note": null
    },
    {
      "feature_id": 1,
      "geometry_type": "Point",
      "crs": "EPSG:4326",
      "geometry": {"type": "Point", "coordinates": [77.5, 12.9]},
      "properties": {"Name": "Control point"},
      "measurement_status": "not_applicable",
      "area_m2": null,
      "length_m": null,
      "note": "No measurement is required for point geometries"
    }
  ]
}
```

Polygons and multipolygons include `area_m2`; lines and multilines include `length_m`. Point geometries have status `not_applicable`. Other unsupported, empty, or unknown-CRS geometries have status `unsupported`; a transformation error is reported as `failed`. Unknown IDs return HTTP 404.

## Architecture

### Project structure

```text
app/
├── main.py                  # FastAPI routes and bounded temporary upload handling
├── models.py                # Pydantic response models
├── storage.py               # In-memory metadata and measurement store
└── services/
    └── geo_service.py       # KML/Shapefile reading, validation, and measurement
tests/
├── test_api.py              # Endpoint and file-processing integration tests
└── test_geo_service.py      # Projection and geometry unit tests
```

### File-processing flow

1. FastAPI validates the filename extension, then reads the upload in bounded chunks into a temporary directory.
2. KML layers are enumerated and read individually. A ZIP is checked for member count, total uncompressed size, and unsafe paths before extraction; all valid Shapefiles are processed. macOS metadata entries are ignored, and each Shapefile must include matching `.shx` and `.dbf` files.
3. Features (including null or empty geometries) are assigned sequential IDs across the layers and retain their properties and source CRS.
4. Parsed output is stored in memory. Temporary upload and extraction files are removed when processing ends.

### Measurement flow and CRS handling

Each supported feature is measured independently. Polygon area and line length use Shapely after projection; the corresponding projected CRS is selected per feature. The feature centroid is first transformed to longitude/latitude when needed, then used to select a UTM zone (clamped to 1–60) regardless of the input CRS. Polar stereographic EPSG:3413 / EPSG:3031 is used outside UTM latitude coverage. This avoids relying on distorted projected coordinates such as Web Mercator for measurements. The transform changes x/y coordinates and preserves any extra coordinate dimensions. Line lengths are two-dimensional ground lengths; altitude/elevation is preserved in output geometry but excluded from measurement.

Coordinates with unknown CRS are never measured. Invalid polygon geometries and unsupported geometry collections are reported without measurement. Projection failures are logged and reported on the affected feature; they do not produce degree-based values labelled as meters.

## Design decisions

- **Per-feature UTM:** selecting a local projected CRS from each feature centroid is straightforward and suitable for local surveys. It is less appropriate for very large geometries, geometries spanning zones, or highly precise geodesic requirements.
- **Alternatives:** `pyproj.Geod` can calculate geodesic lengths and areas directly on an ellipsoid; an equal-area projection may be a better choice for regional area comparisons. Neither is selectable in this initial API.
- **Geometry limitations:** KML multi-geometries that GDAL exposes as `GeometryCollection` are currently reported as unsupported rather than measuring their component geometries. The polar stereographic choices are suitable for polar work but are not universal replacements for a purpose-specific projection.
- **Storage:** parsed results are held in process memory, so they disappear when the process restarts and are not shared between multiple workers. A database/PostGIS store is a future improvement.
- **Request processing:** the upload route is synchronous, allowing FastAPI to run blocking parsing work in its worker threadpool. Processing is still bounded to the request and is not a durable background job.
- **Resource limits:** the 50 MiB upload cap, 10,000-member cap, and 200 MiB expanded ZIP cap reduce accidental or malicious resource consumption. They are fixed constants in `app/main.py` and `app/services/geo_service.py`.
- **Temporary files:** upload and extraction data are kept in a temporary directory and cleaned after each request; files are not retained for later reprocessing.

## Learning and future scope

This project demonstrates end-to-end geospatial upload handling, feature extraction, CRS-aware geometry projection, measurement, input limits, and testable API design.

Known limitations and possible next steps:

- persist results in a database or PostGIS so IDs survive restarts and work across workers
- move long-running jobs to a durable asynchronous worker for larger datasets
- paginate measurement responses for files with many features
- allow clients to select units or opt into a geodesic measurement strategy
- add authentication and authorization before exposing the service beyond local development
- consider projection choice and antimeridian-crossing geometries more carefully for global datasets
