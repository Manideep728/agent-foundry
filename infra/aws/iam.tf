# Three roles, each with only what its holder does.

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# The instance: joins the cluster, pulls the runner image the user data
# prefetches, and accepts SSM sessions.
resource "aws_iam_role" "instance" {
  name               = "${var.name}-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "instance" {
  for_each = toset([
    "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
  ])
  role       = aws_iam_role.instance.name
  policy_arn = each.value
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name}-instance"
  role = aws_iam_role.instance.name
}

# ECS itself, starting a task: pull its image, write its logs, and read the
# one secret the collector and the report task are given.
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_database_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.results.master_user_secret[0].secret_arn]
  }
}

resource "aws_iam_role_policy" "read_database_secret" {
  name   = "read-database-secret"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_database_secret.json
}

# A worker, once running: write artifacts under the prefix, and nothing else.
resource "aws_iam_role" "worker" {
  name               = "${var.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "write_artifacts" {
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/${var.artifact_prefix}/runs/*"]
  }
}

resource "aws_iam_role_policy" "write_artifacts" {
  name   = "write-artifacts"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.write_artifacts.json
}
