# Clyra staging deployment handoff

Last updated: 2026-07-16

## Final architecture

- AWS account/region: `020571892795`, `ap-south-1`
- ECS cluster/service: `clodexa-stag-cluster` / `clyra-stag`
- Launch type: Fargate, `awsvpc`, singleton
- Active task definition: `clyra-stag-fargate:9`
- Image digest: `sha256:dc2d3aabd70c146365348b4fb0f66c8b5ca413e4aff0a49e7b95c959f6a63ec7`
- Task size: 2 vCPU, 4 GiB, 40 GiB ephemeral storage
- Persistent filesystem: Amazon S3 Files `fs-0705b9dbf1a2b9c04`
- S3 Files access point: `fsap-0abe6a3275a999533`
- Backing bucket: `clyra-stag-state-56814c98a4d1f5a76581e7c511`
- Palace backend: Chroma, MongoDB, or DocumentDB via `PALACE_BACKEND`
- Staging palace selection: DocumentDB
- Endpoint: `https://clyra-stag.clodexa.com`

The task has an unprivileged `clyra` container and the AppConfig Agent
sidecar. Application code is immutable image content. AWS has no repository
mount, `.git`, `git pull`, or `systemctl` deployment path.

## Storage ownership

S3 Files persists `/data`, config, knowledge, memory, state, jobs, workflows,
and completion markers. `/app/knowledge` now follows the same first-boot seed
and symlink contract as the other mutable directories.

DocumentDB stores the staging memory palace and operational Mongo collections.
Its adapter uses 384-dimensional embeddings, DocumentDB native HNSW when
available, and exact cosine plus BM25 for ordinary MongoDB or unsupported
vector operators. Chroma remains the public/local default.

Local Docker Compose bind-mounts repository-backed mutable directories so host
edits can be reviewed and committed. AWS runtime edits require an explicit
export and review; task replacement never creates a Git commit.

## EFS removal record

On 2026-07-16:

- the healthy live service was confirmed on Fargate/S3 Files with no EFS volume;
- Terraform removed EFS `fs-0df083e55cde1ffe6`, its access point, both mount
  targets, backup policy, security group, task IAM policy, EC2 task definition,
  and EFS migration/baseline/vector-migration task definitions;
- all identifiable EFS-backed ECS revisions were deregistered and submitted
  for deletion;
- account-wide EFS filesystem and access-point inventories were empty afterward.

The automatic recovery point
`efa1dc90-ef40-47b4-82b4-ba12f4759053` remains only because the current IAM
principal received an explicit resource-policy deny for
`backup:DeleteRecoveryPoint` on vault `aws/efs/automatic-backup-vault`.
An administrator must remove that deny or delete the recovery point with an
authorized principal, then confirm the resource recovery-point list is empty.

There is no EC2/EFS rollback. Roll back by deploying a known-good immutable
image digest to the same Fargate/S3 Files service.

## Operator checks

1. Confirm ECS desired/running is `1/1`, rollout is `COMPLETED`, and the ALB
   target is healthy.
2. Confirm `/healthz` and `/readyz` return 200 and unauthenticated Tower routes
   return 401.
3. Confirm S3 Files `ImportFailures`, `ExportFailures`, and
   `LostAndFoundFiles` alarms are `OK`.
4. Create authenticated palace, diary, and KG test data plus a unique file in
   `/app/knowledge`.
5. Force a Fargate task replacement and verify the data remains.
6. Delete the test records and export any runtime-authored files that should
   enter source control.

Terraform lives in `infra/terraform`; the deployment procedure is in
`DEPLOYMENT.md`.

The 2026-07-17 acceptance run created a unique knowledge file, palace drawer,
diary entry, and KG fact. All four survived a forced task replacement; the
service returned to `1/1` healthy with rollout `COMPLETED`. Test artifacts were
then removed or invalidated.

<!-- Superseded pre-removal migration notes retained temporarily for audit only.
Do not follow any instructions below this line.

Last updated: 2026-07-16

## Current deployment

Clyra is healthy at `https://clyra-stag.clodexa.com`.

