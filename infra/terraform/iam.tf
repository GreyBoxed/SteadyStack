#
# What the box is allowed to do, and nothing else.
#
# Four things: be reached by SSM, read its own secrets, ship its own logs, and
# write its own backups. Each policy names the exact resources rather than a
# wildcard, because the whole reason the credentials live in Secrets Manager
# is that a compromise of this instance should not hand over the broker key
# for every environment at once.
#

data "aws_caller_identity" "current" {}

resource "aws_iam_role" "app" {
  name = "${var.name}-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
      # No aws:SourceAccount condition: EC2 does not document setting it when
      # it assumes an instance profile's role, and a condition it never
      # satisfies is a box with no credentials -- no secrets, no logs. Only
      # this account can attach the profile to an instance in the first place.
    }]
  })
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.name}-instance"
  role = aws_iam_role.app.name
}

# M50: this is the shell. Nothing opens port 22.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# M48: read the credentials at boot, over the instance role. Scoped to this
# system's secrets by ARN -- never `secretsmanager:*` on `*`.
resource "aws_iam_role_policy" "secrets" {
  name = "${var.name}-read-secrets"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
      ]
      Resource = [for secret in aws_secretsmanager_secret.app : secret.arn]
    }]
  })
}

# M42: logs must survive the instance, so the agent needs to be able to ship
# them. Creating streams is allowed; creating groups is not, because the
# groups and their retention are Terraform's to decide.
resource "aws_iam_role_policy" "logs" {
  name = "${var.name}-ship-logs"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams",
        ]
        Resource = "${aws_cloudwatch_log_group.app.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "*"
      },
      # The agent's disk and memory metrics, which the disk alarm reads.
      # PutMetricData takes no resource; the namespace is the fence.
      {
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "CWAgent" } }
      },
    ]
  })
}

# M49: write a nightly dump, and never read or delete one. A box that can
# delete its own backups is a box whose backups a compromise takes with it;
# retention is the bucket's lifecycle rule, not the instance's business.
resource "aws_iam_role_policy" "backups" {
  name = "${var.name}-write-backups"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject"]
      Resource = "${aws_s3_bucket.backups.arn}/*"
    }]
  })
}

# M51's transport. The box publishes an alert; it cannot subscribe anybody to
# the topic or read who is on it.
resource "aws_iam_role_policy" "alerts" {
  name = "${var.name}-raise-alerts"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sns:Publish"
      Resource = aws_sns_topic.alerts.arn
    }]
  })
}
