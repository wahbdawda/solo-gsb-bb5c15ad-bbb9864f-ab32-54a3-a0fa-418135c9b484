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

from pydicom.dataset import Dataset

from . import config
from .anonymizer import Anonymizer, FileInfo
from .dicom_catalog import PROFILES
from .residue_gate import GateBlocked, IdentifierGate


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
    status: str = "uploaded"
    # uploaded|previewed|processing|done|blocked|error
    error: str | None = None
    zip_path: Path | None = None
    zip_filename: str | None = None
    last_preview: dict | None = None
    last_result: dict | None = None
    last_gate: dict | None = None
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


def _build_report(profile_name: str, options: dict[str, bool],
                  anon: Anonymizer, infos: list[FileInfo],
                  warnings: list[str], gate_summary: dict | None
                  ) -> dict:
    valid = [fi for fi in infos if fi.output_path]
    report = {
        "tool": "dicom-anonymisation-workbench",
        "profile": profile_name,
        "profile_label": PROFILES[profile_name]["label"],
        "options": options,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "counts": anon.counts,
        "warnings": warnings,
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
            for fi in valid
        ],
        "skipped": [
            {"source_relative_path": fi.source_path,
             "reason": fi.error}
            for fi in infos if not fi.output_path
        ],
    }
    if gate_summary is not None:
        report["residue_identifier_gate"] = gate_summary
    return report


def _run_gate(anon: Anonymizer, infos: list[FileInfo],
              in_memory: dict[int, Dataset] | None, out_root: Path | None,
              report_text: str):
    """Build the gate from source identifiers and scan every output."""
    names: list[str] = []
    ids: list[str] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for fi in infos:
        if fi.old_patient_name and fi.old_patient_name not in seen_names:
            seen_names.add(fi.old_patient_name)
            names.append(fi.old_patient_name)
        if fi.old_patient_id and fi.old_patient_id not in seen_ids:
            seen_ids.add(fi.old_patient_id)
            ids.append(fi.old_patient_id)
    # Only UIDs the engine actually changed (well-known/synchronisation UIDs
    # map to themselves and cannot be "residual" identifiers).
    original_uids = [old for old, new in anon.uid_map.items() if old != new]
    gate = IdentifierGate(names, ids, original_uids)
    valid = [fi for fi in infos if fi.output_path]
    if in_memory is not None:
        items = [(fi.output_path, in_memory[fi.index])
                 for fi in valid if fi.index in in_memory]
        return gate.scan_in_memory(items, report_text)
    rels = [fi.output_path for fi in valid
            if (out_root / fi.output_path).exists()]
    return gate.scan_outputs(out_root, rels, report_text)


def run(job: Job, profile_name: str, options: dict[str, bool], salt: str,
        write: bool) -> dict:
    """Execute a preview (write=False) or a full anonymisation run."""
    with job.lock:
        job.error = None
        if write:
            job.status = "processing"
            # A previous successful run must not leave a downloadable ZIP
            # that predates the gate decision of this run.
            stale_zip = job.upload_dir.parent / \
                f"anonymized-{job.id[:8]}.zip"
            if stale_zip.exists():
                stale_zip.unlink()
            job.zip_path = None
            job.zip_filename = None
            job.last_gate = None

        infos = _build_file_infos(job)
        anon = Anonymizer(options=options, salt=salt)

        anon.prescan(infos, lambda fi: job.sources[fi.index])

        out_root = job.upload_dir.parent / "out"
        if write:
            out_root.mkdir(parents=True, exist_ok=True)
            # Never leave a report from a previous (passing) run that could
            # be mistaken for the output of this run.
            stale_report = out_root / "anonymization-report.json"
            if stale_report.exists():
                stale_report.unlink()

        # Preview mode keeps the anonymised datasets in memory so the gate can
        # scan the exact bytes that *would* be written.
        in_memory: dict[int, Dataset] = {} if not write else None
        for fi in infos:
            if fi.is_dicom:
                ds = anon.apply_file(fi, job.sources[fi.index],
                                     out_root if write else None)
                if not write and ds is not None and fi.output_path:
                    in_memory[fi.index] = ds
            else:
                anon.counts["skipped"] += 1

        warnings = list(anon.global_warnings)
        for fi in infos:
            for w in fi.warnings:
                msg = f"文件 #{fi.index + 1}（{fi.source_path}）: {w}"
                if msg not in warnings:
                    warnings.append(msg)

        valid = [fi for fi in infos if fi.output_path]
        if not valid:
            # Report writing/blocking is irrelevant without any output, but
            # preview still needs the payload for per-file error display.
            if write:
                job.status = "error"
                job.error = "上传文件中没有可匿名化的 DICOM 影像。"
                raise ValueError(job.error)

        # First report version (without the gate section): the gate scans the
        # very JSON text the user receives, so a leak inside the report is
        # caught rather than self-certified.
        report = _build_report(profile_name, options, anon, infos,
                               warnings, gate_summary=None)
        report_text = json.dumps(report, ensure_ascii=False, indent=2)

        gate_result = _run_gate(anon, infos, in_memory,
                                out_root if write else None, report_text)
        gate_dict = gate_result.as_dict()
        job.last_gate = gate_dict

        if write:
            if not gate_result.passed:
                job.status = "blocked"
                job.error = (
                    f"残留标识门禁未通过：发现 "
                    f"{gate_result.exact_hit_count} 处源标识残留，"
                    "ZIP 未生成。")
                raise GateBlocked(gate_result)

            # Passed: stamp the gate summary into the report before packaging.
            report["residue_identifier_gate"] = gate_result.summary_dict()
            report_path = out_root / "anonymization-report.json"
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

        payload = {
            "files": [_file_payload(fi) for fi in infos],
            "changes": [ch.as_dict()
                        for fi in infos for ch in fi.changes],
            "warnings": warnings,
            "counts": anon.counts,
            "patients": [
                {
                    "key": ctx.key,
                    "pseudonym": ctx.pseudo_name,
                    "pseudonymous_id": ctx.pseudo_id,
                    "day_offset": ctx.day_offset,
                }
                for ctx in anon.contexts.values()
            ],
            "uid_map": anon.uid_map,
            "profile": profile_name,
            "gate": gate_dict,
        }
        if not write:
            job.last_preview = payload
            job.status = "previewed"
        else:
            job.last_result = payload
        return payload


def _file_payload(fi: FileInfo) -> dict:
    return {
        "index": fi.index,
        "source_path": fi.source_path,
        "output_path": fi.output_path,
        "sop_class_uid": fi.sop_class_uid,
        "sop_class_name": fi.sop_class_name,
        "modality": fi.modality,
        "old_patient_id": fi.old_patient_id,
        "old_patient_name": fi.old_patient_name,
        "old_study_uid": fi.old_study_uid,
        "old_series_uid": fi.old_series_uid,
        "old_sop_uid": fi.old_sop_uid,
        "new_patient_id": fi.new_patient_id,
        "new_patient_name": fi.new_patient_name,
        "new_study_uid": fi.new_study_uid,
        "new_series_uid": fi.new_series_uid,
        "new_sop_uid": fi.new_sop_uid,
        "error": fi.error,
    }