- AWS account/region: `020571892795`, `ap-south-1`
- ECS service: `clyra-stag`
- Cluster: `clodexa-stag-cluster`
- Launch type: Fargate, `awsvpc`
- Active task definition: `clyra-stag-fargate:7`
- Desired/running tasks: 1/1
- Task size: 2 vCPU, 4 GiB
- Persistent filesystem: Amazon S3 Files mounted at `/mnt/efs`
- Memory backend: DocumentDB (`PALACE_BACKEND=documentdb`)
- ALB target, `/healthz`, and `/readyz`: healthy
- Tower routes require authentication
- Slack Socket Mode: connected
- Scheduler, morning/goodnight cron, reflection, completion watcher, and worker: running
- DocumentDB and Valkey readiness: passing
- Deployment branch: `clyra`
- Main deployment commits: `7865ce6`, `bc15a19`, `205a89e`, `d834b81`

Cloudflare has an unproxied CNAME matching `backend.clodexa.com`:

```text
clyra-stag.clodexa.com -> STAG-ALB-927318609.ap-south-1.elb.amazonaws.com
```

The existing `*.clodexa.com` ACM certificate provides HTTPS. Rotate the
Cloudflare API token that was pasted into chat.

## AWS resources

Existing shared resources:

- VPC: `vpc-02fa0e418991769b2`
- private subnets: `subnet-0c889a24f877e312c`,
  `subnet-05193de37af956cf4`
- ECS cluster and EC2 capacity provider
- `STAG-ALB` and wildcard ACM certificate
- DocumentDB staging cluster, engine 5.0.0, port 27017
- ElastiCache Valkey staging cluster, TLS port 6379
- staging AppConfig application/environment
- GitHub CodeConnection

Clyra-owned resources:

- immutable ECR repository `stag-clyra`
- ECS task/service `clyra-stag`
- ALB target group and host rule
- CloudWatch log group `/ecs/clyra-stag`
- AppConfig profile `clyra-stag-runtime` (`swqwh37`)
- CodePipeline and CodeBuild project `clyra-stag`
- encrypted private S3 pipeline artifact bucket
- scoped IAM roles and security groups
- EFS `fs-0df083e55cde1ffe6`
- EFS access point `fsap-09c76edc017a7f2d6`

Terraform is in `infra/terraform`; image build logic is in `buildspec.yml`.

## Runtime and secrets

The active Fargate task has two containers:

1. `clyra`: application container, UID 1000, all Linux capabilities dropped.
2. `appconfig`: AWS AppConfig Agent on `127.0.0.1:2772`.

The retained EC2 rollback task also has the short-lived privileged
`imds-firewall` init container.

The hosted Clyra AppConfig profile supplies Gemini, DocumentDB, Valkey, Slack,
Tower-auth, and browser-backend values. Do not put their values in this file.
These secrets currently use AppConfig rather than Secrets Manager and should be
reviewed.

## Galadriel OS and filesystem autonomy

Galadriel has real OS access inside its container.

`harness/tools.py` gives the agent:

- `run_shell`: an actual shell subprocess in `/app`, with a 120-second timeout
- `read_file`: arbitrary readable paths visible to UID 1000
- `write_file`: arbitrary writable paths visible to UID 1000, creating parent
  directories when needed

Containment:

- non-root UID 1000
- all Linux capabilities dropped
- no Docker socket
- no host filesystem mounted
- root filesystem is writable but ephemeral
- `/app` is owned by UID 1000, so code can change during a task but disappears
  on replacement
- `.git` is excluded, so automatic commit/push is not operational
- task egress is unrestricted
- task IAM role is limited to filesystem/AppConfig access, not AWS admin
- shell safety blocks freestyle Mongo shell access and classifies risky commands

Persisted paths currently map to the mounted filesystem:

- `/data`
- `/app/config`
- `/app/memory`
- `/app/state`
- `/app/jobs`
- `/app/workflows`
- completion markers

Application source and `/app/knowledge` remain image-layer files and are not
durable across task replacement.

## Data responsibilities

- DocumentDB: structured workflows, run/event telemetry, worker ticks, Tower
  settings, costs, and operational caches.
- MemPalace/ChromaDB: semantic/vector memory, archives, diary, wake-up state.
- Filesystem: config, jobs, plans, progress, markdown knowledge, conversation
  buffers, workflow specs, memory logs, and completion markers.
