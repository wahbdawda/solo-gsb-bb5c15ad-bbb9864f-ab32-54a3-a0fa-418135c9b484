"""DICOM tag anonymisation rules and profile presets.

The rule table mirrors DICOM PS3.15 Annex E "Basic Application
Confidentiality Profile" plus the standard retain-option attributes.

Actions
-------
remove : delete the attribute (PS3.15 action D)
blank  : empty the value (X/Z target attributes); PatientName/PatientID are
         replaced by stable per-patient pseudonyms instead of empty strings
date   : temporal attribute (DA/TM/DT). Blanked by default; shifted by a
         per-patient fixed offset when ``keep_dates`` is enabled (retain
         longitudinal temporal information, action Z)
uid    : remap deterministically (action X). Any *other* attribute whose VR
         is UI is remapped as well, except SOP-class / transfer-syntax /
         implementation UIDs (see ``PRESERVED_UID_TAGS``)
keep   : retained; registered only so the UI can render the decision

Gates
-----
A rule may carry an option gate.  ``gate="keep_xxx"`` means the attribute is
kept when that option is enabled; ``gate="!strip_overlays"`` is inverted
(the removal applies when the option *is* enabled).

Tags not listed here are handled by the engine fallback: PN person names are
blanked, DA/TM/DT values follow the date policy, UI values are remapped, and
everything else is retained.
"""
from __future__ import annotations

from pydicom.tag import Tag

REMOVE = "remove"
BLANK = "blank"
DATE = "date"
UID = "uid"
KEEP = "keep"

TAG_RULES: list[tuple[int, str, str | None, str]] = []


def _r(group: int, element: int, action: str, gate: str | None = None,
       note: str = "") -> None:
    TAG_RULES.append((Tag(group, element), action, gate, note))


# ---------------------------------------------------------------------------
# Patient identity (pseudonymised) / demographics
# ---------------------------------------------------------------------------
_r(0x0010, 0x0010, BLANK, note="PatientName -> stable pseudonym")
_r(0x0010, 0x0020, BLANK, note="PatientID -> stable pseudonymous id")
_r(0x0010, 0x0021, BLANK)                 # Issuer of Patient ID
_r(0x0010, 0x0024, REMOVE)                # Issuer of Patient ID Qualifiers Seq
_r(0x0010, 0x1000, REMOVE)                # Other Patient IDs
_r(0x0010, 0x1001, REMOVE)                # Other Patient Names
_r(0x0010, 0x1002, REMOVE)                # Other Patient IDs Sequence
_r(0x0010, 0x1005, REMOVE)                # Patient's Birth Name
_r(0x0010, 0x1060, REMOVE)                # Patient's Mother's Birth Name
_r(0x0010, 0x1040, REMOVE)                # Patient's Address
_r(0x0010, 0x1080, REMOVE)                # Military Rank
_r(0x0010, 0x1081, REMOVE)                # Branch of Service
_r(0x0010, 0x1090, REMOVE)                # Medical Record Locator
_r(0x0010, 0x2000, REMOVE)                # Medical Alerts
_r(0x0010, 0x2110, REMOVE)                # Allergies
_r(0x0010, 0x2150, REMOVE)                # Country of Residence
_r(0x0010, 0x2152, REMOVE)                # Region of Residence
_r(0x0010, 0x2154, REMOVE)                # Patient's Telephone Numbers
_r(0x0010, 0x21B0, REMOVE)                # Additional Patient History
_r(0x0010, 0x21D0, REMOVE)                # Last Menstrual Date
_r(0x0010, 0x21F0, REMOVE)                # Patient's Religious Preference
_r(0x0010, 0x4000, REMOVE)                # Patient Comments

# Retain-patient-characteristics option (PS3.15)
_r(0x0010, 0x0030, DATE, "keep_chars",
   "PatientBirthDate (retain-patient-characteristics)")
_r(0x0010, 0x0032, REMOVE)                # Patient's Birth Time
_r(0x0010, 0x0040, BLANK, "keep_chars", "PatientSex")
_r(0x0010, 0x1010, BLANK, "keep_chars", "PatientAge")
_r(0x0010, 0x2160, BLANK, "keep_chars", "Ethnic Group")
_r(0x0010, 0x2180, BLANK, "keep_chars", "Occupation")
_r(0x0010, 0x21A0, BLANK, "keep_chars", "Smoking Status")
_r(0x0010, 0x21C0, BLANK, "keep_chars", "Pregnancy Status")

