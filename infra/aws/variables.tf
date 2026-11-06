variable "region" {
  description = "Where everything is created."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Prefix for every resource name, and the Project tag."
  type        = string
  default     = "agentfoundry"
}

variable "alert_email" {
  description = "Who the budget alert emails. AWS asks this address to confirm."
  type        = string
}

variable "budget_usd" {
  description = "Monthly spend that triggers the alert."
  type        = number
  default     = 10
}

variable "instance_type" {
  description = "The container instance. Each validation is capped at 1 GiB, two run at once, and the broker needs about 1 GiB. An account on the AWS free plan can start only free-tier-eligible types; m7i-flex.large is one, with 2 vCPU and 8 GiB."
  type        = string
  default     = "m7i-flex.large"
}

variable "image_tag" {
  description = "The tag both images are pushed under: the commit they were built from."
  type        = string
}

variable "workers" {
  description = "One ECS service per worker, so each has its own database and runs directory."
  type        = set(string)
  default     = ["w1", "w2"]
}

variable "artifact_prefix" {
  description = "Where in the bucket run artifacts go."
  type        = string
  default     = "fanout"
}
