from __future__ import annotations

import logging
from typing import Any
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.models import DecideCheckpoint, RunningTask
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.local_containers import build_container_manager
from astra.dispatcher.runtime.startup_healthcheck import format_failure_summary, run_startup_healthchecks
from astra.dispatcher.scheduler.worker_select import choose_worker
from astra.dispatcher.tasks.bootstrap import run_bootstrap_task
from astra.dispatcher.tasks.execute import run_execute_task
from astra.dispatcher.tasks.decide import run_decide_task
from astra.server.models import Step, ProjectDetail, ProjectSummary

LOG = logging.getLogger(__name__)
UNHEALTHY_RETRY_AFTER_SECONDS = 5
REJECTED_RETRY_AFTER_SECONDS = 5
FAILED_RETRY_AFTER_SECONDS = 15
BOOTSTRAP_STEP_DESCRIPTION = "bootstrap"
BOOTSTRAP_STEP_CREATOR = "dispatcher.bootstrap"


@dataclass(slots=True)
class WorkerSelection:
    worker: WorkerConfig | None
    blocked_busy: list[str]
    blocked_unhealthy: list[str]
    blocked_rejected: list[str]
    blocked_task_type: list[str]


class DispatcherLoop:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = DispatchConfig.load(config_path)
        self.client = ASTRAClient(self.config.server)
        self.container_manager = build_container_manager(self.config.container, self.config.runtime.execution)
        self.executor = ThreadPoolExecutor(max_workers=self.config.runtime.max_workers)
        self.cleanup_executor = ThreadPoolExecutor(max_workers=max(1, min(8, self.config.runtime.max_workers)))
        self.futures: dict[Future[str], RunningTask] = {}
        self.cleanup_futures: dict[Future[bool], tuple[str, str | None, str | None]] = {}
        self.decide_checkpoints: dict[str, DecideCheckpoint] = {}
        self.runtime_project_ids: set[str] = set()
        self.worker_unhealthy_until: dict[str, float] = {}
        self.worker_rejected_until: dict[tuple[str, str, str], float] = {}
        self._log_state: dict[str, tuple[int, str, tuple[object, ...]]] = {}
        self._cleanup_pending: set[str] = set()
        self._inactive_cleanup_done: dict[str, str] = {}
        self.project_cursor = 0
        self._settings_checked = False
        self._startup_healthchecks_checked = False

    def close(self) -> None:
        if self.futures:
            LOG.info(
                "dispatcher shutting down waiting_for_tasks=%s running_projects=%s",
                len(self.futures),
                sorted({task.project_id for task in self.futures.values()}),
            )
        self.executor.shutdown(wait=True)
        self.cleanup_executor.shutdown(wait=True)
        self.container_manager.close()
        self.client.close()

    def run(self, once: bool = False) -> None:
        try:
            self.run_startup_healthchecks()
            while True:
                try:
                    if not self._settings_checked:
                        self._validate_server_settings()
                        self._settings_checked = True
                    self._reap_futures()
                    self._reap_cleanup_futures()
                    summaries = self.client.list_projects()
                    self._initialize_decide_checkpoints(summaries)
                    self._refresh_runtime_projects(summaries)
                    self._cancel_inactive_tasks(summaries)
                    self._queue_container_cleanups(summaries)
                    self._dispatch_available(summaries)
                except requests.RequestException as exc:
                    if once:
                        raise
                    LOG.warning(
                        "dispatcher server request failed error=%s retry_in=%ss",
                        exc,
                        self.config.runtime.interval,
                    )
                    time.sleep(self.config.runtime.interval)
                    continue
                if once:
                    break
                time.sleep(self.config.runtime.interval)
        finally:
            self.close()

    def run_startup_healthchecks_only(self) -> None:
        try:
            self.run_startup_healthchecks(show_commands=True, force=True)
        finally:
            self.close()

    def run_startup_healthchecks(self, *, show_commands: bool = False, force: bool = False) -> None:
        if self._startup_healthchecks_checked:
            return
        if not force and self.config.runtime.worker_healthcheck == "disabled":
            LOG.info("skip startup worker healthchecks because runtime.worker_healthcheck=disabled")
            self._startup_healthchecks_checked = True
            return
        self._run_startup_healthchecks(show_commands=show_commands)
        self._startup_healthchecks_checked = True

    def _dispatch_available(self, summaries: list[ProjectSummary]) -> None:
        if len(self.futures) >= self.config.runtime.max_workers:
            self._log_changed(
                "dispatch/global",
                logging.INFO,
                "skip dispatch because max_workers reached running_tasks=%s",
                len(self.futures),
            )
            return
        active = [summary for summary in summaries if summary.status == "active"]
        if not active:
            self._log_changed("dispatch/global", logging.INFO, "skip dispatch because no active projects")
            return

        running_projects = self._ordered_projects(
            [summary for summary in active if summary.id in self.runtime_project_ids]
        )
        idle_projects = self._ordered_projects(
            [summary for summary in active if summary.id not in self.runtime_project_ids]
        )

        dispatched = True
        while dispatched and len(self.futures) < self.config.runtime.max_workers:
            dispatched = False
            for summary in running_projects:
                if self._try_dispatch_project(summary):
                    dispatched = True
                    if len(self.futures) >= self.config.runtime.max_workers:
                        return
            if dispatched:
                continue
            if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                self._log_changed(
                    "dispatch/idle-limit",
                    logging.INFO,
                    "skip idle project dispatch because max_running_projects reached running_projects=%s",
                    self._running_project_count(active),
                )
                return
            for summary in idle_projects:
                if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                    self._log_changed(
                        "dispatch/idle-limit",
                        logging.INFO,
                        "stop idle project dispatch because max_running_projects reached running_projects=%s",
                        self._running_project_count(active),
                    )
                    return
                if self._try_dispatch_project(summary):
                    dispatched = True
                    break

    def _ordered_projects(self, summaries: list[ProjectSummary]) -> list[ProjectSummary]:
        if not summaries:
            return []
        ids = [summary.id for summary in summaries]
        ids.sort()
        offset = self.project_cursor % len(ids)
        ordered_ids = ids[offset:] + ids[:offset]
        by_id = {summary.id: summary for summary in summaries}
        self.project_cursor += 1
        return [by_id[project_id] for project_id in ordered_ids]

    def _try_dispatch_project(self, summary: ProjectSummary) -> bool:
        skip_scope = f"project:{summary.id}:skip"
        container_name = self.container_manager.container_name(summary.id)
        if container_name in self._cleanup_pending:
            self._log_changed(
                f"{skip_scope}:cleanup_pending",
                logging.DEBUG,
                "skip project=%s because container cleanup is still pending container=%s",
                summary.id,
                container_name,
            )
            return False
        if self._project_running_task_count(summary.id) >= self.config.runtime.max_project_workers:
            self._log_changed(
                f"{skip_scope}:max_project_workers",
                logging.INFO,
                "skip project=%s because max_project_workers reached running_tasks=%s",
                summary.id,
                self._project_running_task_summary(summary.id),
            )
            return False

        project = self.client.get_project(summary.id)
        if project.project.status != "active":
            self._log_changed(
                f"{skip_scope}:status",
                logging.INFO,
                "skip project=%s because status=%s",
                summary.id,
                project.project.status,
            )
            return False
        if self._is_initial_project(project):
            if project.project.decide is not None:
                return False
            if self._project_requires_bootstrap(project):
                return self._dispatch_initial_project(project)
            export_yaml = self.client.export_project(summary.id)
            return self._dispatch_decide(project, export_yaml, "initial")
        if project.project.decide is None:
            decide_trigger = self._decide_trigger(project)
            if decide_trigger is not None:
                export_yaml = self.client.export_project(summary.id)
                return self._dispatch_decide(project, export_yaml, decide_trigger)
        running_step_ids = self._project_running_execute_steps(summary.id)
        unclaimed_steps = [
            step
            for step in project.steps
            if step.to is None
            and step.status == "open"
            and step.worker is None
            and step.id not in running_step_ids
            and not self._is_bootstrap_step(step)
        ]
        if running_step_ids and not unclaimed_steps:
            self._log_changed(
                f"{skip_scope}:execute_running",
                logging.DEBUG,
                "skip execute project=%s because all unclaimed steps are already running locally steps=%s",
                summary.id,
                sorted(running_step_ids),
            )
        if unclaimed_steps:
            newest = max(unclaimed_steps, key=lambda i: i.created_at)
            export_yaml = self.client.export_project(summary.id)
            return self._dispatch_execute(project, export_yaml, newest)
        if project.project.decide is not None:
            self._log_changed(
                f"{skip_scope}:decide_claimed",
                logging.DEBUG,
                "skip decide project=%s because decide is already claimed by %s",
                summary.id,
                project.project.decide.worker,
            )
            return False
        self._log_changed(
            f"{skip_scope}:graph_unchanged",
            logging.DEBUG,
            "skip decide project=%s because decide state unchanged facts=%s hints=%s open_steps=%s steps=%s",
            summary.id,
            len(project.facts),
            len(project.hints),
            self._project_open_step_count(project),
            len(project.steps),
        )
        return False

    def _dispatch_initial_project(self, project: ProjectDetail) -> bool:
        step = self._get_bootstrap_step(project)
        if step is None:
            step = self._create_bootstrap_step(project.project.id)
            if step is None:
                return False
        if self._project_has_running_bootstrap(project.project.id):
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_running",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap task is already running locally",
                project.project.id,
            )
            return False
        if step.worker is not None:
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_claimed",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap step=%s is already claimed by %s",
                project.project.id,
                step.id,
                step.worker,
            )
            return False
        return self._dispatch_bootstrap(project, step)

    def _dispatch_decide(self, project: ProjectDetail, export_yaml: str, trigger: str) -> bool:
        selection = self._select_worker(project.project.id, "decide")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:decide",
                logging.INFO,
                "no worker available for decide project=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:decide")
        claim = self.client.claim_decide(project.project.id, worker.name, trigger)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "decide claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "decide claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        # 审计修复（租约令牌）：claim 响应携带持有凭证，透传任务与清理路径
        lease_token = (claim.data or {}).get("decide_token") if isinstance(claim.data, dict) else None
        try:
            future = self.executor.submit(
                run_decide_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                worker,
                cancellation := TaskCancellation(),
                lease_token,
            )
        except Exception:
            LOG.exception("failed to submit decide task project=%s worker=%s", project.project.id, worker.name)
            self._best_effort_release_decide(project.project.id, worker.name, lease_token)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "decide",
            worker.name,
            cancellation,
            step_id=None,
            fact_count=len(project.facts),
            hint_count=len(project.hints),
            open_step_count=self._project_open_step_count(project),
            lease_token=lease_token,
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched decide project=%s worker=%s trigger=%s", project.project.id, worker.name, trigger)
        return True

    def _dispatch_bootstrap(self, project: ProjectDetail, step: Step) -> bool:
        selection = self._select_worker(project.project.id, "bootstrap")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:bootstrap",
                logging.INFO,
                "no worker available for bootstrap project=%s step=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                step.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:bootstrap")
        claim = self.client.heartbeat(project.project.id, step.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "bootstrap claim failed project=%s step=%s worker=%s status=%s",
                project.project.id,
                step.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "bootstrap claim failed project=%s step=%s worker=%s status=%s",
                project.project.id,
                step.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_bootstrap_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                step,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit bootstrap task project=%s step=%s worker=%s", project.project.id, step.id, worker.name)
            self._best_effort_release(project.project.id, step.id, worker.name)
            return False
        self.futures[future] = RunningTask(project.project.id, "bootstrap", worker.name, cancellation, step_id=step.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched bootstrap project=%s step=%s worker=%s", project.project.id, step.id, worker.name)
        return True

    def _dispatch_execute(self, project: ProjectDetail, export_yaml: str, step: Step) -> bool:
        selection = self._select_worker(project.project.id, "execute")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:execute",
                logging.INFO,
                "no worker available for execute project=%s step=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                step.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:execute")
        claim = self.client.heartbeat(project.project.id, step.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "execute claim failed project=%s step=%s worker=%s status=%s",
                project.project.id,
                step.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "execute claim failed project=%s step=%s worker=%s status=%s",
                project.project.id,
                step.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_execute_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                step,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit execute task project=%s step=%s worker=%s", project.project.id, step.id, worker.name)
            self._best_effort_release(project.project.id, step.id, worker.name)
            return False
        self.futures[future] = RunningTask(project.project.id, "execute", worker.name, cancellation, step_id=step.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched execute project=%s step=%s worker=%s", project.project.id, step.id, worker.name)
        return True

    def _select_worker(self, project_id: str, task_type: str) -> WorkerSelection:
        now = time.time()
        candidates: list[WorkerConfig] = []
        blocked_busy: list[str] = []
        blocked_unhealthy: list[str] = []
        blocked_rejected: list[str] = []
        blocked_task_type: list[str] = []
        running_counts = self._worker_counts()
        for worker in self.config.workers:
            if task_type not in worker.task_types:
                blocked_task_type.append(worker.name)
                continue
            running = running_counts.get(worker.name, 0)
            if running >= worker.max_running:
                blocked_busy.append(f"{worker.name}({running}/{worker.max_running})")
                continue
            unhealthy_until = self.worker_unhealthy_until.get(worker.name, 0)
            if unhealthy_until > now:
                blocked_unhealthy.append(f"{worker.name}({unhealthy_until - now:.1f}s)")
                continue
            rejected_until = self.worker_rejected_until.get((project_id, task_type, worker.name), 0)
            if rejected_until > now:
                blocked_rejected.append(f"{worker.name}({rejected_until - now:.1f}s)")
                continue
            candidates.append(worker)
        if not candidates:
            LOG.debug(
                "worker selection project=%s task=%s no candidates blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s",
                project_id,
                task_type,
                blocked_busy,
                blocked_unhealthy,
                blocked_rejected,
                blocked_task_type,
            )
            return WorkerSelection(
                worker=None,
                blocked_busy=blocked_busy,
                blocked_unhealthy=blocked_unhealthy,
                blocked_rejected=blocked_rejected,
                blocked_task_type=blocked_task_type,
            )
        ordered = choose_worker(candidates, running_counts)
        LOG.debug(
            "worker selection project=%s task=%s candidates=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s chosen=%s",
            project_id,
            task_type,
            [f"{worker.name}({running_counts.get(worker.name, 0)}/{worker.max_running},p{worker.priority})" for worker in candidates],
            blocked_busy,
            blocked_unhealthy,
            blocked_rejected,
            blocked_task_type,
            ordered[0].name if ordered else None,
        )
        return WorkerSelection(
            worker=ordered[0] if ordered else None,
            blocked_busy=blocked_busy,
            blocked_unhealthy=blocked_unhealthy,
            blocked_rejected=blocked_rejected,
            blocked_task_type=blocked_task_type,
        )

    def _worker_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.futures.values():
            counts[task.worker_name] = counts.get(task.worker_name, 0) + 1
        return counts

    def _project_running_task_count(self, project_id: str) -> int:
        return sum(1 for task in self.futures.values() if task.project_id == project_id)

    def _project_running_task_summary(self, project_id: str) -> list[str]:
        summary: list[str] = []
        for task in self.futures.values():
            if task.project_id != project_id:
                continue
            if task.step_id is None:
                summary.append(f"{task.task_type}:{task.worker_name}")
            else:
                summary.append(f"{task.task_type}:{task.worker_name}:{task.step_id}")
        summary.sort()
        return summary

    def _project_has_running_bootstrap(self, project_id: str) -> bool:
        return any(task.project_id == project_id and task.task_type == "bootstrap" for task in self.futures.values())

    def _project_running_execute_steps(self, project_id: str) -> set[str]:
        return {
            task.step_id
            for task in self.futures.values()
            if task.project_id == project_id and task.task_type == "execute" and task.step_id is not None
        }

    def _running_project_count(self, summaries: list[ProjectSummary]) -> int:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        return len(self.runtime_project_ids & active_ids)

    def _project_open_step_count(self, project: ProjectDetail) -> int:
        # 只数真正可认领的步骤：已收束（to 非空）与已关闭（status=closed 的死路账本）都不算
        return sum(1 for step in project.steps if step.to is None and step.status == "open")

    def _is_bootstrap_step(self, step: Step) -> bool:
        return (
            step.description == BOOTSTRAP_STEP_DESCRIPTION
            and step.creator == BOOTSTRAP_STEP_CREATOR
            and step.from_ == ["origin"]
            and step.to is None
        )

    def _get_bootstrap_step(self, project: ProjectDetail) -> Step | None:
        steps = [step for step in project.steps if self._is_bootstrap_step(step)]
        if not steps:
            return None
        if len(steps) > 1:
            LOG.warning("project has multiple bootstrap steps project=%s steps=%s", project.project.id, [step.id for step in steps])
        steps.sort(key=lambda step: (step.worker is not None, step.created_at, step.id))
        return steps[0]

    def _is_initial_project(self, project: ProjectDetail) -> bool:
        fact_ids = {fact.id for fact in project.facts}
        if fact_ids != {"origin", "goal"} or len(project.facts) != 2:
            return False
        if not project.steps:
            return True
        return all(self._is_bootstrap_step(step) for step in project.steps)

    def _project_requires_bootstrap(self, project: ProjectDetail) -> bool:
        if not project.project.bootstrap_enabled:
            return False
        if self._get_bootstrap_step(project) is not None:
            return True
        return any("bootstrap" in worker.task_types for worker in self.config.workers)

    def _create_bootstrap_step(self, project_id: str) -> Step | None:
        response = self.client.create_step(
            project_id,
            ["origin"],
            BOOTSTRAP_STEP_DESCRIPTION,
            BOOTSTRAP_STEP_CREATOR,
        )
        if response.status_code == 403:
            LOG.info("project became inactive before bootstrap step create project=%s", project_id)
            return None
        if not response.ok:
            LOG.warning(
                "bootstrap step write failed project=%s status=%s body=%s",
                project_id,
                response.status_code,
                response.text,
            )
            return None
        if not isinstance(response.data, dict):
            LOG.warning("bootstrap step create returned empty body project=%s", project_id)
            return None
        step = Step.model_validate(response.data)
        LOG.info("created bootstrap step project=%s step=%s", project_id, step.id)
        return step

    def _decide_trigger(self, project: ProjectDetail) -> str | None:
        open_step_count = self._project_open_step_count(project)
        checkpoint = self.decide_checkpoints.get(project.project.id)
        if checkpoint is None:
            return "initial"
        changes: list[str] = []
        if len(project.facts) > checkpoint.fact_count:
            changes.append(f"facts:{checkpoint.fact_count}->{len(project.facts)}")
        if len(project.hints) > checkpoint.hint_count:
            changes.append(f"hints:{checkpoint.hint_count}->{len(project.hints)}")
        if checkpoint.open_step_count > 0 and open_step_count == 0:
            changes.append(f"open_steps:{checkpoint.open_step_count}->0")
        if not changes:
            return None
        return ",".join(changes)

    def _reap_futures(self) -> None:
        done = [future for future in self.futures if future.done()]
        # 调度器 J2：先收集本批每个 worker 的结局——unhealthy 优先于 success
        batch_unhealthy_workers: set[str] = set()
        batch_results: list[tuple[Any, str]] = []
        for future in done:
            task = self.futures.pop(future)
            try:
                outcome = future.result()
                batch_results.append((task, outcome))
                if outcome == "unhealthy":
                    batch_unhealthy_workers.add(task.worker_name)
            except Exception as exc:  # noqa: BLE001 —— worker 函数崩溃
                LOG.exception("task crashed project=%s", getattr(task, "project_id", "?"))
                batch_results.append((task, "crashed"))
        for task, outcome in batch_results:
                if outcome == "cancelled":
                    LOG.info(
                        "task cancelled project=%s task=%s worker=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                    )
                elif outcome != "success":
                    LOG.warning(
                        "task finished project=%s task=%s worker=%s outcome=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        outcome,
                    )
                self._clear_project_log_state(task.project_id)
                # 调度器 D1 修复：项目在 futures 中已无任务时从 runtime_project_ids
                # 移除——否则只增不减，长跑后 max_running_projects 被"碰过的"而非
                # "在跑的"项目耗尽，新项目永久饿死
                if not any(t.project_id == task.project_id for t in self.futures.values()):
                    self.runtime_project_ids.discard(task.project_id)
                if outcome == "unhealthy":
                    retry_after_seconds = UNHEALTHY_RETRY_AFTER_SECONDS
                    self.worker_unhealthy_until[task.worker_name] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked unhealthy worker=%s retry_after=%.0fs",
                        task.worker_name,
                        retry_after_seconds,
                    )
                elif task.worker_name not in batch_unhealthy_workers:
                    # 调度器 J2：本批该 worker 有 unhealthy 结局时不清除冷却
                    self.worker_unhealthy_until.pop(task.worker_name, None)
                rejection_key = (task.project_id, task.task_type, task.worker_name)
                if outcome == "rejected":
                    retry_after_seconds = REJECTED_RETRY_AFTER_SECONDS
                    self.worker_rejected_until[rejection_key] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked rejected project=%s task=%s worker=%s retry_after=%.0fs",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        retry_after_seconds,
                    )
                elif outcome == "failed":
                    # 失败也进入冷却，防止 API 故障/环境异常时无限重派同一任务的失败风暴
                    self.worker_rejected_until[rejection_key] = time.time() + FAILED_RETRY_AFTER_SECONDS
                    LOG.info(
                        "worker cooled after failure project=%s task=%s worker=%s retry_after=%.0fs",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        FAILED_RETRY_AFTER_SECONDS,
                    )
                else:
                    self.worker_rejected_until.pop(rejection_key, None)
                if outcome == "success" and task.task_type == "decide":
                    assert task.fact_count is not None
                    assert task.hint_count is not None
                    assert task.open_step_count is not None
                    self.decide_checkpoints[task.project_id] = DecideCheckpoint(
                        fact_count=task.fact_count,
                        hint_count=task.hint_count,
                        open_step_count=task.open_step_count,
                    )
                    LOG.debug(
                        "decide checkpoint updated project=%s facts=%s hints=%s open_steps=%s",
                        task.project_id,
                        task.fact_count,
                        task.hint_count,
                        task.open_step_count,
                    )
                if outcome == "crashed":
                    # 崩溃（未捕获异常）同样进冷却：触发条件往往仍成立
                    # （图超预算等），不冷却会每个调度周期重派崩溃、无限刷屏
                    crash_key = (task.project_id, task.task_type, task.worker_name)
                    self.worker_rejected_until[crash_key] = time.time() + FAILED_RETRY_AFTER_SECONDS

    def _cleanup_completed_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "completed":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_completed_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_completed, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _cleanup_stopped_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "stopped":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_stopped_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_stopped, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _queue_container_cleanups(self, summaries: list[ProjectSummary]) -> None:
        self._cleanup_completed_containers(summaries)
        self._cleanup_stopped_containers(summaries)

    def _reap_cleanup_futures(self) -> None:
        done = [future for future in self.cleanup_futures if future.done()]
        for future in done:
            name, project_id, target_status = self.cleanup_futures.pop(future)
            self._cleanup_pending.discard(name)
            try:
                success = future.result()
                if success and project_id is not None and target_status in ("completed", "stopped"):
                    self._inactive_cleanup_done[project_id] = target_status
                elif project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
            except Exception:
                if project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
                LOG.exception("container cleanup failed container=%s", name)

    def _refresh_runtime_projects(self, summaries: list[ProjectSummary]) -> None:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        self.runtime_project_ids.intersection_update(active_ids)
        inactive_status_by_id = {summary.id: summary.status for summary in summaries if summary.status != "active"}
        for project_id, status in list(self._inactive_cleanup_done.items()):
            current_status = inactive_status_by_id.get(project_id)
            if current_status != status:
                self._inactive_cleanup_done.pop(project_id, None)
        # P1-7：清理已从服务端消失（删除）的项目在调度器字典中的残留键，防跨项目
        # 无界增长。注意判据用"服务端项目列表"而非 runtime_project_ids——后者只含
        # 本实例派发过的活跃项目，按它清理会把"活跃但暂未派发"项目的 decide
        # checkpoint 逐周期驱逐，_decide_trigger 误判 initial 导致反复重派 decide
        # 任务（回归）；项目删除后其键才是真正的死数据，驱逐零风险。
        known_ids = {summary.id for summary in summaries}
        for project_id in list(self.decide_checkpoints):
            if project_id not in known_ids:
                self.decide_checkpoints.pop(project_id, None)
        for key in list(self.worker_rejected_until):
            if key[0] not in known_ids:
                self.worker_rejected_until.pop(key, None)
        for scope in list(self._log_state):
            if scope.startswith("project:") and scope.split(":", 2)[1] not in known_ids:
                self._log_state.pop(scope, None)

    def _cancel_inactive_tasks(self, summaries: list[ProjectSummary]) -> None:
        status_by_project = {summary.id: summary.status for summary in summaries}
        for task in self.futures.values():
            status = status_by_project.get(task.project_id, "deleted")
            if status != "active" and task.cancellation.cancel(status):
                LOG.info(
                    "cancelling running task for inactive project project=%s task=%s worker=%s status=%s",
                    task.project_id,
                    task.task_type,
                    task.worker_name,
                    status,
                )

    def _initialize_decide_checkpoints(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "active":
                continue
            if summary.id in self.decide_checkpoints:
                continue
            open_step_count = summary.working_step_count + summary.unclaimed_step_count
            if open_step_count == 0:
                continue
            self.decide_checkpoints[summary.id] = DecideCheckpoint(
                fact_count=summary.fact_count,
                hint_count=summary.hint_count,
                open_step_count=open_step_count,
            )
            LOG.debug(
                "decide checkpoint initialized project=%s facts=%s hints=%s open_steps=%s",
                summary.id,
                summary.fact_count,
                summary.hint_count,
                open_step_count,
            )

    def _best_effort_release(self, project_id: str, step_id: str, worker_name: str) -> None:
        response = self.client.release(project_id, step_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("release failed project=%s step=%s worker=%s status=%s", project_id, step_id, worker_name, response.status_code)

    def _best_effort_release_decide(self, project_id: str, worker_name: str, lease_token: str | None = None) -> None:
        response = self.client.release_decide(project_id, worker_name, lease_token)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("decide release failed project=%s worker=%s status=%s", project_id, worker_name, response.status_code)

    def _log_changed(self, scope: str, level: int, message: str, *args: object) -> None:
        state = (level, message, args)
        if self._log_state.get(scope) == state:
            return
        self._log_state[scope] = state
        LOG.log(level, message, *args)

    def _clear_log_state(self, scope: str) -> None:
        self._log_state.pop(scope, None)

    def _clear_project_log_state(self, project_id: str) -> None:
        prefix = f"project:{project_id}:"
        for scope in list(self._log_state):
            if scope.startswith(prefix):
                self._log_state.pop(scope, None)

    def _validate_server_settings(self) -> None:
        settings = self.client.get_settings()
        interval = self.config.runtime.interval
        needs_patch: dict[str, int] = {}
        for name, value in (("step_timeout", settings.step_timeout), ("decide_timeout", settings.decide_timeout)):
            if value <= interval:
                # 审计修复（2026-08-28 二修）：此前只 LOG 不动作，"钳制"是空头支票——
                # 心跳仍按 interval 发，租约 value<=interval 必过期，同任务重派双跑。
                # 现在真的修：把 server 端超时 PATCH 到 interval*2 安全下限；修不动
                # （网络故障/只读）才退化为告警（自愈优先于崩溃）。
                needs_patch[name] = interval * 2
                LOG.error(
                    "server %s=%ss <= dispatcher interval=%ss; patching server setting to %ss",
                    name, value, interval, interval * 2,
                )
        if needs_patch:
            try:
                self.client.update_settings(
                    step_timeout=needs_patch.get("step_timeout", settings.step_timeout),
                    decide_timeout=needs_patch.get("decide_timeout", settings.decide_timeout),
                )
                LOG.info("server settings patched to safe floor %s", needs_patch)
            except Exception as exc:  # noqa: BLE001
                LOG.error("server settings patch failed (%s); heartbeat/lease mismatch risk until server fixed", exc)
        for name, value in (("step_timeout", settings.step_timeout), ("decide_timeout", settings.decide_timeout)):
            if value < interval * 2 and name not in needs_patch:
                LOG.warning(
                    "server %s is tight %s=%ss interval=%ss; heartbeat slack is only %ss",
                    name,
                    name,
                    value,
                    interval,
                    value - interval,
                )

    def _run_startup_healthchecks(self, *, show_commands: bool) -> None:
        results = run_startup_healthchecks(self.config, self.container_manager, show_commands=show_commands)
        if any(result.ok for result in results):
            return
        raise RuntimeError(format_failure_summary(results))