# ---------------------------------------------------------------------------
# Institution / station / device identity (retain-device-identity option)
# ---------------------------------------------------------------------------
_r(0x0008, 0x0080, BLANK, "keep_site", "Institution Name")
_r(0x0008, 0x0081, REMOVE, "keep_site", "Institution Address")
_r(0x0008, 0x0082, BLANK, "keep_device", "Institution Code Sequence")
_r(0x0008, 0x1040, BLANK, "keep_device", "Institutional Department Name")
_r(0x0008, 0x1010, BLANK, "keep_device", "Station Name")
_r(0x0008, 0x0070, BLANK, "keep_device", "Manufacturer")
_r(0x0008, 0x1090, BLANK, "keep_device", "Manufacturer's Model Name")
_r(0x0018, 0x1000, BLANK, "keep_device", "Device Serial Number")
_r(0x0018, 0x1002, BLANK, "keep_device", "Device ID")
_r(0x0018, 0x100A, UID, "keep_device", "Device UID")
_r(0x0018, 0x1020, BLANK, "keep_device", "Software Versions")
_r(0x0002, 0x0016, BLANK, "keep_device", "Source Application Entity Title")
_r(0x0040, 0x0001, BLANK, "keep_device", "Scheduled Station AE Title")
_r(0x0040, 0x0010, BLANK, "keep_device", "Scheduled Station Name")
_r(0x0040, 0x0241, BLANK, "keep_device", "Performed Station AE Title")
_r(0x0040, 0x0242, BLANK, "keep_device", "Performed Station Name")

# ---------------------------------------------------------------------------
# Staff / physician identity (retain-staff/physician-identity option)
# ---------------------------------------------------------------------------
for _g, _e, _n in [
    (0x0008, 0x0090, "Referring Physician's Name"),
    (0x0008, 0x0092, "Referring Physician's Address"),
    (0x0008, 0x0094, "Referring Physician's Telephone Numbers"),
    (0x0008, 0x0096, "Referring Physician Identification Sequence"),
    (0x0008, 0x1048, "Physician(s) of Record"),
    (0x0008, 0x1049, "Physician(s) of Record Identification Sequence"),
    (0x0008, 0x1050, "Performing Physician's Name"),
    (0x0008, 0x1052, "Performing Physician Identification Sequence"),
    (0x0008, 0x1060, "Name of Physician(s) Reading Study"),
    (0x0008, 0x1070, "Operators' Name"),
    (0x0008, 0x1072, "Operator Identification Sequence"),
    (0x0032, 0x1031, "Requesting Physician Identification Sequence"),
    (0x0032, 0x1032, "Requesting Physician"),
    (0x0032, 0x1033, "Requesting Service"),
    (0x0070, 0x0084, "Content Creator's Name"),
    (0x0070, 0x0086, "Content Creator's Identification Code Sequence"),
    (0x0040, 0xA073, "Verifying Observer Sequence"),
    (0x0040, 0xA075, "Verifying Observer Name"),
]:
    _r(_g, _e, BLANK, "keep_staff", f"{_n} (retain-physician/staff option)")

# ---------------------------------------------------------------------------
# Study / order identifiers
# ---------------------------------------------------------------------------
_r(0x0008, 0x0050, BLANK)                   # Accession Number
_r(0x0008, 0x0051, REMOVE)                  # Issuer of Accession Number Sequence
_r(0x0020, 0x0010, BLANK)                   # Study ID
_r(0x0038, 0x0010, BLANK)                   # Admission ID
_r(0x0038, 0x0011, REMOVE)                  # Issuer of Admission ID
_r(0x0038, 0x0014, REMOVE)                  # Issuer of Admission ID Sequence
_r(0x0040, 0x1001, REMOVE)                  # Requested Procedure ID
_r(0x0040, 0x1006, REMOVE)                  # Placer Order Number / Procedure
_r(0x0040, 0x1007, REMOVE)                  # Filler Order Number / Procedure
_r(0x0040, 0x2011, REMOVE)                  # Order Callback Phone Number
_r(0x0040, 0x2016, REMOVE)                  # Placer Order Number / Imaging Svc Req
_r(0x0040, 0x2017, REMOVE)                  # Filler Order Number / Imaging Svc Req
_r(0x0040, 0x3000, REMOVE)                  # Referenced Patient Alias Sequence
_r(0x0038, 0x0050, REMOVE)                  # Special Needs

# Patient/visit-embedding sequences (PS3.15 D); Referenced Study sequences are
# deliberately kept so cross-study examination relationships survive -- the
# UIDs inside them are remapped during the recursive walk.
_r(0x0008, 0x1120, REMOVE)                  # Referenced Patient Sequence
_r(0x0008, 0x1125, REMOVE)                  # Referenced Visit Sequence
_r(0x0008, 0x1080, REMOVE)                  # Admitting Diagnoses Description
_r(0x0008, 0x1084, REMOVE)                  # Admitting Diagnoses Code Sequence
_r(0x0032, 0x1030, REMOVE)                  # Reason for Study

