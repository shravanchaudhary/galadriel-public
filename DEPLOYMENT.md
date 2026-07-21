# Replika provider ECS runbook

The customer experience is a managed product: customers authenticate, choose
an available username, and receive `https://<username>.<product-domain>`.
AWS resource names, regions, task state, and credentials are provider-only.
The control plane provisions one isolated Fargate service and S3 Files access
point per Replika. Application code comes only from immutable ECR images.

## Prerequisites

1. Store non-secret control-plane settings in AppConfig. Put `MONGO_URI`,
   `REDIS_URL`, and a strong shared `TOWER_SECRET_KEY` in Secrets Manager and
   map their ARNs through `secret_arns`; isolated runtimes do not receive the
   control plane's AppConfig profile. Tower serves a public `/login` form that validates
   `TOWER_AUTH_USERNAME` / `TOWER_AUTH_TOKEN` and issues a signed session
   cookie; protected HTML redirects to `/login`, APIs return 401, and
   `/healthz` + `/readyz` remain public.
2. For AWS DocumentDB, set `PALACE_BACKEND=documentdb` and include
   `tls=true`, `replicaSet=rs0`, `retryWrites=false`, and
   `tlsCAFile=/etc/ssl/certs/rds-global-bundle.pem` in `MONGO_URI`.
   `PALACE_BACKEND=mongo` uses the same adapter with exact cosine plus BM25
   when native vector search is unavailable. `chroma` remains the default.
3. Store the provisioning callback token in Secrets Manager and pass only its
   ARN as `replika_callback_token_secret_arn`. Customer provider keys are
   encrypted with KMS through the product UI; they do not belong in AppConfig.
4. Copy `infra/terraform/staging.tfvars.example` to a secure location and fill
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
step discovers services tagged `ReplikaManaged=true`, clones each current task
definition, changes only the application image, and waits for stability. A
failed tenant rollout is returned to its previous task definition and tagged
`ReplikaRollout=rolled-back`; other tenant rollouts continue.

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
- `/app/personal-tools`
- `/mnt/efs/completion-markers`

The entrypoint runs versioned, idempotent state migrations and seeds image
defaults only when a target file is absent. The container root filesystem is
read-only. Agent writes are restricted to persistent Replika paths; `main.py`
and `harness/` remain developer-owned.

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
- Verify unauthenticated HTML GETs enter provider-managed authentication,
  cross-tenant identities are rejected, unauthenticated API calls return 401
  (no `WWW-Authenticate` browser prompt), and `/login`,
  `/healthz`, and `/readyz` remain reachable without credentials.
- Create a unique palace drawer and knowledge file, force-stop the running
  task, wait for its replacement, then verify both remain.
- Verify S3 Files import/export/lost-and-found alarms remain `OK`.
- Verify the task runs as UID 1000 with all Linux capabilities dropped.

Rollback means redeploying a previously known-good immutable ECR digest while
leaving each tenant access point, Markdown files, personal tools, vectors, and
encrypted provider credentials in place.
