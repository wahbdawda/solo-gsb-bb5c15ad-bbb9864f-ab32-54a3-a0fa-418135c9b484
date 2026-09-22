"""Tests for the residual identifier gate ("残留标识门禁")."""
from __future__ import annotations

import json

import pydicom
import pytest
from pydicom.dataset import Dataset

from app.dicom_catalog import PROFILES
from app.residue_gate import (
    ORIGINAL_UID,
    PATIENT_ID,
    PATIENT_NAME,
    GateBlocked,
    IdentifierGate,
    _id_tokens,
    _name_tokens,
)
from app.jobs import Job, run

from test_anonymizer import make_dicom


# ---------------------------------------------------------------------------
# Tokenisation / matching helpers
# ---------------------------------------------------------------------------

def test_name_tokens_split_components_and_words():
    tokens = _name_tokens("Zhang^San")
    assert "zhang" in tokens and "san" in tokens
    assert "zhang san" in tokens


def test_id_tokens_keep_full_value():
    tokens = _id_tokens("PID-001")
    assert "pid-001" in tokens and "001" not in tokens  # digit fragment <4
    assert "pid" in tokens  # 3-letter latin component qualifies


def test_short_fragments_ignored():
    assert _name_tokens("Ab") == set()
    assert _id_tokens("12") == set()


def test_cjk_name_tokens():
    tokens = _name_tokens("张三")
    assert "张三" in tokens


# ---------------------------------------------------------------------------
# Dataset scanning
# ---------------------------------------------------------------------------

def _gate_for(names=("Zhang^San",), ids=("PID-001",), uids=()):
    return IdentifierGate(names, ids, uids)


def test_gate_passes_clean_dataset():
    ds = Dataset()
    ds.PatientName = "ANON^KQML5SMX"
    ds.PatientID = "ANON-ABCDEF"
    ds.add_new(pydicom.tag.Tag(0x0018, 0x9424), "LO", "clean study notes")
    result = _gate_for().scan_in_memory([("x.dcm", ds)], "{}")
    assert result.passed is True
    assert result.exact_hit_count == 0
    assert result.scanned_dicom_files == 1


def test_gate_detects_patient_name_in_text_tag():
    ds = Dataset()
    ds.add_new(pydicom.tag.Tag(0x0018, 0x9424), "LO",
               "follow-up for Zhang San")
    result = _gate_for().scan_in_memory([("x.dcm", ds)], "{}")
    assert result.passed is False
    types = {h.hit_type for h in result.hits}
    assert PATIENT_NAME in types


def test_gate_word_boundary_no_false_positive():
    ds = Dataset()
    # "sandy" contains "san" but is a different word -> no hit.
    ds.add_new(pydicom.tag.Tag(0x0018, 0x9424), "LO",
               "sandy beach protocol")
    result = _gate_for(names=("San",)).scan_in_memory([("x.dcm", ds)], "{}")
    assert result.passed is True


def test_gate_detects_patient_id():
    ds = Dataset()
    ds.add_new(pydicom.tag.Tag(0x0018, 0x9424), "LO",
               "mrn PID-001 recorded")
    result = _gate_for().scan_in_memory([("x.dcm", ds)], "{}")
    assert result.passed is False
    assert any(h.hit_type == PATIENT_ID for h in result.hits)


def test_gate_detects_original_ui_in_sequence():
    original_sop = "1.2.840.113619.2.55.3.604688119.976.1383215123.220"
    ds = Dataset()
    ds.PatientName = "ANON^X"
    ref = Dataset()
    ref.ReferencedSOPInstanceUID = original_sop  # nested sequence, UI VR
    seq_item = Dataset()
    seq_item.ReferencedSOPSequence = pydicom.sequence.Sequence([ref])
    ds.SourceImageSequence = pydicom.sequence.Sequence([seq_item])

    # free-text occurrence deep inside a sequence must be found too; the
    # uncatalogued LO element is reached via recursive SQ traversal.
    ref.add_new(pydicom.tag.Tag(0x0018, 0x9424), "LO",
                f"see {original_sop} please")
    result = _gate_for(uids=(original_sop,)).scan_in_memory(
        [("x.dcm", ds)], "{}")
    assert result.passed is False
    hits = [h for h in result.hits if h.hit_type == ORIGINAL_UID]
    assert len(hits) >= 2
    assert all("SourceImageSequence" in h.location for h in hits)


def test_gate_ignores_preserved_well_known_uid():
    # Synchronisation frame of reference UIDs are intentionally preserved;
    # they are never passed to the gate as a source identifier.
    ds = Dataset()
    ds.FrameOfReferenceUID = "1.2.840.10008.15.1.1"
    result = _gate_for(uids=()).scan_in_memory([("x.dcm", ds)], "{}")
    assert result.passed is True


