#
# M49. Nightly pg_dump to S3, and the lifecycle that keeps it affordable.
#
# Versioned, because the failure this is insurance against includes "the dump
# ran and wrote a corrupt file over yesterday's". Encrypted, blocked from the
# public internet by every switch AWS offers, and refusing plaintext requests
# outright. The instance can write and cannot read or delete (see iam.tf), so
# a compromise of the box does not take the backups with it.
#
# Restore must be tested rather than assumed. README.md has the procedure and
# it is a calendar item, not a comment.
#

resource "aws_s3_bucket" "backups" {
  bucket = "${var.name}-backups-${data.aws_caller_identity.current.account_id}"

  # A bucket holding the only copy of the ledger should not be removable by a
  # stray `destroy`.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket = aws_s3_bucket.backups.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyPlaintextTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.backups.arn,
        "${aws_s3_bucket.backups.arn}/*",
      ]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket     = aws_s3_bucket.backups.id
  depends_on = [aws_s3_bucket_versioning.backups]

  rule {
    id     = "age-out-the-dumps"
    status = "Enabled"

    filter {}

    # Warm for a month, because a restore anybody actually needs is a restore
    # of something recent. After that it is a regulatory-shaped copy and can
    # take hours to retrieve.
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }

    expiration {
      days = var.backup_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

#
# The other half of M49: scheduled EBS snapshots, so a restore can be the whole
# box rather than a database into a machine somebody has to rebuild first. DLM
# runs these; nothing on the instance has permission to.
#
resource "aws_iam_role" "dlm" {
  name = "${var.name}-dlm"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "dlm.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "snapshots" {
  description        = "${var.name}: nightly root-volume snapshots"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]

    target_tags = {
      Snapshot = var.name
    }

    schedule {
      name = "nightly"

      create_rule {
        # 19:30 UTC is 01:00 IST: the day has settled, the ledger row is
        # written, and nothing is going to touch the database for hours.
        cron_expression = "cron(30 19 * * ? *)"
      }

      retain_rule {
        count = var.snapshot_retention_count
      }

      copy_tags = true
    }
  }
}
