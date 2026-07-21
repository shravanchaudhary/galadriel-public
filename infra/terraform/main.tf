data "aws_caller_identity" "current" {}
data "aws_ecs_cluster" "staging" { cluster_name = var.ecs_cluster_name }
data "aws_lb" "staging" { name = var.alb_name }
data "aws_lb_listener" "https" {
  load_balancer_arn = data.aws_lb.staging.arn
  port              = 443
}

locals {
  name                = var.service_name
  image_uri           = "${aws_ecr_repository.clyra.repository_url}:bootstrap"
  candidate_image_uri = coalesce(var.candidate_image_uri, local.image_uri)
  provisioner_enabled = (
    var.replika_provisioner_function_arn != ""
    || var.replika_callback_token_secret_arn != ""
  )
  provisioner_function_arn = var.replika_provisioner_function_arn != "" ? var.replika_provisioner_function_arn : (
    var.replika_callback_token_secret_arn != "" ? aws_lambda_function.replika_provisioner[0].arn : ""
  )
  runtime_secrets = merge(
    var.secret_arns,
    var.replika_callback_token_secret_arn == "" ? {} : {
      REPLIKA_PROVISIONER_CALLBACK_TOKEN = var.replika_callback_token_secret_arn
    }
  )
  secret_list = [for name, arn in local.runtime_secrets : { name = name, valueFrom = arn }]
  environment_list = [
    for name, value in merge(var.environment, {
      APPCONFIG_APPLICATION            = var.appconfig_application_id
      APPCONFIG_ENVIRONMENT            = var.appconfig_environment_id
      APPCONFIG_CONFIGURATION          = var.appconfig_configuration_id
      PALACE_BACKEND                   = var.palace_backend
      REPLIKA_KMS_KEY_ID               = aws_kms_key.byom.arn
      REPLIKA_CONTROL_PLANE_URL        = "https://${var.host_name}"
      REPLIKA_CONTROL_PLANE_ONLY       = tostring(var.replika_control_plane_only)
      REPLIKA_COOKIE_DOMAIN            = ".${var.replika_product_domain}"
      REPLIKA_PRODUCT_DOMAIN           = var.replika_product_domain
      REPLIKA_PROVISIONER_FUNCTION_ARN = local.provisioner_function_arn
      REPLIKA_PROVISIONING_MODE        = local.provisioner_function_arn == "" ? "local" : "managed"
      REPLIKA_TENANT_ID                = var.replika_tenant_id
      REPLIKA_TRUST_ALB_IDENTITY       = tostring(var.enable_replika_managed_auth)
      TMPDIR                           = "/dev/shm"
    }) : { name = name, value = value }
  ]
}

