data "aws_caller_identity" "current" {}
data "aws_ecs_cluster" "staging" { cluster_name = var.ecs_cluster_name }
data "aws_lb" "staging" { name = var.alb_name }
data "aws_lb_listener" "https" {
  load_balancer_arn = data.aws_lb.staging.arn
  port              = 443
}

locals {
  name        = var.service_name
  image_uri   = "${aws_ecr_repository.clyra.repository_url}:bootstrap"
  secret_list = [for name, arn in var.secret_arns : { name = name, valueFrom = arn }]
  environment_list = [
    for name, value in merge(var.environment, {
      APPCONFIG_APPLICATION   = var.appconfig_application_id
      APPCONFIG_ENVIRONMENT   = var.appconfig_environment_id
      APPCONFIG_CONFIGURATION = var.appconfig_configuration_id
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

resource "aws_security_group" "efs" {
  name_prefix = "${local.name}-efs-"
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

resource "aws_efs_file_system" "clyra" {
  encrypted = true
  lifecycle_policy { transition_to_ia = "AFTER_30_DAYS" }
  lifecycle_policy { transition_to_primary_storage_class = "AFTER_1_ACCESS" }
  tags = { Name = "${local.name}-state" }
}

resource "aws_efs_backup_policy" "clyra" {
  file_system_id = aws_efs_file_system.clyra.id
  backup_policy { status = "ENABLED" }
}

resource "aws_efs_mount_target" "clyra" {
  for_each        = var.private_subnet_ids
  file_system_id  = aws_efs_file_system.clyra.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "clyra" {
  file_system_id = aws_efs_file_system.clyra.id
  posix_user {
    uid = 1000
    gid = 1000
  }
  root_directory {
    path = "/clyra"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "750"
    }
  }
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
    resources = values(var.secret_arns)
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  count  = length(var.secret_arns) > 0 ? 1 : 0
  name   = "runtime-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.execution_assume.json
}

data "aws_iam_policy_document" "task_efs" {
  statement {
    actions   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    resources = [aws_efs_file_system.clyra.arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.clyra.arn]
    }
  }
}

resource "aws_iam_role_policy" "task_efs" {
  name   = "efs-state"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_efs.json
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

resource "aws_lb_listener_rule" "clyra" {
  listener_arn = data.aws_lb_listener.https.arn
  priority     = var.listener_rule_priority
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.clyra.arn
  }
  condition {
    host_header {
      values = [var.host_name]
    }
  }
}

resource "aws_ecs_task_definition" "clyra" {
  family                   = local.name
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = "2048"
  memory                   = "4096"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  volume {
    name = "state"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.clyra.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.clyra.id
        iam             = "ENABLED"
      }
    }
  }
  container_definitions = jsonencode([
    {
      name        = "appconfig"
      image       = "public.ecr.aws/aws-appconfig/aws-appconfig-agent:2.x"
      essential   = true
      environment = [{ name = "SERVICE_REGION", value = var.aws_region }]
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
      name            = "imds-firewall"
      image           = local.image_uri
      essential       = false
      privileged      = true
      user            = "0"
      entryPoint      = ["/bin/sh", "-c"]
      command         = ["iptables -A OUTPUT -d 169.254.169.254 -j REJECT"]
      linuxParameters = { capabilities = { add = ["NET_ADMIN"] } }
    },
    {
      name      = "clyra"
      image     = local.image_uri
      essential = true
      user      = "1000"
      dependsOn = [
        { containerName = "imds-firewall", condition = "SUCCESS" },
        { containerName = "appconfig", condition = "START" },
      ]
      portMappings    = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
      environment     = local.environment_list
      secrets         = local.secret_list
      mountPoints     = [{ sourceVolume = "state", containerPath = "/mnt/efs", readOnly = false }]
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
}

resource "aws_ecs_service" "clyra" {
  name                               = local.name
  cluster                            = data.aws_ecs_cluster.staging.arn
  task_definition                    = aws_ecs_task_definition.clyra.arn
  desired_count                      = 1
  launch_type                        = null
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  health_check_grace_period_seconds  = 90
  enable_execute_command             = true
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  capacity_provider_strategy {
    capacity_provider = var.capacity_provider_name
    weight            = 1
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
  depends_on = [aws_efs_mount_target.clyra, aws_lb_listener_rule.clyra]
}
