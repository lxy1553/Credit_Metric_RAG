#!/usr/bin/env python3
"""Copy the reviewed knowledge-note inventory into the local notes project.

This is a preservation/export step only. It does not parse, chunk, index, or
otherwise register documents as RAG material.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

SOURCE_ROOTS = {
    "credit_risk_control_system": Path("/Users/lxy/Documents/ChatGPT/credit_risk_control_system"),
    "ai_transition": Path("/Users/lxy/Documents/ai_transition"),
    "credit_risk_ai_system": Path("/Users/lxy/Documents/ChatGPT/credit_risk_ai_system"),
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("credit/data/knowledge_source_plan/manifest.jsonl"),
    )
    parser.add_argument(
        "--notes-project",
        type=Path,
        default=Path("/Users/lxy/Documents/ChatGPT/notes"),
    )
    args = parser.parse_args()
    manifest = [json.loads(line) for line in args.manifest.read_text(encoding="utf-8").splitlines() if line]
    records = [item for item in manifest if item["curation_decision"] in {"include", "review"}]
    destination_root = args.notes_project / "knowledge"
    if destination_root.exists():
        parser.error(f"destination already exists; refusing to overwrite: {destination_root}")

    copies = []
    for record in records:
        project = record["source_project"]
        relative = Path(record["source_relative_path"])
        if relative.is_absolute() or ".." in relative.parts or project not in SOURCE_ROOTS:
            parser.error(f"unsafe or unknown source path: {project}/{relative}")
        source_candidate = SOURCE_ROOTS[project] / relative
        if source_candidate.is_symlink():
            parser.error(f"symbolic-link source is not allowed: {project}/{relative}")
        source = source_candidate.resolve()
        if SOURCE_ROOTS[project].resolve() not in source.parents or not source.is_file():
            parser.error(f"source no longer matches inventory: {project}/{relative}")
        if digest(source) != record["content_sha256"]:
            parser.error(f"source changed after inventory; regenerate the review list: {project}/{relative}")
        target = destination_root / project / relative
        copies.append((source, target, record))

    for source, target, _record in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    notes_manifest = []
    for source, target, record in copies:
        notes_manifest.append({
            **record,
            "notes_project_path": target.relative_to(args.notes_project).as_posix(),
            "source_path": str(source),
            "rag_ingestion_status": "not_ingested",
        })
    catalog_path = args.notes_project / "knowledge_manifest.jsonl"
    catalog_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in notes_manifest),
        encoding="utf-8",
    )
    readme = """# 个人知识笔记

这里集中保存来自 `credit_risk_control_system`、`ai_transition` 和 `credit_risk_ai_system` 的知识文档与学习笔记，按来源项目及原目录结构归档，文件可直接在桌面编辑。

## 使用说明

- `knowledge/` 下的文档是独立副本；编辑不会修改三个来源项目中的原文件。
- `knowledge_manifest.jsonl` 记录来源、原相对路径、版本哈希和整理状态。
- `review` 状态的对话式学习记录与 bad case 也先作为笔记保留，后续可人工删改。
- 这些内容目前仅作为个人笔记保存，未接入 RAG、未建立索引，也不等同于权威信贷政策。

"""
    (args.notes_project / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps({
        "notes_project": str(args.notes_project),
        "copied_documents": len(copies),
        "review_documents": sum(item["curation_decision"] == "review" for _, _, item in copies),
        "manifest": str(catalog_path),
        "readme": str(args.notes_project / "README.md"),
        "rag_ingested": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