- Valkey: deployed and pinged by `/readyz`, but no application cache, queue, or
  lock usage was found.
- S3: currently pipeline artifacts only.

## S3 Files findings

Amazon S3 Files is real and generally available. The earlier blanket statement
that S3 could not act as a filesystem was outdated.

Official AWS documentation:

- https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html
- https://aws.amazon.com/blogs/aws/launching-s3-files-making-s3-buckets-accessible-as-file-systems/
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-quotas.html
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-synchronization.html
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-mounting-ecs.html

S3 Files provides NFSv4.1/4.2, POSIX permissions, close-to-open consistency,
advisory file locking, and bidirectional synchronization with a versioned S3
bucket.

### Current hard blocker

Native ECS `s3filesVolumeConfiguration` supports Fargate and ECS Managed
Instances. AWS explicitly states it is not supported on the ECS EC2 launch
type.

Clyra currently runs on the shared EC2 capacity provider. Attaching S3 Files to
this task would make it fail at launch.

Using managed S3 Files therefore requires:

1. moving Clyra to Fargate;
2. moving it to ECS Managed Instances;
3. waiting for ECS EC2 support; or
4. a brittle custom host mount, which is not the managed solution.

### Good S3 Files candidates

With a single writer, these should be evaluated:

- markdown config, jobs, plans, progress, knowledge, and memory logs
- workflow JSON
- conversation archives and completion markers
- user-created normal files

Tradeoffs:

- small file and metadata operations are metered
- updates produce new S3 object versions
- directory renames become copy/delete operations for every object
- bucket-side and filesystem-side concurrent writes can conflict; S3 wins
- hard links, mandatory locks, ACLs, sparse operations, and several NFS
  features are unsupported

Mountpoint for S3 is not equivalent: it cannot modify existing files and does
not support file locking or full POSIX behavior.

### Do not move the embedded vector store unchanged

MemPalace/Chroma should not move to S3 Files without vendor-supported testing:

- embedded database/index files use small random writes and locking
- S3 Files has advisory, not mandatory, locking
- S3 objects lack native atomic rename
- `fsync`, metadata, and small random writes are metered
- bucket/filesystem conflicts treat S3 as authoritative
- singleton execution reduces concurrency but does not prove crash safety

This is not proof it can never work. It means it is unsafe as the first
production migration without destructive database/recovery tests.

## Vector replacement options

### Existing DocumentDB 5.0

The deployed DocumentDB supports native vector search:

- HNSW and IVFFlat indexes
- cosine, dot-product, and Euclidean similarity
- exact and approximate nearest-neighbor queries
- up to 2,000 indexed dimensions

Official docs:

- https://docs.aws.amazon.com/documentdb/latest/devguide/vector-search.html
- https://docs.aws.amazon.com/documentdb/latest/devguide/vectorSearch.html

This is the strongest first candidate because it already exists. It is not a
drop-in MemPalace replacement: MemPalace also supplies mining, chunking,
wing/room/hall metadata, diary behavior, archive routing, wake-up snapshots,
and hybrid retrieval. An adapter and migration are required, and the current
embedding dimension must be confirmed.

### S3 Vectors

S3 Vectors is another managed candidate:

- strongly consistent vector writes
- metadata filtering
- dimensions up to 4,096
- up to 2 billion vectors per index
- approximate nearest-neighbor queries
- roughly 100 ms for frequent queries and sub-second for infrequent queries

Docs:

- https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-query.html

It also needs a MemPalace adapter and is not equivalent to a local embedded
store.

## Recommended migration sequence

Do not delete EFS first.

1. Inventory all writes and actual storage growth.
2. Confirm MemPalace embedding dimensions and retrieval semantics.
3. Prototype a DocumentDB vector backend.
4. Test recall, archive migration, diary, wake-up, and crash recovery.
5. Decide between Fargate and ECS Managed Instances for native S3 Files.
6. Mount S3 Files for ordinary mutable files.
7. Force task replacements and verify persistence and vector recall.
8. Delete EFS only after rollback-tested migration succeeds.

## Fargate/S3 Files migration handoff

### Completed on 2026-07-16

