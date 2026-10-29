#
# M42 and M51: the logs have to outlive the instance, and somebody has to be
# told when a trading day goes wrong.
#
# Shipping is the point. An EBS snapshot does not contain the last hour, and
# the last hour is exactly what anybody asks about after an instance failure.
# journald captures the structured JSON the app writes to stdout and the
# CloudWatch agent ships it; every line carries the day's trace id, so one
# query reconstructs a day end to end (M45).
#

resource "aws_cloudwatch_log_group" "app" {
  name              = "/${var.name}/app"
  retention_in_days = var.log_retention_days
}

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "operator" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.operator_email

  # An email subscription is confirmed by clicking a link, which Terraform
  # cannot do. It sits at "pending confirmation" until the operator does, and
  # that is not drift -- but it is also not delivery, so the first apply is
  # not finished until the email is clicked.
}

#
# M51 names what is worth waking somebody for. Each of these is a metric
# filter over the shipped logs plus an alarm, rather than the application
# holding an SNS client: an alert that depends on the failing process being
# healthy enough to send it is an alert that goes missing exactly when it
# matters.
#
locals {
  alarms = {
    no_session = {
      pattern     = "{ $.message = \"*no valid Kite session*\" }"
      description = "No Kite session at the 09:00 check. The day will park (M6)."
    }
    order_rejected = {
      pattern     = "{ $.message = \"order rejected\" }"
      description = "The broker refused an order."
    }
    day_halted = {
      pattern     = "{ $.message = \"*halted*\" }"
      description = "A day stopped after repeated rejections. It will not resume by itself."
    }
    fallback_withheld = {
      pattern     = "{ $.message = \"*withheld*\" }"
      description = "The 15:00 market order was withheld: the market left the sanity band (M22a)."
    }
    divergence = {
      pattern     = "{ $.message = \"holdings and lots disagree\" }"
      description = "The broker and the lots disagree. Gains for that name are withheld (M31b)."
    }
    overspend = {
      pattern     = "{ $.message = \"*spent more than it was sized against*\" }"
      description = "A day overspent; tomorrow's budget carries the difference (D7)."
    }
    unbooked_fill = {
      pattern     = "{ $.message = \"*cannot cover it*\" }"
      description = "A sell filled against lots that do not account for it."
    }
    orphan_order = {
      pattern     = "{ $.message = \"*orders tagged by this system that it has no record of*\" }"
      description = "An order is resting at the broker that this system has no row for."
    }
    task_failed = {
      pattern     = "{ $.level = \"ERROR\" && $.logger = \"steadystack.*\" }"
      description = "Any unhandled error in a scheduled task."
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "alerts" {
  for_each = local.alarms

  name           = "${var.name}-${each.key}"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = each.value.pattern

  metric_transformation {
    name          = "${var.name}_${each.key}"
    namespace     = "steadystack"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "alerts" {
  for_each = local.alarms

  alarm_name        = "${var.name}-${each.key}"
  alarm_description = each.value.description

  namespace   = "steadystack"
  metric_name = "${var.name}_${each.key}"
  statistic   = "Sum"
  period      = 300

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1

  # A period with no matching line reports nothing at all, not zero. Treated
  # as "not breaching" so a quiet night is not an alarm.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

#
# The dead-man's switch. Every other alarm fires on something the system
# said; this one fires on the box itself failing, which is what nothing being
# said actually looks like.
#
# It is deliberately not "the log group went quiet". That alarm is unbuildable
# here: the longest legitimate silence is a weekend, from Friday's 15:45
# settlement to Sunday's 01:00 backup, and CloudWatch caps an alarm's
# evaluation window at 24 hours -- so any threshold long enough to survive a
# Saturday is longer than the window allows. The instance's own status checks
# answer the same question without needing the application to be well enough
# to speak.
#
resource "aws_cloudwatch_metric_alarm" "instance_health" {
  alarm_name        = "${var.name}-instance-unhealthy"
  alarm_description = "The instance is failing its status checks."

  namespace   = "AWS/EC2"
  metric_name = "StatusCheckFailed"
  dimensions  = { InstanceId = aws_instance.app.id }
  statistic   = "Maximum"
  period      = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 2
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

#
# And the day that never settled. Not a silence alarm either: it counts the
# settlements that did happen over a whole day, so a weekend's two zero days
# are indistinguishable from a broken Tuesday -- which is why the window is a
# day and the operator reads it against the calendar rather than being paged.
#
resource "aws_cloudwatch_metric_alarm" "no_settlement" {
  alarm_name        = "${var.name}-no-settlement"
  alarm_description = <<-EOT
    No day has settled in 24 hours. Expected over a weekend or an exchange
    holiday; on a trading day it means the 15:45 timer did not run or did not
    finish.
  EOT

  namespace   = "steadystack"
  metric_name = "${var.name}_settled"
  statistic   = "Sum"
  period      = 86400

  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_log_metric_filter" "settled" {
  name           = "${var.name}-settled"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.message = \"day settled\" }"

  metric_transformation {
    name          = "${var.name}_settled"
    namespace     = "steadystack"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "disk" {
  alarm_name        = "${var.name}-disk-filling"
  alarm_description = "The root volume is filling. Postgres lives on it."

  namespace   = "CWAgent"
  metric_name = "disk_used_percent"
  dimensions = {
    InstanceId = aws_instance.app.id
    path       = "/"
    fstype     = "xfs"
  }
  statistic = "Maximum"
  period    = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  evaluation_periods  = 2
  treat_missing_data  = "missing"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

#
# The outside probe. Every other alarm here is raised by something the box
# says; this one asks the box, from outside, every thirty seconds.
#
# /health/ answers 503 when something that should have happened by now has
# not (src/web/health.py): no Kite session after 09:00, no placing round in
# two minutes while an authorized day is placing, no close round through the
# close, the tick feed dead or deaf in market hours, the last trading day not
# settled. Each check knows when it applies, so a weekend or a paused system
# answers 200. A probe that cannot reach the box at all -- nginx down,
# gunicorn down, the instance gone -- fails the same way.
#
resource "aws_route53_health_check" "app" {
  fqdn              = var.domain_name
  port              = 443
  type              = "HTTPS"
  resource_path     = "/health/"
  request_interval  = 30
  failure_threshold = 3

  tags = { Name = "${var.name}-health" }
}

resource "aws_sns_topic" "alerts_us_east_1" {
  provider = aws.us_east_1
  name     = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "operator_us_east_1" {
  provider  = aws.us_east_1
  topic_arn = aws_sns_topic.alerts_us_east_1.arn
  protocol  = "email"
  endpoint  = var.operator_email
  # A second confirmation email, for this region's topic. Unconfirmed, the
  # probe's alarm fires into nothing.
}

resource "aws_cloudwatch_metric_alarm" "health_probe" {
  provider          = aws.us_east_1
  alarm_name        = "${var.name}-health-failing"
  alarm_description = <<-EOT
    /health/ is answering 503 or not answering at all. Open it: the "checks"
    object says which of session, rounds, close, feed or settled failed.
  EOT

  namespace   = "AWS/Route53"
  metric_name = "HealthCheckStatus"
  dimensions  = { HealthCheckId = aws_route53_health_check.app.id }
  statistic   = "Minimum"
  period      = 60

  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 2
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts_us_east_1.arn]
  ok_actions    = [aws_sns_topic.alerts_us_east_1.arn]
}

#
# The backup that never ran. backup.sh alerts when it fails; it cannot alert
# when the timer never started it. Backups run every day including weekends,
# so a UTC day without "backup uploaded" is always wrong.
#
resource "aws_cloudwatch_log_metric_filter" "backup_uploaded" {
  name           = "${var.name}-backup-uploaded"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.message = \"backup uploaded\" }"

  metric_transformation {
    name          = "${var.name}_backup_uploaded"
    namespace     = "steadystack"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "no_backup" {
  alarm_name        = "${var.name}-no-backup"
  alarm_description = "No nightly backup was uploaded in the last day."

  namespace   = "steadystack"
  metric_name = "${var.name}_backup_uploaded"
  statistic   = "Sum"
  period      = 86400

  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "memory" {
  alarm_name        = "${var.name}-memory-high"
  alarm_description = "Memory is nearly exhausted: Postgres, gunicorn and the feed share 2 GiB."

  namespace   = "CWAgent"
  metric_name = "mem_used_percent"
  dimensions  = { InstanceId = aws_instance.app.id }
  statistic   = "Maximum"
  period      = 300

  comparison_operator = "GreaterThanThreshold"
  threshold           = 90
  evaluation_periods  = 3
  treat_missing_data  = "missing"

  alarm_actions = [aws_sns_topic.alerts.arn]
}
