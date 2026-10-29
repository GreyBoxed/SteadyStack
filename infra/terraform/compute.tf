#
# The box. One instance, chosen deliberately over a container or serverless
# topology: one operator, one portfolio, about sixty scheduled ticks a day.
#
# The known trade-off is stated in the PRD §9 and is not solved here. If this
# instance dies mid-ladder, every order the day has placed so far is resting
# at the broker with nothing watching it. What makes that survivable is not
# redundancy: it is that the orders are DAY validity and expire at the close,
# and that startup reconciliation rebuilds the day from Kite by tag. Adding a
# second instance would add a split brain placing orders twice, which is
# worse than the failure it prevents.
#

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.*-arm64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  # No key pair. There is nothing to SSH into and no key to lose (M50).
  user_data                   = data.cloudinit_config.app.rendered
  user_data_replace_on_change = false

  metadata_options {
    http_endpoint = "enabled"
    # IMDSv2 only. v1 is a plain GET, so any server-side request forgery in
    # the app reads the instance role's credentials; v2's PUT-for-a-token
    # cannot be reached the same way.
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true

    tags = {
      Name = "${var.name}-root"
      # What DLM looks for. Without this tag the nightly snapshot silently
      # covers nothing, which is the failure mode a backup has.
      Snapshot = var.name
    }
  }

  lifecycle {
    # A new AMI is published most weeks and is not a reason to replace the
    # box underneath a live portfolio. Replacing it is a deliberate act:
    # taint it, or bump the AMI with the ladder stopped.
    ignore_changes = [ami, user_data]
  }

  tags = { Name = var.name }
}

#
# Boot. Everything the instance needs to know that is not a secret; the
# secrets it fetches for itself with the instance role, and writes to a
# root-owned file that never leaves tmpfs (M48).
#
data "cloudinit_config" "app" {
  gzip          = true
  base64_encode = true

  part {
    content_type = "text/x-shellscript"
    filename     = "bootstrap.sh"
    content = templatefile("${path.module}/templates/bootstrap.sh.tftpl", {
      region         = var.region
      name           = var.name
      domain_name    = var.domain_name
      operator_email = var.operator_email
      log_group      = aws_cloudwatch_log_group.app.name
      backup_bucket  = aws_s3_bucket.backups.bucket
      alerts_topic   = aws_sns_topic.alerts.arn
      app_repository = var.app_repository
      secret_prefix  = var.name
    })
  }
}

#
# DNS. Optional: an operator who keeps their zone elsewhere points the record
# at the elastic IP by hand, and everything else here is unaffected.
#
resource "aws_route53_record" "app" {
  count = var.hosted_zone_id == "" ? 0 : 1

  zone_id = var.hosted_zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}