- Captured the read-only EFS baseline: 65 files, 10 directories, 245,442
  logical bytes, manifest SHA-256
  `286d1aa75fe1f7ed112a1e10ea609608e3cd681f4237a0ef0a1bea7f04cd236b`.
- Retained encrypted, backed-up EFS `fs-0df083e55cde1ffe6` and access point
  `fsap-09c76edc017a7f2d6`. Both have Terraform `prevent_destroy` protection.
- Provisioned versioned, encrypted S3 bucket
  `clyra-stag-state-56814c98a4d1f5a76581e7c511`.
- Provisioned S3 Files filesystem `fs-0705b9dbf1a2b9c04`, access point
  `fsap-0abe6a3275a999533`, and one mount target per private subnet.
- Copied the quiesced EFS tree to S3 Files and compared portable manifests.
- Ran isolated filesystem CRUD, rename, symlink, SQLite crash recovery, Chroma
  recall, and forced-crash tests.
- Chroma failed the S3 Files correctness gate (`errno 524`), so the service now
  uses the implemented DocumentDB palace backend. Staging had no live Chroma
  drawers to migrate; the existing knowledge graph migration path was retained.
- Cut the service over to Fargate with S3 Files at `/mnt/efs`.
- Forced a Fargate task replacement. The replacement reached 1/1 running,
  deployment rollout `COMPLETED`, healthy ALB target, and healthy `/healthz`
  and `/readyz`.
- Verified the S3 Files import-failure, export-failure, and lost-and-found
  alarms are all `OK` after replacement.
- Verified the final Terraform configuration and compiled all changed Python
  modules.

`activate_s3_files` still defaults to `false` as a safety control, but the
currently applied staging value is `true`.

### Remaining handoff plan

1. **Observe the cutover.** Keep the service on Fargate for an agreed observation
   window. Check ECS stability, ALB health, application logs, S3 Files alarms,
   scheduler/worker activity, Slack connectivity, and DocumentDB/Valkey
   readiness at least daily.
2. **Run an authenticated palace proof.** With the current Tower credential,
   create a uniquely named test drawer, verify semantic recall and diary/KG
   operations, force another task replacement, verify recall again, then remove
   the test data. The local `.env` credential returned 401 and was not used to
   mutate staging.
3. **Exercise rollback.** In a maintenance window, apply
   `activate_s3_files=false`, prove the retained EC2/EFS service becomes healthy,
   then reapply `activate_s3_files=true` and prove Fargate/S3 Files becomes
   healthy. This is the outstanding destructive circuit-breaker/restore drill.
4. **Capture operating cost.** Record S3 Files request/metadata metrics, Fargate
   cost, S3 version growth, and DocumentDB vector workload after the observation
   window. The migrated tree is too small for storage bytes alone to be a useful
   estimate.
5. **Review release hygiene.** Review the approximately 6.5 GB image and the
   latest observed ECR findings (4 critical, 8 high, 3 medium), then commit and
   push the migration changes through the normal review path.
6. **Request explicit EFS deletion approval.** Only after steps 1-5 pass should
   an owner approve a separate Terraform change that removes `prevent_destroy`
   and deletes EFS. Until then, do not alter or delete EFS.

### Immediate rollback

Apply Terraform with `activate_s3_files=false`. This switches the service back
to the retained EC2 task family and EFS. Do not copy post-cutover S3 Files state
back to EFS without first quiescing the service and reviewing the delta.

## Other known issues

- BCE has no endpoint yet.
- No automatic repository commit/push exists in the container.
- Native S3 Files cannot mount on the retained ECS EC2 rollback launch type.

## New-chat prompt

```text
Continue the post-cutover Clyra staging handoff using
@CLYRA_DEPLOYMENT_HANDOFF.md as the source of truth.

The service is healthy on Fargate task definition clyra-stag-fargate:7 with
Amazon S3 Files at /mnt/efs and DocumentDB as the palace backend. EFS is retained
and protected for rollback. Do not delete it.

Complete the Remaining handoff plan in order. Record evidence for the
observation window, authenticated palace persistence proof, and EC2/EFS rollback
drill. EFS deletion requires separate explicit owner approval.
```
-->
