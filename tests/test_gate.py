"""Tests for the residual identifier gate."""
from __future__ import annotations

import json

import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    generate_uid,
)

from app.anonymizer import Anonymizer, FileInfo
from app.dicom_catalog import PROFILES
from app.gate import ResidualIdentifierGate


def make_dicom(path, patient_name="Zhao^Si", patient_id="MRN-77777",
               study_uid=None, description="正常检查"):
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=file_meta,
                     preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.StudyInstanceUID = study_uid or generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19800101"
    ds.Modality = "CT"
    ds.AccessionNumber = "ACC-1"
    ds.StudyDescription = description
    ds.StationName = "CT-1"
    ds.StudyDate = "20240315"
    ds.StudyTime = "101530"
    ds.Rows = ds.Columns = 2
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHRO2"
    ds.PixelRepresentation = 0
    ds.PixelData = b"\0" * 8
    ds.save_as(path, enforce_file_format=True)
    return ds


def _run_engine(path, profile="recommended"):
    infos = [FileInfo(index=0, source_path=path.name)]
    anon = Anonymizer(PROFILES[profile]["options"], "gate-salt")
    anon.prescan(infos, lambda fi: path)
    ds = anon.apply_file(infos[0], path, None)  # preview: in-memory
    return anon, infos, ds


def _gate_for(anon, names=(), ids=()):
    gate = ResidualIdentifierGate()
    for n in names:
        gate.add_patient_name(n)
    for i in ids:
        gate.add_patient_id(i)
    for original, new in anon.uid_map.items():
        if original != new:
            gate.add_uid(original)
    return gate


def test_gate_passes_for_clean_output(tmp_path):
    f = tmp_path / "a.dcm"
    make_dicom(f)
    anon, infos, ds = _run_engine(f)

    gate = _gate_for(anon, ["Zhao^Si"], ["MRN-77777"])
    result = gate.scan_outputs([(infos[0].output_path, ds)], {
        "tool": "dicom-anonymisation-workbench",
        "files": [{"new_sop_instance_uid": infos[0].new_sop_uid}],
    })
    assert result.status == "passed"
    assert result.hit_count == 0
    assert result.scanned_dicom_files == 1
    assert result.scanned_report is True
    assert result.identifiers["patient_name"] >= 1
    assert result.identifiers["uid"] >= 3
    assert result.as_dict(include_evidence=False)["status"] == "passed"


def test_gate_catches_name_in_kept_free_text(tmp_path):
    # "light" profile keeps StudyDescription; embed the source patient name.
    f = tmp_path / "a.dcm"
    make_dicom(f, description="复查患者 Zhao^Si 的旧片")
    anon, infos, ds = _run_engine(f, profile="light")

    gate = _gate_for(anon, ["Zhao^Si"], ["MRN-77777"])
    result = gate.scan_outputs([(infos[0].output_path, ds)], None)
    assert result.status == "blocked"
    hit = result.hits[0]
    assert hit["hit_type"] == "patient_name"
    assert hit["tag"].startswith("(0008,1030)")
    assert hit["file"] == infos[0].output_path
    # full PN wins over the shorter given-name component (one hit/type/tag)
    same = [h for h in result.hits
            if h["tag"] == hit["tag"] and h["hit_type"] == "patient_name"]
    assert len(same) == 1


def test_gate_catches_name_inside_nested_sequence(tmp_path):
    f = tmp_path / "a.dcm"
    make_dicom(f)
    anon, infos, ds = _run_engine(f)

    # Residual identifier hidden deep inside an output sequence, e.g. a
    # vendor free-text element the rewrite engine did not touch.
    item = Dataset()
    item.add_new(0x0040A160, "UT", "referral for ZHAO SI xyz")
    inner = Dataset()
    inner.CodeMeaning = "ZhaoSi review"
    item.ConceptNameCodeSequence = pydicom.sequence.Sequence([inner])
    ds.RequestAttributesSequence = pydicom.sequence.Sequence([item])

    gate = _gate_for(anon, ["Zhao^Si"], [])
    result = gate.scan_outputs([(infos[0].output_path, ds)], None)
    tags = " ".join(h["tag"] for h in result.hits)
    assert result.status == "blocked"
    assert "[" in tags and "→" in tags  # sequence path rendered


