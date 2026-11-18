import json
from pathlib import Path

from credit.models import User
from credit.security import AccessDenied, authorize


class Catalog:
    def __init__(self):
        self.document = json.loads((Path(__file__).parent / "data/catalog.json").read_text())
        self.metrics = {metric["id"]: metric for metric in self.document["metrics"]}
        self.fields = {field["id"]: field for field in self.document["fields"]}
        schema_path = Path(__file__).parent / "data/schema_catalog.json"
        self.schema_document = json.loads(schema_path.read_text()) if schema_path.exists() else {
            "corpus_version": "schema-empty", "tables": [], "source_tables": [], "warehouse_tables": [],
        }
        self.document["version"] = f"{self.document['version']}+{self.schema_document['corpus_version']}"
        self.schema_tables = [
            ("base", table) for table in self.schema_document.get("tables", [])
        ] + [("donghua_source", table) for table in self.schema_document.get("source_tables", [])] + [
            ("donghua_dw", table) for table in self.schema_document.get("warehouse_tables", [])
        ]

    def metric(self, metric_id: str, user: User, tool="definition") -> dict:
        metric = self.metrics.get(metric_id)
        if metric is None:
            raise AccessDenied("指标未认证或不可访问")
        authorize(user, tool, metric["roles"])
        return metric

    def match(self, question: str) -> str | None:
        matches = []
        for metric in self.metrics.values():
            for alias in [metric["id"], metric["name"], *metric["aliases"]]:
                if alias in question:
                    matches.append((len(alias), metric["id"]))
        ids = {item[1] for item in matches}
        # 同一轮只查询一个指标；多个互不包含指标交由用户澄清。
        if len(ids) > 1:
            return None
        return max(matches)[1] if matches else None

    def citation(self, item: dict, kind="metric") -> dict:
        return {
            "id": f"{kind}:{item['id']}", "title": item["name"],
            "source": "credit/data/catalog.json", "version": self.document["version"],
            "updated_at": self.document["updated_at"], "domain": "credit",
        }

    def lookup_fields(self, question: str, user: User) -> list[dict]:
        result = []
        metric_id = self.match(question)
        for field in self.fields.values():
            try:
                authorize(user, "field", field["roles"])
            except AccessDenied:
                continue
            metric_columns = self.metrics[metric_id]["columns"] if metric_id else []
            if field["id"] in question or field["name"] in question or field["id"] in metric_columns:
                result.append({**field, "table": "ads_credit_daily", "citation": self.citation(field, "field")})
        return result

    @staticmethod
    def _table_aliases(kind: str, table: dict) -> list[str]:
        aliases = [table.get("name", ""), table.get("id", ""), table.get("name_en", ""),
                   table.get("warehouse_table", "")]
        if kind == "base":
            mapping = table.get("donghua_mapping", {})
            aliases.extend(mapping.get("donghua_names", []))
            aliases.extend(mapping.get("donghua_tables", []))
        return sorted({value for value in aliases if value}, key=len, reverse=True)

    def schema_table_matches(self, question: str) -> list[tuple[str, dict]]:
        folded = question.casefold()
        canonical_matches = []
        mapping_matches = []
        for kind, table in self.schema_tables:
            canonical = [table.get("name", ""), table.get("id", ""), table.get("name_en", ""),
                         table.get("warehouse_table", "")]
            if any(alias and alias.casefold() in folded for alias in canonical):
                canonical_matches.append((kind, table))
                continue
            if kind == "base":
                mapping = table.get("donghua_mapping", {})
                mapped = [*mapping.get("donghua_names", []), *mapping.get("donghua_tables", [])]
                if any(alias and alias.casefold() in folded for alias in mapped):
                    mapping_matches.append((kind, table))
        matches = canonical_matches or mapping_matches
        return sorted(matches, key=lambda item: (item[0] != "base", -len(item[1].get("name", "")),
                                                  item[1].get("id", "")))

    def has_schema_term(self, question: str) -> bool:
        terms = ("表结构", "表字段", "有哪些字段", "字段列表", "字段类型", "字段名", "字段是什么",
                 "主键", "建表", "数仓表", "源表", "映射到", "对应表", "在哪张表", "什么表",
                 "是什么类型", "什么类型", "对应字段")
        return any(term in question for term in terms)

    def is_schema_question(self, question: str) -> bool:
        if self.schema_table_matches(question):
            return True
        catalog_field_mentioned = any(field["name"] in question for field in self.fields.values())
        return self.has_schema_term(question) and not catalog_field_mentioned

    def lookup_schema(self, question: str, user: User) -> list[dict]:
        authorize(user, "schema", ["analyst", "operator"])
        table_matches = self.schema_table_matches(question)
        question_folded = question.casefold()
        rows = []
        for kind, table in table_matches:
            field_matches = []
            for field in table.get("fields", []):
                field_aliases = [field.get("name", ""), field.get("name_en", "")]
                if any(alias and alias.casefold() in question_folded for alias in field_aliases):
                    field_matches.append(field)
            selected = field_matches or table.get("fields", [])
            base = {
                "table_id": table.get("id", ""), "table_name": table.get("name", ""),
                "table_name_en": table.get("name_en", table.get("id", "")),
                "table_kind": kind, "update_strategy": table.get("update_strategy", ""),
                "warehouse_table": table.get("warehouse_table", ""),
                "source_document": table.get("source_document", ""),
                "source_sheet": table.get("source_sheet", ""),
                "donghua_declared_base_table": table.get("donghua_mapping", {}).get("declared_base_table", ""),
                "donghua_source_tables": table.get("donghua_mapping", {}).get("donghua_tables", []),
                "source_availability": {},
            }
            if not selected:
                rows.append(base)
                continue
            for field in selected:
                rows.append({
                    **base, "field_name": field.get("name", ""),
                    "field_name_en": field.get("name_en", ""),
                    "data_type": field.get("data_type", ""),
                    "primary_key": field.get("primary_key"),
                    "nullable": field.get("nullable"),
                    "description": field.get("description", ""),
                    "source_interface": field.get("source_interface", ""),
                    "source_availability": field.get("source_availability", {}),
                })
        if rows:
            return rows

        # Field-only queries can locate matching schema fields across tables.
        for kind, table in self.schema_tables:
            for field in table.get("fields", []):
                if any(value and value.casefold() in question_folded
                       for value in (field.get("name", ""), field.get("name_en", ""))):
                    rows.append({
                        "table_id": table.get("id", ""), "table_name": table.get("name", ""),
                        "table_name_en": table.get("name_en", table.get("id", "")),
                        "table_kind": kind, "update_strategy": table.get("update_strategy", ""),
                        "warehouse_table": table.get("warehouse_table", ""),
                        "source_document": table.get("source_document", ""),
                        "source_sheet": table.get("source_sheet", ""),
                        "donghua_declared_base_table": table.get("donghua_mapping", {}).get("declared_base_table", ""),
                        "donghua_source_tables": table.get("donghua_mapping", {}).get("donghua_tables", []),
                        "field_name": field.get("name", ""),
                        "field_name_en": field.get("name_en", ""),
                        "data_type": field.get("data_type", ""),
                        "primary_key": field.get("primary_key"),
                        "nullable": field.get("nullable"),
                        "description": field.get("description", ""),
                        "source_interface": field.get("source_interface", ""),
                        "source_availability": field.get("source_availability", {}),
                    })
            if len(rows) >= 100:
                break
        return rows[:100]
