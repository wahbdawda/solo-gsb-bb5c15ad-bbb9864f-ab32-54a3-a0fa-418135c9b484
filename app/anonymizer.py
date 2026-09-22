"""Deterministic DICOM anonymisation engine.

The engine works per *job* (a set of uploaded files belonging to one or more
examinations):

1. :meth:`Anonymizer.prescan` reads metadata of every file, groups files by
   patient identity, and collects the universe of every UI-valued attribute
   in the upload (used to detect dangling references).
2. :meth:`Anonymizer.apply_file` rewrites one dataset:

* PatientName / PatientID are replaced by a **stable per-patient pseudonym**
  derived from a server salt (``SHA-256``); the same source patient always
  maps to the same pseudonym within and across jobs.
* Every UID (Patient/Study/Series/SOP/FrameOfReference and every *referenced*
  UID anywhere in the dataset, including sequences) is replaced by a
  deterministic ``2.25.<128-bit>`` UID.  Because the mapping is a pure
  function of the salt and the original UID, every intra-examination
  reference keeps pointing at the rewritten target.
* DA/DT values are blanked or shifted by a per-patient fixed day offset;
  TM values are blanked or retained.
* Private tags, and (optionally) overlay/curve groups, are stripped.

No third-party network service is used anywhere.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pydicom
from pydicom.dataset import Dataset
from pydicom.tag import BaseTag, Tag
from pydicom.uid import UID_dictionary

from .dicom_catalog import (
    BLANK,
    DATE,
    KEEP,
    PRESERVED_UID_TAGS,
    REMOVE,
    RULE_BY_TAG,
    UID,
    WELL_KNOWN_UID_PREFIXES,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_PREVIEW_LEN = 160
DICOMDIR_SOP_CLASS = "1.2.840.10008.1.3.10"


def preview_value(value: Any, limit: int = MAX_PREVIEW_LEN) -> str | None:
    """Human-readable, length-capped rendering of an attribute value."""
    if value is None:
        return None
    if isinstance(value, pydicom.sequence.Sequence):
        return f"<序列，{len(value)} 项>"
    if isinstance(value, (list, pydicom.multival.MultiValue)):
        text = "\\".join("" if v is None else str(v) for v in value)
    else:
        text = str(value)
    if len(text) > limit:
        return text[:limit] + f" …(+{len(text) - limit} 字符)"
    return text


def tag_name(tag: BaseTag, ds: Dataset | None = None) -> str:
    if ds is not None:
        try:
            de = ds[tag]
            if de.name:
                return de.name
        except (KeyError, IndexError):
            pass
    return pydicom.datadict.keyword_for_tag(tag) or f"未知标签 {tag}"


def display_tag(tag: BaseTag) -> str:
    return f"({tag.group:04X},{tag.element:04X})"


def uid_type_name(uid: str | None) -> str | None:
    if not uid:
        return None
    entry = UID_dictionary.get(uid)
    return entry[0] if entry else None


# ---------------------------------------------------------------------------
# Patient context
# ---------------------------------------------------------------------------

@dataclass
class PatientContext:
    key: str
    pseudo_name: str
    pseudo_id: str
    day_offset: int          # number of days shifted backwards (0 = none)

    @classmethod
    def derive(cls, key: str, salt: str) -> "PatientContext":
        digest = hashlib.sha256(
            salt.encode("utf-8") + b"|patient|" + key.encode("utf-8")
        ).digest()
        token = base64.b32encode(digest[:5]).decode("ascii").rstrip("=")
        name = f"ANON^{token}"
        pid = "ANON-" + digest[:6].hex().upper()
        day_offset = 1 + int.from_bytes(digest[16:20], "big") % 364
        return cls(key=key, pseudo_name=name, pseudo_id=pid,
                   day_offset=day_offset)


# ---------------------------------------------------------------------------
# Per-file outcome containers
# ---------------------------------------------------------------------------

@dataclass
class Change:
    tag: str
    name: str
    vr: str | None
    action: str
    old: str | None = None
    new: str | None = None
    file_index: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_index": self.file_index,
            "tag": self.tag,
            "name": self.name,
            "vr": self.vr,
            "action": self.action,
            "old": self.old,
            "new": self.new,
        }


@dataclass
class FileInfo:
    index: int
    source_path: str
    is_dicom: bool = True
    is_dicomdir: bool = False
    sop_class_uid: str | None = None
    sop_class_name: str | None = None
    modality: str | None = None
    patient_key: str | None = None
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
    output_path: str | None = None
    error: str | None = None
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Temporal helpers
# ---------------------------------------------------------------------------

_DA_RE = re.compile(r"^\d{8}$")
_DT_PREFIX_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(.*)$")


def shift_da(value: str, delta_days: datetime.timedelta) -> str | None:
    if not _DA_RE.match(value or ""):
        return None
    try:
        d = datetime.datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None
    return (d + delta_days).strftime("%Y%m%d")


def shift_dt(value: str, delta_days: datetime.timedelta) -> str | None:
    """Shift the date prefix of a DT value, keep time-of-day & UTC offset."""
    m = _DT_PREFIX_RE.match(value or "")
    if not m:
        return None
    try:
        d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return (d + delta_days).strftime("%Y%m%d") + m.group(4)


def shift_temporal_value(vr: str, value: Any,
                         delta_days: datetime.timedelta) -> Any | None:
    """Apply the shift to a scalar or backslash-multi-valued temporal value."""
    if value is None:
        return None

    def _one(part: str) -> tuple[bool, str]:
        if vr == "DA":
            shifted = shift_da(part, delta_days)
        elif vr == "DT":
            shifted = shift_dt(part, delta_days)
        else:  # TM: time-of-day is preserved
            return part is not None and part != "", part
        if shifted is None:
            return False, part
        return True, shifted

    if isinstance(value, (list, pydicom.multival.MultiValue)):
        out: list[str] = []
        ok = True
        for part in value:
            changed, nv = _one(part)
            ok = ok and changed
            out.append(nv)
        return out if ok else None
    changed, nv = _one(str(value))
    return nv if changed else None


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

# Tags holding references whose target is expected *inside* the upload.
_REFERENCE_TAGS = {
    Tag(0x0008, 0x1155),  # Referenced SOP Instance UID
    Tag(0x3006, 0x0024),  # Referenced Frame of Reference UID
    Tag(0x3006, 0x0084),  # Related Frame of Reference UID
}

# Tags that *define* an identity inside the upload.
_DEFINED_UID_TAGS = {
    Tag(0x0002, 0x0003),  # Media Storage SOP Instance UID
    Tag(0x0008, 0x0018),  # SOP Instance UID
    Tag(0x0020, 0x000D),  # Study Instance UID
    Tag(0x0020, 0x000E),  # Series Instance UID
    Tag(0x0020, 0x0052),  # Frame of Reference UID
    Tag(0x0020, 0x9161),  # Concatenation UID
    Tag(0x0020, 0x9164),  # Concatenation UID
    Tag(0x0020, 0x9165),  # Dimension Organization UID
    Tag(0x3006, 0x0020),  # Frame of Reference UID (RT)
    Tag(0x300A, 0x0013),  # Frame of Reference UID (RT plan)
}

# Curve groups 5000-50FF, overlay groups 6000-60FF (even groups only).
def _is_overlay_or_curve_group(group: int) -> bool:
    if group & 1:
        return False
    return 0x5000 <= group <= 0x50FF or 0x6000 <= group <= 0x60FF


class Anonymizer:
    def __init__(self, options: dict[str, bool], salt: str):
        self.opts = options
        self.salt = salt
        self.uid_map: dict[str, str] = {}
        self.universe: set[str] = set()
        self.contexts: dict[str, PatientContext] = {}
        self.counts = {
            "removed": 0,
            "blanked": 0,
            "pseudonymised": 0,
            "uid_remapped": 0,
            "shifted": 0,
            "added": 0,
            "private_removed": 0,
            "overlay_removed": 0,
            "files": 0,
            "skipped": 0,
            "errors": 0,
        }
        self.global_warnings: list[str] = []
        self._used_outputs: set[str] = set()

    # -- deterministic derivations ----------------------------------------

    def remap_uid(self, original: str) -> str:
        if not original:
            return original
        if original in self.uid_map:
            return self.uid_map[original]
        if original.startswith(WELL_KNOWN_UID_PREFIXES):
            self.uid_map[original] = original
            return original
        digest = hashlib.sha256(
            self.salt.encode("utf-8") + b"|uid|" + original.encode("utf-8")
        ).digest()
        number = int.from_bytes(digest[:16], "big")
        new_uid = f"2.25.{number}"
        self.uid_map[original] = new_uid
        return new_uid

    def patient_context(self, ds: Dataset) -> PatientContext:
        pid = str(getattr(ds, "PatientID", "") or "").strip()
        pname = str(getattr(ds, "PatientName", "") or "").strip()
        bdate = str(getattr(ds, "PatientBirthDate", "") or "").strip()
        key = f"id={pid}|name={pname}|birth={bdate}"
        ctx = self.contexts.get(key)
        if ctx is None:
            ctx = PatientContext.derive(key, self.salt)
            self.contexts[key] = ctx
        return ctx

    # -- prescan ------------------------------------------------------------

    def prescan(self, files: list[FileInfo], path_for) -> None:
        """Populate patient contexts and the UI-value universe."""
        for fi in files:
            try:
                ds = pydicom.dcmread(path_for(fi), stop_before_pixels=True,
                                     force=True)
            except Exception as exc:  # noqa: BLE001 - reported per file
                fi.is_dicom = False
                fi.error = f"无法解析为 DICOM 文件：{exc}"
                continue
            if not (hasattr(ds, "SOPInstanceUID")
                    or (ds.file_meta
                        and getattr(ds.file_meta, "MediaStorageSOPInstanceUID",
                                    None))):
                fi.is_dicom = False
                fi.error = "缺少 SOP Instance UID，判定为非 DICOM 文件。"
                continue

            fi.sop_class_uid = str(
                getattr(ds, "SOPClassUID", None)
                or getattr(ds.file_meta, "MediaStorageSOPClassUID", None)
                or ""
            ) or None
            fi.sop_class_name = uid_type_name(fi.sop_class_uid)
            fi.is_dicomdir = fi.sop_class_uid == DICOMDIR_SOP_CLASS
            fi.modality = str(getattr(ds, "Modality", "") or "") or None
            fi.old_patient_id = str(getattr(ds, "PatientID", "") or "") or None
            fi.old_patient_name = (
                str(getattr(ds, "PatientName", "") or "") or None
            )
            fi.old_study_uid = str(getattr(ds, "StudyInstanceUID", "")
                                   or "") or None
            fi.old_series_uid = str(getattr(ds, "SeriesInstanceUID", "")
                                    or "") or None
            fi.old_sop_uid = str(getattr(ds, "SOPInstanceUID", "")
                                  or "") or None

            ctx = self.patient_context(ds)
            fi.patient_key = ctx.key
            for _dset in self._all_datasets(ds):
                for de in _dset:
                    if de.VR != "UI" or de.value is None:
                        continue
                    if Tag(de.tag.group, de.tag.element) not in \
                            _DEFINED_UID_TAGS:
                        continue
                    vals = (de.value if isinstance(
                        de.value, (list, pydicom.multival.MultiValue))
                        else [de.value])
                    for v in vals:
                        if v:
                            self.universe.add(str(v))

    # -- file processing ----------------------------------------------------

    def apply_file(self, fi: FileInfo, src: Path, dst_dir: Path | None,
                   ) -> Dataset | None:
        """Rewrite one file.  When ``dst_dir`` is None, nothing is written
        (preview mode); the modified dataset is returned instead."""
        if not fi.is_dicom:
            self.counts["skipped"] += 1
            return None
        if fi.is_dicomdir:
            fi.is_dicom = True
            fi.error = ("DICOMDIR 介质目录已跳过：其内部记录引用基于原始 "
                        "UID，重新生成需使用介质创建工具。")
            self.counts["skipped"] += 1
            self.global_warnings.append(
                f"文件 #{fi.index + 1} 是 DICOMDIR，未包含在输出 ZIP 中。")
            return None

        try:
            ds = pydicom.dcmread(src, force=True)
        except Exception as exc:  # noqa: BLE001
            fi.error = f"读取失败：{exc}"
            self.counts["errors"] += 1
            return None

        ctx = self.contexts[fi.patient_key] if fi.patient_key else \
            self.patient_context(ds)
        try:
            self._rewrite_dataset(ds, ctx, fi)
            self._add_deidentification_attributes(ds, ctx, fi)
        except Exception as exc:  # noqa: BLE001
            fi.error = f"匿名化处理失败：{exc}"
            self.counts["errors"] += 1
            return None

        fi.new_patient_name = str(ds.PatientName)
        fi.new_patient_id = str(ds.PatientID)
        fi.new_sop_uid = str(getattr(ds, "SOPInstanceUID", "") or "")
        fi.new_study_uid = str(getattr(ds, "StudyInstanceUID", "") or "")
        fi.new_series_uid = str(getattr(ds, "SeriesInstanceUID", "") or "")

        rel = self._output_relpath(fi)
        fi.output_path = rel

        self._warn_burned_in(ds, fi)

        if dst_dir is not None:
            dst = dst_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                ds.save_as(dst, enforce_file_format=True)
            except Exception:
                # exotic inputs without proper meta information
                ds.save_as(dst, enforce_file_format=False)
        self.counts["files"] += 1
        return ds

    # -- internals ----------------------------------------------------------

    def _output_relpath(self, fi: FileInfo) -> str:
        study = fi.new_study_uid or "UNKNOWN-STUDY"
        series = fi.new_series_uid or "UNKNOWN-SERIES"
        sop = fi.new_sop_uid or f"INSTANCE-{fi.index:06d}"
        base = f"{study}/{series}/{sop}.dcm"
        rel = base
        n = 2
        while rel in self._used_outputs:
            rel = f"{study}/{series}/{sop}-{n}.dcm"
            n += 1
        if rel != base:
            fi.warnings.append(
                f"检测到重复的 SOP Instance UID（{fi.old_sop_uid}），"
                "输出文件已自动重命名。")
            self.global_warnings.append(
                f"文件 #{fi.index + 1} 与其他文件的 SOP Instance UID 重复。")
        self._used_outputs.add(rel)
        return rel

    @staticmethod
    def _all_datasets(ds: Dataset, include_meta: bool = True
                      ) -> Iterable[Dataset]:
        stack: list[Dataset] = []
        if include_meta and ds.file_meta is not None:
            stack.append(ds.file_meta)
        stack.append(ds)
        while stack:
            current = stack.pop()
            yield current
            for de in current:
                if de.VR == "SQ" and de.value is not None:
                    stack.extend(de.value)

    def _rewrite_dataset(self, ds: Dataset, ctx: PatientContext,
                         fi: FileInfo) -> None:
        if ds.file_meta is not None:
            self._apply_level(ds.file_meta, ctx, fi, is_meta=True)
        self._apply_level(ds, ctx, fi, is_meta=False)

    def _resolve_action(self, tag: BaseTag, vr: str
                        ) -> tuple[str, str | None, str]:
        """Return (action, gate-prefixed-as-stored, note) for an element."""
        rule = RULE_BY_TAG.get(Tag(tag.group, tag.element))
        if rule is not None:
            return rule
        # Engine fallback for uncatalogued attributes.
        if vr == "PN":
            return BLANK, "keep_staff", "人员姓名（默认清空策略）"
        if vr in ("DA", "DT", "TM"):
            return DATE, None, "日期/时间（默认日期策略）"
        if vr == "UI":
            return UID, None, "UID（默认重写策略）"
        return KEEP, None, ""

    def _decide(self, action: str, gate: str | None) -> str:
        """Collapse (action, gate) to an effective action for current opts."""
        if not gate:
            return action
        if gate.startswith("!"):
            option = gate[1:]
            return action if self.opts.get(option, False) else KEEP
        return KEEP if self.opts.get(gate, False) else action

    def _apply_level(self, ds: Dataset, ctx: PatientContext, fi: FileInfo,
                     is_meta: bool) -> None:
        # 1) strip private attributes (Basic profile: always remove)
        for de in list(ds):
            if de.tag.is_private:
                self._record(fi, de, "removed",
                             preview_value(de.value), None)
                del ds[de.tag]
                self.counts["private_removed"] += 1

        # 2) strip curve/overlay repeating groups when requested
        if not is_meta and self.opts.get("strip_overlays", False):
            for de in list(ds):
                if _is_overlay_or_curve_group(de.tag.group):
                    self._record(fi, de, "removed",
                                 preview_value(de.value), None)
                    del ds[de.tag]
                    self.counts["overlay_removed"] += 1

        # 3) walk standard attributes
        for de in list(ds):
            tag = de.tag
            if tag.is_private:
                continue  # already removed
            if is_meta:
                self._apply_meta_element(ds, de, fi)
                continue

            action, gate, _note = self._resolve_action(tag, de.VR)
            effective = self._decide(action, gate)

            if effective == REMOVE:
                self._record(fi, de, "removed",
                             preview_value(de.value), None)
                del ds[tag]
                self.counts["removed"] += 1
                continue

            # identity pseudonyms take precedence regardless of rule action
            if tag == Tag(0x0010, 0x0010):
                old = preview_value(de.value)
                ds[tag].value = ctx.pseudo_name
                self._record(fi, de, "pseudonymised", old, ctx.pseudo_name)
                self.counts["pseudonymised"] += 1
                continue
            if tag == Tag(0x0010, 0x0020):
                old = preview_value(de.value)
                ds[tag].value = ctx.pseudo_id
                self._record(fi, de, "pseudonymised", old, ctx.pseudo_id)
                self.counts["pseudonymised"] += 1
                continue

            if de.VR == "SQ":
                if de.value is not None:
                    for item in de.value:
                        self._apply_level(item, ctx, fi, is_meta=False)
                continue

            # temporal attributes: explicit DATE rules and uncatalogued
            # DA/DT/TM elements all follow the selected date policy
            if de.VR in ("DA", "DT", "TM"):
                self._apply_temporal_element(ds, de, ctx, fi, effective,
                                             gate)
                continue

            if de.VR == "UI":
                self._apply_uid_element(ds, de, fi)
                continue

            if effective == BLANK:
                old = preview_value(de.value)
                ds[tag].value = ""
                self._record(fi, de, "blanked", old, "")
                self.counts["blanked"] += 1
                continue
            # effective == KEEP: retain untouched


    def _apply_meta_element(self, ds: Dataset, de, fi: FileInfo) -> None:
        if de.VR != "UI":
            return
        tag = Tag(de.tag.group, de.tag.element)
        if tag in PRESERVED_UID_TAGS:
            return
        self._apply_uid_element(ds, de, fi)

    def _apply_uid_element(self, ds: Dataset, de, fi: FileInfo) -> None:
        tag = Tag(de.tag.group, de.tag.element)
        if tag in PRESERVED_UID_TAGS:
            return
        values = de.value
        old_preview = preview_value(values)
        if isinstance(values, (list, pydicom.multival.MultiValue)):
            new_values = [self.remap_uid(str(v)) if v else v
                          for v in values]
            changed = any(a != b for a, b in zip(values, new_values))
            if not changed:
                return
            ds[tag].value = new_values
        else:
            if values in (None, ""):
                return
            new_value = self.remap_uid(str(values))
            if new_value == values:
                return
            ds[tag].value = new_value
        self._record(fi, de, "uid_remapped", old_preview,
                     preview_value(ds[tag].value))
        self.counts["uid_remapped"] += 1

        if tag in _REFERENCE_TAGS and values not in (None, ""):
            originals = (values if isinstance(
                values, (list, pydicom.multival.MultiValue)) else [values])
            for ref in originals:
                if ref and str(ref) not in self.universe:
                    shown = str(ref)
                    if len(shown) > 48:
                        shown = shown[:48] + "…"
                    msg = (f"引用的 UID {shown} 未出现在本次上传的影像中；"
                           "已按同一映射重写，但目标对象缺失（悬挂引用）。")
                    fi.warnings.append(msg)
                    if msg not in self.global_warnings:
                        self.global_warnings.append(
                            f"文件 #{fi.index + 1}: {msg}")

    def _apply_temporal_element(self, ds: Dataset, de, ctx: PatientContext,
                                fi: FileInfo, effective: str,
                                gate: str | None) -> None:
        tag = Tag(de.tag.group, de.tag.element)
        old_preview = preview_value(de.value)

        # PatientBirthDate under retain-patient-characteristics is generalised
        # to <year>0101 (PS3.15), never shifted with examination dates.
        if tag == Tag(0x0010, 0x0030):
            if self.opts.get("keep_chars", False) and de.value \
                    and len(str(de.value)) >= 4:
                year = str(de.value)[:4]
                if year.isdigit():
                    generalised = year + "0101"
                    if str(de.value) != generalised:
                        ds[tag].value = generalised
                        self._record(fi, de, "shifted", old_preview,
                                     generalised)
                        self.counts["shifted"] += 1
                    return
            ds[tag].value = ""
            self._record(fi, de, "blanked", old_preview, "")
            self.counts["blanked"] += 1
            return

        if self.opts.get("keep_dates", False):
            if ctx.day_offset <= 0:
                return
            delta = datetime.timedelta(days=-ctx.day_offset)
            try:
                new_value = shift_temporal_value(de.VR, de.value, delta)
            except Exception:  # noqa: BLE001
                new_value = None
            if new_value is None:
                # unparsable: blank it rather than leak an original date
                ds[tag].value = ""
                self._record(fi, de, "blanked", old_preview, "")
                self.counts["blanked"] += 1
                fi.warnings.append(
                    f"{display_tag(tag)} {de.name} 日期格式无法解析，已置空。")
                return
            ds[tag].value = new_value
            self._record(fi, de, "shifted", old_preview,
                         preview_value(ds[tag].value))
            self.counts["shifted"] += 1
        else:
            ds[tag].value = ""
            self._record(fi, de, "blanked", old_preview, "")
            self.counts["blanked"] += 1

    def _add_deidentification_attributes(self, ds: Dataset,
                                         ctx: PatientContext,
                                         fi: FileInfo) -> None:
        methods = ["PS3.15 Basic Application Confidentiality Profile"]
        codes = [("113100", "Basic Application Confidentiality Profile")]
        if self.opts.get("keep_dates"):
            methods.append(
                f"Retain Longitudinal Temporal Info Modified ({ctx.day_offset}d)")
            codes.append(("113106",
                          "Retain Longitudinal Temporal Information with "
                          "Modified Values"))
        if self.opts.get("keep_chars"):
            methods.append("Retain Patient Characteristics Option")
            codes.append(("113108", "Retain Patient Characteristics Option"))
        if self.opts.get("keep_device"):
            methods.append("Retain Device Identity Option")
            codes.append(("113109", "Retain Device Identity Option"))
        methods.append("UIDs replaced with deterministic 2.25 UIDs")
        if self.opts.get("strip_overlays"):
            methods.append("Overlays/curves/graphic annotations removed")

        ds.PatientIdentityRemoved = "YES"
        self._record_simple(fi, Tag(0x0012, 0x0062),
                            "Patient Identity Removed", "CS", "added",
                            None, "YES")
        self.counts["added"] += 1

        ds.DeidentificationMethod = methods
        self._record_simple(fi, Tag(0x0012, 0x0063),
                            "De-identification Method", "LO", "added",
                            None, "; ".join(methods))
        self.counts["added"] += 1

        items = []
        for code, meaning in codes:
            item = Dataset()
            item.CodeValue = code
            item.CodingSchemeDesignator = "DCM"
            item.CodeMeaning = meaning
            items.append(item)
        seq = pydicom.sequence.Sequence(items)
        ds.DeidentificationMethodCodeSequence = seq
        self._record_simple(fi, Tag(0x0012, 0x0064),
                            "De-identification Method Code Sequence", "SQ",
                            "added", None, f"<序列，{len(items)} 项>")
        self.counts["added"] += 1

    def _warn_burned_in(self, ds: Dataset, fi: FileInfo) -> None:
        burned = getattr(ds, "BurnedInAnnotation", None)
        if burned and str(burned).strip().upper() in ("YES", "Y", "01"):
            msg = ("Burned In Annotation (0028,0301) 标记为 YES：像素中可能"
                   "烧录了身份信息，本工具不修改像素，请人工核查。")
            fi.warnings.append(msg)
            if msg not in self.global_warnings:
                self.global_warnings.append(
                    f"文件 #{fi.index + 1}: {msg}")
        visual = getattr(ds, "RecognizableVisualFeatures", None)
        if visual and str(visual).strip().upper() in ("YES", "Y", "01"):
            msg = ("Recognizable Visual Features (0028,0302) 标记为 YES："
                   "图像可能含可识别面部/体表特征，请人工核查。")
            fi.warnings.append(msg)
            if msg not in self.global_warnings:
                self.global_warnings.append(
                    f"文件 #{fi.index + 1}: {msg}")

    def _record(self, fi: FileInfo, de, action: str,
                old: str | None, new: str | None) -> None:
        fi.changes.append(Change(
            file_index=fi.index,
            tag=display_tag(de.tag),
            name=de.name or tag_name(de.tag),
            vr=de.VR,
            action=action,
            old=old,
            new=new,
        ))

    def _record_simple(self, fi: FileInfo, tag: BaseTag, name: str,
                       vr: str, action: str, old: str | None,
                       new: str | None) -> None:
        fi.changes.append(Change(
            file_index=fi.index,
            tag=display_tag(tag),
            name=name,
            vr=vr,
            action=action,
            old=old,
            new=new,
        ))
