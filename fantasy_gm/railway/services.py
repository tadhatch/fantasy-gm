# fantasy_gm/railway/services.py

from __future__ import annotations

import os
import time
import uuid

from rich.console import Console

from fantasy_gm.railway.client import RailwayClient, RailwayService


console = Console()

SHARED_VARIABLES = [
    "ESPN_LEAGUE_ID",
    "ESPN_TEAM_ID",
    "ESPN_SEASON",
    "ESPN_SWID",
    "ESPN_S2",
    "ESPN_POLL_SECONDS",
    "OPENAI_API_KEY",
    "FANTASY_GM_FAVORITE_TEAM",
    "FANTASY_GM_FANDOM_WEIGHT",
    "FANTASY_GM_CONTEXT_MODEL",
    "FANTASY_GM_DEEP_DIVE_MODEL",
    "FANTASY_GM_DEEP_DIVE_THRESHOLD",
    "FANTASY_GM_MAX_DEEP_DIVES_PER_REFRESH",
    "FANTASY_GM_AUTO_SELECT",
    "FANTASY_GM_TRANSACTIONS_MODE",
    "FANTASY_GM_CHATBOT_ENABLED",
    "FANTASY_GM_CHATBOT_MODE",
    "FANTASY_GM_CHATBOT_MODEL",
    "FANTASY_GM_CHAT_TOPIC_ID",
    "FANTASY_GM_CHAT_POLL_SECONDS",
    "FANTASY_GM_CHAT_DRAFT_REFRESH_SECONDS",
    "FANTASY_GM_CHAT_MAX_ACTIONS_PER_MINUTE",
    "FANTASY_GM_CHAT_MAX_REPLIES_PER_MINUTE",
    "FANTASY_GM_CHAT_MAX_CHARS",
    "FANTASY_GM_CHAT_RECENT_MESSAGES",
    "FANTASY_GM_CHAT_RECENT_PICKS",
    "FANTASY_GM_CHAT_REACTIONS",
    "FANTASY_GM_CHAT_PROACTIVE",
    "FANTASY_GM_CHAT_DEBUG",
]

# A cross-service reference to the Postgres plugin, set directly on each
# service that needs DATABASE_URL rather than routed through a project
# Shared Variable (a Shared Variable holding this same reference was
# observed not to resolve). This exact form — no spaces inside the
# braces, referencing DATABASE_URL rather than DATABASE_PRIVATE_URL — is
# the one confirmed working by hand in the Railway dashboard; other
# variants (spaced, or pointed at DATABASE_PRIVATE_URL) resolved to an
# empty string for reasons that were never pinned down.
DATABASE_URL_REFERENCE = os.getenv(
    "FANTASY_GM_DATABASE_URL_REFERENCE",
    "${{Postgres.DATABASE_URL}}",
)

def has_actually_finished(service: RailwayService) -> bool:
    """
    True only once a deployment reached SUCCESS (i.e. actually started
    running) and has since stopped.

    `deploymentStopped` alone is not enough: it is also true for a
    deployment that was queued and then abandoned/superseded before it
    ever ran (observed in practice when a deploy request is rate-limited
    and Railway ends up cycling through several deployment attempts for
    the same service) — that is not the job finishing, it is the job
    never having started under that deployment id.
    """
    return (
        service.deployment_status == "SUCCESS"
        and bool(service.deployment_stopped)
    )


