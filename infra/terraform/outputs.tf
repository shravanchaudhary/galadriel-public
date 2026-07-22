output "ecr_repository_url" {
  value = aws_ecr_repository.clyra.repository_url
}

output "ecs_service_name" {
  value = aws_ecs_service.clyra.name
}

output "s3_state_bucket" {
  value = aws_s3_bucket.state.id
}

output "s3files_file_system_id" {
  value = aws_s3files_file_system.clyra.id
}

output "s3files_access_point_arn" {
  value = aws_s3files_access_point.clyra.arn
}

output "canary_task_definition_arn" {
  value = aws_ecs_task_definition.clyra_canary.arn
}

output "fargate_task_definition_arn" {
  value = aws_ecs_task_definition.clyra_fargate.arn
}

output "target_group_arn" {
  value = aws_lb_target_group.clyra.arn
}

output "replika_control_plane_url" {
  value = "https://${var.host_name}"
}

output "replika_product_url_pattern" {
  value = "https://<username>.${var.replika_product_domain}"
}

output "replika_provisioner_function_arn" {
  value = local.provisioner_function_arn
}
