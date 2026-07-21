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
      "s3files:CreateAccessPoint",
      "s3files:DescribeAccessPoints",
      "s3files:TagResource",
    ]
    resources = ["*"]
  }

  statement {
    actions = [
      "elasticloadbalancing:CreateRule",
      "elasticloadbalancing:CreateTargetGroup",
      "elasticloadbalancing:DescribeRules",
      "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:AddTags",
    ]
    resources = ["*"]
  }

  statement {
    actions = [
      "ecs:CreateService",
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:TagResource",
      "ecs:UpdateService",
    ]
    resources = ["*"]
  }

  statement {
    actions = [
      "iam:CreateRole",
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
      BASE_TASK_DEFINITION      = "${local.name}-fargate"
      BYOM_KMS_KEY_ARN          = aws_kms_key.byom.arn
      CALLBACK_TOKEN_SECRET_ARN = var.replika_callback_token_secret_arn
      CALLBACK_URL              = "https://${var.host_name}/internal/replika/provisioning"
      CONTAINER_NAME            = "clyra"
      DATABASE_BROKER_URL       = "https://${var.host_name}/internal/replika/database"
      ECS_CLUSTER               = data.aws_ecs_cluster.staging.cluster_name
      HTTPS_LISTENER_ARN        = data.aws_lb_listener.https.arn
      PRIVATE_SUBNET_IDS        = jsonencode(tolist(var.private_subnet_ids))
      PRODUCT_DOMAIN            = var.replika_product_domain
      RUNTIME_SECRET_NAMES      = jsonencode(tolist(var.replika_runtime_secret_names))
      S3FILES_FILE_SYSTEM_ARN   = aws_s3files_file_system.clyra.arn
      S3FILES_FILE_SYSTEM_ID    = aws_s3files_file_system.clyra.id
      TASK_SECURITY_GROUP_ID    = aws_security_group.task.id
      VPC_ID                    = var.vpc_id
    }
  }

  depends_on = [aws_iam_role_policy.replika_provisioner]
}
