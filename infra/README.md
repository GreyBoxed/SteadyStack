# The box

Phase 4. One EC2 instance in Mumbai running nginx, gunicorn, Django and a
local PostgreSQL, provisioned entirely from `terraform/` (M46, M47). Nothing
here has been applied: these files describe the box, and the first `apply`
is a deliberate act with a checklist, below.

## What is here

```
terraform/     the infrastructure: VPC, instance, elastic IP, IAM, secrets,
               backups, log shipping and alarms
systemd/       the schedule: two long-running services and eight timers
nginx/         the web tier, HTTPS only with HSTS
bin/backup.sh  the nightly pg_dump, which verifies before it uploads
```

The schedule those timers keep is the trading day the PRD describes:

```
09:00  session-check .... no valid Kite session -> alert, while there is
                          still time to log in
09:05  balance-check .... Zerodha's opening balance against the books -> alert
                          on a disagreement beyond a rupee
09:45  tradesheet ....... rank gaps, size the day
09:45  ladder ........... a round every 30s until 14:00, what each name owes
15:00  fallback ......... cancel what rests, then market in rounds to 15:25
15:45  eod .............. settle, reconcile, write the ledger row
16:15  settle-retry ..... settle again unless the row exists; also 17:30,
                          20:00 and 22:30 -- a live day must settle before
                          midnight, while Kite's trade book holds its trades
01:00  backup ........... pg_dump to S3, every day including weekends
boot   settle-catchup ... the latest closed trading day, if it never settled;
                          eod.timer is not persistent, so a caught-up run
                          never settles a day before it is planned
```

Every timer states `Asia/Kolkata` rather than inheriting the box's clock, and
every order-touching unit takes the same `flock` — which is one half of M24's
idempotency; the other half is the unique constraint on
`(trading_date, task, stream, tick_no)`, and neither is sufficient alone.

## Three things this deliberately does not do

**It holds no secret.** Terraform creates the Secrets Manager entries empty
and ignores their contents from then on. A secret passed through a variable
is a secret in the state file, and the state file is plaintext JSON that ends
up on a laptop. They are written once, out-of-band, with the CLI.

**It opens no SSH port.** There is no key pair, and nothing listens on 22.
Shell access is SSM Session Manager, which works outbound through the same
route the instance already needs for Kite (M50).

**It does not make the instance redundant.** A single box is a single point
of failure and the PRD says so. What makes that survivable is that orders are
DAY validity and expire at the close, and that startup reconciliation rebuilds
the day from Kite by tag. A second instance would place every order twice,
which is worse than the failure it prevents.

## First apply

The state backend is a chicken and egg: the bucket that holds the state has to
exist before Terraform can use it. Either create it by hand once and uncomment
the `backend` block in `versions.tf`, or run with local state and accept that
the state file is a thing to look after.

```sh
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # gitignored; edit it
terraform init
terraform plan -out=tf.plan                    # read it
terraform apply tf.plan
```

Then, in order:

1. **Populate the secrets.** They are created with a placeholder so the first
   boot does not fail on a missing version; the box will not work until they
   are real.

   ```sh
   aws secretsmanager put-secret-value --secret-id steadystack/django_secret_key \
       --secret-string "$(python -c 'import secrets;print(secrets.token_urlsafe(64))')"
   aws secretsmanager put-secret-value --secret-id steadystack/kite_token_key \
       --secret-string "$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
   # and kite_api_key, kite_api_secret, kite_user_id, database_url, database_owner_url
   ```

2. **Confirm the alert subscription.** AWS emails a link; until it is clicked
   the topic has no subscribers and M51's alarms fire into nothing.

3. **Point DNS at the elastic IP**, if `hosted_zone_id` was left empty, then
   re-run certbot on the box so nginx serves a real certificate.

4. **Register the static IP with Zerodha** against the API key, and record it
   in the application's configuration. This is M12a's launch gate: until both
   are done the engine refuses to place a live order, and says so rather than
   failing at the broker. `terraform output static_ip`.

5. **Register the postback URL** with Zerodha:
   `terraform output postback_url`.

6. **Test the restore.** Not "confirm the backup ran" — restore it, into a
   scratch database, and check the ledger reconciles:

   ```sh
   aws s3 cp s3://<bucket>/<key>.dump /tmp/t.dump
   createdb steadystack_restore_test
   pg_restore --no-owner --dbname steadystack_restore_test /tmp/t.dump
   # then: manage.py shell -c "from engine.ledger import reconcile; print(reconcile())"
   ```

   A backup nobody has restored is a hypothesis. Put it in the calendar.

7. **Leave the order mode on paper.** It is the default and it is not a
   staging step: a paper order is recorded exactly as a live one and fills
   against the live tick feed. Going live is a separate, audited configuration
   change that M19b refuses unless step 4 is complete.

## Getting a shell

```sh
terraform output shell        # prints the whole command
aws ssm start-session --region ap-south-1 --target i-...
```

## What is not covered here

Log *retention* is set; log *review* is not automated beyond the alarms in
`observability.tf`. The alarms match on the messages the application emits, so
a log line reworded in `src/` without a change here quietly stops alerting —
which is the usual failure of metric filters and is worth a grep when
messages change.
