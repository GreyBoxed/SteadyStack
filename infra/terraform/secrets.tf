#
# M48. Terraform creates the *containers*; it never holds the values.
#
# This is deliberate and is the whole discipline. A secret written in a
# `.tf` file, a `.tfvars` file or a variable is a secret in the state file,
# and the state file is a plaintext JSON document somebody will eventually
# copy to a laptop. So each secret is created empty and populated
# out-of-band, once, with the AWS CLI -- see README.md -- and the version is
# ignored from then on so a later `apply` cannot blank it.
#
# The set is exactly what `.env.example` lists for a real host, because the
# box fetches these at boot and writes no environment file to disk.
#

locals {
  secrets = {
    django_secret_key  = "Django's SECRET_KEY, unique to this host."
    kite_api_key       = "Zerodha Kite Connect API key."
    kite_api_secret    = "Zerodha Kite Connect API secret; also verifies postback checksums."
    kite_token_key     = "Fernet key encrypting the daily access token at rest."
    kite_user_id       = "The operator's Zerodha client ID; a login for any other account is refused."
    database_url       = "The restricted runtime role's connection string."
    database_owner_url = "The schema owner's connection string, used only by migrate."
  }
}

resource "aws_secretsmanager_secret" "app" {
  for_each = local.secrets

  name        = "${var.name}/${each.key}"
  description = each.value

  # Long enough to undo a mistaken delete, short enough that a rotated
  # credential does not linger. A leaked credential is fixed by rotating it,
  # never by deleting the commit that leaked it.
  recovery_window_in_days = 7
}

# A placeholder so the secret exists and the instance's boot does not fail
# with "no version" on the very first apply. The real value is written once
# with the CLI, and `ignore_changes` keeps every later apply away from it.
resource "aws_secretsmanager_secret_version" "placeholder" {
  for_each = aws_secretsmanager_secret.app

  secret_id     = each.value.id
  secret_string = "REPLACE-ME-OUT-OF-BAND"

  lifecycle {
    ignore_changes = [secret_string]
  }
}
