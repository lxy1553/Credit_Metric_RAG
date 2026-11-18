import os
import threading
import time
from collections import defaultdict, deque
from contextlib import closing
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from credit import traces
from credit.agent import QuestionService
from credit.alerts import AlertPolicy
from credit.async_tasks import (
    QuestionTaskManager,
    TaskExpired,
    TaskIdempotencyConflict,
    TaskNotFound,
    TaskNotResumable,
    TaskQueueFull,
)
from credit.catalog import Catalog
from credit.deployment import validate_deployment_profile
from credit.embeddings import DEFAULT_MODEL, FastEmbedder
from credit.identity import AuthenticationError, DemoIdentityProvider, OidcJwtIdentityProvider
from credit.memory import MemoryStore, RedisMemory, StatelessMemory
from credit.models import (
    ExplorationQuery,
    MetricQuery,
    Question,
    QuestionTaskReceipt,
    QuestionTaskView,
    Result,
)
from credit.operations import OperationsMetrics
from credit.readiness import ReadinessChecker
from credit.redis_async_tasks import RedisQuestionTaskManager
from credit.response_cache import MemoryResultCache, NullResultCache, RedisResultCache
from credit.retrieval import ElasticsearchRetrieval, FallbackRetrieval, LocalRetrieval
from credit.security import DEMO_USERS
from credit.task_configuration import load_task_payload_cipher
from credit.warehouse import connect


