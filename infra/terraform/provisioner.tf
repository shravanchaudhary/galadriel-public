data "archive_file" "replika_provisioner" {
  count       = var.replika_callback_token_secret_arn == "" ? 0 : 1
  type        = "zip"
  source_file = "${path.module}/../provisioner/handler.py"
  output_path = "${path.module}/.replika-provisioner.zip"
}

resource "aws_iam_role" "replika_provisioner" {
  count = var.replika_callback_token_secret_arn == "" ? 0 : 1
  name  = "${local.name}-replika-provisioner"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_iam_policy_document" "replika_provisioner" {
  count = var.replika_callback_token_secret_arn == "" ? 0 : 1

  statement {
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }

  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.replika_callback_token_secret_arn]
  }

  statement {
    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:DescribeSecret",
      "secretsmanager:DeleteSecret",
      "secretsmanager:TagResource",
    ]
    resources = local.slack_secret_arns
  }

  statement {
    actions = [
      "s3files:CreateAccessPoint",
      "s3files:DeleteAccessPoint",
      "s3files:DescribeAccessPoints",
      "s3files:ListAccessPoints",
      "s3files:TagResource",
    ]
    resources = ["*"]
  }

  dynamic "statement" {
    for_each = var.enable_replika_managed_auth ? [1] : []
    content {
      actions = [
        "cognito-idp:CreateUserPoolClient",
        "cognito-idp:DeleteUserPoolClient",
        "cognito-idp:DescribeUserPoolClient",
        "cognito-idp:ListUserPoolClients",
      ]
      resources = ["*"]
    }
  }

  statement {
    actions = [
      "elasticloadbalancing:CreateRule",
      "elasticloadbalancing:CreateTargetGroup",
      "elasticloadbalancing:DeleteRule",
      "elasticloadbalancing:DeleteTargetGroup",
      "elasticloadbalancing:DescribeRules",
      "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:AddTags",
      "elasticloadbalancing:ModifyRule",
    ]
    resources = ["*"]
  }

  statement {
    actions = [
      "ecs:CreateService",
      "ecs:DeleteService",
      "ecs:DeregisterTaskDefinition",
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:ListTaskDefinitions",
      "ecs:RegisterTaskDefinition",
      "ecs:TagResource",
      "ecs:UpdateService",
    ]
    resources = ["*"]
  }

  statement {
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:GetRole",
      "iam:PutRolePolicy",
      "iam:TagRole",
    ]
    resources = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/replika-*"]
  }

  statement {
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.execution.arn,
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/replika-*",
    ]
  }
}

resource "aws_iam_role_policy" "replika_provisioner" {
  count  = var.replika_callback_token_secret_arn == "" ? 0 : 1
  name   = "provision-replika-runtime"
  role   = aws_iam_role.replika_provisioner[0].id
  policy = data.aws_iam_policy_document.replika_provisioner[0].json
}

resource "aws_lambda_function" "replika_provisioner" {
  count            = var.replika_callback_token_secret_arn == "" ? 0 : 1
  function_name    = "${local.name}-replika-provisioner"
  role             = aws_iam_role.replika_provisioner[0].arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.replika_provisioner[0].output_path
  source_code_hash = data.archive_file.replika_provisioner[0].output_base64sha256
  timeout          = 900
  memory_size      = 256

  environment {
    variables = {
      BASE_TASK_DEFINITION      = "${local.name}-replika-runtime-base"
      BYOM_KMS_KEY_ARN          = aws_kms_key.byom.arn
      CALLBACK_TOKEN_SECRET_ARN = var.replika_callback_token_secret_arn
      CALLBACK_URL              = "https://${var.host_name}/internal/replika/provisioning"
      COGNITO_USER_POOL_ARN     = var.enable_replika_managed_auth ? aws_cognito_user_pool.replika[0].arn : ""
      COGNITO_USER_POOL_DOMAIN = (
        var.enable_replika_managed_auth ? aws_cognito_user_pool_domain.replika[0].domain : ""
      )
      CONTAINER_NAME                  = "clyra"
      DATABASE_BROKER_URL             = "https://${var.host_name}/internal/replika/database"
      ECS_CLUSTER                     = data.aws_ecs_cluster.staging.cluster_name
      HTTPS_LISTENER_ARN              = data.aws_lb_listener.https.arn
      MANAGED_AUTH_ENABLED            = tostring(var.enable_replika_managed_auth)
      PRIVATE_SUBNET_IDS              = jsonencode(tolist(var.private_subnet_ids))
      PRODUCT_DOMAIN                  = var.replika_product_domain
      RUNTIME_SECRET_NAMES            = jsonencode(tolist(var.replika_runtime_secret_names))
      SLACK_TENANT_AUTH_KMS_KEY_ID    = "alias/aws/secretsmanager"
      SLACK_TENANT_AUTH_SECRET_PREFIX = "replika/slack-auth"
      S3FILES_FILE_SYSTEM_ARN         = aws_s3files_file_system.clyra.arn
      S3FILES_FILE_SYSTEM_ID          = aws_s3files_file_system.clyra.id
      TASK_SECURITY_GROUP_ID          = aws_security_group.task.id
      VPC_ID                          = var.vpc_id
      VOICE_TRANSCRIBE_ROLE_ARN       = aws_iam_role.browser_transcription.arn
    }
  }

  depends_on = [
    aws_ecs_task_definition.replika_runtime_base,
    aws_iam_role_policy.replika_provisioner,
  ]
}
