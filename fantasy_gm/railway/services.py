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

    def log_services(self) -> list[RailwayService]:
        services = self.client.list_services()

        console.print(
            "[bold cyan]Controlled Railway services:[/bold cyan]"
        )

        for service in services:
            console.print(
                f"  {service.name:<30} "
                f"{service.deployment_status or 'NO DEPLOYMENT'}"
            )

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

        return service

    def launch_test_worker(self) -> RailwayService:
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

        console.print(
            f"[cyan][WORKER][/cyan] "
            f"created {name} id={service.id}"
        )

        self.client.configure_service(
            service_id=service.id,
            start_command=(
                "fantasy-gm worker test "
                f"--job-id {job_id} "
                "--sleep 600"
            ),
            restart_policy="NEVER",
        )

        console.print(
            f"[cyan][WORKER][/cyan] "
            f"configured {name}: restart=NEVER runtime=600s"
        )

        deployment_id = self.client.deploy_service(service.id)

        console.print(
            f"[green][WORKER][/green] "
            f"deployment requested for {name} "
            f"id={deployment_id}"
        )

        return service

    def wait_and_delete(
        self,
        service: RailwayService,
        *,
        poll_seconds: int = 15,
    ) -> None:
        console.print(
            f"[cyan][WORKER][/cyan] "
            f"monitoring {service.name}"
        )

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

            if status in {
                "SUCCESS",
                "FAILED",
                "CRASHED",
                "REMOVED",
            }:
                break

            time.sleep(poll_seconds)

        console.print(
            f"[yellow][WORKER][/yellow] "
            f"{service.name} finished with status={status}; "
            "deleting service"
        )

        self.client.delete_service(service.id)

        console.print(
            f"[green][WORKER][/green] "
            f"{service.name} deleted"
        )

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
