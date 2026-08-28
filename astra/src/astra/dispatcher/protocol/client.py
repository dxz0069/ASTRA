from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import os
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from astra.server.models import Intent, ProjectDetail, ProjectSummary, Settings

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class ASTRAClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()
        # 审计修复：服务端启用 ASTRA_AUTH_TOKEN 时客户端自动带 Bearer 头
        # （原客户端从不发送——认证一旦真正生效会把自家 dispatcher 全部 401）
        self._auth_headers: dict[str, str] = {}
        _token = os.environ.get("ASTRA_AUTH_TOKEN", "")
        if _token:
            self._auth_headers = {"Authorization": f"Bearer {_token}"}

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def update_settings(self, intent_timeout: int, reason_timeout: int) -> Settings:
        response = self._session().put(
            self._url("/settings"),
            json={"intent_timeout": intent_timeout, "reason_timeout": reason_timeout},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str, lease_token: str | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker, "lease_token": lease_token},
        )

    def release_reason(self, project_id: str, worker: str, lease_token: str | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker, "lease_token": lease_token},
        )

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def conclude(
        self,
        project_id: str,
        intent_id: str,
        worker: str,
        description: str,
        confidence: str = "medium",
        evidence: str | None = None,
        challenged: bool = False,
        kind: str = "regular",
    ) -> ApiResult:
        body: dict[str, Any] = {"worker": worker, "description": description, "confidence": confidence}
        if evidence:
            body["evidence"] = evidence
        if challenged:
            body["challenged"] = True
        if kind != "regular":
            body["kind"] = kind
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json=body,
        )

    def create_fact(self, project_id: str, description: str, kind: str = "regular", creator: str = "system") -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/facts",
            json={"description": description, "kind": kind, "creator": creator},
        )

    def create_hint(self, project_id: str, content: str, creator: str = "human") -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/hints",
            json={"content": content, "creator": creator},
        )

    def archive_facts(self, project_id: str, fact_ids: list[str]) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/facts/archive",
            json={"fact_ids": fact_ids},
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str, lease_token: str | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={"from": from_ids, "description": description, "worker": worker, "lease_token": lease_token},
        )

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json={"from": from_ids, "description": description, "creator": creator, "worker": None},
        )

    def _request_json(self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                data = response.json()
            except (ValueError, UnicodeDecodeError) as exc:
                # D3 修复：content-type 声称 json 但 body 非法（网关错误页/截断响应）
                # ——裸抛会杀死心跳线程导致租约静默失效→同 intent 双跑
                LOG.warning("response json parse failed method=%s path=%s error=%s", method, path, exc)
                return ApiResult(status_code=response.status_code, text=response.text)
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        if self._auth_headers:
            session.headers.update(self._auth_headers)
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session

    def _remove_session(self) -> None:
        """按当前线程 ident 注销并关闭其 Session（P1-2：修复 _sessions 只增不减泄漏）。

        HeartbeatLease 每个任务新建心跳线程并在其中调用 client 接口（_session()
        会在线程 ident 下注册 Session），线程结束后 Session 残留在 _sessions 里；
        长跑进程字典无线膨胀。心跳线程退出时调用本方法回收自身 Session。
        """
        ident = threading.get_ident()
        with self._sessions_lock:
            session = self._sessions.pop(ident, None)
        if session is not None:
            session.close()
