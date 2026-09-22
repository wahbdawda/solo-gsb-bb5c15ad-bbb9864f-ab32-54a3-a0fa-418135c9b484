"""Job orchestration: upload storage, preview runs and ZIP packaging."""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .anonymizer import Anonymizer, FileInfo
from .dicom_catalog import PROFILES
from .gate import ResidualIdentifierGate


class GateBlockedError(Exception):
    """Raised when the residual identifier gate finds source identifiers."""

    def __init__(self, gate: dict):
        self.gate = gate
        super().__init__("残留标识门禁未通过：输出中仍存在原始患者标识或 UID。")


def _safe_relpath(name: str) -> str:
    """Normalise an upload filename into a safe relative POSIX path."""
    name = name.replace("\\", "/").lstrip("/")
    parts: list[str] = []
    for part in name.split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        parts.append(part)
    if not parts:
        part = "file"
        parts = [part]
    return "/".join(parts) or f"file-{uuid.uuid4().hex[:8]}"


@dataclass
class Job:
    id: str
    created_at: float
    upload_dir: Path
    sources: list[Path]
    relpaths: list[str]
    status: str = "uploaded"          # uploaded|previewed|processing|done|error
    error: str | None = None
    zip_path: Path | None = None
    zip_filename: str | None = None
    last_preview: dict | None = None
    last_result: dict | None = None
    gate: dict | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._guard = threading.Lock()

    def create(self, pairs: list[tuple[str, bytes]]) -> Job:
        job_id = uuid.uuid4().hex
        upload_dir = config.DATA_DIR / job_id / "in"
        upload_dir.mkdir(parents=True, exist_ok=True)

        seen: set[str] = set()
        sources: list[Path] = []
        relpaths: list[str] = []
        for raw_name, data in pairs:
            rel = _safe_relpath(raw_name or f"file-{uuid.uuid4().hex[:8]}")
            if rel in seen:
                stem, dot, suffix = rel.rpartition(".")
                rel = f"{stem}-{uuid.uuid4().hex[:6]}{dot}{suffix}" if dot \
                    else f"{rel}-{uuid.uuid4().hex[:6]}"
            seen.add(rel)
            target = upload_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            sources.append(target)
            relpaths.append(rel)

        job = Job(
            id=job_id,
            created_at=time.time(),
            upload_dir=upload_dir,
            sources=sources,
            relpaths=relpaths,
        )
        with self._guard:
            self._jobs[job_id] = job
        self.cleanup_expired()
        return job

    def get(self, job_id: str) -> Job:
        with self._guard:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def cleanup_expired(self) -> None:
        now = time.time()
        with self._guard:
            expired = [j for j in self._jobs.values()
                       if now - j.created_at > config.JOB_TTL_SECONDS]
            for job in expired:
                self._jobs.pop(job.id, None)
                shutil.rmtree(job.upload_dir.parent, ignore_errors=True)

    def remove(self, job_id: str) -> None:
        with self._guard:
            job = self._jobs.pop(job_id, None)
        if job is not None:
            shutil.rmtree(job.upload_dir.parent, ignore_errors=True)


STORE = JobStore()


def resolve_options(profile_name: str, overrides: dict | None = None
                    ) -> tuple[str, dict[str, bool]]:
    """Merge a profile preset with explicit client-side overrides."""
    profile_name = profile_name or "recommended"
    if profile_name not in PROFILES:
        raise ValueError(f"未知配置：{profile_name}")
    opts = dict(PROFILES[profile_name]["options"])
    if overrides:
        for key, value in overrides.items():
            if key in opts:
                opts[key] = bool(value)
    return profile_name, opts


def _build_file_infos(job: Job) -> list[FileInfo]:
    return [FileInfo(index=i, source_path=rel)
            for i, rel in enumerate(job.relpaths)]


def _build_gate(anon: Anonymizer) -> ResidualIdentifierGate:
    """Assemble the source-identifier watch list from a prescanned run."""
    gate = ResidualIdentifierGate()
    for ctx_key in anon.contexts:
        # Patient context keys are built by the engine as
        # "id=<id>|name=<name>|birth=<date>"; recover both components.
        pid, pname = "", ""
        for part in ctx_key.split("|"):
            if part.startswith("id="):
                pid = part[3:]
            elif part.startswith("name="):
                pname = part[5:]
        gate.add_patient_name(pname)
        gate.add_patient_id(pid)
    # Original identity also exists on per-file metadata (patients not
    # captured through a context key, e.g. odd encodings) -- add those too.
    for values, add in (
        (anon.source_patient_names, gate.add_patient_name),
        (anon.source_patient_ids, gate.add_patient_id),
    ):
        for value in values:
            add(value)
    # Every UI value the engine actually remapped (preserved well-known
    # UIDs are excluded because old == new there).
    for original, replacement in anon.uid_map.items():
        if original != replacement:
            gate.add_uid(original)
    return gate


def _build_report(profile_name: str, options: dict[str, bool],
                  anon: Anonymizer, infos: list[FileInfo],
                  warnings: list[str], gate_summary: dict | None) -> dict:
    return {
        "tool": "dicom-anonymisation-workbench",
        "profile": profile_name,
        "profile_label": PROFILES[profile_name]["label"],
        "options": options,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime()),
        "counts": anon.counts,
        "warnings": warnings,
        "residual_identifier_gate": gate_summary,
        "files": [
            {
                "output_path": fi.output_path,
                "source_relative_path": fi.source_path,
                "modality": fi.modality,
                "sop_class_uid": fi.sop_class_uid,
                "sop_class_name": fi.sop_class_name,
                "pseudonymous_patient_id": fi.new_patient_id,
                "pseudonymous_patient_name": fi.new_patient_name,
                "new_study_instance_uid": fi.new_study_uid,
                "new_series_instance_uid": fi.new_series_uid,
                "new_sop_instance_uid": fi.new_sop_uid,
            }
            for fi in infos if fi.output_path
        ],
        "skipped": [
            {"source_relative_path": fi.source_path,
             "reason": fi.error}
            for fi in infos if not fi.output_path
        ],
    }


