"""End-to-end engine tests using in-memory generated DICOM objects."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    generate_uid,
)

from app.anonymizer import Anonymizer, FileInfo
from app.dicom_catalog import PROFILES


def make_dicom(path: Path, patient_name="Zhang^San", patient_id="PID-001",
               study_uid=None, series_uid=None, sop_uid=None,
               for_uid=None, referenced_sop_uid=None, modality="CT",
               accession="ACC-2024-009", study_desc="急诊CT",
               birth_date="19800101", study_date="20240315",
               private=True, burned_in=None) -> None:
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid or generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.StudyInstanceUID = study_uid or generate_uid()
    ds.SeriesInstanceUID = series_uid or generate_uid()
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = birth_date
    ds.PatientSex = "M"
    ds.Modality = modality
    ds.AccessionNumber = accession
    ds.StudyDescription = study_desc
    ds.StudyDate = study_date
    ds.StudyTime = "101530"
    ds.AcquisitionDateTime = study_date + "101530"
    ds.StationName = "CT-ROOM-3"
    ds.InstitutionName = "第一人民医院"
    ds.ReferringPhysicianName = "Dr^Li"
    ds.StudyID = "1234"
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = b"\0" * (4 * 4 * 2)
    if for_uid:
        ds.FrameOfReferenceUID = for_uid
    if referenced_sop_uid:
        item = Dataset()
        item.ReferencedSOPClassUID = CTImageStorage
        item.ReferencedSOPInstanceUID = referenced_sop_uid
        seq = Dataset()
        seq.ReferencedSOPSequence = pydicom.sequence.Sequence([item])
        ds.SourceImageSequence = pydicom.sequence.Sequence([seq])
    if private:
        block = ds.private_block(0x0009, "VENDOR", create=True)
        block.add_new(0x10, "LO", patient_id + "-secret")
    if burned_in is not None:
        ds.BurnedInAnnotation = burned_in
    ds.save_as(path, enforce_file_format=True)
    return ds


@pytest.fixture
def study(tmp_path):
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop1 = generate_uid()
    sop2 = generate_uid()
    for_uid = generate_uid()
    f1 = tmp_path / "a.dcm"
    f2 = tmp_path / "b.dcm"
    make_dicom(f1, study_uid=study_uid, series_uid=series_uid,
               sop_uid=sop1, for_uid=for_uid, referenced_sop_uid=sop2)
    make_dicom(f2, study_uid=study_uid, series_uid=series_uid,
               sop_uid=sop2, for_uid=for_uid, referenced_sop_uid=sop1)
    return tmp_path, [f1, f2], {
        "study": study_uid, "series": series_uid,
        "sop1": sop1, "sop2": sop2, "for": for_uid,
    }


def _run(tmp_path, files, profile="recommended", salt="test-salt",
         write=True):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    infos = [FileInfo(index=i, source_path=f.name)
             for i, f in enumerate(files)]
    anon = Anonymizer(PROFILES[profile]["options"], salt)
    anon.prescan(infos, lambda fi: files[fi.index])
    for fi in infos:
        anon.apply_file(fi, files[fi.index], out)
    return anon, infos, out


def test_stable_pseudonyms_and_uid_remap(study, tmp_path):
    root, files, uids = study
    anon, infos, out = _run(tmp_path, files)

    # pseudonyms applied
    for fi in infos:
        ds = pydicom.dcmread(out / fi.output_path)
        assert str(ds.PatientName).startswith("ANON^")
        assert str(ds.PatientID).startswith("ANON-")
        assert ds.PatientName != "Zhang^San"
        assert "Zhang" not in str(ds.PatientName)

    # both instances of the same patient share a pseudonym
    d0 = pydicom.dcmread(out / infos[0].output_path)
    d1 = pydicom.dcmread(out / infos[1].output_path)
    assert str(d0.PatientName) == str(d1.PatientName)
    assert str(d0.PatientID) == str(d1.PatientID)

    # study/series stay shared (relation preserved), SOPs differ
    assert str(d0.StudyInstanceUID) == str(d1.StudyInstanceUID)
    assert str(d0.SeriesInstanceUID) == str(d1.SeriesInstanceUID)
    assert str(d0.SOPInstanceUID) != str(d1.SOPInstanceUID)

    # all original UIDs replaced
    assert str(d0.StudyInstanceUID) != str(uids["study"])
    assert str(d0.SOPInstanceUID).startswith("2.25.")
    # frame of reference shared and remapped
    assert str(d0.FrameOfReferenceUID) == str(d1.FrameOfReferenceUID)
    assert str(d0.FrameOfReferenceUID) != str(uids["for"])


def test_references_point_to_rewritten_targets(study, tmp_path):
    root, files, uids = study
    anon, infos, out = _run(tmp_path, files)
    d0 = pydicom.dcmread(out / infos[0].output_path)
    ref = d0.SourceImageSequence[0].ReferencedSOPSequence[0] \
        .ReferencedSOPInstanceUID
    d1 = pydicom.dcmread(out / infos[1].output_path)
    assert ref == str(d1.SOPInstanceUID)
    # class UID preserved
    assert str(ref) and ref.startswith("2.25.")
    assert str(d0.SOPClassUID) == CTImageStorage
    assert str(d0.file_meta.TransferSyntaxUID) == ExplicitVRLittleEndian


def test_determinism_across_runs(study, tmp_path):
    root, files, uids = study
    a1, i1, o1 = _run(tmp_path / "run1" if (tmp_path / "run1").exists()
                      else tmp_path, files, salt="S")
    o2 = tmp_path / "out2"; o2.mkdir()
    infos2 = [FileInfo(index=i, source_path=f.name) for i, f in enumerate(files)]
    anon2 = Anonymizer(PROFILES["recommended"]["options"], "S")
    anon2.prescan(infos2, lambda fi: files[fi.index])
    for fi in infos2:
        anon2.apply_file(fi, files[fi.index], o2)
    x = pydicom.dcmread(o1 / i1[0].output_path)
    y = pydicom.dcmread(o2 / infos2[0].output_path)
    assert str(x.StudyInstanceUID) == str(y.StudyInstanceUID)
    assert str(x.PatientName) == str(y.PatientName)


def test_salt_changes_pseudonyms(study, tmp_path):
    root, files, uids = study
    a1, i1, o1 = _run(tmp_path, files, salt="salt-A")
    out2 = tmp_path / "o2"; out2.mkdir()
    infos2 = [FileInfo(index=i, source_path=f.name) for i, f in enumerate(files)]
    anon2 = Anonymizer(PROFILES["recommended"]["options"], "salt-B")
    anon2.prescan(infos2, lambda fi: files[fi.index])
    for fi in infos2:
        anon2.apply_file(fi, files[fi.index], out2)
    x = pydicom.dcmread(o1 / i1[0].output_path)
    y = pydicom.dcmread(out2 / infos2[0].output_path)
    assert str(x.PatientName) != str(y.PatientName)
    assert str(x.StudyInstanceUID) != str(y.StudyInstanceUID)


def test_recommended_shifts_dates_and_strips_private(study, tmp_path):
    root, files, uids = study
    anon, infos, out = _run(tmp_path, files, profile="recommended")
    ds = pydicom.dcmread(out / infos[0].output_path)
    assert ds.StudyDate != "20240315"
    assert len(ds.StudyDate) == 8
    assert ds.AcquisitionDateTime[:8] != "20240315"
    # private tags gone
    private = [el for el in ds if el.tag.is_private]
    assert private == []
    # identity removed marker
    assert ds.PatientIdentityRemoved == "YES"
    assert hasattr(ds, "DeidentificationMethodCodeSequence")
    # gated free text cleared
    assert "医院" not in str(ds.InstitutionName) if "InstitutionName" in ds else True
    assert str(ds.get("StudyDescription", "")) == ""
    # patient char retained
    assert ds.PatientSex == "M"
    # protocol option: StudyDescription blanked in recommended
    anon2, i2, o2 = _run(tmp_path / "p2", files, profile="strict") \
        if False else (None, None, None)


def test_birth_date_generalised_not_shifted(study, tmp_path):
    # PS3.15 retain-patient-characteristics: PatientBirthDate is generalised
    # to <year>0101, it must never follow the per-patient examination-date
    # shift (which would alter the implied age).
    root, files, uids = study
    anon, infos, out = _run(tmp_path, files, profile="recommended")
    ds = pydicom.dcmread(out / infos[0].output_path)
    assert str(ds.PatientBirthDate) == "19800101"

    # strict profile blanks it instead
    out2 = tmp_path / "birth-strict"; out2.mkdir()
    infos2 = [FileInfo(index=i, source_path=f.name)
              for i, f in enumerate(files)]
    anon2 = Anonymizer(PROFILES["strict"]["options"], "x")
    anon2.prescan(infos2, lambda fi: files[fi.index])
    for fi in infos2:
        anon2.apply_file(fi, files[fi.index], out2)
    ds2 = pydicom.dcmread(out2 / infos2[0].output_path)
    assert str(ds2.get("PatientBirthDate", "")) == ""


def test_strict_blanks_dates(study, tmp_path):
    root, files, uids = study
    out2 = tmp_path / "strict"; out2.mkdir()
    infos = [FileInfo(index=i, source_path=f.name) for i, f in enumerate(files)]
    anon = Anonymizer(PROFILES["strict"]["options"], "x")
    anon.prescan(infos, lambda fi: files[fi.index])
    for fi in infos:
        anon.apply_file(fi, files[fi.index], out2)
    ds = pydicom.dcmread(out2 / infos[0].output_path)
    assert str(ds.StudyDate) == ""
    assert "StudyTime" not in ds or str(ds.StudyTime) == ""
    assert str(ds.get("PatientSex", "")) == ""
    assert str(ds.get("AccessionNumber", "")) == ""


def test_burned_in_warning(tmp_path):
    f = tmp_path / "burned.dcm"
    make_dicom(f, burned_in="YES")
    anon, infos, out = _run(tmp_path / "w", [f]) if False else (None, None, None)
    out2 = tmp_path / "w2"; out2.mkdir()
    infos = [FileInfo(index=0, source_path="burned.dcm")]
    anon = Anonymizer(PROFILES["recommended"]["options"], "x")
    anon.prescan(infos, lambda fi: f)
    anon.apply_file(infos[0], f, out2)
    assert any("Burned" in w for w in anon.global_warnings)


def test_dangling_reference_warning(tmp_path):
    f = tmp_path / "d.dcm"
    make_dicom(f, referenced_sop_uid=generate_uid())
    out2 = tmp_path / "d2"; out2.mkdir()
    infos = [FileInfo(index=0, source_path="d.dcm")]
    anon = Anonymizer(PROFILES["strict"]["options"], "x")
    anon.prescan(infos, lambda fi: f)
    anon.apply_file(infos[0], f, out2)
    assert any("悬挂引用" in w for w in anon.global_warnings)


def test_non_dicom_skipped(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hello")
    infos = [FileInfo(index=0, source_path="note.txt")]
    anon = Anonymizer(PROFILES["strict"]["options"], "x")
    anon.prescan(infos, lambda fi: f)
    assert infos[0].is_dicom is False
    result = anon.apply_file(infos[0], f, tmp_path / "out")
    assert result is None
