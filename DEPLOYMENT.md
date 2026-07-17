# Clyra staging ECS runbook

Clyra runs as a singleton Fargate service on `clodexa-stag-cluster`. Amazon S3
Files is mounted at `/mnt/efs`; there is no EFS or ECS EC2 rollback path.
Application code comes only from the immutable ECR image.

## Prerequisites

1. Store runtime settings in the staging AppConfig profile. It supplies
   `GEMINI_API_KEY`, `MONGO_URI`, `MONGO_DB`, `REDIS_URL`,
   `TOWER_AUTH_TOKEN`, a strong `TOWER_SECRET_KEY` (session signing; must not
   be the default `change-me`), browser credentials, and the chat-gateway
   token. Tower serves a public `/login` form that validates
   `TOWER_AUTH_USERNAME` / `TOWER_AUTH_TOKEN` and issues a signed session
   cookie; protected HTML redirects to `/login`, APIs return 401, and
   `/healthz` + `/readyz` remain public.
2. For AWS DocumentDB, set `PALACE_BACKEND=documentdb` and include
   `tls=true`, `replicaSet=rs0`, `retryWrites=false`, and
   `tlsCAFile=/etc/ssl/certs/rds-global-bundle.pem` in `MONGO_URI`.
   `PALACE_BACKEND=mongo` uses the same adapter with exact cosine plus BM25
   when native vector search is unavailable. `chroma` remains the default.
3. Copy `infra/terraform/staging.tfvars.example` to a secure location and fill
   in shared VPC, subnet, ALB, database/cache security-group, listener-priority,
   and CodeConnection values. Never commit the populated file.

## Infrastructure and deployment

```sh
cd infra/terraform
terraform init
terraform fmt -check
terraform validate
terraform plan -var-file=/secure/path/staging.tfvars
terraform apply -var-file=/secure/path/staging.tfvars
```

CodePipeline builds immutable amd64 images from the `clyra` branch. Its deploy
CodeBuild project clones the service's current task definition, changes only
the `clyra` container image, registers the complete definition, updates the
service, and waits for stability. This preserves the native S3 Files volume;
do not replace it with CodePipeline's standard ECS deploy action, which drops
unsupported task-definition fields. The deploy fails before updating the
service unless S3 Files is present in both the source and registered task
definitions. It also retries the singleton scheduler once if ECS drains the
old task without starting its replacement.

ECS task replacement, not `git pull`, deploys code. The runtime image has no
`.git` directory and does not run `systemctl`. `candidate_image_uri` is only
for initial task registration and the isolated storage canary; Terraform
ignores live container-definition drift so an infrastructure apply cannot
roll back CodePipeline's image.

## Persistence contract

The S3 Files mount persists:

- `/data`
- `/app/config`
- `/app/knowledge`
- `/app/memory`
- `/app/state`
- `/app/jobs`
- `/app/workflows`
- `/mnt/efs/completion-markers`

The entrypoint seeds image defaults only when a target file is absent. Runtime
changes in AWS are durable across task replacement but are not Git commits.
Export and review them explicitly before adding them to the repository.

Local Compose bind-mounts the six repository-backed mutable `/app` directories
so edits are visible to Git on the host. `/data` remains a named volume.
To run a local MongoDB palace:

```sh
docker compose --profile mongo up -d mongo
# Set PALACE_BACKEND=mongo, MONGO_URI=mongodb://mongo:27017, MONGO_DB=galadriel
docker compose up -d --build galadriel
```

For private staging database access, set the environment variables documented
by `scripts/stag_tunnel.sh` and run that script. It contains no hosts, keys, or
credentials.

## Verification and rollback

- Require ECS deployment rollout `COMPLETED`, desired/running `1/1`, healthy
  ALB target, `/healthz` 200, and `/readyz` 200.
- Verify unauthenticated HTML GETs redirect to `/login`, unauthenticated API
  calls return 401 (no `WWW-Authenticate` browser prompt), and `/login`,
  `/healthz`, and `/readyz` remain reachable without credentials.
- Create a unique palace drawer and knowledge file, force-stop the running
  task, wait for its replacement, then verify both remain.
- Verify S3 Files import/export/lost-and-found alarms remain `OK`.
- Verify the task runs as UID 1000 with all Linux capabilities dropped.

Rollback means redeploying a previously known-good immutable ECR digest to the
same Fargate/S3 Files service. EFS and EC2 task definitions no longer exist.
