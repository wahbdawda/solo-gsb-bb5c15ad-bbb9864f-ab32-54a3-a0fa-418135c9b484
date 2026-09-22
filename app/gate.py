"""Residual identifier gate.

Runs *after* anonymisation and before the ZIP is produced.  It collects the
identifiers of the source upload (patient names, patient IDs and every
original UID) and recursively scans the output DICOM objects (including
sequences and file meta information) plus ``anonymization-report.json`` for
residual occurrences:

* ``UI`` valued attributes are compared **exactly** against the set of
  original UIDs (a remapped/preserved UID never matches);
* character-valued attributes (names, identifiers, free text) are searched
  for source names / IDs / UIDs with normalisation that ignores case,
  whitespace and DICOM PN separators;
* the JSON report is walked leaf by leaf with JSON-pointer locations.

Short / generic tokens (single given names, tiny numeric IDs) are ignored to
avoid false positives; when a real residual is found the gate reports the
output file, the tag, the hit type and a truncated excerpt, and the caller
blocks ZIP generation.  On pass a machine-readable summary is returned and
embedded in the report.

The gate is deliberately conservative (false alarms are preferable to
leaks), but it only inspects DICOM metadata / text -- like the rest of the
workbench it cannot see text burned into pixels.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

import pydicom
from pydicom.dataset import Dataset
from pydicom.tag import BaseTag

# Character VRs that may carry identifiers / free text.  Binary, numeric and
# code-only VRs are excluded on purpose to avoid noisy false positives.
TEXT_VRS = {
    "AE", "AS", "CS", "DA", "DT", "LO", "LT", "PN", "SH", "ST",
    "TM", "UC", "UR", "UT",
}

MAX_HITS = 200
MAX_EVIDENCE = 120

# Token acceptance thresholds (prevents "M", "Li" or "12" from flagging half
# of every output dataset).
MIN_ASCII_NAME_PART = 3
MIN_CJK_NAME_PART = 2
MIN_ID_LEN = 4
MIN_DIGIT_ID_LEN = 6
MIN_UID_LEN = 10

_UID_RE = re.compile(r"^\d+(?:\.\d+)+$")
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿가-힯]")
_UID_BOUNDARY_RE_TMPL = r"(?<![0-9.]){uid}(?![0-9.])"
_SEP_RE = re.compile(r"[\s^=_&\-–—]+")


def display_tag(tag: BaseTag) -> str:
    return f"({tag.group:04X},{tag.element:04X})"


def nfkc(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text if text is not None else ""))


def norm_flat(text: Any) -> str:
    """Case-folded NFKC text with PN / whitespace / hyphen separators removed."""
    return _SEP_RE.sub("", nfkc(text).casefold())


def clip(text: Any, limit: int = MAX_EVIDENCE) -> str:
    s = str(text or "")
    if len(s) <= limit:
        return s
    return s[:limit] + f" …(+{len(s) - limit} 字符)"


@dataclass(frozen=True)
class Needle:
    """One source identifier to look for."""
    value: str                       # raw, trimmed source value
    hit_type: str                    # patient_name | patient_id | original_uid
    flat: str = ""                   # normalised form for substring matching
    min_len: int = 0

    def matches_text(self, haystack_flat: str) -> bool:
        return len(self.flat) >= self.min_len and self.flat in haystack_flat


@dataclass
class GateResult:
    status: str = "passed"          # passed | blocked (finalised in scan)
    scanned_dicom_files: int = 0
    scanned_report: bool = False
    values_checked: int = 0
    identifiers: dict[str, int] = field(
        default_factory=lambda:
        {"patient_name": 0, "patient_id": 0, "uid": 0})
    hits: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    @property
    def hit_count(self) -> int:
        return len(self.hits)

    def as_dict(self, include_evidence: bool = True) -> dict[str, Any]:
        out = {
            "status": self.status,
            "scanned_dicom_files": self.scanned_dicom_files,
            "scanned_report": self.scanned_report,
            "values_checked": self.values_checked,
            "identifiers": dict(self.identifiers),
            "hit_count": self.hit_count,
            "truncated": self.truncated,
            "hits": [
                {k: v for k, v in hit.items()
                 if include_evidence or k != "evidence"}
                for hit in self.hits
            ],
        }
        return out


class ResidualIdentifierGate:
    """Build the source-identifier watch list and scan output artefacts."""

    def __init__(self) -> None:
        self._needles: list[Needle] = []
        self._uid_exact: set[str] = set()
        self._seen_id_values: dict[str, set[str]] = {
            "patient_name": set(),
            "patient_id": set(),
            "uid": set(),
        }

    # ------------------------------------------------------------------
    # watch-list construction
    # ------------------------------------------------------------------

    def add_patient_name(self, value: str | None) -> None:
        raw = (value or "").strip()
        if not raw or raw in self._seen_id_values["patient_name"]:
            return
        self._seen_id_values["patient_name"].add(raw)
        flat_full = norm_flat(raw)
        if flat_full:
            self._needles.append(Needle(
                value=raw, hit_type="patient_name", flat=flat_full))
        # DICOM PN components: family^given^middle^prefix^suffix, '=' empties
        for part in re.split(r"[\^=&]", nfkc(raw)):
            token = part.strip()
            if not token:
                continue
            flat = norm_flat(token)
            if not self._acceptable_name_part(flat):
                continue
            self._needles.append(Needle(
                value=token, hit_type="patient_name", flat=flat))

    @staticmethod
    def _acceptable_name_part(flat: str) -> bool:
        cjk = "".join(_CJK_RE.findall(flat))
        if len(cjk) >= MIN_CJK_NAME_PART:
            return True
        alnum = re.sub(r"[^0-9a-z]", "", flat)
        return len(alnum) >= MIN_ASCII_NAME_PART

    def add_patient_id(self, value: str | None) -> None:
        raw = (value or "").strip()
        if not raw or raw in self._seen_id_values["patient_id"]:
            return
        self._seen_id_values["patient_id"].add(raw)
        flat = norm_flat(raw)
        digits = re.sub(r"\D", "", flat)
        if len(flat) < MIN_ID_LEN or (flat.isdigit()
                                      and len(digits) < MIN_DIGIT_ID_LEN):
            return
        self._needles.append(Needle(
            value=raw, hit_type="patient_id", flat=flat))

    def add_uid(self, value: str | None) -> None:
        raw = (value or "").strip()
        if not raw or raw in self._seen_id_values["uid"]:
            return
        if len(raw) < MIN_UID_LEN or not _UID_RE.match(raw):
            return
        self._seen_id_values["uid"].add(raw)
        self._uid_exact.add(raw)
        flat = norm_flat(raw)
        self._needles.append(Needle(
            value=raw, hit_type="original_uid", flat=flat,
            min_len=MIN_UID_LEN))

    def identifier_counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._seen_id_values.items()}

    # ------------------------------------------------------------------
    # scanning
    # ------------------------------------------------------------------

    def scan_outputs(self, dicom_targets: Iterable[tuple[str, Any]],
                     report_obj: Any | None = None) -> GateResult:
        """Scan ``(output-relative-path, dataset-or-path)`` pairs and the
        JSON report object.  DICOM targets may be live datasets (preview) or
        paths on disk (real run)."""
        result = GateResult()
        for label, target in dicom_targets:
            try:
                ds = target if isinstance(target, Dataset) \
                    else pydicom.dcmread(target, force=True)
            except Exception as exc:  # noqa: BLE001 - unreadable output is a fail
                self._record(result, label, "(文件)", "无法读取输出 DICOM",
                             None, "scan_error", clip(exc))
                result.status = "blocked"
                continue
            result.scanned_dicom_files += 1
            self._scan_dataset(ds, label, result)

        if report_obj is not None:
            result.scanned_report = True
            self._scan_json(report_obj, "anonymization-report.json",
                            result, "")

        result.identifiers = self.identifier_counts()
        result.status = "blocked" if result.hits else "passed"
        return result

    def _scan_dataset(self, ds: Dataset, file_label: str,
                      result: GateResult, seq_path: str = "") -> None:
        levels: list[tuple[Dataset, str]] = []
        if ds.file_meta is not None:
            levels.append((ds.file_meta, "文件元信息 "))
        levels.append((ds, seq_path))
        while levels:
            current, prefix = levels.pop()
            for de in current:
                t = display_tag(de.tag)
                location = f"{prefix}{t} {de.name or ''}".strip()
                if de.VR == "SQ":
                    if de.value is not None:
                        for i, item in enumerate(de.value):
                            levels.append(
                                (item, f"{prefix}{t}[{i}] → "))
                    continue
                result.values_checked += 1
                if de.VR == "UI":
                    self._check_uid_element(file_label, location, de, result)
                elif de.VR in TEXT_VRS:
                    self._check_text_element(file_label, location, de.VR,
                                             de.value, result)

    def _check_uid_element(self, file_label: str, location: str,
                           de, result: GateResult) -> None:
        values = de.value
        if values is None:
            return
        vals = values if isinstance(
            values, (list, pydicom.multival.MultiValue)) else [values]
        for v in vals:
            if v is not None and str(v) in self._uid_exact:
                self._record(result, file_label, location, de.name,
                             de.VR, "original_uid", clip(v))

    def _check_text_element(self, file_label: str, location: str, vr: str,
                            value: Any, result: GateResult) -> None:
        text = self._element_text(value)
        if not text:
            return
        flat = norm_flat(text)
        # Several needles can match the same value (full PN plus a given-name
        # component); keep only the most specific (longest) one per type.
        best: dict[str, Needle] = {}
        for needle in self._needles:
            hit = False
            if needle.hit_type == "original_uid":
                if needle.flat and len(needle.flat) >= MIN_UID_LEN:
                    hit = re.search(
                        _UID_BOUNDARY_RE_TMPL.format(
                            uid=re.escape(needle.flat)),
                        flat) is not None
            else:
                hit = needle.matches_text(flat)
            if hit:
                prior = best.get(needle.hit_type)
                if prior is None or len(needle.flat) > len(prior.flat):
                    best[needle.hit_type] = needle
        for needle in best.values():
            self._record(result, file_label, location, None, vr,
                         needle.hit_type, clip(text))

    @staticmethod
    def _element_text(value: Any) -> str | None:
        if value is None or isinstance(value, (bytes, int, float)):
            return None
        if isinstance(value, (list, pydicom.multival.MultiValue)):
            parts = ["" if v is None else str(v) for v in value]
            return "\\".join(parts)
        return str(value)

    def _scan_json(self, node: Any, file_label: str, result: GateResult,
                   pointer: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                self._scan_json(val, file_label, result,
                                f"{pointer}/{key}")
            return
        if isinstance(node, list):
            for i, val in enumerate(node):
                self._scan_json(val, file_label, result,
                                f"{pointer}/{i}")
            return
        if isinstance(node, str):
            result.values_checked += 1
            flat = norm_flat(node)
            if not flat:
                return
            best: dict[str, Needle] = {}
            for needle in self._needles:
                hit = False
                if needle.hit_type == "original_uid":
                    if len(needle.flat) >= MIN_UID_LEN:
                        hit = re.search(
                            _UID_BOUNDARY_RE_TMPL.format(
                                uid=re.escape(needle.flat)),
                            flat) is not None
                else:
                    hit = needle.matches_text(flat)
                if hit:
                    prior = best.get(needle.hit_type)
                    if prior is None or len(needle.flat) > len(prior.flat):
                        best[needle.hit_type] = needle
            tag = pointer or "/"
            for needle in best.values():
                self._record(result, file_label, tag, "JSON 字段",
                             "json", needle.hit_type, clip(node))

    def _record(self, result: GateResult, file_label: str, tag: str,
                tag_name: str | None, vr: str | None, hit_type: str,
                evidence: str) -> None:
        if len(result.hits) >= MAX_HITS:
            result.truncated = True
            return
        result.hits.append({
            "file": file_label,
            "tag": tag,
            "tag_name": tag_name,
            "vr": vr,
            "hit_type": hit_type,
            "evidence": evidence,
        })
