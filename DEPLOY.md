# Deploying SteadyStack

Provisioning the production box, first boot, shipping code to it, and going
live. Application setup after first boot is in [`SETUP.md` part 2](SETUP.md#part-2--production);
running it day to day is [`OPERATIONS.md`](OPERATIONS.md).

> **Status.** Phase 4. The Terraform, systemd units, nginx config and backup
> script exist in `infra/` and have **never been applied**. No live order has
> ever been sent. §2 lists the steps outside the repository.

---

## 1. What gets built

```
                         Zerodha Kite (REST + websocket + postbacks)
                                   ▲            │
                    elastic IP ────┘            ▼ HTTPS 443
┌──────────────────────── EC2 t4g.small, AL2023 arm64, ap-south-1 ────────────────────────┐
│ nginx :80/:443 (Let's Encrypt, HSTS) ──► gunicorn 127.0.0.1:8000 (3 workers, Django)     │
│ steadystack-ticker.service  — the Kite tick feed; fills paper orders                         │
│ systemd timers          — session-check, tradesheet, ladder, fallback, eod, backup       │
│ PostgreSQL 16, localhost only — two roles: steadystack_owner (migrate), steadystack_app (runtime)│
│ /run/steadystack/env        — secrets from Secrets Manager, tmpfs, rewritten at every boot   │
│ CloudWatch agent        — ships /var/log/steadystack/*.log                                   │
└──────────────────────────────────────────────────────────────────────────────────────────┘
   S3 (nightly pg_dump, versioned, write-only for the box)   DLM (nightly EBS snapshots)
   CloudWatch Logs (365 days) + metric-filter alarms ──► SNS ──► operator email
```

| Terraform file | Creates |
|---|---|
| `network.tf` | VPC `10.42.0.0/16`, one public subnet, internet gateway, security group (443 and 80 in; nothing else — **no 22**), the **elastic IP** (`prevent_destroy`) |
| `compute.tf` | The instance (AL2023 arm64, gp3 encrypted root, IMDSv2 only, no key pair), cloud-init running `templates/bootstrap.sh.tftpl`, optional Route 53 A record |
| `iam.tf` | Instance role: SSM, read its own secrets, write-only to the backup bucket, CloudWatch agent |
| `secrets.tf` | Seven Secrets Manager entries, created with a placeholder and **never** holding real values in Terraform |
| `storage.tf` | Backup bucket (versioned, encrypted, TLS-only, lifecycle: IA at 30 d, Glacier IR at 90 d, expire at 400 d, `prevent_destroy`), DLM nightly snapshots (14 kept) |
| `observability.tf` | Log group `/steadystack/app` (365 d), SNS topics + email subscriptions (ap-south-1, and us-east-1 for the probe), metric-filter alarms, a Route 53 health check on `/health/` every 30 s, no-backup, disk and memory alarms |

Why one box and not two: orders are DAY validity and expire at the close, and
startup reconciliation rebuilds the day from Kite by tag. A second instance
would place every order twice.

Rough monthly cost: t4g.small, 30 GB gp3, an elastic IP, a few GB of
S3/CloudWatch, plus the Kite market-data subscription (₹500).

---

## 2. Before the first apply: what is not code

The gaps found reading `infra/` against the application are fixed in the
repository: gunicorn is a dependency and finds `config` under `src/`;
bootstrap sets both role passwords from the secrets, applies the grants and
switches the loopback to `scram-sha-256`; `migrate` is non-fatal on a first
boot with placeholder secrets; the order lock lives in
`/var/lib/steadystack/orders.lock`, which every unit can write and none wraps in
a second `flock`; `refresh_instruments` runs before the 09:45 plan and
`snapshot_valuation` after the settle; `steadystack-env.service` rewrites the
environment at every boot; `rebalance_tick` runs the sell ladder inside the
ladder and fallback units; nginx serves only the ACME challenge until a
certificate exists; every log file is shipped on its own.
`src/config/tests/test_box_wiring.py` and `test_order_lock_topology.py` pin
each of these.

What remains is outside the repository and has to be done by hand:

| # | Step | Where |
|---|---|---|
| 1 | Set the Kite Connect app's redirect URL to `https://<domain>/broker/kite/callback/` -- or create a second Kite app for production, since an app holds one redirect URL and the laptop needs its own | developers.kite.trade |
| 2 | Register the postback URL `https://<domain>/orders/kite/postback/` | developers.kite.trade |
| 3 | Whitelist the Elastic IP (`terraform output static_ip`) against the API key | developers.kite.trade |
| 4 | Populate the seven secrets (§4), then rerun bootstrap (§5.2) | AWS CLI |
| 5 | Point DNS at the Elastic IP if there is no hosted zone (§5.3) | your DNS host |
| 6 | Confirm both alert subscriptions (§5.4) | email |
| 7 | Test a restore before the first live day (OPERATIONS.md §9) | the box |

---

## 3. Provision

### 3.1 Prerequisites

- An AWS account with admin rights for the first apply, and the AWS CLI v2
  configured (`aws sts get-caller-identity` works) with the Session Manager
  plugin installed.
- Terraform ≥ 1.9.
- A domain name you control (Route 53 zone optional).
- The repository reachable from the box if `app_repository` is set (a
  private GitHub repo needs a deploy key or token — bootstrap does a plain
  `git clone`). Leaving it empty and deploying by hand (§6) is simpler.

### 3.2 State backend

The state holds the instance id, the elastic IP and every ARN (not secrets).
Keep it somewhere versioned and locked:

```sh
aws s3api create-bucket --bucket steadystack-tfstate --region ap-south-1 \
    --create-bucket-configuration LocationConstraint=ap-south-1
aws s3api put-bucket-versioning --bucket steadystack-tfstate \
    --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket steadystack-tfstate \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

Then uncomment the `backend "s3"` block in `infra/terraform/versions.tf`.
(Or keep local state and look after the file.)

### 3.3 Variables

```sh
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars      # gitignored
```

| Variable | Default | Set it to |
|---|---|---|
| `region` | `ap-south-1` | Keep — latency to Zerodha |
| `name` | `steadystack` | Prefix for every resource and secret |
| `domain_name` | — (required) | e.g. `steadystack.example.com`; becomes `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` |
| `hosted_zone_id` | `""` | Your Route 53 zone, or empty to manage DNS elsewhere |
| `operator_email` | — (required) | Where alarms go |
| `app_repository` | `""` | Git URL to clone at boot, or empty to deploy by hand |
| `instance_type` | `t4g.small` | |
| `root_volume_gb` | `30` | Holds the database |
| `log_retention_days` | `365` | |
| `backup_retention_days` | `400` | |
| `snapshot_retention_count` | `14` | |

**Nothing secret goes in `terraform.tfvars`.**

### 3.4 Apply

```sh
terraform init
terraform plan -out=tf.plan      # read it
terraform apply tf.plan
terraform output                 # static_ip, instance_id, shell, backup_bucket, postback_url, secrets_to_populate
```

---

## 4. Secrets

Terraform created seven secrets named `steadystack/<key>` holding
`REPLACE-ME-OUT-OF-BAND`. Write each once with the CLI; later applies ignore
their values.

| Secret | Value |
|---|---|
| `steadystack/django_secret_key` | `python -c 'import secrets;print(secrets.token_urlsafe(64))'` |
| `steadystack/kite_token_key` | `python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'` |
| `steadystack/kite_api_key` | From the Kite Connect app |
| `steadystack/kite_api_secret` | From the Kite Connect app |
| `steadystack/kite_user_id` | Your Zerodha client ID (e.g. `AB1234`). A Kite login for any other account is refused, so a stray request token cannot swap the session to someone else's account |
| `steadystack/database_url` | `postgres://steadystack_app:<app-password>@127.0.0.1:5432/steadystack` |
| `steadystack/database_owner_url` | `postgres://steadystack_owner:<owner-password>@127.0.0.1:5432/steadystack` |

```sh
aws secretsmanager put-secret-value --region ap-south-1 \
    --secret-id steadystack/django_secret_key \
    --secret-string "$(python -c 'import secrets;print(secrets.token_urlsafe(64))')"
# ...one call per secret
```

Generate the two database passwords yourself. Bootstrap reads them back out
of these two URLs and sets them on the roles, so there is nothing else to
keep in step. Avoid characters that need URL-escaping, or escape them.

The box keeps two environment files in tmpfs, rewritten at every boot by
`steadystack-env.service` (`infra/bin/write-env.sh`): `/run/steadystack/env`, which
every unit reads and which holds only the runtime role's URL, and
`/run/steadystack/owner.env`, root's alone, holding the schema owner's. The web
process never has the owner URL. After changing a secret, run
`sudo systemctl restart steadystack-env steadystack-web steadystack-ticker`.

Losing `kite_token_key` only loses the stored daily session — log in to Kite
again. Changing `django_secret_key` signs everyone out.

---

## 5. First boot

### 5.1 What bootstrap does

`templates/bootstrap.sh.tftpl`, via cloud-init, idempotent:

1. Waits for a route out (the Elastic IP attaches after the instance runs).
2. On the first run only: `dnf update` and the packages (nginx,
   postgresql16, python3.12, the CloudWatch agent, AWS CLI, jq). A rerun never
   upgrades packages under a running day. Starts the SSM agent.
3. Creates the `steadystack` system user and `/opt/steadystack`, `/run/steadystack`,
   `/var/lib/steadystack` (the order lock) and `/var/log/steadystack`, and a
   logrotate rule for the logs.
4. Initialises PostgreSQL, listening on `localhost` only; creates the two
   roles and the `steadystack` database, and grants the runtime role its reach.
5. Writes `/etc/steadystack/bootstrap.env` (region, secret prefix, domain,
   bucket, topic -- nothing secret); clones `app_repository` if set; runs
   `infra/bin/write-env.sh`, which writes `/run/steadystack/env` and
   `/run/steadystack/owner.env` from Secrets Manager (§4).
6. Sets both role passwords from the database URLs and switches the loopback
   to `scram-sha-256` -- skipped, with a note, while they are placeholders.
7. `uv sync --frozen --no-dev`; `migrate` as the owner; `collectstatic`.
   Neither is fatal.
8. Installs every unit from `infra/systemd/`; enables `steadystack-env` (boot),
   restarts `steadystack-web` and `steadystack-ticker`, enables the eight timers
   (`tradesheet`, `ladder`, `fallback`, `eod`, `settle-retry`,
   `session-check`, `balance-check`, `backup`), and `settle-catchup` for the
   next boot: it settles the previous trading day if that day never did.
9. Serves the ACME challenge on port 80 (`infra/nginx/steadystack-http.conf`),
   obtains the certificate with `certbot certonly --webroot`, then installs
   `infra/nginx/steadystack.conf` -- deferred, with a note, until DNS points here.
   Installs certbot from dnf or its own virtualenv, and a renewal timer.
10. Configures the CloudWatch agent to ship each log file on its own stream,
    and disk/memory metrics.

Watch it: `sudo tail -f /var/log/cloud-init-output.log` over SSM.

### 5.2 Re-running bootstrap

`steadystack-env.service` rewrites the environment at every boot, so a reboot
needs nothing. Rerun bootstrap after populating the secrets for the first
time, after pointing DNS at the box, or after anything in it failed:

```sh
sudo ls /var/lib/cloud/instance/scripts/      # cloud-init keeps the rendered script here
sudo bash /var/lib/cloud/instance/scripts/<the bootstrap script>
```

After rotating a secret, bootstrap is not needed:
`sudo systemctl restart steadystack-env steadystack-web steadystack-ticker`.

### 5.3 DNS and TLS

- With `hosted_zone_id` set, the A record already points at the elastic IP.
- Otherwise create `A <domain> → terraform output static_ip` at your DNS host.
- Then rerun bootstrap (§5.2): it obtains the certificate through the
  webroot and installs the HTTPS config. Renewal is a twice-daily timer that
  reloads nginx.

### 5.4 Confirm the alert subscriptions

AWS emails `operator_email` **two** confirmation links: the ap-south-1 topic
(every log-based alarm) and the us-east-1 topic (the outside probe on
`/health/` — Route 53 publishes health-check metrics only in us-east-1). Until
both are clicked, those alarms fire into nothing.

The probe starts failing the moment it is created if DNS, TLS or the app is
not up yet; expect a burst of `health-failing` emails during first boot, and
one "OK" once the box answers 200.

### 5.5 Verify the box

```sh
systemctl status steadystack-web steadystack-ticker nginx postgresql
systemctl list-timers 'tradesheet*' 'ladder*' 'fallback*' 'eod*' 'settle-retry*' \
    'session-check*' 'balance-check*' 'backup*'
curl -s https://<domain>/health/                # 200 and "ok"; 503 names the failing check
tx preflight                                    # see SETUP.md §2.1 for `tx`
sudo tail -n 50 /var/log/steadystack/web.log
```

Then do the application setup in [`SETUP.md` §2.2](SETUP.md#22-application-setup-on-the-box).

---

## 6. Shipping code

There is no CI/CD and no deploy script. A deploy is a pull, a sync, a migrate
and a restart, done over SSM.

**When:** outside market hours (before 09:00 or after 16:00 IST), or on a
non-trading day. Never between 09:45 and 15:45 — the ladder and the close
must not change underneath a day. If you must, press **Stop trading** first
(OPERATIONS.md §6).

```sh
# on your machine: everything committed, tests green, pushed
uv run pytest && git push

# on the box, as root via SSM
cd /opt/steadystack
sudo -u steadystack git fetch origin
sudo -u steadystack git log --oneline HEAD..origin/master        # what is coming
sudo -u steadystack git merge --ff-only origin/master
sudo -H -u steadystack python3.12 -m uv sync --frozen --no-dev
txm migrate                     # as the schema owner (SETUP.md §2.1)
tx collectstatic --noinput
tx preflight

# units or nginx changed?
sudo install -m 0644 infra/systemd/*.service infra/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
# a unit that is new to this box is installed but not enabled: enable it,
# or rerun bootstrap (§5.2), which enables every unit. Currently:
sudo systemctl enable settle-catchup.service     # at the next boot, not now
sudo systemctl enable --now settle-retry.timer
sed "s/__DOMAIN__/<domain>/g" infra/nginx/steadystack.conf | sudo tee /etc/nginx/conf.d/steadystack.conf >/dev/null
sudo nginx -t && sudo systemctl reload nginx

sudo systemctl restart steadystack-web steadystack-ticker
curl -sI https://<domain>/health/
```

Notes:

- `app.css` is committed; the box never runs Node or `npm`.
- Static files are served by nginx from `/opt/steadystack/staticfiles/`, which
  `collectstatic` fills (the app's files and the admin's).
- Migrations run as the schema owner. Treat them as forward-only: the
  append-only tables are guarded by triggers and revoked privileges, and
  unwinding a migration on a production ledger is a restore decision, not a
  `migrate <app> <previous>`.

### 6.1 Rolling back

```sh
cd /opt/steadystack
sudo -u steadystack git log --oneline -10
sudo -u steadystack git checkout <previous-good-sha>      # detached; fine for an emergency
sudo -u steadystack python3.12 -m uv sync --frozen --no-dev
sudo systemctl restart steadystack-web steadystack-ticker
```

If the bad release ran a migration, rolling the code back is not enough.
Restore from the last dump into a scratch database, compare, and decide —
see OPERATIONS.md §9. Do not reverse migrations on the append-only tables.

### 6.2 Replacing the instance

The instance ignores AMI and user-data changes on purpose. To rebuild:

1. Stop trading, let the day settle, take a manual backup (`sudo systemctl
   start backup.service`).
2. `terraform taint aws_instance.app && terraform apply`. The elastic IP is
   separate and survives, so the Zerodha registration still holds.
3. Populate nothing new — the secrets are unchanged. Re-run bootstrap if the
   first boot failed on anything.
4. Restore the latest dump into the new database (OPERATIONS.md §9), then
   `txm migrate`, `tx preflight`.

---

## 7. Paper in production

Run the box in paper mode for a meaningful stretch — at least a couple of
weeks of full trading days — before going live. Paper is not a staging mode:
a paper order is recorded exactly as a live one, through the same state
machine and tables, and fills against the live feed. What paper cannot prove
is a live order's acceptance, the postback path and a real contract note.

Check daily (OPERATIONS.md §2):

- every scheduled timer ran (`journalctl -u ladder.service --since today`),
- the ledger row settled and reconciles,
- no alarm fired unexpectedly, the backup landed in S3.

---

## 8. Going live

This section is the go-live procedure; OPERATIONS.md §10 points here. Do the
steps in order. Code enforces the ones it can: the switch to live in step 8
is refused, and says why, unless the registered static IP is recorded and
matches the address the box calls from, the live book's opening entry exists
(step 6), and no paper day is in progress (planned, funded or with an order
open, and not yet settled). With the order mode live, `tx preflight` marks a
missing opening entry `!`.

**Beforehand**, at any time before go-live day:

- **Register the static IP with Zerodha** against the API key
  (`terraform output static_ip`) at developers.kite.trade → your app → IP
  whitelist. Order requests from any other address are rejected by Zerodha.
- **Register the postback URL** on the same app: `terraform output
  postback_url` → `https://<domain>/orders/kite/postback/`.
- **Test a restore** (OPERATIONS.md §9), including the privilege check in
  the verification list below (item 10). A backup nobody has restored is a
  hypothesis.

**The checklist.** Outside market hours, ideally the evening before go-live
day, after that day's paper settle:

1. **Back up** the paper database and keep the dump: `sudo systemctl start
   backup.service`, then confirm the object landed in S3.
2. **Recreate the database.** Do it after the 22:30 settle retry, or expect
   retry alarms: the new database has no tradesheet for the day, so every
   `settle-retry` run after the recreate settles nothing. While it holds no
   tradesheet at all `settle_day` logs that at INFO, and the boot catch-up
   reports it and exits cleanly; any `task_failed` from a retry between the
   recreate and midnight is this. Stop the services and timers, then drop
   and recreate `steadystack` owned by `steadystack_owner`. The runtime role's default
   privileges are stored in the database that was just dropped, so grant
   them again on the new one, as `postgres`, **before** migrating -- the
   migrations' REVOKEs then narrow real grants on the append-only tables:

   ```sh
   sudo -u postgres psql -v ON_ERROR_STOP=1 --dbname steadystack <<'SQL'
   GRANT CONNECT ON DATABASE steadystack TO steadystack_app;
   GRANT USAGE ON SCHEMA public TO steadystack_app;
   ALTER DEFAULT PRIVILEGES FOR ROLE steadystack_owner IN SCHEMA public
       GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO steadystack_app;
   ALTER DEFAULT PRIVILEGES FOR ROLE steadystack_owner IN SCHEMA public
       GRANT USAGE, SELECT ON SEQUENCES TO steadystack_app;
   SQL
   ```

   These are bootstrap's own four statements. Never grant on `ALL TABLES` after
   the tables exist, and never point `DATABASE_URL` at the owner to get past
   a "permission denied": either hands UPDATE and DELETE back on the journal,
   the cash ledger and the audit tables. Then, in SETUP.md §2.2's order:
   `txm migrate` -- and check the runtime role's privileges now
   (verification item 12) -- `tx createsuperuser` (the user table went with
   the old database), `tx enrol_totp <username>`, `tx load_calendar`; start
   the services and timers again; sign in, **Settings → Kite session →
   Connect**; `tx refresh_instruments`; then the model portfolio and the
   configuration -- including *registered static IP*, with a reason, and
   order mode still `paper`. The paper history is not carried over: lots, gains and the
   valuation series of paper days are not real money, and the append-only
   tables cannot be cleaned up in place.
3. **Import the real holdings**: `tx import_opening_positions` (one row per
   purchase, from the tradebook or contract notes), or `tx
   import_opening_positions --empty` if the account holds none of the model
   names. This is the adult's own demat, so **the account must hold nothing
   but the portfolio, or every holding in it is imported**. A model name
   held personally counts as the portfolio's in ranking and in the
   divergence scan, so either import it or leave the name out of the model
   portfolio. A holding with no lot that is not a model name is not
   compared: Records → Divergences lists it under "Held, but not the
   portfolio's" instead of opening a divergence, since every classification
   of one would post it into the live book. A name that is later added to
   the model portfolio is compared from then on, so import it first. It
   must run before step 6: the opening entry takes the cost of every lot
   acquired on or before its date.
4. **Log in to Kite, sync holdings, scan.** Kite session → Connect (again,
   if step 2's session has lapsed -- it expires each morning); a
   holdings sync (Generate on Today does one, or `tx shell -c "from broker
   import get_broker, session; from portfolio import holdings; b=get_broker();
   session.attach(b); holdings.sync(b.holdings())"`); then confirm the
   divergence scan opens nothing (Records → Divergences).
5. **Verify the margins field** -- verification list item 1 below. The
   09:05 balance check reads it every morning from go-live on.
6. **Open the books**: `tx open_books --on <the last trading day before
   go-live> --cash <that day's closing funds balance> --actor <you>`. The
   closing balance is Console → Funds → Statement's balance at the end of
   that day, which is also go-live morning's opening balance, so the first
   09:05 check agrees by construction. The entry posts only the difference
   between `--cash` and what the live book already records, plus the cost
   of every imported lot. A second call is refused; if the opening was
   wrong, reverse it on Books and run `open_books` again. From here on the
   live book refuses any real event dated on or before `--on` ("… is on or
   before the opening balance of …; the live books begin there"): `--cash`
   already holds what Zerodha's cash saw by then. **Record no lumpsum
   between this step and step 8**: the order mode is still paper, so its
   allocation would fund the paper engine while the money is live. The
   lumpsum form says so while the window is open. Money credited on or
   before `--on` is not a lumpsum: it is float inside the opening balance,
   and the live book refuses it as one. Only money credited after `--on` is
   a lumpsum, and it is recorded after the switch, dated when it arrived.
   If the owner meant earlier money for the engine, record a lumpsum after
   the switch for a fresh transfer of that amount.
7. **`tx preflight`** -- clean: nothing marked `!`. `tx` runs as
   `steadystack_app`, the runtime role, which is what the "runtime role" row
   checks: SELECT and INSERT held on every append-only table, and each
   privilege a migration revoked refused. A `!` there means step 2's grants
   are missing or the runtime URL names the wrong role. Also show no
   "settlement history" `~` line: it names any tradesheet dated before
   today whose own book has no `CashLedger` row -- paper-era history most
   likely, left over from before this recreate -- and the boot catch-up
   only ever reaches one trading day back, so an older gap would sit
   unsettled through the switch. Settle each named day
   (`settle_day --date <day> --retry`, OPERATIONS.md §1) before step 8.
8. **Switch order mode to live** in Settings → Configuration, with a reason
   and a current authenticator code: after the previous day's settle and
   before 09:45 on go-live day. A mode is fixed on each order when it is
   placed, and a day's budget is allocated in the book of the mode that
   planned it, so the switch is refused while a paper day is in progress.
   **If step 8 cannot happen before the next 09:45** -- the IP is not yet
   whitelisted, preflight shows a `!` -- pause (Settings → pause) before
   09:45 and stay paused until it can. A paper day run against the imported
   lots would open divergences for the names held in them, and classifying
   one posts to the now-open live book.

Live rebalance sells are not part of go-live: executing a batch stays
refused in live mode until live sale fills are recorded (the sale's fills,
its lots and its reinvested proceeds). Sell at the broker and record it as
an external transaction instead -- that is, classify the divergence the
sale opens that evening (Records → Divergences) as a sale made at the
broker; classifying is the only way an external transaction is recorded.

**Go-live verification.** Things only the real account can prove. Each
names when it is done and what shows it was:

| # | What | When | What proves it |
|---|---|---|---|
| 1 | **The margins field.** Log in to Kite and run `tx shell -c "from broker import get_broker, session; import json; b=get_broker(); session.attach(b); print(json.dumps(b.margins()['equity'], default=str, indent=1))"`. Compare `available.opening_balance`, `available.cash` and `available.live_balance` with that morning's Console → Funds → Statement opening balance. If it is not `opening_balance`, change `journal.checks.zerodha_opening_balance` to the field that matches. **And `live_balance`**, which caps a live day's plan (`engine/planning.py`, `cash_available`): before any trade that morning it must equal the same opening balance, and it must rise with an intraday payin. If it does not, change the field `cash_available` reads to the one that does -- absent, the cap falls back silently to the books' own Zerodha cash, which the sheet's `cash_cap` source then names | Step 5, in paper mode, before `open_books` | The `user.margins` `BrokerCall` row beside the Console statement's opening balance for the same morning, `live_balance` included |
| 2 | **The trade-book response shape.** `record_fills` reads `trade_id`, `order_id`, `quantity`, `average_price` and `fill_timestamp`. Read the `order.trades` `BrokerCall` rows for the first filled order and confirm those keys; confirm `Fill.kite_trade_id` matches Kite's trade ids and the lot's cost matches the contract note | First live day, before 15:45 | Fills and lots for the day's filled orders by 15:25 (each round records them), and the day's `CashLedger` row that evening. Stay available until the row exists or midnight (OPERATIONS.md §7). **And `Order.exchange_order_id` set on each order after the first round**, not only after the settle: a Console tradebook import matches an order the box never saw complete by that number (or by the day's one order of its symbol and side) |
| 3 | **Settlement posting date.** Upload the next Console statement after the first live trade. The "Net settlement … settlement number N" line's `posting_date` should equal the trade date and auto-link to that date's `settlement` entry, within paise. If it is the next day, record that and swap the order of `match_for`'s date loop in `journal/statement/reconcile.py` | The first statement upload after the first live trade | The line linked to the trade date's settlement entry, with no "Settlements that disagree" row |
| 4 | **UPI reference text.** Record the first live lumpsum with the reference exactly as the bank app shows it; after the statement upload, check it matched *by reference* rather than by the nearby-amount fallback. If not, note the reference Zerodha prints and change the lumpsum form's help text to ask for that | The first lumpsum after go-live, and its statement | The statement line matched to the lumpsum's contribution entry by its reference |
| 5 | **Real Console CSV shape.** Every statement fixture is synthetic. Download a real funds-ledger CSV, upload it to a scratch database (never production: the chain and the fingerprints are permanent), confirm it parses and every spec §7.2 classification is proposed as expected. Keep the file out of git | Before the first production upload | A scratch upload that parses, with the expected proposals |
| 6 | **Owner-payout and Kite Connect fee wording.** Confirm the classification by hand the first time each appears; add the observed text to `journal/statement/classify.py` RULES | The first statement carrying each | A RULES entry for each observed wording |
| 7 | **Charges against a real contract note.** Compare the `buy_fill` component lines with the contract note. The rates in `portfolio/charges.py` were last confirmed on 22 September 2026. **Also note whether STT and stamp duty are rounded per scrip (or per note) rather than per fill**: `charges.estimate` rounds each component per fill, so a note that rounds per scrip leaves paise a day in the Zerodha cash until the statement's settlement correction, and the 09:05 check's one-rupee tolerance may trip most mornings. Record what the note does; change the rounding in `orders/booking` (per order-day) only once that is seen | First live day, when the contract note arrives | Per-component agreement within paise, or a correction to `charges.py` and its pinning tests; and the note's STT and stamp rounding written down |
| 8 | **Quarterly sweep dates.** Put each quarter's first-Friday settlement date in the operator's calendar. On each: upload the statement that evening, confirm the sweep, and transfer the carry owed back before the next session -- or skip the day (Tradesheet → skip) if the account has not been re-funded | Before go-live, then every quarter | The calendar entries; after each sweep, Today's funding warning cleared |
| 9 | **Budget session.** Confirm whether NSE traded on Sunday 1 February 2026; if so, add it to `ops/calendar/nse-2026.csv`, which the settlement-number evidence depends on | Before go-live | The calendar CSV, or a note that the session did not trade |
| 10 | **Restore test with privileges.** Restore the latest dump to a scratch database and confirm, as `steadystack_app`, that `UPDATE journal_journalentry SET description = '' WHERE false` is refused -- which the test suite, running as the owner, cannot show -- **and that `SELECT 1 FROM journal_journalentry LIMIT 1` succeeds**: a refused UPDATE alone cannot tell a surviving REVOKE from grants that are missing altogether | Before go-live (Beforehand), then monthly | `permission denied for table journal_journalentry` on the UPDATE, and a row (or none, without an error) from the SELECT |
| 11 | **Console tradebook shape.** Download the account's own Console → Reports → Tradebook CSV for any past trade and run `tx import_tradebook <file> --actor <you> --dry-run` on a scratch database. It parses, then refuses the orders as not ours -- parsing is what this proves. Note whether `order_id` is Kite's order id (`yymmdd…`) or the exchange's order number (16 digits); either is matched, but only once `Order.exchange_order_id` is filled from the order book or the trade book, so confirm it is on the first live day's orders. Note whether `quantity` is written as an integer, and any column the command refuses as unknown | Before go-live | A dry run that passes parsing, and the `order_id` form written down; on the first live day, `exchange_order_id` set on its orders |
| 12 | **Runtime-role privileges on the recreated database.** Straight after step 2, as `steadystack_app` (`tx dbshell`): `SELECT 1 FROM journal_journalentry LIMIT 1` succeeds and `UPDATE journal_journalentry SET description = '' WHERE false` is refused. Step 7's `tx preflight` checks the same on every append-only table (its "runtime role" row) | Step 2, then step 7 | Both results, and a clean "runtime role" row in preflight |
| 13 | **Whether Kite returns `order_history` for the previous day's order id.** A past day's live orders still open are levelled from their fills without a broker call (audit `order.expired_unseen`), because Kite's book is believed to hold today's orders only; this confirms it. With a session, run `tx shell -c "from broker import get_broker, session; b=get_broker(); session.attach(b); print(b.order_history('<yesterday's kite_order_id>'))"` | Any evening after the first live day | The `order.history` `BrokerCall` row: an error or an empty history confirms the levelling is needed; a full history means the sweep could ask Kite instead |

The first live day: watch it. Check the first order in Kite's order book, the
postback arriving, the fill, the evening settlement, and items 2 and 7 above.

Rules the broker and exchange enforce, already handled in code:

| Rule | Where it is handled |
|---|---|
| 10 orders/second per account (429 above it); 400/minute; 5,000/day | The system places at most 5 orders per 30 s in total |
| SEBI retail algo registration above 10 orders/second | Not required at this rate |
| MARKET orders need `market_protection` (from 1 April 2026) | Every close order carries the sanity band (5%) |
| Order-to-trade penalty exemption within ±0.75% of LTP (from 6 April 2026) | Every limit is priced inside that band |
| Static IP for order placement | M12a gate above |

To go back to paper: Settings → Configuration → order mode `paper`, best
after a day's settle. It is always allowed, but it is not a no-op. From the
next plan the daily amount is allocated in the paper book, not the live
one; paper fills write paper lots and paper journal entries; and the live
book receives only real movements (lumpsums, statement lines, account
charges) until live resumes. While in paper the screens -- weights, gains,
Today, the value series -- read every lot, live and paper together, and the
paper engine ranks and holds against its own paper lots only. Once live
resumes, every lot reader (the divergence scan, weights, gains, valuation)
reads the live lots alone, so the paper fills made in between never reach
a live figure or a divergence, and the paper book's value series stays
apart from the live one. Returning to live passes the same gate as the
first switch, less the opening entry, which already exists: after a
settle, before 09:45, with no paper order open.

**While back in paper, do not classify divergences.** The paper evening's
holdings are the paper lots only, while the scan compares against every
lot, so a name held in both books opens a divergence each paper evening;
every classification posts to the live book, which would put a paper
difference into the real accounts. The classify page refuses one that
would post while the mode is paper. Leave them open; they close on their
own, or are classified once live resumes.

A real share movement made during the paper stint -- an IPO allotment, a
sale made directly at the broker -- is not lost. The paper evenings cannot
see it (their holdings are the paper lots'), so it is recorded once live
resumes: the first live evening's holdings sync shows it, the scan opens a
divergence for it, and you classify that divergence, dated when the
movement happened (the allotment or the trade date, not the evening).