def create_app(db: Path, users: dict, memory=None, retrieval=None, result_cache=None, alert_policy=None,
               identity_provider=None, question_tasks=None) -> FastAPI:
    app = FastAPI(title="信贷数仓 · 受控智能问数", version="0.1.0")
    service = QuestionService(db, memory, retrieval=retrieval, result_cache=result_cache)
    task_backend = os.getenv("CREDIT_TASK_BACKEND", "local").strip().lower()
    if question_tasks is None:
        if task_backend == "redis":
            import redis

            task_client = redis.Redis.from_url(
                os.environ["CREDIT_TASK_REDIS_URL"], decode_responses=True,
                socket_connect_timeout=1, socket_timeout=2,
            )
            try:
                task_client.ping()
            except redis.RedisError as exc:
                task_client.close()
                raise RuntimeError("configured Redis task backend is unavailable") from exc
            user_lookup = {user.user_id: user for user in users.values()}
            question_tasks = RedisQuestionTaskManager(
                service, user_lookup, task_client, load_task_payload_cipher(),
                stream=os.getenv("CREDIT_TASK_STREAM", "credit:question-tasks"),
                key_prefix=os.getenv("CREDIT_TASK_KEY_PREFIX", "credit:question-task"),
                workers=int(os.getenv("CREDIT_TASK_WORKERS", "1")),
            )
        else:
            question_tasks = QuestionTaskManager(db, service)
    app.state.service = service
    app.state.question_tasks = question_tasks
    app.state.users = users
    app.state.identity_provider = identity_provider or DemoIdentityProvider(users)
    metrics = OperationsMetrics()
    app.state.operations = metrics
    app.state.alert_policy = alert_policy or AlertPolicy.from_environment()
    app.state.readiness = ReadinessChecker(db, service.retrieval, service.memory,
                                           task_queue=question_tasks if task_backend == "redis" else None)
    concurrent = threading.BoundedSemaphore(4)
    recent = defaultdict(deque)
    limiter_lock = threading.Lock()

    def resolver(request: Request):
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""

        def resolve():
            try:
                user = app.state.identity_provider.resolve(token)
            except AuthenticationError:
                raise HTTPException(401, "请提供有效服务端身份凭证", headers={"WWW-Authenticate": "Bearer"}) from None
            return user

        resolve()
        return resolve

    def enforce_user_rate_limit(user):
        with limiter_lock:
            timestamps = recent[user.user_id]
            moment = time.monotonic()
            while timestamps and timestamps[0] <= moment - 60:
                timestamps.popleft()
            if len(timestamps) >= 60:
                metrics.record_rejection("rate_limited")
                raise HTTPException(429, "每用户每分钟最多 60 次", headers={"Retry-After": "60"})
            timestamps.append(moment)

    def run(resolve, operation):
        user = resolve()
        enforce_user_rate_limit(user)
        if not concurrent.acquire(blocking=False):
            metrics.record_rejection("concurrency_limited")
            raise HTTPException(503, "查询并发已满，请稍后重试", headers={"Retry-After": "1"})
        started = time.perf_counter()
        try:
            result = operation()
            metrics.record(result, time.perf_counter() - started)
            return result
        except Exception:
            metrics.record_system_failure()
            raise
        finally:
            concurrent.release()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (Path(__file__).parent / "web/index.html").read_text()

    @app.get("/healthz")
    def health():
        try:
            with closing(connect(db)) as conn:
                conn.execute("SELECT trace_id FROM query_audit LIMIT 1").fetchone()
        except Exception as exc:
            raise HTTPException(503, "warehouse unavailable") from exc
        return {"status": "ok", "mode": "synthetic-learning", "memory": service.memory.mode,
                "retrieval": service.retrieval.mode, "response_cache": service.result_cache.mode,
                "llm": service.intent_router.mode}

    @app.get("/livez")
    def live():
        return {"status": "live"}

    @app.get("/readyz")
    def ready():
        if not app.state.readiness.check():
            raise HTTPException(503, "service not ready")
        return {"status": "ready"}

    @app.get("/v1/catalog")
    def catalog(resolve=Depends(resolver)):
        user = resolve()
        return [{"id": metric["id"], "name": metric["name"], "unit": metric["unit"],
                 "definition": metric["definition"], "citation": service.catalog.citation(metric)}
                for metric in service.catalog.metrics.values()
                if "credit" in user.domains and user.roles.intersection(metric["roles"])]

    @app.get("/v1/operations/summary")
    def operations_summary(resolve=Depends(resolver)):
        if "platform" not in resolve().roles:
            raise HTTPException(403, "运行摘要仅限平台身份访问")
        return metrics.summary()

    @app.get("/v1/operations/alerts")
    def operations_alerts(resolve=Depends(resolver)):
        if "platform" not in resolve().roles:
            raise HTTPException(403, "运行告警状态仅限平台身份访问")
        return app.state.alert_policy.evaluate(metrics.summary())

    @app.post("/v1/questions", response_model=Result)
    def ask(body: Question, resolve=Depends(resolver)):
        return run(resolve, lambda: service.ask(body, resolve))

    @app.post("/v1/explorations", response_model=Result)
    def explore(body: ExplorationQuery, resolve=Depends(resolver)):
        return run(resolve, lambda: service.explore(body, resolve))

    @app.post("/v1/question-tasks", response_model=QuestionTaskReceipt, status_code=202)
    def submit_question_task(body: Question, resolve=Depends(resolver),
                             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key",
                                                                  min_length=1, max_length=128,
                                                                  pattern=r"^[A-Za-z0-9._:-]+$")):
        user = resolve()
        enforce_user_rate_limit(user)
        try:
            return question_tasks.submit(body, resolve, user, idempotency_key=idempotency_key)
        except TaskIdempotencyConflict:
            raise HTTPException(409, "该幂等键已用于不同的问答请求") from None
        except TaskQueueFull:
            raise HTTPException(503, "异步问答队列已满或服务正在关闭", headers={"Retry-After": "2"}) from None

    @app.get("/v1/question-tasks/{request_id}", response_model=QuestionTaskView)
    def get_question_task(request_id: str, resolve=Depends(resolver)):
        user = resolve()
        try:
            return question_tasks.get(request_id, user)
        except TaskNotFound:
            raise HTTPException(404, "任务不存在或不可访问") from None
        except TaskExpired:
            raise HTTPException(410, "任务已过期，请重新发起") from None

    @app.post("/v1/question-tasks/{request_id}/resume", response_model=QuestionTaskReceipt, status_code=202)
    def resume_question_task(request_id: str, body: Question, resolve=Depends(resolver)):
        user = resolve()
        enforce_user_rate_limit(user)
        try:
            return question_tasks.resume(request_id, body, resolve, user)
        except TaskNotFound:
            raise HTTPException(404, "任务不存在或不可访问") from None
        except TaskExpired:
            raise HTTPException(410, "任务已过期，请重新发起") from None
        except TaskNotResumable:
            raise HTTPException(409, "任务当前状态不支持恢复") from None
        except TaskQueueFull:
            raise HTTPException(503, "异步问答队列已满或服务正在关闭", headers={"Retry-After": "2"}) from None

    @app.delete("/v1/question-tasks/{request_id}", status_code=204)
    def cancel_question_task(request_id: str, resolve=Depends(resolver)):
        user = resolve()
        enforce_user_rate_limit(user)
        try:
            question_tasks.cancel(request_id, user)
        except TaskNotFound:
            raise HTTPException(404, "任务不存在或不可访问") from None
        except TaskExpired:
            raise HTTPException(410, "任务已过期，请重新发起") from None

    app.add_event_handler("shutdown", question_tasks.close)

    @app.post("/v1/metrics/query", response_model=Result)
    def query(body: MetricQuery, resolve=Depends(resolver)):
        return run(resolve, lambda: service.query(body, resolve))

    @app.get("/v1/audit/{trace_id}")
    def audit(trace_id: str, resolve=Depends(resolver)):
        user = resolve()
        with closing(connect(db)) as conn:
            row = conn.execute("SELECT * FROM query_audit WHERE trace_id=? AND user_id=?",
                               (trace_id, user.user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "审计记录不存在或不可访问")
        return dict(row)

    @app.get("/v1/traces/{trace_id}")
    def trace(trace_id: str, resolve=Depends(resolver)):
        record = traces.get(db, trace_id, resolve().user_id)
        if record is None:
            raise HTTPException(404, "Trace 不存在或不可访问")
        return record

    return app


def from_environment():
    validate_deployment_profile()
    auth_mode = os.getenv("CREDIT_AUTH_MODE", "demo")
    if auth_mode == "demo":
        if os.getenv("CREDIT_DEMO_MODE") != "1":
            raise RuntimeError("演示身份仅限显式 CREDIT_DEMO_MODE=1；生产需使用 CREDIT_AUTH_MODE=oidc")
        users, identity_provider = dict(DEMO_USERS), DemoIdentityProvider(DEMO_USERS)
    elif auth_mode == "oidc":
        if os.getenv("CREDIT_DEMO_MODE"):
            raise ValueError("CREDIT_DEMO_MODE cannot be enabled with CREDIT_AUTH_MODE=oidc")
        def required(name):
            value = os.getenv(name)
            if not value:
                raise ValueError(f"{name} is required when CREDIT_AUTH_MODE=oidc")
            return value

        users = {}
        identity_provider = OidcJwtIdentityProvider(
            issuer=required("CREDIT_OIDC_ISSUER"), audience=required("CREDIT_OIDC_AUDIENCE"),
            jwks_url=required("CREDIT_OIDC_JWKS_URL"),
            roles_claim=os.getenv("CREDIT_OIDC_ROLES_CLAIM", "roles"),
            domains_claim=os.getenv("CREDIT_OIDC_DOMAINS_CLAIM", "domains"),
            policy_version_claim=os.getenv("CREDIT_OIDC_POLICY_VERSION_CLAIM", "policy_version"),
        )
    else:
        raise ValueError("CREDIT_AUTH_MODE must be demo or oidc")
    mode = os.getenv("CREDIT_MEMORY", "memory")
    if mode not in {"memory", "redis", "stateless"}:
        raise ValueError("CREDIT_MEMORY must be memory, redis or stateless")
    memory = (RedisMemory(os.environ["CREDIT_REDIS_URL"]) if mode == "redis"
              else MemoryStore() if mode == "memory" else StatelessMemory())
    cache_mode = os.getenv("CREDIT_RESPONSE_CACHE", "memory")
    if cache_mode not in {"memory", "redis", "off"}:
        raise ValueError("CREDIT_RESPONSE_CACHE must be memory, redis or off")
    result_cache = (RedisResultCache(os.environ["CREDIT_REDIS_URL"]) if cache_mode == "redis"
                    else MemoryResultCache() if cache_mode == "memory" else NullResultCache())
    retrieval = None
    retrieval_mode = os.getenv("CREDIT_RETRIEVAL", "local")
    if retrieval_mode == "elasticsearch":
        catalog = Catalog()
        model = os.getenv("CREDIT_EMBEDDING_MODEL", DEFAULT_MODEL)
        embedder = FastEmbedder(model, Path(os.getenv("CREDIT_MODEL_CACHE", "var/models")))
        # Serving only uses an already-provisioned cache: a user question must
        # never trigger a model download or silently change its index contract.
        embedder.prepare(download=False)
        primary = ElasticsearchRetrieval(
            os.getenv("CREDIT_ELASTICSEARCH_URL", "http://127.0.0.1:9200"),
            os.getenv("CREDIT_ELASTICSEARCH_INDEX", "credit-knowledge-current"),
            catalog.document["version"], model, embedder.embed_query,
            fingerprint=embedder.fingerprint, catalog=catalog,
        )
        retrieval = FallbackRetrieval(primary, LocalRetrieval(catalog))
    elif retrieval_mode != "local":
        raise ValueError("CREDIT_RETRIEVAL must be local or elasticsearch")
    return create_app(Path(os.getenv("CREDIT_DB", "var/credit.db")), users, memory, retrieval,
                      result_cache=result_cache, identity_provider=identity_provider)
