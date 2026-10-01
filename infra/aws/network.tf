# The account's default VPC, so there is no NAT gateway to pay for: the
# instance has a public address for outbound traffic only, and nothing is
# allowed in. Shell access, when needed, is through SSM Session Manager.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

resource "aws_security_group" "instance" {
  name        = "${var.name}-instance"
  description = "The ECS container instance: outbound only"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_egress_rule" "instance_out" {
  security_group_id = aws_security_group.instance.id
  description       = "ECR, S3, Docker Hub, SSM and the database"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "database" {
  name        = "${var.name}-database"
  description = "RDS: reachable from the container instance only"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "database_from_instance" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from the container instance"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.instance.id
}
