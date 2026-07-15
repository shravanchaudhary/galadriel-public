output "ecr_repository_url" {
  value = aws_ecr_repository.clyra.repository_url
}

output "ecs_service_name" {
  value = aws_ecs_service.clyra.name
}

output "efs_file_system_id" {
  value = aws_efs_file_system.clyra.id
}

output "target_group_arn" {
  value = aws_lb_target_group.clyra.arn
}