resource "aws_ecr_repository" "clyra" {
  name                 = "stag-clyra"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_ecr_lifecycle_policy" "clyra" {
  repository = aws_ecr_repository.clyra.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain the newest 30 immutable deployment images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "clyra" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_kms_key" "byom" {
  description             = "Encrypt tenant BYOM credentials"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "byom" {
  name          = "alias/${local.name}-byom"
  target_key_id = aws_kms_key.byom.key_id
}

resource "aws_security_group" "task" {
  name_prefix = "${local.name}-task-"
  vpc_id      = var.vpc_id
  ingress {
    description     = "Tower only from staging ALB"
    protocol        = "tcp"
    from_port       = 8080
    to_port         = 8080
    security_groups = [var.alb_security_group_id]
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "documentdb" {
  count                        = var.documentdb_security_group_id == null ? 0 : 1
  security_group_id            = var.documentdb_security_group_id
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 27017
  to_port                      = 27017
  ip_protocol                  = "tcp"
  description                  = "Clyra ECS task access"
}

resource "aws_vpc_security_group_ingress_rule" "valkey" {
  count                        = var.valkey_security_group_id == null ? 0 : 1
  security_group_id            = var.valkey_security_group_id
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
  description                  = "Clyra ECS task access"
}

resource "aws_s3_bucket" "state" {
  bucket_prefix = "${local.name}-state-"
  force_destroy = false
  tags          = { Name = "${local.name}-state" }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "retain-rollback-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.s3_state_noncurrent_version_expiration_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.state]
}

data "aws_iam_policy_document" "s3files_assume" {
  statement {
    sid     = "AllowS3FilesAssumeRole"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["elasticfilesystem.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:s3files:${var.aws_region}:${data.aws_caller_identity.current.account_id}:file-system/*"]
    }
  }
}

resource "aws_iam_role" "s3files" {
  name               = "${local.name}-s3files"
  assume_role_policy = data.aws_iam_policy_document.s3files_assume.json
}

data "aws_iam_policy_document" "s3files" {
  statement {
    sid       = "S3BucketPermissions"
    actions   = ["s3:ListBucket", "s3:ListBucketVersions"]
    resources = [aws_s3_bucket.state.arn]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
  statement {
    sid = "S3ObjectPermissions"
    actions = [
      "s3:AbortMultipartUpload", "s3:DeleteObject*", "s3:GetObject*",
      "s3:List*", "s3:PutObject*",
    ]
    resources = ["${aws_s3_bucket.state.arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
  statement {
    sid = "EventBridgeManage"
    actions = [
      "events:DeleteRule", "events:DisableRule", "events:EnableRule",
      "events:PutRule", "events:PutTargets", "events:RemoveTargets",
    ]
    resources = ["arn:aws:events:*:*:rule/DO-NOT-DELETE-S3-Files*"]
    condition {
      test     = "StringEquals"
      variable = "events:ManagedBy"
      values   = ["elasticfilesystem.amazonaws.com"]
    }
  }
  statement {
    sid       = "EventBridgeRead"
    actions   = ["events:DescribeRule", "events:ListRuleNamesByTarget", "events:ListRules", "events:ListTargetsByRule"]
    resources = ["arn:aws:events:*:*:rule/*"]
  }
}

resource "aws_iam_role_policy" "s3files" {
  name   = "bucket-sync"
  role   = aws_iam_role.s3files.id
  policy = data.aws_iam_policy_document.s3files.json
}

resource "aws_s3files_file_system" "clyra" {
  bucket   = aws_s3_bucket.state.arn
  role_arn = aws_iam_role.s3files.arn
  tags     = { Name = "${local.name}-state" }
  depends_on = [
    aws_iam_role_policy.s3files,
    aws_s3_bucket_public_access_block.state,
    aws_s3_bucket_server_side_encryption_configuration.state,
    aws_s3_bucket_versioning.state,
  ]
}

resource "aws_security_group" "s3files" {
  name_prefix = "${local.name}-s3files-"
  vpc_id      = var.vpc_id
  ingress {
    description     = "NFS only from Clyra tasks"
    protocol        = "tcp"
    from_port       = 2049
    to_port         = 2049
    security_groups = [aws_security_group.task.id]
  }
  lifecycle { create_before_destroy = true }
}

resource "aws_s3files_mount_target" "clyra" {
  for_each        = var.private_subnet_ids
  file_system_id  = aws_s3files_file_system.clyra.id
  subnet_id       = each.value
  security_groups = [aws_security_group.s3files.id]
}

resource "aws_s3files_access_point" "clyra" {
  file_system_id = aws_s3files_file_system.clyra.id
  posix_user {
    uid = 1000
    gid = 1000
  }
  root_directory {
    path = "/clyra"
    creation_permissions {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0750"
    }
  }
}

resource "aws_s3files_file_system_policy" "clyra" {
  file_system_id = aws_s3files_file_system.clyra.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowProviderManagedTenantRoles"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = ["s3files:ClientMount", "s3files:ClientWrite"]
        Resource  = aws_s3files_file_system.clyra.arn
        Condition = {
          ArnLike = {
            "aws:PrincipalArn" = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/replika-*"
          }
        }
      },
      {
        Sid       = "AllowClyraOnlyViaAccessPoint"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.task.arn }
        Action    = ["s3files:ClientMount", "s3files:ClientWrite"]
        Resource  = aws_s3files_file_system.clyra.arn
        Condition = {
          StringEquals = { "s3files:AccessPointArn" = aws_s3files_access_point.clyra.arn }
        }
      },
      {
        Sid       = "DenyClyraViaAnyOtherAccessPoint"
        Effect    = "Deny"
        Principal = { AWS = aws_iam_role.task.arn }
        Action    = "s3files:Client*"
        Resource  = aws_s3files_file_system.clyra.arn
        Condition = {
          StringNotEquals = { "s3files:AccessPointArn" = aws_s3files_access_point.clyra.arn }
        }
      },
    ]
  })
}

data "aws_iam_policy_document" "execution_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.execution_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = values(local.runtime_secrets)
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  count  = length(local.runtime_secrets) > 0 ? 1 : 0
  name   = "runtime-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.execution_assume.json
}