# ---------------------------------------------------------------------------
# Free-text descriptions / comments
# ---------------------------------------------------------------------------
_r(0x0008, 0x1030, BLANK, "keep_descriptions", "Study Description")
_r(0x0008, 0x103E, BLANK, "keep_descriptions", "Series Description")
_r(0x0018, 0x1030, BLANK, "keep_protocol", "Protocol Name")
_r(0x0018, 0x4000, REMOVE)                  # Acquisition Comments
_r(0x0020, 0x4000, REMOVE)                  # Study Comments
_r(0x0028, 0x4000, REMOVE)                  # Image Comments
_r(0x0032, 0x4000, REMOVE)                  # Study Request Comments
_r(0x0040, 0x0500, REMOVE)                  # Patient Request Comments Sequence
_r(0x0040, 0x0254, BLANK, "keep_descriptions",
   "Performed Procedure Step Description")
_r(0x0070, 0x0081, REMOVE)                  # Content Description
_r(0x0070, 0x0087, REMOVE)                  # Alternate Content Description Sequence

# Overlays / curves / graphic annotation sequences (extra scrub option)
_r(0x0040, 0x0555, REMOVE, "!strip_overlays",
   "Acquisition Context Sequence")
_r(0x0070, 0x0001, REMOVE, "!strip_overlays",
   "Graphic Annotation Sequence")
_r(0x0070, 0x0060, REMOVE, "!strip_overlays",
   "Graphic Layer Sequence")

# ---------------------------------------------------------------------------
# Temporal attributes (Z family)
# ---------------------------------------------------------------------------
for _g, _e, _n in [
    (0x0008, 0x0020, "Study Date"),
    (0x0008, 0x0021, "Series Date"),
    (0x0008, 0x0022, "Acquisition Date"),
    (0x0008, 0x0023, "Content Date"),
    (0x0008, 0x0024, "Overlay Date"),
    (0x0008, 0x0025, "Curve Date"),
    (0x0008, 0x002A, "Acquisition DateTime"),
    (0x0008, 0x0030, "Study Time"),
    (0x0008, 0x0031, "Series Time"),
    (0x0008, 0x0032, "Acquisition Time"),
    (0x0008, 0x0033, "Content Time"),
    (0x0008, 0x0034, "Overlay Time"),
    (0x0008, 0x0035, "Curve Time"),
    (0x0008, 0x1082, "Instance Creation Date"),
    (0x0008, 0x1083, "Instance Creation Time"),
    (0x0018, 0x1200, "Date of Secondary Capture"),
    (0x0018, 0x1201, "Time of Secondary Capture"),
    (0x0032, 0x1000, "Scheduled Study Date"),
    (0x0032, 0x1001, "Scheduled Study Time"),
    (0x0038, 0x0020, "Admitting Date"),
    (0x0038, 0x0021, "Admitting Time"),
    (0x0040, 0x0002, "Scheduled Procedure Step Start Date"),
    (0x0040, 0x0003, "Scheduled Procedure Step Start Time"),
    (0x0040, 0x0244, "Performed Procedure Step Start Date"),
    (0x0040, 0x0245, "Performed Procedure Step Start Time"),
    (0x0040, 0x0250, "Performed Procedure Step End Date"),
    (0x0040, 0x0251, "Performed Procedure Step End Time"),
    (0x0040, 0x2000, "Issue Date of Imaging Service Request"),
    (0x0040, 0x2001, "Issue Time of Imaging Service Request"),
    (0x0040, 0xA030, "Verification DateTime"),
    (0x0070, 0x0082, "Presentation Creation Date"),
    (0x0070, 0x0083, "Presentation Creation Time"),
    (0x3006, 0x0008, "Structure Set Date"),
    (0x3006, 0x0009, "Structure Set Time"),
    (0x3008, 0x0250, "Treatment Date"),
    (0x3008, 0x0251, "Treatment Time"),
    (0x300A, 0x0006, "RT Plan Date"),
    (0x300A, 0x0007, "RT Plan Time"),
]:
    _r(_g, _e, DATE, None, _n)

# ---------------------------------------------------------------------------
# UIDs (X family) -- explicitly labelled; generic UI walk covers the rest
# ---------------------------------------------------------------------------
for _g, _e, _n in [
    (0x0002, 0x0003, "Media Storage SOP Instance UID"),
    (0x0008, 0x0018, "SOP Instance UID"),
    (0x0020, 0x000D, "Study Instance UID"),
    (0x0020, 0x000E, "Series Instance UID"),
    (0x0020, 0x0052, "Frame of Reference UID"),
    (0x0008, 0x1155, "Referenced SOP Instance UID"),
    (0x3006, 0x0024, "Referenced Frame of Reference UID"),
    (0x0020, 0x9161, "Concatenation UID"),
    (0x0020, 0x9164, "Concatenation UID"),
    (0x0020, 0x9165, "Dimension Organization UID"),
    (0x0008, 0x1195, "Transaction UID"),
    (0x0018, 0x2042, "Target UID"),
]:
    _r(_g, _e, UID, None, _n)

