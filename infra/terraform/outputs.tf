output "static_ip" {
  description = <<-EOT
    M12a's launch gate. Register this address with Zerodha against the API
    key, then record it in the application's configuration. Until both are
    done the engine refuses to place a live order, and says so rather than
    failing at the broker.
  EOT
  value = aws_eip.app.public_ip
}

output "instance_id" {
  description = "For `aws ssm start-session --target <id>`, which is the only way in."
  value       = aws_instance.app.id
}

output "shell" {
  description = "The whole command, so nobody goes looking for a key pair."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.app.id}"
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "backup_bucket" {
  value = aws_s3_bucket.backups.bucket
}

output "alerts_topic" {
  description = "Confirm the email subscription once; it stays pending until you do."
  value       = aws_sns_topic.alerts.arn
}

output "secrets_to_populate" {
  description = <<-EOT
    Created empty on purpose (M48): a value passed through Terraform is a
    value in the state file. Populate each once with
    `aws secretsmanager put-secret-value --secret-id <name> --secret-string ...`.
  EOT
  value = sort([for secret in aws_secretsmanager_secret.app : secret.name])
}

output "postback_url" {
  description = "Register this with Zerodha so order updates arrive (M25)."
  value       = "https://${var.domain_name}/orders/kite/postback/"
}

output "health_url" {
  description = "What the outside probe polls. 200 is healthy; 503 names the failing check."
  value       = "https://${var.domain_name}/health/"
}
