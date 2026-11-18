#!/usr/bin/env python3
"""Extract only schema metadata from the approved base-layer and Donghua workbooks."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook


def text(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rows_by_header(sheet, header_row: int, max_column: int | None = None):
    headers = [text(value) for value in next(sheet.iter_rows(
        min_row=header_row, max_row=header_row, max_col=max_column, values_only=True,
    ))]
    for row_number, values in enumerate(sheet.iter_rows(min_row=header_row + 1, max_col=max_column,
                                                         values_only=True),
                                        start=header_row + 1):
        row = {header: values[index] if index < len(values) else None
               for index, header in enumerate(headers) if header}
        if any(value is not None for value in values):
            yield row_number, row


def marked(value) -> bool | None:
    value = text(value)
    if value in {"√", "Y", "是", "1", "PK"}:
        return True
    if value in {"N", "否", "0"}:
        return False
    return None


def split_names(value) -> list[str]:
    return [name for name in (text(part) for part in str(value or "").replace("、", "\n").splitlines())
            if name]


def extract(base_path: Path, donghua_path: Path) -> dict:
    base = load_workbook(base_path, read_only=True, data_only=True)
    donghua = load_workbook(donghua_path, read_only=True, data_only=True)
    try:
        if "表清单" not in base.sheetnames or "基础层-数仓-全部" not in donghua.sheetnames:
            raise ValueError("指定工作簿缺少预期的数据字典工作表")

        base_tables = {}
        for _, row in rows_by_header(base["表清单"], 1):
            table_name = text(row.get("中文表名"))
            table_id = text(row.get("英文表名"))
            if not table_name or not table_id:
                continue
            sheet = base[table_name] if table_name in base.sheetnames else None
            fields = []
            if sheet:
                for row_number, field in rows_by_header(sheet, 2):
                    name = text(field.get("数仓字段中文名"))
                    english = text(field.get("数仓字段英文名"))
                    if not name and not english:
                        continue
                    fields.append({
                        "name": name, "name_en": english,
                        "data_type": text(field.get("字段类型")),
                        "primary_key": marked(field.get("是否主键")),
                        "nullable": marked(field.get("是否为空")),
                        "source_availability": {
                            key.removesuffix("系统是否有该字段"): text(value)
                            for key, value in field.items()
                            if key.endswith("系统是否有该字段") and text(value)
                        },
                        "description": text(field.get("字段说明")),
                        "source_sheet": table_name, "source_row": row_number,
                    })
            base_tables[table_id] = {
                "id": table_id, "name": table_name,
                "description": text(row.get("描述")),
                "update_strategy": text(row.get("更新策略")),
                "fields": fields,
                "source_document": base_path.name, "source_sheet": table_name,
            }

        by_chinese_table = {table["name"]: table for table in base_tables.values()}
        for _, row in rows_by_header(donghua["基础层-数仓-全部"], 1):
            table_id = text(row.get("基础层"))
            table = base_tables.get(table_id) or by_chinese_table.get(text(row.get("接口中文名")))
            if table is None:
                continue
            mapping = {
                "declared_base_table": table_id,
                "theme": text(row.get("主题")),
                "donghua_names": split_names(row.get("数仓-东华")),
                "donghua_tables": split_names(row.get("数仓-东华-英文")),
                "update_strategy": text(row.get("更新策略")),
            }
            table["donghua_mapping"] = mapping

        interfaces = {}
        for _, row in rows_by_header(donghua["接口列表"], 1, max_column=12):
            name = text(row.get("接口中文名"))
            english = text(row.get("接口英文名"))
            if row.get("序号") is not None and name and english:
                # Deliberately do not read or store the SFTP path / account columns.
                interfaces[name] = {
                    "name": name, "name_en": english,
                    "load_frequency": text(row.get("加载频度")),
                    "load_method": text(row.get("加载方式")),
                    "increment_rule": text(row.get("增量规则")),
                    "increment_date_field": text(row.get("增量判断日期")),
                    "notes": text(row.get("备注")),
                }

        source_fields: dict[str, list[dict]] = defaultdict(list)
        for row_number, row in rows_by_header(donghua["接口数据结构"], 1):
            table_name = text(row.get("结构名"))
            name = text(row.get("字段中文"))
            english = text(row.get("字段英文"))
            if not table_name or (not name and not english):
                continue
            source_fields[table_name].append({
                "name": name, "name_en": english,
                "data_type": text(row.get("字段类型")),
                "length": text(row.get("长度")),
                "primary_key": marked(row.get("是否主键")),
                "nullable": marked(row.get("是否为空")),
                "code_table": text(row.get("代码表")),
                "description": text(row.get("字段说明")),
                "source_sheet": "接口数据结构", "source_row": row_number,
            })

        dw_table_by_interface = {}
        for _, row in rows_by_header(donghua["数据仓库表列表"], 1):
            interface_name = text(row.get("接口中文名"))
            dw_name = text(row.get("数据仓库表名"))
            if interface_name and dw_name:
                dw_table_by_interface[interface_name] = dw_name

        warehouse_fields: dict[str, list[dict]] = defaultdict(list)
        for row_number, row in rows_by_header(donghua["数据仓库表结构"], 1):
            table_name = text(row.get("DW层表明"))
            name = text(row.get("字段名称"))
            english = text(row.get("DW层字段名"))
            if not table_name or (not name and not english):
                continue
            warehouse_fields[table_name].append({
                "name": name, "name_en": english,
                "data_type": text(row.get("字段类型")),
                "source_interface": text(row.get("源系统中文名")),
                "source_interface_en": interfaces.get(text(row.get("源系统中文名")), {}).get("name_en", ""),
                "source_sheet": "数据仓库表结构", "source_row": row_number,
            })

        source_tables = []
        for name in sorted(set(interfaces) | set(source_fields)):
            interface = interfaces.get(name, {"name": name, "name_en": ""})
            source_tables.append({
                **interface, "id": interface["name_en"] or name,
                "warehouse_table": dw_table_by_interface.get(name, ""),
                "fields": source_fields.get(name, []),
                "source_document": donghua_path.name,
                "source_sheet": "接口数据结构",
            })

        warehouse_tables = [{
            "id": name, "name": name, "fields": fields,
            "source_document": donghua_path.name,
            "source_sheet": "数据仓库表结构",
        } for name, fields in sorted(warehouse_fields.items())]

        base_digest, donghua_digest = sha256(base_path), sha256(donghua_path)
        return {
            "schema_version": 1,
            "corpus_version": f"base-v1.9-donghua-{donghua_digest[:12]}",
            "source_documents": [
                {"source_file": base_path.name, "doc_version": "v1.9",
                 "sha256": base_digest, "authority": "unified_base_layer_standard"},
                {"source_file": donghua_path.name, "doc_version": "2021-06-10",
                 "sha256": donghua_digest, "authority": "donghua_source_schema"},
            ],
            "tables": list(base_tables.values()),
            "source_tables": source_tables,
            "warehouse_tables": warehouse_tables,
            "important_boundary": "Only schema metadata is included; no business records or SFTP credentials are ingested.",
        }
    finally:
        base.close()
        donghua.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help="基础层模型工作簿")
    parser.add_argument("--donghua", type=Path, required=True, help="东华数据字典工作簿")
    parser.add_argument("--output", type=Path, default=Path("credit/data/schema_catalog.json"))
    args = parser.parse_args()
    data = extract(args.base, args.donghua)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": args.output.as_posix(), "base_tables": len(data["tables"]),
        "base_fields": sum(len(table["fields"]) for table in data["tables"]),
        "donghua_interfaces": len(data["source_tables"]),
        "donghua_fields": sum(len(table["fields"]) for table in data["source_tables"]),
        "donghua_dw_tables": len(data["warehouse_tables"]),
        "donghua_dw_fields": sum(len(table["fields"]) for table in data["warehouse_tables"]),
        "corpus_version": data["corpus_version"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