def run(job: Job, profile_name: str, options: dict[str, bool], salt: str,
        write: bool) -> dict:
    """Execute a preview (write=False) or a full anonymisation run."""
    with job.lock:
        if write:
            job.status = "processing"
            job.error = None
            job.gate = None
            job.zip_path = None
            job.zip_filename = None
            # Remove artefacts of earlier (possibly blocked) runs so the gate
            # only scans files produced by this exact configuration.
            out_root = job.upload_dir.parent / "out"
            if out_root.exists():
                shutil.rmtree(out_root, ignore_errors=True)
            stale_zip = job.upload_dir.parent / \
                f"anonymized-{job.id[:8]}.zip"
            if stale_zip.exists():
                stale_zip.unlink()

        infos = _build_file_infos(job)
        anon = Anonymizer(options=options, salt=salt)

        anon.prescan(infos, lambda fi: job.sources[fi.index])

        if write:
            out_root.mkdir(parents=True, exist_ok=True)

        preview_datasets: dict[int, object] = {}
        for fi in infos:
            if fi.is_dicom:
                ds = anon.apply_file(fi, job.sources[fi.index],
                                     out_root if write else None)
                if ds is not None and fi.output_path:
                    preview_datasets[fi.index] = ds
            else:
                anon.counts["skipped"] += 1

        warnings = list(anon.global_warnings)
        for fi in infos:
            for w in fi.warnings:
                msg = f"文件 #{fi.index + 1}（{fi.source_path}）: {w}"
                if msg not in warnings:
                    warnings.append(msg)

        valid = [fi for fi in infos if fi.output_path]
        if write and not valid:
            job.status = "error"
            job.error = "上传文件中没有可匿名化的 DICOM 影像。"
            raise ValueError(job.error)

        # -- residual identifier gate ------------------------------------
        gate_scanner = _build_gate(anon)
        dicom_targets = [
            (fi.output_path, preview_datasets[fi.index])
            for fi in valid if fi.index in preview_datasets
        ]

        # The report is built first (its contents are part of the scan), but
        # the gate summary is only attached once the scan result is known.
        report = _build_report(profile_name, options, anon, infos,
                               warnings, gate_summary=None)
        gate_result = gate_scanner.scan_outputs(dicom_targets, report)
        gate_summary = gate_result.as_dict(include_evidence=False)
        report["residual_identifier_gate"] = gate_summary
        job.gate = gate_result.as_dict(include_evidence=True)

        if write:
            report_path = out_root / "anonymization-report.json"

        payload = {
            "files": [_file_payload(fi, include_source=not write)
                      for fi in infos],
            "changes": [ch.as_dict()
                        for fi in infos for ch in fi.changes]
            if not write else [],
            "warnings": warnings,
            "counts": anon.counts,
            "patients": [
                {
                    "key": ctx.key if not write else None,
                    "pseudonym": ctx.pseudo_name,
                    "pseudonymous_id": ctx.pseudo_id,
                    "day_offset": ctx.day_offset,
                }
                for ctx in anon.contexts.values()
            ],
            "uid_map": anon.uid_map if not write else {},
            "profile": profile_name,
            "gate": job.gate,
        }
        if not write:
            job.last_preview = payload
            if gate_result.status != "blocked":
                job.status = "previewed"
            return payload

        job.last_result = payload
        if gate_result.status == "blocked":
            job.status = "blocked"
            job.error = ("残留标识门禁未通过：输出中检测到原始患者姓名 / "
                         "患者 ID / UID，已阻止 ZIP 生成。")
            # Keep the unshipped outputs for inspection under the job's
            # TTL, but never produce a downloadable ZIP.
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8")
            raise GateBlockedError(job.gate)

        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8")

        zip_path = job.upload_dir.parent / \
            f"anonymized-{job.id[:8]}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fi in valid:
                zf.write(out_root / fi.output_path, fi.output_path)
            zf.write(report_path, "anonymization-report.json")
        job.zip_path = zip_path
        job.zip_filename = f"anonymized-{job.id[:8]}.zip"
        job.status = "done"
        return payload


def _file_payload(fi: FileInfo, include_source: bool = True) -> dict:
    payload = {
        "index": fi.index,
        "source_path": fi.source_path,
        "output_path": fi.output_path,
        "sop_class_uid": fi.sop_class_uid,
        "sop_class_name": fi.sop_class_name,
        "modality": fi.modality,
        "new_patient_id": fi.new_patient_id,
        "new_patient_name": fi.new_patient_name,
        "new_study_uid": fi.new_study_uid,
        "new_series_uid": fi.new_series_uid,
        "new_sop_uid": fi.new_sop_uid,
        "error": fi.error,
    }
    if include_source:
        # Original identifiers are needed for the interactive preview only;
        # they must not linger in server-side state of a write run.
        payload.update({
            "old_patient_id": fi.old_patient_id,
            "old_patient_name": fi.old_patient_name,
            "old_study_uid": fi.old_study_uid,
            "old_series_uid": fi.old_series_uid,
            "old_sop_uid": fi.old_sop_uid,
        })
    return payload
