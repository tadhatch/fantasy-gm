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

class RailwayServiceManager:
    def __init__(self) -> None:
        self.client = RailwayClient()
        self.repo = os.environ["FANTASY_GM_REPO"]
        # Names of workers whose expected runtime has elapsed (or that have
        # actually exited). Railway's own deployment status has no notion of
        # "the job finished its work" — it goes to SUCCESS as soon as the
        # container starts and stays there for the worker's whole life — so
        # this is tracked here instead, populated by wait_and_delete().
        self._finished: set[str] = set()

    def log_services(self) -> list[RailwayService]:
        services = self.client.list_services()

        console.print(
            "[bold cyan]Controlled Railway services:[/bold cyan]"
        )

        for service in services:
            status = (
                "FINISHED"
                if service.name in self._finished
                else (service.deployment_status or "NO DEPLOYMENT")
            )
            console.print(f"  {service.name:<30} {status}")

        return services

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
        name = f"worker-test-{job_id}"

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

            self.client.configure_service(
                service_id=service.id,
                start_command=(
                    "fantasy-gm worker test "
                    f"--job-id {job_id} "
                    f"--sleep {sleep_seconds}"
                ),
                restart_policy="NEVER",
            )

            console.print(
                f"[cyan][WORKER][/cyan] "
                f"configured {name}: restart=NEVER runtime={sleep_seconds}s"
            )

            deployment_id = self.client.deploy_service(service.id)

            console.print(
                f"[green][WORKER][/green] "
                f"deployment requested for {name} "
                f"id={deployment_id}"
            )

        except Exception:
            console.print(
                f"[bold red][WORKER][/bold red] "
                f"provisioning failed after creation for {name}; "
                "it would otherwise deploy with no start command"
            )
            self._try_delete_orphan(service.id, name, label="WORKER")
            raise

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
        grace_seconds: float = 20.0,
    ) -> None:
        """
        Delete `service` once its job has finished.

        Railway's deployment status only tracks build/deploy lifecycle —
        it reaches SUCCESS as soon as the container starts and stays there
        for the worker's entire life, so it cannot tell us when the job
        itself is done. Since the supervisor is the one that launched the
        worker with a known runtime, it tracks completion by elapsed time
        instead (plus a grace window), and only reacts early to a genuine
        failure status.
        """
        console.print(
            f"[cyan][WORKER][/cyan] "
            f"monitoring {service.name} "
            f"(expected runtime {expected_runtime_seconds:.0f}s)"
        )

        deadline = time.monotonic() + expected_runtime_seconds + grace_seconds
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
                f"{service.name} status={status}"
            )

            if status in {"FAILED", "CRASHED", "REMOVED"}:
                break

            if time.monotonic() >= deadline:
                break

            time.sleep(poll_seconds)

        console.print(
            f"[yellow][WORKER][/yellow] "
            f"{service.name} finished with status={status}; "
            "deleting service"
        )

        self._finished.add(service.name)
        self._try_delete_orphan(service.id, service.name, label="WORKER")

    def set_variable(
        self,
        *,
        service_id: str,
        name: str,
        value: str,
    ) -> None:
        mutation = """
        mutation VariableUpsert($input: VariableUpsertInput!) {
          variableUpsert(input: $input)
        }
        """

        self._graphql(
            mutation,
            {
                "input": {
                    "projectId": self.project_id,
                    "environmentId": self.environment_id,
                    "serviceId": service_id,
                    "name": name,
                    "value": value,
                }
            },
        )

    def attach_shared_variables(
        self,
        *,
        service_id: str,
        variable_names: list[str],
    ) -> None:
        for name in variable_names:
            self.set_variable(
                service_id=service_id,
                name=name,
                value=f"${{{{ shared.{name} }}}}",
            )

    def _attach_shared_variables(
        self,
        service: RailwayService,
        *,
        label: str,
    ) -> None:
        console.print(
            f"[cyan][{label}][/cyan] "
            f"attaching {len(SHARED_VARIABLES)} shared variables"
        )

        self.client.attach_shared_variables(
            service_id=service.id,
            variable_names=SHARED_VARIABLES,
        )

        console.print(
            f"[green][{label}][/green] "
            f"attached {len(SHARED_VARIABLES)} shared variables"
        )