# Registry / transfer-syntax UIDs are structure identifiers, never identity.
PRESERVED_UID_TAGS = {
    Tag(0x0002, 0x0010),  # Transfer Syntax UID
    Tag(0x0002, 0x0002),  # Media Storage SOP Class UID
    Tag(0x0008, 0x0016),  # SOP Class UID
    Tag(0x0008, 0x1150),  # Referenced SOP Class UID
    Tag(0x0002, 0x0012),  # Implementation Class UID
    Tag(0x0008, 0x0014),  # Implementation Class UID
    Tag(0x0008, 0x010D),  # Context Group Extension Creator UID
}
for _t in (0x0002, 0x0010), (0x0002, 0x0002), (0x0008, 0x0016), \
          (0x0008, 0x1150), (0x0002, 0x0012), (0x0008, 0x0014), \
          (0x0008, 0x010D):
    _r(_t[0], _t[1], KEEP)

# Well-known synchronisation frame-of-reference UIDs (values, not tags).
WELL_KNOWN_UID_PREFIXES = ("1.2.840.10008.15.1.",)

# ---------------------------------------------------------------------------
# A few explicitly retained clinical attributes (labelled for the UI)
# ---------------------------------------------------------------------------
for _g, _e, _n in [
    (0x0008, 0x0005, "Specific Character Set"),
    (0x0008, 0x0008, "Image Type"),
    (0x0008, 0x0060, "Modality"),
    (0x0020, 0x0011, "Series Number"),
    (0x0020, 0x0013, "Instance Number"),
    (0x0028, 0x0010, "Rows"),
    (0x0028, 0x0011, "Columns"),
]:
    _r(_g, _e, KEEP, None, _n)

# Build the lookup
RULE_BY_TAG: dict[int, tuple[str, str | None, str]] = {}
for _tag, _action, _gate, _note in TAG_RULES:
    RULE_BY_TAG[_tag] = (_action, _gate, _note)

# ---------------------------------------------------------------------------
# Profile presets
# ---------------------------------------------------------------------------
OPTION_KEYS = [
    "keep_dates",        # shift dates instead of blanking them
    "keep_chars",        # retain patient characteristics (PS3.15 option)
    "keep_staff",        # retain staff/physician identity
    "keep_site",         # retain institution identity
    "keep_device",       # retain device/station identity
    "keep_protocol",     # retain protocol name
    "keep_descriptions", # retain study/series descriptions
    "strip_overlays",    # remove overlay/curve groups & graphic annotations
]

PROFILES: dict[str, dict] = {
    "light": {
        "label": "轻度（内部流转）",
        "description": "仅重写患者身份字段与全部 UID，保留机构、设备、日期与描述。适用于院内可信系统间流转，不用于外发。",
        "options": {
            "keep_dates": True,
            "keep_chars": True,
            "keep_staff": True,
            "keep_site": True,
            "keep_device": True,
            "keep_protocol": True,
            "keep_descriptions": True,
            "strip_overlays": False,
        },
    },
    "basic": {
        "label": "标准（DICOM PS3.15 基础保密方案）",
        "description": "遵循 DICOM PS3.15 Annex E 基础应用保密配置文件：移除/置空身份标识与自由文本，重写所有 UID，日期置空。",
        "options": {k: False for k in OPTION_KEYS},
    },
    "recommended": {
        "label": "推荐（外发研究，保留时间纵向一致性）",
        "description": "在标准基础上按患者进行固定天数日期平移（检查间隔保持一致），保留年龄/性别特征与协议名；同时移除覆盖层与图形注释中可能烧录的文字。",
        "options": {
            "keep_dates": True,
            "keep_chars": True,
            "keep_staff": False,
            "keep_site": False,
            "keep_device": False,
            "keep_protocol": True,
            "keep_descriptions": False,
            "strip_overlays": True,
        },
    },
    "strict": {
        "label": "严格（最大化去标识）",
        "description": "不保留日期、人口学特征、协议名与自由文本，移除全部私有标签、覆盖层、曲线与图形注释。像素烧录文字仍需人工核查。",
        "options": {k: False for k in OPTION_KEYS} | {"strip_overlays": True},
    },
}

DEFAULT_PROFILE = "recommended"