data "aws_iam_policy_document" "task_s3files" {
  statement {
    actions   = ["s3files:ClientMount", "s3files:ClientWrite"]
    resources = [aws_s3files_file_system.clyra.arn]
    condition {
      test     = "StringEquals"
      variable = "s3files:AccessPointArn"
      values   = [aws_s3files_access_point.clyra.arn]
    }
  }
  statement {
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.state.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
}

resource "aws_iam_role_policy" "task_s3files" {
  name   = "s3files-state"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_s3files.json
}

data "aws_iam_policy_document" "task_appconfig" {
  statement {
    actions = ["appconfig:StartConfigurationSession"]
    resources = [
      "arn:aws:appconfig:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application/${var.appconfig_application_id}/environment/${var.appconfig_environment_id}/configuration/${var.appconfig_configuration_id}",
    ]
  }
  statement {
    actions   = ["appconfig:GetLatestConfiguration"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task_appconfig" {
  name   = "appconfig-runtime"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_appconfig.json
}

data "aws_iam_policy_document" "task_byom" {
  statement {
    actions   = ["kms:Decrypt", "kms:Encrypt"]
    resources = [aws_kms_key.byom.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:tenant_id"
      values   = [var.replika_tenant_id]
    }
  }
}

resource "aws_iam_role_policy" "task_byom" {
  name   = "tenant-byom"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_byom.json
}

data "aws_iam_policy_document" "task_provisioner" {
  count = local.provisioner_enabled ? 1 : 0
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [local.provisioner_function_arn]
  }
}

resource "aws_iam_role_policy" "task_provisioner" {
  count  = local.provisioner_enabled ? 1 : 0
  name   = "replika-provisioner"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_provisioner[0].json
}

resource "aws_lb_target_group" "clyra" {
  name        = local.name
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id
  health_check {
    path                = "/healthz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 15
    matcher             = "200"
  }
}

resource "aws_lb_listener_rule" "clyra_health" {
  count        = var.enable_replika_managed_auth ? 1 : 0
  listener_arn = data.aws_lb_listener.https.arn
  priority     = var.listener_rule_priority - 1
  action {
    type  = "forward"
    order = 1
    forward {
      target_group {
        arn = aws_lb_target_group.clyra.arn
      }
    }
  }
  condition {
    host_header {
      values = [var.host_name]
    }
  }
  condition {
    path_pattern {
      values = ["/healthz", "/readyz"]
    }
  }
}

resource "aws_lb_listener_rule" "clyra" {
  listener_arn = data.aws_lb_listener.https.arn
  priority     = var.listener_rule_priority
  dynamic "action" {
    for_each = var.enable_replika_managed_auth ? [1] : []
    content {
      type  = "authenticate-cognito"
      order = 1
      authenticate_cognito {
        user_pool_arn       = aws_cognito_user_pool.replika[0].arn
        user_pool_client_id = aws_cognito_user_pool_client.replika[0].id
        user_pool_domain    = aws_cognito_user_pool_domain.replika[0].domain
      }
    }
  }
  action {
    type  = "forward"
    order = var.enable_replika_managed_auth ? 2 : 1
    forward {
      target_group {
        arn = aws_lb_target_group.clyra.arn
      }
    }
  }
  condition {
    host_header {
      values = [var.host_name]
    }
  }
}

resource "aws_route53_record" "replika_wildcard" {
  count   = var.replika_route53_zone_id == "" ? 0 : 1
  zone_id = var.replika_route53_zone_id
  name    = "*.${var.replika_product_domain}"
  type    = "A"
  alias {
    name                   = data.aws_lb.staging.dns_name
    zone_id                = data.aws_lb.staging.zone_id
    evaluate_target_health = true
  }
}

resource "aws_acm_certificate" "replika_wildcard" {
  count             = var.replika_route53_zone_id == "" ? 0 : 1
  domain_name       = "*.${var.replika_product_domain}"
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "replika_certificate_validation" {
  for_each = var.replika_route53_zone_id == "" ? {} : {
    for option in aws_acm_certificate.replika_wildcard[0].domain_validation_options :
    option.domain_name => {
      name   = option.resource_record_name
      record = option.resource_record_value
      type   = option.resource_record_type
    }
  }
  zone_id = var.replika_route53_zone_id
  name    = each.value.name
  type    = each.value.type
  ttl     = 60
  records = [each.value.record]
}

resource "aws_acm_certificate_validation" "replika_wildcard" {
  count                   = var.replika_route53_zone_id == "" ? 0 : 1
  certificate_arn         = aws_acm_certificate.replika_wildcard[0].arn
  validation_record_fqdns = [for record in aws_route53_record.replika_certificate_validation : record.fqdn]
}

resource "aws_lb_listener_certificate" "replika_wildcard" {
  count           = var.replika_route53_zone_id == "" ? 0 : 1
  listener_arn    = data.aws_lb_listener.https.arn
  certificate_arn = aws_acm_certificate_validation.replika_wildcard[0].certificate_arn
}

resource "aws_ecs_task_definition" "clyra_fargate" {
  family                   = "${local.name}-fargate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "2048"
  memory                   = "4096"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  track_latest             = true
  enable_fault_injection   = false
  tags                     = {}
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  ephemeral_storage { size_in_gib = var.fargate_ephemeral_storage_gib }
  volume {
    name                = "state"
    configure_at_launch = false
    s3files_volume_configuration {
      file_system_arn         = aws_s3files_file_system.clyra.arn
      access_point_arn        = aws_s3files_access_point.clyra.arn
      root_directory          = "/"
      transit_encryption_port = 0
    }
  }
  container_definitions = jsonencode([
    {
      name           = "appconfig"
      image          = "public.ecr.aws/aws-appconfig/aws-appconfig-agent:2.x"
      cpu            = 0
      essential      = true
      environment    = [{ name = "SERVICE_REGION", value = var.aws_region }]
      portMappings   = []
      mountPoints    = []
      volumesFrom    = []
      systemControls = []
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.clyra.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "appconfig"
        }
      }
    },
    {
      name      = "clyra"
      image     = local.candidate_image_uri
      cpu       = 0
      essential = true
      user      = "1000"
      dependsOn = [
        { containerName = "appconfig", condition = "START" },
      ]
      portMappings = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
      environment  = local.environment_list
      secrets      = local.secret_list
      mountPoints = [
        { sourceVolume = "state", containerPath = "/mnt/efs", readOnly = false },
      ]
      volumesFrom            = []
      systemControls         = []
      readonlyRootFilesystem = true
      restartPolicy = {
        enabled              = true
        restartAttemptPeriod = 60
      }
      linuxParameters = { capabilities = { drop = ["ALL"] } }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.clyra.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "clyra"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=5)\""]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])
  lifecycle {
    # CodePipeline owns application image revisions. Infrastructure applies
    # must not replace a known-good deployed image with the bootstrap value.
    ignore_changes = [container_definitions]
  }
}

