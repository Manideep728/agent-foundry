output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "runner_repository" {
  value = aws_ecr_repository.runner.repository_url
}

output "control_plane_repository" {
  value = aws_ecr_repository.control_plane.repository_url
}

output "registry" {
  value = local.registry
}

output "artifacts" {
  value = local.artifacts
}

output "database_endpoint" {
  value = aws_db_instance.results.endpoint
}

output "log_group" {
  value = aws_cloudwatch_log_group.tasks.name
}

output "instance_id" {
  value = aws_instance.container_host.id
}

output "one_off_tasks" {
  value = {
    topics   = aws_ecs_task_definition.topics.arn
    dispatch = aws_ecs_task_definition.dispatch.arn
    report   = aws_ecs_task_definition.report.arn
  }
}

output "worker_services" {
  value = [for id in var.workers : "worker-${id}"]
}
