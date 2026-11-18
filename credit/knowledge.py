"""Turn the certified catalog into ACL-bearing, versioned retrieval chunks."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from credit.catalog import Catalog


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("knowledge timestamps must include a timezone")
    return parsed


def is_effective(document: dict, moment: datetime) -> bool:
    """Fail closed for future, expired, missing or malformed publication dates."""
    try:
        return (timestamp(document["effective_from"]) <= moment
                and (not document.get("effective_until")
                     or moment < timestamp(document["effective_until"])))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _chunk(*, chunk_id: str, parent_id: str, knowledge_id: str, kind: str, title: str,
           aliases: list[str], text: str, roles: list[str], catalog: Catalog,
           source: str = "credit/data/catalog.json", doc_version: str | None = None) -> dict[str, Any]:
    document = {
        "chunk_id": chunk_id,
        "parent_id": parent_id,
        "id": knowledge_id,
        "kind": kind,
        "title": title,
        "aliases": sorted(set(aliases)),
        "text": text,
        "source": source,
        "version": catalog.document["version"],
        "doc_version": doc_version or catalog.document["version"],
        "updated_at": catalog.document["updated_at"],
        "effective_from": catalog.document.get("effective_from", catalog.document["updated_at"]),
        "allowed_roles": sorted(set(roles)),
        "domain": "credit",
        "certified": True,
    }
    if catalog.document.get("effective_until"):
        document["effective_until"] = catalog.document["effective_until"]
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    document["content_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
    return document


def build_documents(catalog: Catalog) -> list[dict[str, Any]]:
    documents = []
    for metric in catalog.metrics.values():
        parent = f"metric:{metric['id']}"
        aliases = [metric["name"], *metric["aliases"], metric["id"]]
        sections = {
            "definition": metric["definition"],
            "calculation": (
                f"指标 {metric['name']} 的认证表达式为 {metric['expression']}。"
                f"单位为 {metric['unit']}，时间类型为 {metric['time_kind']}，"
                f"实时口径{'已认证' if metric['realtime'] else '未认证'}。"
            ),
            "lineage": f"指标 {metric['name']} 的数据血缘为：{' → '.join(metric['lineage'])}。",
        }
        for section, text in sections.items():
            documents.append(_chunk(
                chunk_id=f"{parent}:{section}", parent_id=parent, knowledge_id=metric["id"],
                kind=f"metric_{section}", title=metric["name"], aliases=aliases,
                text=text, roles=metric["roles"], catalog=catalog,
            ))
    for field in catalog.fields.values():
        parent = f"field:{field['id']}"
        documents.append(_chunk(
            chunk_id=f"{parent}:definition", parent_id=parent, knowledge_id=parent,
            kind="field_definition", title=field["name"],
            aliases=[field["name"], field["name"].removesuffix("字段"), field["id"]],
            text=f"字段 {field['name']}（{field['id']}）：{field['definition']}",
            roles=field["roles"], catalog=catalog,
        ))
    for category, table in catalog.schema_tables:
        table_key = f"schema:{category}:{table['id']}"
        source = f"{table.get('source_document', 'schema_catalog.json')}#{table.get('source_sheet', '')}"
        if category == "base":
            mapping = table.get("donghua_mapping", {})
            map_text = "；东华源表：" + "、".join(mapping.get("donghua_tables", [])) if mapping else ""
            aliases = [table.get("name", ""), table.get("id", ""), *mapping.get("donghua_names", []),
                       *mapping.get("donghua_tables", [])]
        else:
            map_text = f"；对应数仓表：{table['warehouse_table']}" if table.get("warehouse_table") else ""
            aliases = [table.get("name", ""), table.get("id", ""), table.get("name_en", ""),
                       table.get("warehouse_table", "")]
        version = "v1.9" if category == "base" else "2021-06-10"
        summary = (
            f"{table.get('name', '')}（{table.get('id', '')}）表结构，知识类别：{category}。"
            f"更新策略：{table.get('update_strategy', '未注明')}。{table.get('description', '')}{map_text}"
        )
        documents.append(_chunk(
            chunk_id=f"{table_key}:table", parent_id=table_key, knowledge_id=table_key,
            kind="schema_table", title=table.get("name", table.get("id", "表结构")),
            aliases=aliases, text=summary, roles=["analyst", "operator"], catalog=catalog,
            source=source, doc_version=version,
        ))
        seen_field_ids = set()
        for field in table.get("fields", []):
            field_id = field.get("name_en") or field.get("name") or "unknown"
            if field_id in seen_field_ids:
                field_id = f"{field_id}-row-{field.get('source_row', len(seen_field_ids) + 1)}"
            seen_field_ids.add(field_id)
            field_text = (
                f"{table.get('name', '')}（{table.get('id', '')}）的字段："
                f"{field.get('name', '')}（{field.get('name_en', '')}），"
                f"类型 {field.get('data_type', '未注明')}，"
                f"主键 {'是' if field.get('primary_key') else '否/未标记'}，"
                f"可空 {'是' if field.get('nullable') else '否/未标记'}。"
                f"{field.get('description', '')}"
            )
            if field.get("source_interface"):
                field_text += f"源接口：{field['source_interface']}。"
            field_aliases = [field.get("name", ""), field.get("name_en", "")]
            documents.append(_chunk(
                chunk_id=f"{table_key}:field:{field_id}", parent_id=table_key,
                knowledge_id=f"{table_key}:{field_id}", kind="schema_field",
                title=field.get("name", field_id), aliases=field_aliases,
                text=field_text, roles=["analyst", "operator"], catalog=catalog,
                source=source, doc_version=version,
            ))
    return sorted(documents, key=lambda item: item["chunk_id"])