resource "aws_ecs_task_definition" "clyra_canary" {
  family                   = "${local.name}-storage-canary"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "2048"
  memory                   = "4096"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  enable_fault_injection   = false
  tags                     = {}
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  ephemeral_storage { size_in_gib = var.fargate_ephemeral_storage_gib }
  volume {
    name                = "state"
    configure_at_launch = false
    s3files_volume_configuration {
      file_system_arn         = aws_s3files_file_system.clyra.arn
      access_point_arn        = aws_s3files_access_point.clyra.arn
      root_directory          = "/"
      transit_encryption_port = 0
    }
  }
  container_definitions = jsonencode([{
    name       = "canary"
    image      = local.candidate_image_uri
    cpu        = 0
    essential  = true
    user       = "1000"
    entryPoint = ["python", "/app/scripts/clyra_storage_acceptance.py"]
    command    = ["--root", "/mnt/efs", "--palace", "/mnt/efs/data/.mempalace/palace"]
    mountPoints = [
      { sourceVolume = "state", containerPath = "/mnt/efs", readOnly = false },
    ]
    environment     = []
    portMappings    = []
    volumesFrom     = []
    systemControls  = []
    linuxParameters = { capabilities = { add = [], drop = ["ALL"] } }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.clyra.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "storage-canary"
      }
    }
  }])
}

resource "aws_ecs_service" "clyra" {
  name                               = local.name
  cluster                            = data.aws_ecs_cluster.staging.arn
  task_definition                    = aws_ecs_task_definition.clyra_fargate.arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  health_check_grace_period_seconds  = 90
  enable_execute_command             = true
  force_new_deployment               = true
  propagate_tags                     = "SERVICE"
  tags = {
    ReplikaManaged = "true"
    ReplikaRelease = "bootstrap"
    ReplikaRollout = "ready"
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = tolist(var.private_subnet_ids)
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.clyra.arn
    container_name   = "clyra"
    container_port   = 8080
  }
  depends_on = [
    aws_s3files_mount_target.clyra,
    aws_s3files_file_system_policy.clyra,
    aws_lb_listener_rule.clyra_health,
    aws_lb_listener_rule.clyra,
  ]
}

resource "aws_cloudwatch_metric_alarm" "s3files_failures" {
  for_each = toset(["ImportFailures", "ExportFailures", "LostAndFoundFiles"])

  alarm_name          = "${local.name}-s3files-${lower(each.value)}"
  alarm_description   = "S3 Files reported ${each.value}; investigate before cutover or rollback."
  namespace           = "AWS/S3/Files"
  metric_name         = each.value
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { FileSystemId = aws_s3files_file_system.clyra.id }
}
