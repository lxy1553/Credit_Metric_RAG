#!/usr/bin/env python3
"""Preview/curate authored knowledge documents from the three local projects.

The script copies only an explicit, conservative allowlist into the current
workspace and writes a provenance-rich manifest. Source repositories are read
only; excluded files (for example resumes, outputs, and cloned upstream code)
are never copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_ROOTS = {
    "credit_risk_control_system": Path("/Users/lxy/Documents/ChatGPT/credit_risk_control_system"),
    "ai_transition": Path("/Users/lxy/Documents/ai_transition"),
    "credit_risk_ai_system": Path("/Users/lxy/Documents/ChatGPT/credit_risk_ai_system"),
}
SUPPORTED = {".md", ".txt", ".rst", ".html", ".docx", ".pdf"}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "output", "tmp", "logs", "cache"}
UNTRUSTED_DIRS = {"github", "references"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def category_for(project: str, relative: str) -> str:
    parts = Path(relative).parts
    if project == "ai_transition" and parts[:2] in (("interview", "interview_core"), ("interview", "learning")):
        return "curated_interview_knowledge"
    if "Learning_Review_Plan" in parts or "notes" in parts or "study" in parts:
        return "learning_note"
    if "docs" in parts or "contracts" in parts or "validation" in parts:
        return "project_documentation"
    return "project_architecture_or_readme"


def classify(project: str, relative: str) -> tuple[str, str]:
    """Return (decision, reason), where decision is include/review/exclude."""
    path = Path(relative)
    parts = path.parts
    lowered = relative.casefold()

    if any(part in SKIP_DIRS for part in parts):
        return "exclude", "generated_output_or_runtime_directory"
    if any(part in {"github", "references"} for part in parts):
        return "exclude", "upstream_or_external_reference_material"
    if any(term in lowered for term in ("resume", "简历", "person.md", "jd_tracking", "job_targets", "application_tracking")):
        return "exclude", "personal_resume_or_job_search"
    if path.name.casefold() in {"agents.md", "claude.md", "skill.md", "skill_cn.md"}:
        return "exclude", "agent_instruction_or_skill_source"
    if project == "credit_risk_control_system":
        if "2026-08-13-ai问答" in parts:
            return "review", "conversation_derived_learning_log"
        if relative.startswith(("study/", "credit_risk_control_system/study/",
                                "credit_risk_control_system/Learning_Review_Plan/")):
            if any(term in lowered for term in ("招聘市场", "jd分析", "岗位分析")):
                return "exclude", "job_market_material"
            return "include", "authored_study_material"
        if relative in {"README.md", "credit_risk_control_system/README.md",
                        "credit_risk_control_system/01_金融信贷风控 AI 应用系统 — 系统架构设计.md",
                        "credit_risk_control_system/02_AI 应用技术栈在信贷风控系统中的落地映射.md",
                        "credit_risk_control_system/docs/reference_project_optimization_plan.md",
                        "credit_risk_control_system/src/ai_assistant/README.md"}:
            return "include", "project_architecture_or_guide"
        if relative.endswith("/bad_cases.md"):
            return "review", "evaluation_bad_cases_not_general_knowledge"
        return "exclude", "outside_authored_knowledge_allowlist"

    if project == "ai_transition":
        if relative.startswith(("notes/", "study/", "interview/interview_core/", "interview/learning/")):
            return "include", "authored_learning_or_curated_knowledge"
        if relative.startswith("projects/") and (path.name.casefold() == "readme.md" or "/docs/" in f"/{relative}"):
            return "include", "learning_project_documentation"
        if relative.startswith("docs/"):
            excluded_docs = ("merged_questions", "user_questions_answers", "xiaolinnote_questions",
                             "mianshiya", "interview", "progress_tracking", "tracking",
                             "job_targets", "jd_", "day28_application", "person")
            if any(term in lowered for term in excluded_docs):
                return "exclude", "interview_aggregation_or_job_tracking"
            return "include", "curated_project_knowledge_document"
        if relative == "README.md":
            return "include", "project_learning_map"
        return "exclude", "outside_authored_learning_allowlist"

    if project == "credit_risk_ai_system":
        if relative in {"README.md", "ARCHITECTURE.md", "IMPLEMENTATION_PLAN.md"}:
            return "include", "project_architecture_or_plan"
        if relative.startswith(("docs/", "metadata/", "governance/", "orchestration/", "observability/",
                               "ai_app/", "ingestion/", "warehouse/contracts/", "lakehouse/")):
            return "include", "project_domain_or_operations_document"
        if path.name.casefold() == "readme.md" and len(parts) == 2:
            return "include", "component_documentation"
        return "exclude", "outside_project_document_allowlist"

    return "exclude", "unknown_project"


def iter_documents(root: Path):
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names
                          if name not in SKIP_DIRS | UNTRUSTED_DIRS
                          and not name.startswith(".")
                          and not (Path(directory) / name).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.casefold() in SUPPORTED and not path.is_symlink():
                yield path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for project, root in DEFAULT_ROOTS.items():
        parser.add_argument(f"--{project.replace('_', '-')}", type=Path, default=root)
    parser.add_argument("--output", type=Path, default=Path("credit/data/knowledge_source_plan"))
    parser.add_argument("--apply", action="store_true",
                        help="copy approved include records; default only writes a review manifest")
    args = parser.parse_args()
    roots = {project: getattr(args, project) for project in DEFAULT_ROOTS}
    output = args.output.resolve()
    generated_plan_files = {"manifest.jsonl", "summary.json", "candidate-list.md"}
    if output.exists() and any(output.iterdir()):
        if args.apply or any(item.name not in generated_plan_files for item in output.iterdir()):
            parser.error(f"output directory is not empty: {output}")
    for _project, root in roots.items():
        if not root.is_dir():
            parser.error(f"source project directory not found: {root}")

    snapshot = datetime.now(UTC).date().isoformat()
    manifest: list[dict] = []
    copies: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for project, root in roots.items():
        for source in iter_documents(root):
            relative = source.relative_to(root).as_posix()
            decision, reason = classify(project, relative)
            counts[f"{project}:{decision}"] += 1
            if decision == "exclude":
                continue
            content_hash = sha256(source)
            document_id = hashlib.sha256(f"{project}:{relative}".encode()).hexdigest()
            duplicate_of = copies.get(content_hash)
            record = {
                "doc_id": document_id,
                "source_project": project,
                "source_relative_path": relative,
                "doc_source": source.name,
                "source_format": source.suffix.casefold().lstrip("."),
                "category": category_for(project, relative),
                "curation_decision": decision,
                "curation_reason": reason,
                "authority": "personal_learning_or_project_document",
                "status": "active" if decision == "include" else "candidate_review",
                "doc_version": content_hash[:16],
                "content_sha256": content_hash,
                "effective_date": None,
                "effective_date_basis": "not_specified_in_source",
                "snapshot_date": snapshot,
                "duplicate_of_doc_id": duplicate_of,
                "proposed_copy_path": (Path("corpus") / project / relative).as_posix(),
                "copy_path": None,
            }
            if args.apply and decision == "include" and duplicate_of is None:
                copy_path = Path("corpus") / project / relative
                destination = output / copy_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copies[content_hash] = document_id
                record["copy_path"] = copy_path.as_posix()
                counts[f"{project}:copied"] += 1
            elif duplicate_of:
                record["proposed_copy_path"] = next(
                    item["proposed_copy_path"] for item in manifest if item["doc_id"] == duplicate_of
                )
                counts["exact_duplicate"] += 1
            elif decision == "include" and duplicate_of is None:
                copies[content_hash] = document_id
            manifest.append(record)

    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as stream:
        for record in manifest:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    list_lines = [
        "# RAG 候选知识语料清单",
        "",
        f"盘点快照：{snapshot}。此清单仅供确认；未复制源文件、未分块、未建索引。",
        "默认纳入学习笔记、项目架构/运行文档和整理过的核心知识；简历、求职材料、外部仓库源码、生成输出与问答日志排除或待审核。",
        "这些学习笔记不是权威信贷制度，后续回答需标注来源类型，不能冒充正式政策。",
        "",
        "## 纳入候选与待审核文件",
        "",
    ]
    for project in roots:
        records = [item for item in manifest if item["source_project"] == project]
        list_lines.extend([f"### {project}（纳入 {sum(item['curation_decision'] == 'include' for item in records)}，待审核 {sum(item['curation_decision'] == 'review' for item in records)}）", ""])
        for record in records:
            marker = "待审核" if record["curation_decision"] == "review" else record["category"]
            duplicate = "；与其他项目文件内容完全重复" if record["duplicate_of_doc_id"] else ""
            list_lines.append(f"- [ ] `{record['source_relative_path']}` — {marker}{duplicate}")
        list_lines.append("")
    (output / "candidate-list.md").write_text("\n".join(list_lines) + "\n", encoding="utf-8")
    summary = {
        "schema_version": 1,
        "snapshot_date": snapshot,
        "projects": {project: str(root) for project, root in roots.items()},
        "decisions": dict(sorted(counts.items())),
        "included_records": sum(item["curation_decision"] == "include" for item in manifest),
        "review_records": sum(item["curation_decision"] == "review" for item in manifest),
        "unique_copied_documents": sum(item["copy_path"] is not None for item in manifest),
        "preview_only": not args.apply,
        "manifest": "manifest.jsonl",
        "important_boundary": "Learning notes and project docs are not authoritative credit policies; review provenance before production use.",
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
