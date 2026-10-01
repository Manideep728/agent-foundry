# Registries, the artifact bucket, the results database, and the budget alarm.

resource "aws_ecr_repository" "runner" {
  name                 = "${var.name}/afrunner"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # destroyed with its images; this is a demo
}

resource "aws_ecr_repository" "control_plane" {
  name                 = "${var.name}/control-plane"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true
}

resource "aws_s3_bucket" "artifacts" {
  # The account id makes the name unique without a random suffix to track.
  bucket        = "${var.name}-artifacts-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_db_subnet_group" "results" {
  name       = "${var.name}-results"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_db_instance" "results" {
  identifier     = "${var.name}-results"
  engine         = "postgres"
  engine_version = "17"
  instance_class = "db.t4g.micro"

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "agentfoundry"
  username = "agentfoundry"
  # RDS creates the password and keeps it in Secrets Manager. It never appears
  # in this configuration, the state, or a task definition: the collector's
  # task receives it as PGPASSWORD from the secret.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.results.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false

  # A demonstration database: no backups to wait for, and nothing kept when
  # it is destroyed.
  backup_retention_period = 0
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
