# Clyra staging ECS runbook

This service is a singleton `EC2`-launch-type ECS task on
`clodexa-stag-cluster`. It uses the existing staging VPC, private subnets,
capacity provider, and `STAG-ALB`; Terraform creates only Clyra-owned
resources in `infra/terraform`.

## Prerequisites

1. The task reads runtime settings from the existing staging AppConfig profile
   through the AppConfig Agent sidecar. Populate that profile with
   `GEMINI_API_KEY`, `MONGO_URI`, `REDIS_URL`, `TOWER_AUTH_TOKEN`,
   `BCE_API_KEY`, and the selected chat-gateway token. Do not put values in
   Terraform, `tfvars`, Docker build arguments, or source control.
2. Use the deployed DocumentDB endpoint in `MONGO_URI`, with
   `tls=true`, `replicaSet=rs0`, `retryWrites=false`, and
   `tlsCAFile=/etc/ssl/certs/rds-global-bundle.pem`. Use the deployed
   ElastiCache Valkey TLS endpoint in `REDIS_URL` (`rediss://...`).
   Neither service is created by Docker Compose or this stack.
3. Copy `infra/terraform/staging.tfvars.example` to a secure local file and
   supply the shared-infrastructure IDs, unused ALB listener-rule priority,
   CodeConnection ARN, and any Secrets Manager ARNs used in addition to
   AppConfig.

## Deploy

```sh
cd infra/terraform
terraform init
terraform apply -var-file=/secure/path/staging.tfvars
```

The first apply registers the `bootstrap` image reference so the service may
briefly fail to start until the first CodePipeline execution finishes. Trigger
the `clyra-stag` pipeline (or push to the `clyra` branch), then confirm the
deployment circuit breaker reaches `COMPLETED`.

Create the external DNS CNAME `clyra-stag.clodexa.com` to the existing staging
ALB. DNS is external to this AWS account. The ALB's existing wildcard
certificate terminates TLS; the host-header rule forwards only to port 8080.

## Verification

- Confirm ECR scan results and an amd64 image; inspect that `.env` is absent.
- Confirm ECS runs as UID 1000, `/healthz` returns 200 without credentials,
  `/readyz` returns 200 only after Valkey and DocumentDB respond, and all other
  Tower endpoints return 401 without Tower authentication.
- Force-stop the task, then confirm palace, config, state, jobs, workflows,
  memory, and completion markers remain on the EFS access point.
- From ECS Exec, verify `curl --connect-timeout 2 169.254.169.254` fails and
  the task role has only EFS client permissions. The privileged firewall init
  container is the sole exception.
- Verify BCE, DocumentDB, Valkey, the configured Discord or Slack gateway,
  scheduler, worker, and CloudWatch logs. Perform a deliberately invalid-image
  deployment to confirm the circuit breaker rolls back; singleton rolling
  settings intentionally allow a brief outage rather than two agent brains.

`jobs/daily_state_commit.md` currently makes remote pushes explicitly optional,
and the image excludes `.git`; therefore no automatic self-push can be
validated from this ECS image. This is intentional until a separate,
explicitly approved Git credential and remote-push policy is introduced.

## Accepted host-isolation risk

The task-level IMDS firewall prevents the application container from reaching
IMDS, but the staging EC2 hosts remain shared and their instance role is
over-privileged. This reduces exposure; it does not eliminate host or kernel
escape risk. A dedicated hardened capacity provider is required to remove that
residual risk.
