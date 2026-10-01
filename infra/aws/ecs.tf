# One EC2 container instance, not Fargate (D35). A worker validates by starting
# the runner image as a sibling container through the Docker socket (D1: the
# supervisor is PID 1 of its own container), and Fargate offers no socket.
#
# Every task uses host networking. The broker advertises localhost:9092, which
# is correct for every task on the one instance, and nothing listens on an
# address outside it.

resource "aws_ecs_cluster" "main" {
  name = var.name
}

resource "aws_cloudwatch_log_group" "tasks" {
  name              = "/${var.name}"
  retention_in_days = 1
}

data "aws_ssm_parameter" "ecs_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
}

resource "aws_instance" "container_host" {
  ami                         = nonsensitive(data.aws_ssm_parameter.ecs_ami.value)
  instance_type               = var.instance_type
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids      = [aws_security_group.instance.id]
  iam_instance_profile        = aws_iam_instance_profile.instance.name
  associate_public_ip_address = true

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_size = 40
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh", {
    cluster      = aws_ecs_cluster.main.name
    region       = var.region
    registry     = local.registry
    runner_image = local.runner_image
    host_root    = local.host_root
    docker_gid   = local.docker_gid
  })
  user_data_replace_on_change = true

  tags = { Name = "${var.name}-container-host" }
}

locals {
  log_options = {
    "awslogs-group"         = aws_cloudwatch_log_group.tasks.name
    "awslogs-region"        = var.region
    "awslogs-stream-prefix" = "af"
  }

  # The broker as `infra/kafka/broker.sh` configures it, without a volume:
  # the topics live as long as this demonstration does.
  kafka_environment = [
    for name, value in {
      KAFKA_NODE_ID                                  = "1"
      KAFKA_PROCESS_ROLES                            = "broker,controller"
      KAFKA_LISTENERS                                = "PLAINTEXT://:9092,CONTROLLER://:9093"
      KAFKA_ADVERTISED_LISTENERS                     = "PLAINTEXT://localhost:9092"
      KAFKA_CONTROLLER_QUORUM_VOTERS                 = "1@localhost:9093"
      KAFKA_CONTROLLER_LISTENER_NAMES                = "CONTROLLER"
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP           = "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT"
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR         = "1"
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR = "1"
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR            = "1"
      KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS         = "3000"
      KAFKA_AUTO_CREATE_TOPICS_ENABLE                = "false"
      KAFKA_HEAP_OPTS                                = "-Xmx512m -Xms512m"
    } : { name = name, value = value }
  ]

  database_password = [{
    name      = "PGPASSWORD"
    valueFrom = "${aws_db_instance.results.master_user_secret[0].secret_arn}:password::"
  }]
}

resource "aws_ecs_task_definition" "kafka" {
  family                   = "${var.name}-kafka"
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode([{
    name              = "kafka"
    image             = "apache/kafka:4.1.0"
    essential         = true
    memoryReservation = 1024
    environment       = local.kafka_environment
    logConfiguration  = { logDriver = "awslogs", options = local.log_options }
  }])
}

# One-off: the two topics, as `infra/kafka/topics.sh` creates them.
resource "aws_ecs_task_definition" "topics" {
  family                   = "${var.name}-topics"
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode([{
    name              = "topics"
    image             = "apache/kafka:4.1.0"
    essential         = true
    memoryReservation = 256
    entryPoint        = ["sh", "-c"]
    command = [join(" && ", [
      for topic, partitions in { "agentfoundry-task-runs" = 6, "agentfoundry-run-results" = 1 } :
      "/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --if-not-exists --topic ${topic} --partitions ${partitions} --replication-factor 1"
    ])]
    logConfiguration = { logDriver = "awslogs", options = local.log_options }
  }])
}

resource "aws_ecs_task_definition" "collector" {
  family                   = "${var.name}-collector"
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode([{
    name              = "collector"
    image             = local.control_image
    essential         = true
    memoryReservation = 256
    command           = ["collect", "--db", local.database_url]
    secrets           = local.database_password
    logConfiguration  = { logDriver = "awslogs", options = local.log_options }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  for_each                 = var.workers
  family                   = "${var.name}-worker-${each.key}"
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker.arn

  volume {
    name      = "docker-socket"
    host_path = "/var/run/docker.sock"
  }
  volume {
    name      = "host-root"
    host_path = local.host_root
  }

  container_definitions = jsonencode([{
    name              = "worker"
    image             = local.control_image
    essential         = true
    memoryReservation = 512
    # The image's user, with the docker group's id as the primary group, so
    # the worker reaches the socket without being root; the runner it starts
    # is given the same identity.
    user = "1000:${local.docker_gid}"
    command = [
      "worker", "--agent", "fake", "--image", local.runner_image,
      "--worker-id", each.key,
      "--db", "${local.host_root}/${each.key}/worker.sqlite3",
      "--runs", "${local.host_root}/${each.key}/runs",
      "--artifacts", local.artifacts,
    ]
    environment = [{ name = "AWS_DEFAULT_REGION", value = var.region }]
    mountPoints = [
      { sourceVolume = "docker-socket", containerPath = "/var/run/docker.sock" },
      # The same path inside as on the instance; see `host_root`.
      { sourceVolume = "host-root", containerPath = local.host_root },
    ]
    logConfiguration = { logDriver = "awslogs", options = local.log_options }
  }])
}

# One-off tasks the smoke script runs.
resource "aws_ecs_task_definition" "dispatch" {
  family                   = "${var.name}-dispatch"
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode([{
    name              = "dispatch"
    image             = local.control_image
    essential         = true
    memoryReservation = 256
    command           = ["dispatch", "--agent", "fake", "--image", local.runner_image]
    logConfiguration  = { logDriver = "awslogs", options = local.log_options }
  }])
}

resource "aws_ecs_task_definition" "report" {
  family                   = "${var.name}-report"
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode([{
    name              = "report"
    image             = local.control_image
    essential         = true
    memoryReservation = 256
    command           = ["report", "--db", local.database_url, "--out", "-"]
    secrets           = local.database_password
    logConfiguration  = { logDriver = "awslogs", options = local.log_options }
  }])
}

locals {
  services = merge(
    {
      kafka     = aws_ecs_task_definition.kafka.arn
      collector = aws_ecs_task_definition.collector.arn
    },
    { for id, definition in aws_ecs_task_definition.worker : "worker-${id}" => definition.arn },
  )
}

resource "aws_ecs_service" "long_running" {
  for_each        = local.services
  name            = each.key
  cluster         = aws_ecs_cluster.main.id
  task_definition = each.value
  desired_count   = 1
  launch_type     = "EC2"

  # One instance and host networking: a replacement cannot start beside the
  # task it replaces, so the old one stops first.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
}