def test_gate_scans_report_json():
    report = json.dumps({
        "files": [{"source_relative_path": "Zhang^San/IMG001"}],
        "warnings": [],
    })
    result = _gate_for().scan_in_memory([], report)
    assert result.passed is False
    hit = result.hits[0]
    assert hit.file == "anonymization-report.json"
    assert hit.tag.startswith("json:")


def test_gate_clean_report_passes():
    report = json.dumps({"files": [
        {"output_path": "2.25.123/2.25.456/2.25.789.dcm"}]})
    result = _gate_for(uids=("1.2.3.4.5",)).scan_in_memory([], report)
    assert result.passed is True


def test_gate_summary_never_contains_source_values():
    ds = Dataset()
    ds.add_new(pydicom.tag.Tag(0x0018, 0x9424), "LO", "Zhang San")
    result = _gate_for().scan_in_memory([("x.dcm", ds)], "{}")
    summary = json.dumps(result.summary_dict())
    assert "Zhang" not in summary and "San" not in summary
    assert result.exact_hit_count >= 1


# ---------------------------------------------------------------------------
# End-to-end: engine + gate via jobs.run()
# ---------------------------------------------------------------------------

def _make_job(tmp_path, files_data):
    job_id = "testjob01"
    upload_dir = tmp_path / job_id / "in"
    upload_dir.mkdir(parents=True)
    sources, relpaths = [], []
    for rel, data in files_data:
        target = upload_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        sources.append(target)
        relpaths.append(rel)
    return Job(
        id=job_id, created_at=0.0, upload_dir=upload_dir,
        sources=sources, relpaths=relpaths,
    )


def test_run_preview_gate_detects_leak_in_uncatalogued_sequence(tmp_path):
    # A code sequence whose CodeMeaning embeds the patient ID: the PS3.15
    # rule table does not know this vendor SQ, but the recursive gate must
    # still catch the residual identifier.
    f = tmp_path / "leak.dcm"
    make_dicom(f)
    ds = pydicom.dcmread(f)
    item = Dataset()
    item.CodeValue = "1"
    item.CodingSchemeDesignator = "LOC"
    item.CodeMeaning = "belongs to patient PID-001"
    seq_item = Dataset()
    seq_item.ConceptCodeSequence = pydicom.sequence.Sequence([item])
    ds.ProcedureCodeSequence = pydicom.sequence.Sequence([seq_item])
    ds.save_as(f, enforce_file_format=True)

    job = _make_job(tmp_path, [("leak.dcm", f.read_bytes())])
    payload = run(job, "recommended",
                  PROFILES["recommended"]["options"], "salt", write=False)
    gate = payload["gate"]
    assert gate["passed"] is False
    assert gate["hits_by_type"]["patient_id"] >= 1
    hit = next(h for h in gate["hits"] if h["hit_type"] == PATIENT_ID)
    assert "ProcedureCodeSequence" in hit["location"]
    assert job.status == "previewed"


def test_run_write_blocks_zip_and_reports_hits(tmp_path):
    f = tmp_path / "leak.dcm"
    make_dicom(f)
    # Re-open and stash the patient name in an uncatalogued text element:
    # catalogued free-text tags (StudyDescription etc.) are blanked by the
    # recommended profile, so the leak must come through a side channel.
    ds = pydicom.dcmread(f)
    ds.add_new(pydicom.tag.Tag(0x0018, 0x9424), "LO",
               "review patient Zhang San CT")
    ds.save_as(f, enforce_file_format=True)
    job = _make_job(tmp_path, [("leak.dcm", f.read_bytes())])

    with pytest.raises(GateBlocked) as exc:
        run(job, "recommended",
            PROFILES["recommended"]["options"], "salt", write=True)
    assert job.status == "blocked"
    assert job.zip_path is None
    result = exc.value.result
    assert result.exact_hit_count >= 1
    assert any(h.hit_type == PATIENT_NAME for h in result.hits)


def test_run_write_pass_stamps_gate_into_report_and_zip(tmp_path):
    f = tmp_path / "clean.dcm"
    make_dicom(f)
    job = _make_job(tmp_path, [("clean.dcm", f.read_bytes())])
    payload = run(job, "recommended",
                  PROFILES["recommended"]["options"], "salt", write=True)
    assert payload["gate"]["passed"] is True
    assert payload["gate"]["hit_count"] == 0
    assert job.status == "done"
    import zipfile
    with zipfile.ZipFile(job.zip_path) as zf:
        report = json.loads(
            zf.read("anonymization-report.json").decode("utf-8"))
    gate_section = report["residue_identifier_gate"]
    assert gate_section["passed"] is True
    assert gate_section["scanned_dicom_files"] == 1
    # report section is counts-only
    assert "hits" not in gate_section
    # no source identifiers anywhere in the report
    raw = json.dumps(report, ensure_ascii=False)
    assert "Zhang" not in raw and "PID-001" not in raw
