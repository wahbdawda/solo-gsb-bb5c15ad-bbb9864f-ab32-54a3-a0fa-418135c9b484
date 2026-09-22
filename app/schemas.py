"""Pydantic API schemas."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .dicom_catalog import OPTION_KEYS


class AnonymizationOptions(BaseModel):
    profile: str = "recommended"
    # ``None`` means "follow the profile preset"; explicit booleans override.
    keep_dates: bool | None = None
    keep_chars: bool | None = None
    keep_staff: bool | None = None
    keep_site: bool | None = None
    keep_device: bool | None = None
    keep_protocol: bool | None = None
    keep_descriptions: bool | None = None
    strip_overlays: bool | None = None
    salt: Optional[str] = Field(
        default=None,
        description="可选自定义盐值；留空使用服务器默认盐值。盐值决定伪名与新 UID。",
    )

    def overrides(self) -> dict[str, bool]:
        return {k: bool(getattr(self, k)) for k in OPTION_KEYS
                if getattr(self, k) is not None}


class FieldChange(BaseModel):
    file_index: int
    tag: str                    # e.g. (0010,0010)
    name: str                   # Patient Name
    vr: str | None = None
    action: Literal["removed", "blanked", "pseudonymised", "uid_remapped",
                    "shifted", "kept", "added", "sequence"]
    old: str | None = None
    new: str | None = None


class GateHit(BaseModel):
    file: str
    location: str
    tag: str
    vr: str | None = None
    hit_type: Literal["patient_name", "patient_id", "original_uid"]
    hit_type_label: str
    matched: str


class GateResult(BaseModel):
    passed: bool
    scanned_dicom_files: int
    scanned_report_files: int
    scanned_files: int
    values_checked: int
    identifiers: dict[str, int]
    hit_count: int
    hits_by_type: dict[str, int]
    hits_truncated: bool
    generated_at: str
    hits: list[GateHit] = []


class FileResult(BaseModel):
    index: int
    source_path: str            # sanitised relative upload path
    output_path: str | None = None  # path inside the produced ZIP
    sop_class_uid: str | None = None
    sop_class_name: str | None = None
    modality: str | None = None
    old_patient_id: str | None = None
    old_patient_name: str | None = None
    old_study_uid: str | None = None
    old_series_uid: str | None = None
    old_sop_uid: str | None = None
    new_patient_id: str | None = None
    new_patient_name: str | None = None
    new_study_uid: str | None = None
    new_series_uid: str | None = None
    new_sop_uid: str | None = None
    error: str | None = None


class PreviewResponse(BaseModel):
    job_id: str
    profile_label: str
    options: dict[str, bool]
    files: list[FileResult]
    changes: list[FieldChange]
    warnings: list[str]
    counts: dict[str, int]
    patients: list[dict[str, str]]
    gate: GateResult | None = None


class ProcessResponse(BaseModel):
    job_id: str
    status: str
    download_url: str
    zip_filename: str
    files: list[FileResult]
    warnings: list[str]
    counts: dict[str, int]
    gate: GateResult | None = None
