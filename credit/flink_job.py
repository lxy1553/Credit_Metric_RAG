"""Deploy the local Kafka -> Flink -> Doris demonstration JAR exactly once."""

import argparse
import json
import time
from pathlib import Path

import httpx

JOB_NAME = "credit-loan-realtime-aggregate"


def deploy(jar_path: Path, url: str = "http://127.0.0.1:8081") -> dict:
    with httpx.Client(base_url=url, timeout=30.0) as client:
        response = client.get("/jobs/overview")
        response.raise_for_status()
        active = [job for job in response.json().get("jobs", [])
                  if job.get("name") == JOB_NAME and job.get("state") not in {"CANCELED", "FAILED", "FINISHED"}]
        if active:
            raise RuntimeError(f"已有同名 Flink 作业运行：{active[0]['jid']} ({active[0]['state']})")

        with jar_path.open("rb") as stream:
            upload = client.post("/jars/upload", files={"jarfile": (jar_path.name, stream, "application/java-archive")})
        upload.raise_for_status()
        jar_id = Path(upload.json()["filename"]).name
        if not jar_id.endswith("_" + jar_path.name):
            raise RuntimeError("Flink 返回了意外的 JAR 标识")
        run = client.post(f"/jars/{jar_id}/run", json={})
        run.raise_for_status()
        job_id = run.json()["jobid"]

        deadline = time.monotonic() + 25
        running_since = None
        status = "UNKNOWN"
        while time.monotonic() < deadline:
            state = client.get(f"/jobs/{job_id}")
            state.raise_for_status()
            status = state.json()["state"]
            if status in {"FAILED", "CANCELED"}:
                raise RuntimeError(f"Flink 作业启动失败：{job_id} ({status})")
            if status == "RUNNING":
                running_since = running_since or time.monotonic()
                if time.monotonic() - running_since >= 3:
                    return {"status": "running", "job_id": job_id, "jar_id": jar_id, "url": url}
            else:
                running_since = None
            time.sleep(1)
        try:
            exception_response = client.get(f"/jobs/{job_id}/exceptions")
            exception_response.raise_for_status()
            details = exception_response.json().get("root-exception", "").splitlines()
            reason = details[0] if details else "无异常详情"
        except (httpx.HTTPError, ValueError):
            reason = "无法读取作业异常"
        client.patch(f"/jobs/{job_id}", params={"mode": "cancel"}).raise_for_status()
        raise RuntimeError(f"Flink 作业未稳定进入 RUNNING：{job_id} ({status})；{reason}；已取消作业")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, default=Path("var/flink-job.jar"))
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    args = parser.parse_args()
    print(json.dumps(deploy(args.jar, args.url), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
