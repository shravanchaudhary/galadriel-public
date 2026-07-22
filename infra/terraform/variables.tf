variable "aws_region" {
  type    = string
  default = "ap-south-1"
}

variable "vpc_id" { type = string }
variable "private_subnet_ids" {
  type = set(string)
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Provide private subnets in both staging AZs."
  }
}
variable "alb_name" {
  type    = string
  default = "STAG-ALB"
}
variable "alb_security_group_id" { type = string }
variable "documentdb_security_group_id" {
  description = "Existing DocumentDB security group; permit Clyra task ingress when supplied."
  type        = string
  default     = null
}
variable "valkey_security_group_id" {
  description = "Existing Valkey security group; permit Clyra task ingress when supplied."
  type        = string
  default     = null
}
variable "ecs_cluster_name" {
  type    = string
  default = "clodexa-stag-cluster"
}
variable "candidate_image_uri" {
  description = "Immutable image URI for initial Fargate registration and the storage canary. CodePipeline owns live service image revisions."
  type        = string
  default     = null
}
variable "palace_backend" {
  description = "Memory storage backend: chroma, mongo, or documentdb."
  type        = string
  default     = "chroma"
  validation {
    condition     = contains(["chroma", "mongo", "documentdb"], var.palace_backend)
    error_message = "palace_backend must be chroma, mongo, or documentdb."
  }
}
variable "fargate_ephemeral_storage_gib" {
  description = "Ephemeral storage for the 6.5 GiB image and writable layers."
  type        = number
  default     = 40
  validation {
    condition     = var.fargate_ephemeral_storage_gib >= 30 && var.fargate_ephemeral_storage_gib <= 200
    error_message = "Fargate ephemeral storage must be between 30 and 200 GiB."
  }
}
variable "s3_state_noncurrent_version_expiration_days" {
  description = "Retention period for rollback versions in the S3 Files backing bucket."
  type        = number
  default     = 90
}
variable "listener_rule_priority" {
  description = "ALB priority for the authenticated control-plane rule; the preceding priority is reserved for public health checks."
  type        = number
  validation {
    condition     = var.listener_rule_priority >= 2 && var.listener_rule_priority <= 50000
    error_message = "listener_rule_priority must be between 2 and 50000."
  }
}
variable "github_connection_arn" { type = string }
variable "appconfig_application_id" {
  type    = string
  default = "rjh1ukh"
}
variable "appconfig_environment_id" {
  type    = string
  default = "g635nuc"
}
variable "appconfig_configuration_id" {
  type    = string
  default = "swqwh37"
}
variable "github_repo" {
  type    = string
  default = "shravanchaudhary/galadriel-public"
}

variable "service_name" {
  type    = string
  default = "clyra-stag"
}
variable "replika_tenant_id" {
  description = "Provider-owned immutable tenant identifier injected into this isolated runtime."
  type        = string
  default     = "default"
}
variable "replika_product_domain" {
  description = "Public product domain used to generate customer-facing Replika URLs."
  type        = string
  default     = "replika.clodexa.com"
}
variable "replika_route53_zone_id" {
  description = "Provider Route53 hosted zone used for the wildcard Replika product domain."
  type        = string
  default     = ""
}
variable "replika_provisioner_function_arn" {
  description = "Private Lambda function used by the control plane to provision tenant runtimes."
  type        = string
  default     = ""
}
variable "replika_callback_token_secret_arn" {
  description = "Secrets Manager ARN containing the private provisioning callback bearer token."
  type        = string
  default     = ""
}
variable "enable_replika_managed_auth" {
  description = "Enable provider-managed Cognito authentication at the ALB."
  type        = bool
  default     = false
}
variable "replika_control_plane_only" {
  description = "Expose only customer onboarding and lifecycle routes from this service."
  type        = bool
  default     = false
}
variable "replika_runtime_secret_names" {
  description = "Provider infrastructure secrets exposed to tenant runtimes. Model API keys are intentionally excluded; customers configure BYOM."
  type        = set(string)
  default     = ["TOWER_SECRET_KEY"]
}
variable "host_name" {
  type    = string
  default = "app.replika.clodexa.com"
}
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "secret_arns" {
  description = "ECS environment variable name => existing Secrets Manager ARN."
  type        = map(string)
  sensitive   = true
  default     = {}
}
variable "environment" {
  description = "Non-secret runtime settings."
  type        = map(string)
  default = {
    AWS_REGION                      = "ap-south-1"
    APPCONFIG_AGENT_URL             = "http://127.0.0.1:2772"
    APPCONFIG_APPLICATION           = "rjh1ukh"
    APPCONFIG_CONFIGURATION         = "swqwh37"
    APPCONFIG_ENVIRONMENT           = "g635nuc"
    APPCONFIG_REQUIRED              = "true"
    BROWSER_BACKEND                 = "bce"
    BCE_BASE_URL                    = ""
    BCE_TIMEOUT_MS                  = "10000"
    GALADRIEL_COMPLETION_MARKER_DIR = "/mnt/efs/completion-markers"
    GALADRIEL_STORAGE_ROOT          = "/mnt/efs"
    GALADRIEL_REFLECTION            = "1"
    GALADRIEL_SELF_RESTART_ENABLED  = "true"
    GALADRIEL_WORKER                = "1"
    DAILY_COST_LIMIT_USD            = "5.00"
    TOWER_AUTH_REQUIRED             = "true"
    TOWER_AUTH_USERNAME             = "clyra"
    TOWER_COOKIE_SECURE             = "true"
    TOWER_HOST                      = "0.0.0.0"
    TOWER_PORT                      = "8080"
    TOWER_THREADS                   = "8"
  }
}

check "replika_cookie_domain" {
  assert {
    condition = (
      !var.enable_replika_managed_auth
      || var.host_name == var.replika_product_domain
      || endswith(var.host_name, ".${var.replika_product_domain}")
    )
    error_message = "Managed auth requires host_name to be inside replika_product_domain so the control plane can issue tenant session cookies."
  }
}
