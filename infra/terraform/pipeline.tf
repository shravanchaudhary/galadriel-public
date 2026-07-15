resource "aws_s3_bucket" "pipeline" {
  bucket_prefix = "${local.name}-pipeline-"
}

resource "aws_s3_bucket_public_access_block" "pipeline" {
  bucket                  = aws_s3_bucket.pipeline.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "codebuild_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  name               = "${local.name}-codebuild"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
}

data "aws_iam_policy_document" "codebuild" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.clyra.arn]
  }
  statement {
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
  statement {
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
    resources = ["${aws_s3_bucket.pipeline.arn}/*"]
  }
}

resource "aws_iam_role_policy" "codebuild" {
  name   = "build-image"
  role   = aws_iam_role.codebuild.id
  policy = data.aws_iam_policy_document.codebuild.json
}

resource "aws_codebuild_project" "clyra" {
  name          = local.name
  service_role  = aws_iam_role.codebuild.arn
  build_timeout = 30
  artifacts { type = "CODEPIPELINE" }
  environment {
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    image                       = "aws/codebuild/standard:7.0"
    type                        = "LINUX_CONTAINER"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"
    environment_variable {
      name  = "ECR_REPOSITORY_URL"
      value = aws_ecr_repository.clyra.repository_url
    }
    environment_variable {
      name  = "AWS_DEFAULT_REGION"
      value = var.aws_region
    }
  }
  source {
    type      = "CODEPIPELINE"
    buildspec = "buildspec.yml"
  }
  logs_config {
    cloudwatch_logs {
      status = "ENABLED"
    }
  }
}

data "aws_iam_policy_document" "pipeline_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codepipeline.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "pipeline" {
  name               = "${local.name}-pipeline"
  assume_role_policy = data.aws_iam_policy_document.pipeline_assume.json
}

data "aws_iam_policy_document" "pipeline" {
  statement {
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
    resources = ["${aws_s3_bucket.pipeline.arn}/*"]
  }
  statement {
    actions   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"]
    resources = [aws_codebuild_project.clyra.arn]
  }
  statement {
    actions   = ["codestar-connections:UseConnection"]
    resources = [var.github_connection_arn]
  }
  statement {
    actions   = ["ecs:DescribeServices", "ecs:DescribeTaskDefinition", "ecs:UpdateService"]
    resources = ["*"]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
  }
}

resource "aws_iam_role_policy" "pipeline" {
  name   = "deploy-service"
  role   = aws_iam_role.pipeline.id
  policy = data.aws_iam_policy_document.pipeline.json
}

resource "aws_codepipeline" "clyra" {
  name           = local.name
  role_arn       = aws_iam_role.pipeline.arn
  pipeline_type  = "V2"
  execution_mode = "QUEUED"
  artifact_store {
    location = aws_s3_bucket.pipeline.bucket
    type     = "S3"
  }
  stage {
    name = "Source"
    action {
      name             = "GitHub"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeStarSourceConnection"
      version          = "1"
      output_artifacts = ["source"]
      configuration = {
        ConnectionArn    = var.github_connection_arn
        FullRepositoryId = var.github_repo
        BranchName       = "clyra"
        DetectChanges    = "true"
      }
    }
  }
  stage {
    name = "Build"
    action {
      name             = "BuildImage"
      category         = "Build"
      owner            = "AWS"
      provider         = "CodeBuild"
      version          = "1"
      input_artifacts  = ["source"]
      output_artifacts = ["build"]
      configuration    = { ProjectName = aws_codebuild_project.clyra.name }
    }
  }
  stage {
    name = "Deploy"
    action {
      name            = "ECS"
      category        = "Deploy"
      owner           = "AWS"
      provider        = "ECS"
      version         = "1"
      input_artifacts = ["build"]
      configuration = {
        ClusterName = data.aws_ecs_cluster.staging.cluster_name
        ServiceName = aws_ecs_service.clyra.name
        FileName    = "imagedefinitions.json"
      }
    }
  }
}