def test_gate_catches_exact_original_uid_in_ui_element(tmp_path):
    study_uid = generate_uid()
    f = tmp_path / "a.dcm"
    make_dicom(f, study_uid=study_uid)
    anon, infos, ds = _run_engine(f)

    # A UI element that was never rewritten still holds the source UID.
    ds.add_new(0x00091030, "UI", study_uid)
    gate = _gate_for(anon)
    result = gate.scan_outputs([(infos[0].output_path, ds)], None)
    assert result.status == "blocked"
    assert result.hits[0]["hit_type"] == "original_uid"
    assert result.hits[0]["vr"] == "UI"


def test_gate_catches_uid_substring_in_text(tmp_path):
    study_uid = generate_uid()
    f = tmp_path / "a.dcm"
    make_dicom(f, study_uid=study_uid)
    anon, infos, ds = _run_engine(f, profile="light")
    ds.StationName = f"STATION-{study_uid}"

    gate = _gate_for(anon)
    result = gate.scan_outputs([(infos[0].output_path, ds)], None)
    assert result.status == "blocked"
    assert any(h["hit_type"] == "original_uid" for h in result.hits)


def test_gate_scans_report_json():
    study_uid = generate_uid()
    gate = ResidualIdentifierGate()
    gate.add_patient_name("Zhao^Si")
    gate.add_patient_id("MRN-77777")
    gate.add_uid(study_uid)

    report = {
        "files": [
            {"note": "exported for patient zhao si, id mrn-77777"},
            {"ref": study_uid},
        ],
    }
    result = gate.scan_outputs([], report)
    types = {h["hit_type"] for h in result.hits}
    assert result.status == "blocked"
    assert {"patient_name", "patient_id", "original_uid"} <= types
    assert all(h["file"] == "anonymization-report.json"
               and h["tag"].startswith("/files/") for h in result.hits)


def test_gate_ignores_generic_short_tokens(tmp_path):
    f = tmp_path / "a.dcm"
    make_dicom(f, patient_name="Li^Si", patient_id="12")
    anon, infos, ds = _run_engine(f, profile="light")
    ds.InstitutionName = "San Li Hospital"  # 2-char given names, no full key
    ds.StationName = "M-12"                 # tiny id token

    gate = _gate_for(anon, ["Li^Si"], ["12"])
    result = gate.scan_outputs([(infos[0].output_path, ds)], None)
    assert result.status == "passed"


def test_gate_does_not_flag_new_values(tmp_path):
    f = tmp_path / "a.dcm"
    make_dicom(f)
    anon, infos, ds = _run_engine(f)
    gate = _gate_for(anon, ["Zhao^Si"], ["MRN-77777"])
    result = gate.scan_outputs([(infos[0].output_path, ds)], None)
    assert result.status == "passed"


def test_uid_boundary_avoids_superstring_false_positive(tmp_path):
    study_uid = generate_uid()
    f = tmp_path / "a.dcm"
    make_dicom(f, study_uid=study_uid)
    anon, infos, ds = _run_engine(f, profile="light")
    # A longer dotted-digit string containing the UID as a component must
    # not match (UID component boundary).
    ds.StationName = "9.9." + study_uid
    gate = _gate_for(anon)
    result = gate.scan_outputs([(infos[0].output_path, ds)], None)
    assert result.status == "passed"


def test_report_summary_excludes_raw_evidence():
    gate = ResidualIdentifierGate()
    gate.add_patient_name("Zhao^Si")
    report = {"files": [{"note": "patient ZHAOSI review"}]}
    result = gate.scan_outputs([], report)
    assert result.hit_count >= 1
    public = json.dumps(result.as_dict(include_evidence=False),
                        ensure_ascii=False)
    assert "ZHAOSI review" not in public
