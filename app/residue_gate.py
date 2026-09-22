"""Residual identifier gate ("残留标识门禁").

Runs immediately after anonymisation and *recursively* scans every output
DICOM dataset (file meta information, top-level attributes and all nested
sequences) plus the generated ``anonymization-report.json`` for identifiers
collected from the upload sources:

* patient names (full PN value, every ``^``/``=`` PN component and words),
* patient IDs (full value and separator-delimited components),
* original identity UIDs (Study / Series / SOP / Frame of Reference /
  referenced UIDs ... -- every UID the engine actually remapped).

Text-like elements are matched with word boundaries (so the short component
``san`` does not match inside ``sandy``); UID-shaped substrings embedded in
free text are extracted and compared exactly.  Any hit fails the gate, and
the caller must then block ZIP generation / download.

The gate never opens pixel data and never needs any network service.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pydicom
from pydicom.dataset import Dataset
from pydicom.tag import Tag

# ---------------------------------------------------------------------------
# Hit types
# ---------------------------------------------------------------------------

PATIENT_NAME = "patient_name"
PATIENT_ID = "patient_id"
ORIGINAL_UID = "original_uid"

HIT_TYPE_LABELS = {
    PATIENT_NAME: "患者姓名",
    PATIENT_ID: "患者 ID",
    ORIGINAL_UID: "原始 UID",
}

# Maximum hits carried in an API response / UI payload (counts stay exact).
MAX_HITS = 200
# Matching thresholds keep short/common fragments from causing false alarms.
MIN_LATIN_LEN = 3        # "zha"/"san" qualify, "ab" does not
MIN_DIGIT_LEN = 4        # all-digit fragments need 4+ chars
MIN_CJK_RUN = 2          # CJK ideograph runs of 2+ chars qualify
MAX_MATCHED_DISPLAY = 120

REPORT_FILE_NAME = "anonymization-report.json"

# Text-like VRs that may carry identifier strings; everything else (numbers,
# codes, binary blobs) is skipped.  Pixel data is skipped explicitly.
TEXT_VRS = {"AE", "CS", "LO", "LT", "PN", "SH", "ST", "UC", "UR", "UT"}
BINARY_VRS = {"OB", "OD", "OF", "OL", "OV", "OW", "UN"}
PIXEL_DATA_TAG = Tag(0x7FE0, 0x0010)

# A dotted-number token that *could* be a DICOM UID (>= 3 numeric components,
# root component 1-2 digits).  Membership of the source-UID set makes the
# final decision, so version strings etc. cannot produce false positives.
_UID_CANDIDATE_RE = re.compile(r"\d{1,2}(?:\.\d+){2,}")
_SPLIT_RE = re.compile(r"[\s\-_,;:/\\#]+")
_CJK_RUN_RE = re.compile(
    r"[㐀-䶿一-鿿豈-﫿\U00020000-\U0002A6DF]+"
)
_WORD_RUN_RE = re.compile(r"\w+", re.UNICODE)


class GateBlocked(Exception):
    """Raised by the job runner when the gate finds residual identifiers."""

    def __init__(self, result: "GateResult"):
        self.result = result
        n = result.exact_hit_count
        super().__init__(
            f"残留标识门禁未通过：在输出中发现 {n} 处源标识残留，"
            "ZIP 未生成。请调整匿名化选项后重试。")


# ---------------------------------------------------------------------------
# Normalisation / tokenisation
# ---------------------------------------------------------------------------

def _norm(value: Any) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).casefold()


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RUN_RE.search(text))


def _qualifies(word: str) -> bool:
    """Length/shape rules for a comparable token."""
    if not word:
        return False
    if _has_cjk(word):
        return any(len(run) >= MIN_CJK_RUN
                   for run in _CJK_RUN_RE.findall(word))
    if word.isdigit():
        return len(word) >= MIN_DIGIT_LEN
    return len(word) >= MIN_LATIN_LEN


def _add_token(out: set[str], token: str) -> None:
    token = token.strip(".-_ ")
    if _qualifies(token):
        out.add(token)


def _name_tokens(value: str) -> set[str]:
    """Expand a PatientName into comparable tokens.

    DICOM PN is ``family^given^middle^prefix^suffix=ideographic=phonetic``;
    compare the full name, every component, and whitespace-delimited words.
    """
    raw = str(value or "").strip()
    if not raw:
        return set()
    tokens: set[str] = set()
    whole = _norm(raw.replace("^", " ").replace("=", " "))
    _add_token(tokens, whole)
    for word in _WORD_RUN_RE.findall(whole):
        _add_token(tokens, word)
    for group in re.split(r"=", raw):
        for component in group.split("^"):
            _add_token(tokens, _norm(component))
            for run in _CJK_RUN_RE.findall(_norm(component)):
                if len(run) >= MIN_CJK_RUN:
                    tokens.add(run)
    return tokens


def _id_tokens(value: str) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    tokens: set[str] = set()
    _add_token(tokens, _norm(raw))
    for part in _SPLIT_RE.split(raw):
        _add_token(tokens, _norm(part))
    return tokens


def _contains(haystack: str, needle: str) -> bool:
    """Substring match with identifier-aware word boundaries.

    CJK runs have no inter-word separators, so they use plain substring
    matching (the minimum run length of two keeps false positives rare).
    Other tokens must be bounded by non-word characters (hyphens act as
    boundaries, so the ``pid`` part of ``PID-001`` still matches); this
    prevents ``san`` from matching inside ``sandy``.
    """
    if _has_cjk(needle):
        return needle in haystack
    if re.fullmatch(r"[\w\-]+", needle, flags=re.UNICODE):
        pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
        return re.search(pattern, haystack, flags=re.UNICODE) is not None
    return needle in haystack


def _value_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, pydicom.multival.MultiValue)):
        return "\\".join("" if v is None else str(v) for v in value)
    return str(value)


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class GateHit:
    file: str            # output-relative file, e.g. anonymization-report.json
    location: str        # human-readable tag/JSON-pointer location
    tag: str             # (gggg,eeee) or json:/pointer
    vr: str | None       # DICOM VR (None for report strings)
    hit_type: str        # patient_name | patient_id | original_uid
    matched: str         # source identifier that was found (capped on export)

    def as_dict(self) -> dict[str, Any]:
        matched = self.matched
        if len(matched) > MAX_MATCHED_DISPLAY:
            matched = matched[:MAX_MATCHED_DISPLAY] + "…"
        return {
            "file": self.file,
            "location": self.location,
            "tag": self.tag,
            "vr": self.vr,
            "hit_type": self.hit_type,
            "hit_type_label": HIT_TYPE_LABELS.get(self.hit_type,
                                                  self.hit_type),
            "matched": matched,
        }


@dataclass
class GateResult:
    passed: bool
    name_count: int = 0
    id_count: int = 0
    uid_count: int = 0
    scanned_dicom_files: int = 0
    scanned_report_files: int = 0
    values_checked: int = 0
    exact_hit_count: int = 0
    hits_truncated: bool = False
    name_hits: int = 0
    id_hits: int = 0
    uid_hits: int = 0
    hits: list[GateHit] = field(default_factory=list)
    _reported_locations: set = field(default_factory=set, repr=False)

    @property
    def hits_by_type(self) -> dict[str, int]:
        return {
            PATIENT_NAME: self.name_hits,
            PATIENT_ID: self.id_hits,
            ORIGINAL_UID: self.uid_hits,
        }

    def summary_dict(self) -> dict[str, Any]:
        """Report-ready summary (counts only -- never embeds source values)."""
        return {
            "passed": self.passed,
            "scanned_dicom_files": self.scanned_dicom_files,
            "scanned_report_files": self.scanned_report_files,
            "scanned_files": self.scanned_dicom_files
            + self.scanned_report_files,
            "values_checked": self.values_checked,
            "identifiers": {
                "patient_names": self.name_count,
                "patient_ids": self.id_count,
                "original_uids": self.uid_count,
            },
            "hit_count": self.exact_hit_count,
            "hits_by_type": self.hits_by_type,
            "hits_truncated": self.hits_truncated,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime()),
        }

    def as_dict(self) -> dict[str, Any]:
        data = self.summary_dict()
        data["hits"] = [hit.as_dict() for hit in self.hits]
        return data


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class IdentifierGate:
    def __init__(self, names: Iterable[str], ids: Iterable[str],
                 original_uids: Iterable[str]):
        self.name_tokens: set[str] = set()
        for value in names:
            self.name_tokens |= _name_tokens(value)
        self.id_tokens: set[str] = set()
        for value in ids:
            self.id_tokens |= _id_tokens(value)
        # Exact-match set; UIDs are compared verbatim.
        self.uids = {str(u).strip() for u in original_uids
                     if str(u or "").strip()}
        self.result = GateResult(
            passed=True,
            name_count=len(self.name_tokens),
            id_count=len(self.id_tokens),
            uid_count=len(self.uids),
        )

    # -- public entry points ------------------------------------------------

    def scan_outputs(self, out_root: Path, dicom_rels: list[str],
                     report_text: str) -> GateResult:
        """Scan written DICOM files (recursively, incl. sequences) and the
        serialised anonymization report."""
        for rel in dicom_rels:
            try:
                ds = pydicom.dcmread(out_root / rel, force=True)
            except Exception:  # noqa: BLE001
                # The file was just produced by the engine; an unreadable
                # output is an anonymiser failure (reported separately), not
                # an identity leak, so the gate skips it.
                continue
            self.scan_dataset(rel, ds)
        self._scan_report(report_text)
        self.result.passed = self.result.exact_hit_count == 0
        return self.result

    def scan_in_memory(self, items: list[tuple[str, Dataset]],
                       report_text: str) -> GateResult:
        """Preview-mode variant: scan anonymised datasets held in memory."""
        for rel, ds in items:
            self.scan_dataset(rel, ds)
        self._scan_report(report_text)
        self.result.passed = self.result.exact_hit_count == 0
        return self.result

    # -- DICOM scanning ------------------------------------------------------

    def scan_dataset(self, rel: str, ds: Dataset) -> None:
        self.result.scanned_dicom_files += 1
        # (dataset, location-prefix); file meta information is scanned too.
        stack: list[tuple[Dataset, str]] = [(ds, "")]
        if ds.file_meta is not None:
            stack.append((ds.file_meta, "文件元信息 / "))
        while stack:
            current, prefix = stack.pop()
            for de in current:
                tag = Tag(de.tag.group, de.tag.element)
                vr = de.VR or ""
                label = self._tag_label(tag, de)
                location = prefix + label
                if vr == "SQ":
                    if de.value is not None:
                        for i, item in enumerate(de.value):
                            stack.append((item, f"{location}[{i}] / "))
                    continue
                if tag == PIXEL_DATA_TAG or vr in BINARY_VRS:
                    continue
                if vr == "UI":
                    self._check_ui_element(rel, location, tag, de)
                elif vr in TEXT_VRS:
                    self._check_text_value(rel, location, tag, vr,
                                           _value_to_text(de.value))

    def _check_ui_element(self, rel: str, location: str, tag: Tag,
                          de) -> None:
        values = de.value
        if values is None:
            return
        if not isinstance(values, (list, pydicom.multival.MultiValue)):
            values = [values]
        for value in values:
            self.result.values_checked += 1
            text = str(value or "").strip()
            if text and text in self.uids:
                self._add_hit(GateHit(
                    file=rel, location=location,
                    tag=self._tag_token(tag), vr="UI",
                    hit_type=ORIGINAL_UID, matched=text))

    def _check_text_value(self, rel: str, location: str, tag: Tag,
                          vr: str | None, text: str) -> None:
        self.result.values_checked += 1
        if not text:
            return
        tag_token = "(raw)" if tag is None else self._tag_token(tag)
        normalised = _norm(text)
        for token in self.name_tokens:
            if _contains(normalised, token):
                self._add_hit(GateHit(
                    file=rel, location=location,
                    tag=tag_token, vr=vr,
                    hit_type=PATIENT_NAME, matched=token))
        for token in self.id_tokens:
            if _contains(normalised, token):
                self._add_hit(GateHit(
                    file=rel, location=location,
                    tag=tag_token, vr=vr,
                    hit_type=PATIENT_ID, matched=token))
        for candidate in _UID_CANDIDATE_RE.findall(text):
            if candidate in self.uids:
                self._add_hit(GateHit(
                    file=rel, location=location,
                    tag=tag_token, vr=vr,
                    hit_type=ORIGINAL_UID, matched=candidate))

    # -- report scanning -----------------------------------------------------

    def _scan_report(self, text: str) -> None:
        if not text:
            return
        self.result.scanned_report_files += 1
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            self._check_text_value(REPORT_FILE_NAME, "报告原始文本",
                                   None, None, text)
            return
        self._walk_json(obj, "")

    def _walk_json(self, node: Any, pointer: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                self._walk_json(value, f"{pointer}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                self._walk_json(value, f"{pointer}/{i}")
        elif isinstance(node, str):
            self.result.values_checked += 1
            location = f"anonymization-report.json {pointer or '/'}"
            normalised = _norm(node)
            for token in self.name_tokens:
                if _contains(normalised, token):
                    self._add_hit(GateHit(
                        file=REPORT_FILE_NAME, location=location,
                        tag=f"json:{pointer or '/'}", vr=None,
                        hit_type=PATIENT_NAME, matched=token))
            for token in self.id_tokens:
                if _contains(normalised, token):
                    self._add_hit(GateHit(
                        file=REPORT_FILE_NAME, location=location,
                        tag=f"json:{pointer or '/'}", vr=None,
                        hit_type=PATIENT_ID, matched=token))
            for candidate in _UID_CANDIDATE_RE.findall(node):
                if candidate in self.uids:
                    self._add_hit(GateHit(
                        file=REPORT_FILE_NAME, location=location,
                        tag=f"json:{pointer or '/'}", vr=None,
                        hit_type=ORIGINAL_UID, matched=candidate))

    # -- helpers -------------------------------------------------------------

    def _add_hit(self, hit: GateHit) -> None:
        self.result.exact_hit_count += 1
        if hit.hit_type == PATIENT_NAME:
            self.result.name_hits += 1
        elif hit.hit_type == PATIENT_ID:
            self.result.id_hits += 1
        elif hit.hit_type == ORIGINAL_UID:
            self.result.uid_hits += 1
        # Collapse several tokens of the same kind hitting one element into
        # a single actionable row (counts above remain exact).
        key = (hit.file, hit.location, hit.hit_type)
        if key in self.result._reported_locations:
            return
        self.result._reported_locations.add(key)
        if len(self.result.hits) >= MAX_HITS:
            self.result.hits_truncated = True
            return
        self.result.hits.append(hit)

    @staticmethod
    def _tag_token(tag) -> str:
        return f"({tag.group:04X},{tag.element:04X})"

    def _tag_label(self, tag, de=None) -> str:
        token = self._tag_token(tag)
        name = None
        if de is not None:
            try:
                name = de.name
            except Exception:  # noqa: BLE001
                name = None
        if not name:
            name = pydicom.datadict.keyword_for_tag(tag)
        return f"{token} {name}" if name else token
