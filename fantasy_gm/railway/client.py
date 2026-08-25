# fantasy_gm/railway/client.py

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests


RAILWAY_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"


@dataclass
class RailwayService:
    id: str
    name: str
    deployment_id: str | None = None
    deployment_status: str | None = None


class RailwayClient:
    def __init__(self) -> None:
        self.token = os.environ["RAILWAY_API_TOKEN"]
        self.project_id = os.environ["RAILWAY_PROJECT_ID"]
        self.environment_id = os.environ["RAILWAY_ENVIRONMENT_ID"]

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Project-Access-Token": self.token,
                "Content-Type": "application/json",
            }
        )

    def _graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.session.post(
            RAILWAY_GRAPHQL_URL,
            json={
                "query": query,
                "variables": variables or {},
            },
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json()

        if payload.get("errors"):
            raise RuntimeError(
                f"Railway GraphQL error: {payload['errors']}"
            )

        return payload["data"]

    def list_services(self) -> list[RailwayService]:
        query = """
        query ProjectServices($id: String!) {
          project(id: $id) {
            services {
              edges {
                node {
                  id
                  name
                  serviceInstances {
                    edges {
                      node {
                        environmentId
                        latestDeployment {
                          id
                          status
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """

        data = self._graphql(
            query,
            {"id": self.project_id},
        )

        services: list[RailwayService] = []

        for edge in data["project"]["services"]["edges"]:
            node = edge["node"]

            deployment_id = None
            deployment_status = None

            for instance_edge in node["serviceInstances"]["edges"]:
                instance = instance_edge["node"]

                if instance["environmentId"] != self.environment_id:
                    continue

                deployment = instance.get("latestDeployment")

                if deployment:
                    deployment_id = deployment["id"]
                    deployment_status = deployment["status"]

            services.append(
                RailwayService(
                    id=node["id"],
                    name=node["name"],
                    deployment_id=deployment_id,
                    deployment_status=deployment_status,
                )
            )

        return services

    def find_service(self, name: str) -> RailwayService | None:
        for service in self.list_services():
            if service.name == name:
                return service

        return None

    def create_service(
        self,
        *,
        name: str,
        repo: str,
    ) -> RailwayService:
        mutation = """
        mutation ServiceCreate($input: ServiceCreateInput!) {
          serviceCreate(input: $input) {
            id
            name
          }
        }
        """

        data = self._graphql(
            mutation,
            {
                "input": {
                    "projectId": self.project_id,
                    "name": name,
                    "source": {
                        "repo": repo,
                    },
                }
            },
        )

        node = data["serviceCreate"]

        return RailwayService(
            id=node["id"],
            name=node["name"],
        )

    def configure_service(
        self,
        *,
        service_id: str,
        start_command: str,
        restart_policy: str,
    ) -> None:
        mutation = """
        mutation ServiceInstanceUpdate(
          $serviceId: String!,
          $environmentId: String!,
          $input: ServiceInstanceUpdateInput!
        ) {
          serviceInstanceUpdate(
            serviceId: $serviceId,
            environmentId: $environmentId,
            input: $input
          )
        }
        """

        self._graphql(
            mutation,
            {
                "serviceId": service_id,
                "environmentId": self.environment_id,
                "input": {
                    "startCommand": start_command,
                    "restartPolicyType": restart_policy,
                },
            },
        )

    def deploy_service(
        self,
        service_id: str,
    ) -> str:
        mutation = """
        mutation ServiceDeploy(
          $serviceId: String!,
          $environmentId: String!
        ) {
          serviceInstanceDeployV2(
            serviceId: $serviceId,
            environmentId: $environmentId
          )
        }
        """

        data = self._graphql(
            mutation,
            {
                "serviceId": service_id,
                "environmentId": self.environment_id,
            },
        )

        return data["serviceInstanceDeployV2"]

    def delete_service(self, service_id: str) -> None:
        mutation = """
        mutation ServiceDelete($id: String!) {
          serviceDelete(id: $id)
        }
        """

        self._graphql(
            mutation,
            {"id": service_id},
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
