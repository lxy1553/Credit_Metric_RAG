from credit.release_preflight import release_report


def test_release_report_is_aggregate_only_and_passes_when_all_gates_pass(db):
    report = release_report(
        db, object(), command_runner=lambda args: 0,
        task_redis_url="redis://test", task_redis_probe=lambda url: True,
        warehouse_evaluator=lambda path: {"total": 7, "passed": 7, "failed": 0, "success_rate": 1.0},
        retrieval_evaluator=lambda retrieval: {
            "passed": True, "dataset_label": "development", "cases": 12, "recall_at_5": 1.0,
            "mrr_at_5": 1.0, "acl_leakage_count": 0, "citation_accuracy": 1.0, "unhealthy_cases": 0,
            "results": [{"query": "must not appear"}],
        },
    )
    assert report["passed"] is True
    assert report["checks"]["warehouse_evaluation"]["passed_cases"] == 7
    assert "query" not in str(report)
    assert "must not appear" not in str(report)


def test_release_report_fails_closed_when_any_gate_fails(db):
    report = release_report(
        db, object(), command_runner=lambda args: 1 if "ruff" in args else 0,
        task_redis_url="redis://test", task_redis_probe=lambda url: True,
        warehouse_evaluator=lambda path: {"total": 7, "passed": 7, "failed": 0, "success_rate": 1.0},
        retrieval_evaluator=lambda retrieval: {
            "passed": True, "dataset_label": "development", "cases": 12, "recall_at_5": 1.0,
            "mrr_at_5": 1.0, "acl_leakage_count": 0, "citation_accuracy": 1.0, "unhealthy_cases": 0,
        },
    )
    assert report["passed"] is False
    assert report["checks"]["static_analysis"]["passed"] is False


def test_release_report_fails_closed_when_task_redis_is_not_configured(db):
    report = release_report(
        db, object(), command_runner=lambda args: 0,
        task_redis_url="",
        warehouse_evaluator=lambda path: {"total": 7, "passed": 7, "failed": 0, "success_rate": 1.0},
        retrieval_evaluator=lambda retrieval: {
            "passed": True, "dataset_label": "development", "cases": 12, "recall_at_5": 1.0,
            "mrr_at_5": 1.0, "acl_leakage_count": 0, "citation_accuracy": 1.0, "unhealthy_cases": 0,
        },
    )

    assert report["passed"] is False
    assert report["checks"]["task_queue_integration"]["passed"] is False
    assert report["checks"]["task_queue_integration"]["reason"] == "not_configured"


def test_release_report_runs_queue_and_shared_store_integration_suites(db):
    commands = []
    required_flags = []

    def run(args):
        if any("test_task_queue_integration.py" in arg for arg in args):
            import os
            required_flags.append(os.getenv("CREDIT_TASK_REDIS_REQUIRED"))
        commands.append(args)
        return 0

    release_report(
        db, object(), command_runner=run,
        task_redis_url="redis://test", task_redis_probe=lambda url: True,
        warehouse_evaluator=lambda path: {"total": 7, "passed": 7, "failed": 0, "success_rate": 1.0},
        retrieval_evaluator=lambda retrieval: {
            "passed": True, "dataset_label": "development", "cases": 12, "recall_at_5": 1.0,
            "mrr_at_5": 1.0, "acl_leakage_count": 0, "citation_accuracy": 1.0, "unhealthy_cases": 0,
        },
    )

    integration_commands = [command for command in commands
                            if any("test_task_queue_integration.py" in arg for arg in command)]
    assert integration_commands, commands
    assert "tests/test_task_store_integration.py" in integration_commands[0]
    assert "tests/test_redis_async_tasks.py" in integration_commands[0]
    assert required_flags == ["1"]
    ruff_command = next(command for command in commands if "ruff" in command)
    assert "--no-cache" in ruff_command
