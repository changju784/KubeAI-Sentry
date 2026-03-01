"""
deployer.py – K8s Deployment CRUD via kubernetes-client.
Reads WorkloadRecipe YAMLs and manages Deployments in tenant namespaces.
"""
import yaml
from pathlib import Path
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

# Image registry prefix used when loading images into minikube
IMAGE_PREFIX = "kubeai-sentry"

# Map workload_type → docker directory name / image name
WORKLOAD_IMAGE_MAP = {
    "inference": "mock-inference",
    "training": "mock-training",
    "data-cleansing": "mock-data-cleansing",
    "data_cleansing": "mock-data-cleansing",
}

REQUIRED_RECIPE_FIELDS = ["metadata", "spec"]
REQUIRED_SPEC_FIELDS = ["workload_type", "tenant", "replicas", "resources"]


def _load_k8s_config():
    """Load kubeconfig (in-cluster or local)."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def load_recipe(path: str) -> dict:
    """Parse a WorkloadRecipe YAML and validate required fields."""
    recipe_path = Path(path)
    if not recipe_path.exists():
        raise FileNotFoundError(f"Recipe file not found: {path}")

    with open(recipe_path, "r") as f:
        recipe = yaml.safe_load(f)

    for field in REQUIRED_RECIPE_FIELDS:
        if field not in recipe:
            raise ValueError(f"Recipe missing required field: '{field}'")

    spec = recipe.get("spec", {})
    for field in REQUIRED_SPEC_FIELDS:
        if field not in spec:
            raise ValueError(f"Recipe spec missing required field: '{field}'")

    return recipe


def build_deployment(recipe: dict, replicas_override: Optional[int] = None) -> client.V1Deployment:
    """Construct a V1Deployment from a WorkloadRecipe dict."""
    meta = recipe["metadata"]
    spec = recipe["spec"]

    name = meta["name"]
    workload_type = spec["workload_type"]
    tenant = spec["tenant"]
    replicas = replicas_override if replicas_override is not None else spec.get("replicas", 1)
    priority_class = spec.get("priority_class", "training-low")
    resources_spec = spec.get("resources", {})
    env_vars = spec.get("env", {})

    image_name = WORKLOAD_IMAGE_MAP.get(workload_type, f"mock-{workload_type}")
    image = f"{IMAGE_PREFIX}/{image_name}:latest"

    # Build container env list
    env_list = [
        client.V1EnvVar(name=k, value=str(v))
        for k, v in env_vars.items()
    ]

    # Build resource requirements
    requests = resources_spec.get("requests", {})
    limits = resources_spec.get("limits", {})
    resource_requirements = client.V1ResourceRequirements(
        requests=requests if requests else None,
        limits=limits if limits else None,
    )

    container = client.V1Container(
        name=name,
        image=image,
        image_pull_policy="Never",  # images loaded via minikube image load
        resources=resource_requirements,
        env=env_list if env_list else None,
    )

    pod_spec = client.V1PodSpec(
        containers=[container],
        priority_class_name=priority_class,
        restart_policy="Always",
    )

    pod_template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(
            labels={
                "app": name,
                "kubeai-sentry.io/workload-type": workload_type,
                "kubeai-sentry.io/tenant": tenant.replace("tenant-", ""),
            }
        ),
        spec=pod_spec,
    )

    deployment_spec = client.V1DeploymentSpec(
        replicas=replicas,
        selector=client.V1LabelSelector(
            match_labels={"app": name}
        ),
        template=pod_template,
    )

    deployment = client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=tenant,
            labels={
                "kubeai-sentry.io/workload-type": workload_type,
                "kubeai-sentry.io/tenant": tenant.replace("tenant-", ""),
            },
        ),
        spec=deployment_spec,
    )

    return deployment


def deploy(recipe_path: str, replicas_override: Optional[int] = None) -> dict:
    """Create or replace a Deployment from a recipe YAML. Returns status dict."""
    _load_k8s_config()
    recipe = load_recipe(recipe_path)
    deployment = build_deployment(recipe, replicas_override)

    namespace = recipe["spec"]["tenant"]
    name = recipe["metadata"]["name"]
    apps_v1 = client.AppsV1Api()

    try:
        existing = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
        # Update existing deployment
        result = apps_v1.replace_namespaced_deployment(
            name=name, namespace=namespace, body=deployment
        )
        return {
            "action": "updated",
            "name": name,
            "namespace": namespace,
            "replicas": result.spec.replicas,
        }
    except ApiException as e:
        if e.status == 404:
            result = apps_v1.create_namespaced_deployment(
                namespace=namespace, body=deployment
            )
            return {
                "action": "created",
                "name": name,
                "namespace": namespace,
                "replicas": result.spec.replicas,
            }
        raise


def delete(name: str, namespace: str) -> dict:
    """Delete a Deployment by name and namespace."""
    _load_k8s_config()
    apps_v1 = client.AppsV1Api()
    try:
        apps_v1.delete_namespaced_deployment(
            name=name,
            namespace=namespace,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        )
        return {"action": "deleted", "name": name, "namespace": namespace}
    except ApiException as e:
        if e.status == 404:
            return {"action": "not_found", "name": name, "namespace": namespace}
        raise


def list_workloads(namespace: Optional[str] = None) -> list[dict]:
    """List all Deployments in one or all namespaces. Returns list of status dicts."""
    _load_k8s_config()
    apps_v1 = client.AppsV1Api()
    core_v1 = client.CoreV1Api()

    namespaces_to_query = []
    if namespace in (None, "all"):
        namespaces_to_query = ["tenant-alpha", "tenant-beta"]
    else:
        namespaces_to_query = [namespace]

    results = []
    for ns in namespaces_to_query:
        try:
            deployments = apps_v1.list_namespaced_deployment(namespace=ns)
        except ApiException as e:
            if e.status == 404:
                continue
            raise

        for dep in deployments.items:
            # Count ready pods
            ready = dep.status.ready_replicas or 0
            desired = dep.spec.replicas or 0
            available = dep.status.available_replicas or 0

            # Determine overall status
            if ready == desired and desired > 0:
                status = "Running"
            elif ready == 0:
                status = "Pending"
            else:
                status = f"Partial ({ready}/{desired})"

            results.append({
                "name": dep.metadata.name,
                "namespace": dep.metadata.namespace,
                "replicas": desired,
                "ready": ready,
                "available": available,
                "status": status,
                "workload_type": dep.metadata.labels.get("kubeai-sentry.io/workload-type", "unknown"),
                "priority_class": dep.spec.template.spec.priority_class_name or "default",
            })

    return results


def purge(namespace: str) -> list[dict]:
    """Delete all Deployments in a namespace."""
    _load_k8s_config()
    apps_v1 = client.AppsV1Api()

    try:
        deployments = apps_v1.list_namespaced_deployment(namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return []
        raise

    results = []
    for dep in deployments.items:
        result = delete(dep.metadata.name, namespace)
        results.append(result)

    return results
