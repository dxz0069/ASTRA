"""Real Pi tool -> dispatcher collector -> server Finding/Strike on loopback only."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from astra.dispatcher.evidence_collector import collect_scoped_request_evidence
from astra.dispatcher.runtime.local_process import LocalProcess
from astra.dispatcher.workers.adapters.pi import PiDriver
from astra.server import db
from astra.server.app import app

from test_pi_cli_integration import _ScenarioServer, _pi_runtime_or_skip, _text_chunks
from test_vuln_scope import _Target, _manifest, _tool_chunks, _worker


class _EvidenceClient:
    def __init__(self, api: TestClient):
        self.api = api

    def create_evidence(self, project_id, step_id, payload, *, collector_token):
        response = self.api.post(
            f"/projects/{project_id}/steps/{step_id}/evidence",
            json=payload,
            headers={"X-ASTRA-Collector-Token": collector_token},
        )
        return SimpleNamespace(
            ok=200 <= response.status_code < 300,
            status_code=response.status_code,
            text=response.text,
            data=response.json(),
        )


def test_real_pi_collector_source_and_independent_strike(monkeypatch, tmp_path) -> None:
    _pi_runtime_or_skip(monkeypatch, tmp_path)
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setenv("ASTRA_EVIDENCE_COLLECTOR_TOKEN", "local-collector-token")
    monkeypatch.setenv("ASTRA_VULN_STRICT", "1")
    db.configure(tmp_path / "astra.db")

    with _Target() as target:
        url = target.origin + "/allowed/data"

        def respond(index, _body):
            return (200, _tool_chunks(url)) if index % 2 == 1 else (200, _text_chunks("done"))

        with _ScenarioServer(respond) as model, TestClient(app) as api:
            worker = _worker(model.base_url, _manifest(target.origin), tmp_path / "agent")
            driver = PiDriver()
            created = api.post("/projects", json={
                "title": "local proof chain", "origin": "start", "goal": "finish", "bootstrap_enabled": False,
            })
            assert created.status_code == 201
            project_id = created.json()["project"]["id"]
            step = api.post(f"/projects/{project_id}/steps", json={
                "from": ["origin"], "description": "collect source", "creator": "planner",
            })
            source_step = step.json()["id"]
            assert api.post(f"/projects/{project_id}/steps/{source_step}/heartbeat", json={"worker": worker.name}).status_code == 200

            def collect(step_id: str):
                invocation = driver.build_execute(worker, "Call scoped_request once.", None)
                process = LocalProcess(invocation.argv, worker.env)
                process.start()
                result = process.communicate(timeout=45)
                assert result.returncode == 0, result.stderr
                session = driver.extract_session(None, result.stdout, result.stderr)
                assert session
                refs = collect_scoped_request_evidence(
                    _EvidenceClient(api), worker, project_id, step_id, result, session_id=session,
                )
                assert list(refs) == ["call-scoped"]
                return session, refs["call-scoped"]

            source_session, source_evidence = collect(source_step)
            source = api.post(f"/projects/{project_id}/steps/{source_step}/conclude", json={
                "worker": worker.name, "description": "synthetic observation",
                "finding": "synthetic candidate", "finding_high_value": True,
                "evidence_refs": [source_evidence],
            })
            assert source.status_code == 200, source.text
            finding = source.json()["finding"]
            assert finding["source_evidence_id"] == source_evidence

            strike_step = finding["verification_step_id"]
            assert api.post(f"/projects/{project_id}/steps/{strike_step}/heartbeat", json={"worker": worker.name}).status_code == 200
            strike_session, strike_evidence = collect(strike_step)
            assert strike_session != source_session
            verified = api.post(f"/projects/{project_id}/steps/{strike_step}/conclude", json={
                "worker": worker.name, "description": "independent synthetic recheck",
                "verification_status": "confirmed", "verification_summary": "loopback recheck",
                "evidence_refs": [strike_evidence],
            })
            assert verified.status_code == 200, verified.text
            assert verified.json()["finding"]["verification_evidence_id"] == strike_evidence
            assert verified.json()["finding"]["human_reproduction_status"] == "not_started"
