# AgentFoundry's Kafka fan-out on AWS: one ECS container instance running the
# broker, a collector and two workers; RDS PostgreSQL for the results; S3 for
# the run artifacts; ECR for the two images (D35).
#
# Sized for a demonstration that is created, measured and destroyed in an
# afternoon, not for a standing service. `infra/aws/smoke.sh` drives the whole
# cycle, including `terraform destroy`.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.67"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.name }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  registry   = "${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"

  runner_image  = "${aws_ecr_repository.runner.repository_url}:${var.image_tag}"
  control_image = "${aws_ecr_repository.control_plane.repository_url}:${var.image_tag}"

  # One directory on the instance, mounted into each worker at the same path:
  # the worker hands paths under it to `docker run`, and the daemon resolves
  # them on the instance.
  host_root = "/var/lib/agentfoundry"

  # The docker group's id on the instance, set by the user data, so a worker
  # can run as an ordinary user and still reach the Docker socket.
  docker_gid = 1999

  database_url = format(
    "postgresql://%s@%s/%s?sslmode=require",
    aws_db_instance.results.username,
    aws_db_instance.results.endpoint,
    aws_db_instance.results.db_name,
  )
  artifacts = "s3://${aws_s3_bucket.artifacts.bucket}/${var.artifact_prefix}"
}
