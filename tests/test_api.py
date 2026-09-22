"""End-to-end HTTP tests exercising the full workbench flow.

Uses ``httpx.ASGITransport`` with an async client (the installed Starlette
``TestClient`` is incompatible with the httpx version in this environment).
"""
from __future__ import annotations

import io
import os
import tempfile
import zipfile

import httpx
import pydicom
import pytest
import pytest_asyncio
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

_TMP = tempfile.mkdtemp(prefix="dicombench-test-")
os.environ["DICOMBENCH_DATA_DIR"] = _TMP

from app.main import app  # noqa: E402


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        yield c


def make_bytes() -> bytes:
    sop, study, series = generate_uid(), generate_uid(), generate_uid()
    buf = io.BytesIO()
    meta = Dataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("in.dcm", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = study
    ds.SeriesInstanceUID = series
    ds.PatientName = "Wang^Wu"
    ds.PatientID = "PID-9001"
    ds.PatientBirthDate = "19750505"
    ds.PatientSex = "F"
    ds.Modality = "CT"
    ds.AccessionNumber = "ACC-1"
    ds.StudyDescription = "头颅 CT"
    ds.StudyDate = "20240102"
    ds.StudyTime = "093000"
    ds.StationName = "ST-1"
    ds.Rows = ds.Columns = 2
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = b"\0" * 8
    block = ds.private_block(0x0009, "VENDOR", create=True)
    block.add_new(0x10, "LO", "PHI-secret")
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


@pytest_asyncio.fixture
async def job(client):
    r = await client.post(
        "/api/jobs",
        files=[
            ("files", ("study/a.dcm", make_bytes(), "application/dicom")),
            ("files", ("study/b.dcm", make_bytes(), "application/dicom")),
            ("files", ("notes.txt", b"not dicom", "text/plain")),
        ],
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_health_and_profiles(client):
    assert (await client.get("/api/health")).json()["status"] == "ok"
    prof = (await client.get("/api/profiles")).json()
    assert prof["default"] in prof["profiles"]
    assert {"basic", "recommended", "strict", "light"} <= set(prof["profiles"])


@pytest.mark.asyncio
async def test_index_served(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "DICOM" in r.text


@pytest.mark.asyncio
async def test_preview_reports_changes(client, job):
    r = await client.post(f"/api/jobs/{job['job_id']}/preview",
                          json={"profile": "strict"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["counts"]["files"] == 2
    assert data["counts"]["skipped"] == 1
    actions = {c["action"] for c in data["changes"]}
    assert "uid_remapped" in actions
    assert "pseudonymised" in actions
    pn = [c for c in data["changes"] if c["tag"] == "(0010,0010)"]
    assert pn and pn[0]["new"].startswith("ANON^")
    date_changes = [c for c in data["changes"] if c["tag"] == "(0008,0020)"]
    assert date_changes and date_changes[0]["new"] == ""
    assert data["counts"]["private_removed"] >= 2
    skipped = [f for f in data["files"] if f["source_path"] == "notes.txt"]
    assert skipped and skipped[0]["error"]


@pytest.mark.asyncio
async def test_process_and_download_zip(client, job):
    r = await client.post(f"/api/jobs/{job['job_id']}/process",
                          json={"profile": "recommended"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "done"

    z = await client.get(data["download_url"])
    assert z.status_code == 200
    assert z.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    names = zf.namelist()
    assert "anonymization-report.json" in names
    dicoms = [n for n in names if n.endswith(".dcm")]
    assert len(dicoms) == 2
    parts = dicoms[0].split("/")
    assert len(parts) == 3 and parts[2].startswith("2.25.")

    ds = pydicom.dcmread(io.BytesIO(zf.read(dicoms[0])))
    assert str(ds.PatientName).startswith("ANON^")
    assert ds.PatientIdentityRemoved == "YES"
    assert str(ds.StudyDate) != "20240102"
    assert len(ds.StudyDate) == 8  # shifted, not blanked (recommended)
    report = zf.read("anonymization-report.json").decode()
    assert "Wang" not in report and "PID-9001" not in report
    assert b"PHI-secret" not in zf.read(dicoms[0])


@pytest.mark.asyncio
async def test_unknown_job_404(client):
    r = await client.get("/api/jobs/does-not-exist/status")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_override_options_merge(client, job):
    # basic preset blanks dates; the explicit keep_dates override shifts them
    r = await client.post(f"/api/jobs/{job['job_id']}/preview",
                          json={"profile": "basic", "keep_dates": True})
    assert r.status_code == 200, r.text
    changed = [c for c in r.json()["changes"] if c["tag"] == "(0008,0020)"]
    assert changed and changed[0]["action"] == "shifted"


@pytest.mark.asyncio
async def test_preset_followed_without_overrides(client, job):
    # recommended preset keeps (shifts) dates without an explicit override
    r = await client.post(f"/api/jobs/{job['job_id']}/preview",
                          json={"profile": "recommended"})
    assert r.status_code == 200, r.text
    changed = [c for c in r.json()["changes"] if c["tag"] == "(0008,0020)"]
    assert changed and changed[0]["action"] == "shifted"