class RailwayServiceManager:
    def __init__(self) -> None:
        self.client = RailwayClient()
        self.repo = os.environ["FANTASY_GM_REPO"]
        # Names of workers wait_and_delete() has already decided are done
        # (e.g. it hit its safety-net timeout rather than seeing
        # deploymentStopped flip). log_services() also checks
        # deploymentStopped directly on each service, so this only matters
        # for that fallback case.
        self._finished: set[str] = set()

    def log_services(self) -> list[RailwayService]:
        services = self.client.list_services()

        console.print(
            "[bold cyan]Controlled Railway services:[/bold cyan]"
        )

        for service in services:
            if service.name in self._finished or has_actually_finished(
                service
            ):
                status = "FINISHED"
            else:
                status = service.deployment_status or "NO DEPLOYMENT"
            console.print(f"  {service.name:<30} {status}")

        return services

    def cleanup_finished_workers(
        self,
        *,
        exclude: set[str],
    ) -> list[str]:
        """
        Delete any controlled service whose latest deployment has already
        stopped (Railway's `deploymentStopped` flag).

        Meant to run once at supervisor startup to sweep up workers left
        behind by a previous supervisor process — e.g. one that got
        redeployed/restarted before its own wait_and_delete() got a chance
        to notice a job had finished and clean it up itself.

        `exclude` must include every long-running service name (supervisor,
        chatbot) — this only ever deletes services it finds already
        stopped, but the exclusion is kept explicit rather than relying on
        "a running service can't be stopped" reasoning alone.
        """
        services = self.client.list_services()
        deleted: list[str] = []

        for service in services:
            if service.name in exclude:
                continue

            if not has_actually_finished(service):
                continue

            console.print(
                f"[yellow][WORKER][/yellow] "
                f"found orphaned finished service {service.name} "
                "from a previous run; deleting"
            )

            if self._try_delete_orphan(
                service.id, service.name, label="WORKER"
            ):
                deleted.append(service.name)

        return deleted

    def ensure_chatbot(self) -> RailwayService:
        name = os.getenv(
            "FANTASY_GM_CHATBOT_SERVICE",
            "chatbot",
        )

        service = self.client.find_service(name)

        if service is not None:
            console.print(
                f"[green][CHATBOT][/green] "
                f"service exists: {service.name} "
                f"status={service.deployment_status}"
            )
            return service

        console.print(
            f"[yellow][CHATBOT][/yellow] "
            f"service missing; creating {name}"
        )

        service = self.client.create_service(
            name=name,
            repo=self.repo,
        )

        console.print(
            f"[cyan][CHATBOT][/cyan] "
            f"created service id={service.id}"
        )

        try:
            console.print(
                f"[cyan][CHATBOT][/cyan] "
                "attaching shared variables"
            )

            self.client.attach_shared_variables(
                service_id=service.id,
                variable_names=SHARED_VARIABLES,
            )

            console.print(
                f"[green][CHATBOT][/green] "
                f"attached {len(SHARED_VARIABLES)} shared variables"
            )

            self.client.set_variable(
                service_id=service.id,
                name="DATABASE_URL",
                value=DATABASE_URL_REFERENCE,
            )

            console.print(
                f"[green][CHATBOT][/green] "
                f"attached DATABASE_URL ({DATABASE_URL_REFERENCE})"
            )

            self.client.configure_service(
                service_id=service.id,
                start_command="fantasy-gm chatbot run",
                restart_policy="ON_FAILURE",
            )

            console.print(
                "[cyan][CHATBOT][/cyan] "
                "configured start command and restart policy"
            )

            deployment_id = self.client.deploy_service(service.id)

            console.print(
                f"[green][CHATBOT][/green] "
                f"deployment requested id={deployment_id}"
            )

        except Exception:
            console.print(
                f"[bold red][CHATBOT][/bold red] "
                f"provisioning failed after creation for {name}; "
                "it would otherwise deploy with no start command"
            )
            self._try_delete_orphan(service.id, name, label="CHATBOT")
            raise

        return service

    def launch_test_worker(
        self,
        *,
        sleep_seconds: int = 240,
    ) -> RailwayService:
        job_id = uuid.uuid4().hex[:8]
        return self._launch_disposable_worker(
            name=f"worker-test-{job_id}",
            start_command=(
                "fantasy-gm worker test "
                f"--job-id {job_id} "
                f"--sleep {sleep_seconds}"
            ),
        )

    def launch_evaluation_worker(
        self,
        *,
        max_calls: int = 20,
        freshness_hours: int = 24,
    ) -> RailwayService:
        job_id = uuid.uuid4().hex[:8]
        return self._launch_disposable_worker(
            name=f"worker-evaluate-{job_id}",
            start_command=(
                "fantasy-gm worker evaluate "
                f"--max-calls {max_calls} "
                f"--freshness-hours {freshness_hours}"
            ),
        )

    def launch_waiver_worker(
        self,
        *,
        pool_size: int = 60,
        shortlist_size: int = 15,
        deep_dive_budget: int = 3,
    ) -> RailwayService:
        job_id = uuid.uuid4().hex[:8]
        return self._launch_disposable_worker(
            name=f"worker-waiver-{job_id}",
            start_command=(
                "fantasy-gm worker waiver "
                f"--pool-size {pool_size} "
                f"--shortlist-size {shortlist_size} "
                f"--deep-dive-budget {deep_dive_budget}"
            ),
        )

    def _launch_disposable_worker(
        self,
        *,
        name: str,
        start_command: str,
        restart_policy: str = "NEVER",
    ) -> RailwayService:
        """
        Shared provisioning sequence for any one-shot worker: create the
        service, attach shared variables + DATABASE_URL, configure the
        start command, then deploy.

        Only a failure before deploy is treated as fatal (the service
        genuinely has no valid start command yet, so discarding it is
        safe). A failed deploy call is logged but the service is still
        handed back — Railway has been observed to deploy anyway despite
        returning an error here, so the caller should keep tracking it
        rather than assume it failed.
        """
        console.print(
            f"[yellow][WORKER][/yellow] "
            f"creating disposable worker {name}"
        )

        service = self.client.create_service(
            name=name,
            repo=self.repo,
        )

        console.print(
            f"[cyan][WORKER][/cyan] "
            f"created {name} id={service.id}"
        )

        try:
            console.print(
                f"[cyan][WORKER][/cyan] "
                "attaching shared variables"
            )

            self.client.attach_shared_variables(
                service_id=service.id,
                variable_names=SHARED_VARIABLES,
            )

            console.print(
                f"[green][WORKER][/green] "
                f"attached {len(SHARED_VARIABLES)} shared variables"
            )

            self.client.set_variable(
                service_id=service.id,
                name="DATABASE_URL",
                value=DATABASE_URL_REFERENCE,
            )

            console.print(
                f"[green][WORKER][/green] "
                f"attached DATABASE_URL ({DATABASE_URL_REFERENCE})"
            )

            self.client.configure_service(
                service_id=service.id,
                start_command=start_command,
                restart_policy=restart_policy,
            )

            console.print(
                f"[cyan][WORKER][/cyan] "
                f"configured {name}: restart={restart_policy}"
            )

        except Exception:
            console.print(
                f"[bold red][WORKER][/bold red] "
                f"provisioning failed before deploy for {name}; "
                "it would otherwise deploy with no start command"
            )
            self._try_delete_orphan(service.id, name, label="WORKER")
            raise

        try:
            deployment_id = self.client.deploy_service(service.id)

            console.print(
                f"[green][WORKER][/green] "
                f"deployment requested for {name} "
                f"id={deployment_id}"
            )

        except Exception as exc:
            console.print(
                f"[bold yellow][WORKER][/bold yellow] "
                f"deploy request for {name} returned an error ({exc!r}); "
                "Railway may still deploy it, so continuing to track it "
                "rather than assuming it failed"
            )

        return service

    def _try_delete_orphan(
        self,
        service_id: str,
        name: str,
        *,
        label: str,
    ) -> bool:
        """
        Best-effort cleanup. Never raises: a failure here must not mask
        whatever error actually triggered the cleanup, and the caller
        needs to know the service may still be sitting there so it can
        say so.
        """
        try:
            self.client.delete_service(service_id)
        except Exception as exc:
            console.print(
                f"[bold red][{label}][/bold red] "
                f"could not delete {name} automatically ({exc!r}); "
                "delete it manually in the Railway dashboard"
            )
            return False

        console.print(
            f"[green][{label}][/green] deleted {name}"
        )
        return True

    def wait_and_delete(
        self,
        service: RailwayService,
        *,
        expected_runtime_seconds: float,
        poll_seconds: int = 15,
        max_wait_seconds: float | None = None,
    ) -> None:
        """
        Delete `service` once its job has finished.

        The authoritative signal is Railway's `deploymentStopped` flag,
        but only once the deployment has actually reached SUCCESS —
        `deploymentStopped` is also true for a deployment that was queued
        and then abandoned/superseded before it ever ran (seen in practice
        when a deploy request gets rate-limited and Railway cycles through
        several deployment attempts for the same service), which is not
        the job finishing.
        `expected_runtime_seconds` only sets a safety-net ceiling (default
        3x the expected runtime plus a minute) in case a genuine
        SUCCESS+stopped never arrives, so this doesn't poll forever.
        """
        max_wait = (
            max_wait_seconds
            if max_wait_seconds is not None
            else expected_runtime_seconds * 3 + 60
        )

        console.print(
            f"[cyan][WORKER][/cyan] "
            f"monitoring {service.name} "
            f"(expected runtime {expected_runtime_seconds:.0f}s, "
            f"max wait {max_wait:.0f}s)"
        )

        deadline = time.monotonic() + max_wait
        status = "UNKNOWN"

        while True:
            current = self.client.find_service(service.name)

            if current is None:
                console.print(
                    f"[yellow][WORKER][/yellow] "
                    f"{service.name} disappeared"
                )
                return

            status = current.deployment_status or "UNKNOWN"

            console.print(
                f"[cyan][WORKER][/cyan] "
                f"{service.name} status={status} "
                f"stopped={current.deployment_stopped}"
            )

            if has_actually_finished(current):
                break

            if status in {"FAILED", "CRASHED", "REMOVED"}:
                break

            if time.monotonic() >= deadline:
                console.print(
                    f"[bold yellow][WORKER][/bold yellow] "
                    f"{service.name} never reported deploymentStopped "
                    f"within {max_wait:.0f}s; cleaning up anyway"
                )
                break

            time.sleep(poll_seconds)

        console.print(
            f"[yellow][WORKER][/yellow] "
            f"{service.name} finished with status={status}; "
            "deleting service"
        )

        self._finished.add(service.name)
        self._try_delete_orphan(service.id, service.name, label="WORKER")
