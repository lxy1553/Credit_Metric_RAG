import argparse
import json
from collections import Counter
from pathlib import Path

from credit.agent import QuestionService
from credit.memory import StatelessMemory
from credit.models import Question
from credit.security import DEMO_USERS

CASES = [
    ("申请通过率怎么计算", "definition", "ok"),
    ("贷款余额在哪个字段", "field", "ok"),
    ("还款金额的血缘", "lineage", "ok"),
    ("2026-03-01 至 2026-03-02 贷款余额按产品查询", "query", "ok"),
    ("2026-03-02 实时逾期率", "query", "ok"),
    ("利润是多少", "other", "refused"),
    ("昨天的贷款余额", "other", "refused"),
]


def evaluate(db: Path) -> dict:
    service = QuestionService(db, StatelessMemory())
    def resolver():
        return DEMO_USERS["demo-analyst"]

    rows, counts = [], Counter()
    for question, expected_intent, expected_status in CASES:
        result = service.ask(Question(question=question), resolver)
        passed = result.intent == expected_intent and result.status == expected_status
        counts["passed" if passed else "failed"] += 1
        rows.append({"question": question, "expected_intent": expected_intent,
                     "actual_intent": result.intent, "expected_status": expected_status,
                     "actual_status": result.status, "passed": passed})
    return {"total": len(CASES), "passed": counts["passed"], "failed": counts["failed"],
            "success_rate": counts["passed"] / len(CASES), "cases": rows}


def main():
    parser = argparse.ArgumentParser(description="运行确定性脱敏评测集")
    parser.add_argument("--db", type=Path, default=Path("var/credit.db"))
    args = parser.parse_args()
    report = evaluate(args.db)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
