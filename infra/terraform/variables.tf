variable "region" {
  description = "Mumbai, for latency to Zerodha and because the data is Indian."
  type        = string
  default     = "ap-south-1"
}

variable "name" {
  description = "Prefix for every resource name, so one account can hold more than one of these."
  type        = string
  default     = "steadystack"
}

variable "instance_type" {
  description = <<-EOT
    One operator, one portfolio, about sixty scheduled ticks a day, and
    Postgres on the same box. t4g.small is two vCPU and 2 GiB on Graviton,
    which is ample and cheap; the tick daemon is the only thing that runs
    continuously and it is idle between ticks.
  EOT
  type        = string
  default     = "t4g.small"
}

variable "root_volume_gb" {
  description = "Root volume, holding the database. Snapshotted nightly by DLM."
  type        = number
  default     = 30
}

variable "domain_name" {
  description = <<-EOT
    The hostname the operator reaches the app on, e.g. steadystack.example.com.
    Used for the Route 53 record and for CSRF_TRUSTED_ORIGINS on the box.
  EOT
  type        = string
}

variable "hosted_zone_id" {
  description = "The Route 53 zone the record goes in. Empty to skip DNS entirely."
  type        = string
  default     = ""
}

variable "operator_email" {
  description = "Where M51's alerts go. Confirm the SNS subscription by email once."
  type        = string
}

variable "log_retention_days" {
  description = <<-EOT
    How long CloudWatch keeps the shipped logs. A year, because the thing
    these are for is reconstructing a trading day long after it happened, and
    a trading day is only interesting once somebody asks about it.
  EOT
  type        = number
  default     = 365
}

variable "backup_retention_days" {
  description = "How long a nightly pg_dump stays in S3 before the lifecycle rule expires it."
  type        = number
  default     = 400
}

variable "snapshot_retention_count" {
  description = "How many nightly EBS snapshots DLM keeps."
  type        = number
  default     = 14
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "subnet_cidr" {
  type    = string
  default = "10.42.1.0/24"
}

variable "app_repository" {
  description = "Where cloud-init clones the application from."
  type        = string
  default     = ""
}
