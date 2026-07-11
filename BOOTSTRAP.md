# Bootstrap: first-time VPS setup

Run these once on the Vultr VPS (Ubuntu 24.04, already hardened: SSH key auth only,
root login disabled, ufw allowing 22 / 80 / 443, fail2ban, unattended-upgrades).

Assumes Docker and Caddy are not yet installed. Run as a sudo-capable user.

## 1. Create the deploy user

```bash
sudo adduser --disabled-password --gecos "" deploy
sudo mkdir -p /home/deploy/.ssh
# Give the deploy user its own key. Either copy the existing authorized_keys:
sudo cp ~/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
# ...or paste the PUBLIC half of the dedicated deploy keypair (see step 9) instead.
sudo chown -R deploy:deploy /home/deploy/.ssh
sudo chmod 700 /home/deploy/.ssh
sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

## 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo bash
sudo usermod -aG docker deploy
```

## 3. Install Caddy (with the rate_limit module)

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# The rate_limit directive ships in a community module. The official binary can
# pull it in place, then restart:
sudo caddy add-package github.com/mholt/caddy-ratelimit
sudo systemctl restart caddy
```

## 4. Create app and data directories

```bash
sudo mkdir -p /opt/tesserae-api /var/lib/tesserae-api /var/log/caddy
sudo chown -R deploy:deploy /opt/tesserae-api
# The API container runs as uid 999 (the "app" user baked into the image). The
# /data bind mount holds version_cache.json and must be owned by that uid.
sudo chown -R 999:999 /var/lib/tesserae-api
sudo chown -R caddy:caddy /var/log/caddy
```

Stats are stored in PostgreSQL (a `postgres` service in docker-compose, on a
Docker-managed named volume), not in the /data bind mount.

## 4b. Create the database password env file

docker-compose reads `POSTGRES_PASSWORD` from `/opt/tesserae-api/.env`. Generate a
strong password and write it once (never commit this file):

```bash
sudo -u deploy bash -c 'umask 077 && printf "POSTGRES_PASSWORD=%s\n" "$(openssl rand -base64 32)" > /opt/tesserae-api/.env'
sudo chmod 600 /opt/tesserae-api/.env
```

## 5. Install the compose file and Caddyfile

Fetch both files from the repo (run on the VPS):

```bash
sudo -u deploy bash -c '
  cd /opt/tesserae-api
  curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/dmellok/tesserae-api/main/docker-compose.yml
'
sudo curl -fsSL -o /etc/caddy/Caddyfile https://raw.githubusercontent.com/dmellok/tesserae-api/main/Caddyfile
sudo systemctl reload caddy
```

Point the DNS A record for `api.tesserae.ink` at this VPS now. Caddy will obtain a
Let's Encrypt certificate automatically on the first request once DNS resolves.

## 6. Log in to GHCR as the deploy user (optional)

This step is only needed if the GHCR image is private. If the `tesserae-api`
package has been set to public visibility, `docker compose pull` works
unauthenticated and you can skip to step 7.

For a private image, create a GitHub Personal Access Token with the
`read:packages` scope, then:

```bash
sudo -u deploy bash -c 'echo <GHCR_TOKEN> | docker login ghcr.io -u dmellok --password-stdin'
```

The credential is stored in `/home/deploy/.docker/config.json` and persists, so
`docker compose pull` in the deploy workflow works unattended.

## 7. Install the systemd poll timer

Fetch the two unit files from the repo (run on the VPS):

```bash
sudo curl -fsSL -o /etc/systemd/system/tesserae-api-poll.service https://raw.githubusercontent.com/dmellok/tesserae-api/main/systemd/tesserae-api-poll.service
sudo curl -fsSL -o /etc/systemd/system/tesserae-api-poll.timer https://raw.githubusercontent.com/dmellok/tesserae-api/main/systemd/tesserae-api-poll.timer
sudo systemctl daemon-reload
sudo systemctl enable --now tesserae-api-poll.timer
```

## 8. Start the service

Compose starts `postgres` first, waits for it to become healthy, then starts the
API (which creates the `hits` table on startup):

```bash
cd /opt/tesserae-api
sudo -u deploy docker compose up -d
# Populate the version cache immediately (the app also does a best-effort poll on boot):
sudo -u deploy docker exec tesserae-api python -m scripts.poll_github
```

## 9. Verify

```bash
curl -fsS https://api.tesserae.ink/version/latest
# Local check bypassing Caddy:
curl -fsS http://127.0.0.1:8000/healthz
# Stats reader (talks to Postgres via the app config):
sudo -u deploy docker exec tesserae-api python -m scripts.dump_stats
```

## 10. Remote database access (SSH tunnel)

Postgres listens only on the VPS loopback (`127.0.0.1:5432`); it is never exposed
to the internet and ufw stays at 22/80/443. To query it from your machine with
psql or an ODBC/BI tool, forward the port over your existing SSH login:

```bash
# On your workstation, leave this running:
ssh -N -L 5432:127.0.0.1:5432 <sudo-user>@api.tesserae.ink
```

(`<sudo-user>` is the sudo-capable account you created when provisioning the VPS,
distinct from the `deploy` user that the CI workflow uses.)

Then connect a client to `localhost:5432`:

```
host=localhost  port=5432  dbname=tesserae  user=tesserae  password=<from /opt/tesserae-api/.env>
```

For ODBC, install the PostgreSQL driver (psqlODBC) and point the DSN at
`localhost:5432`. Everything rides the encrypted SSH channel; no new firewall
rules are needed.

## 11. Database backups

`scripts/backup_db.sh` dumps the database to a gzipped SQL file under
`/opt/tesserae-api/backups` and prunes files older than 7 days. Install it and
schedule a daily run as the deploy user (no sudo needed):

```bash
scp scripts/backup_db.sh deploy@<vps>:/opt/tesserae-api/backup_db.sh
ssh deploy@<vps> '
  chmod +x /opt/tesserae-api/backup_db.sh
  /opt/tesserae-api/backup_db.sh   # take one now
  ( crontab -l 2>/dev/null | grep -v backup_db.sh
    echo "0 3 * * * /opt/tesserae-api/backup_db.sh >> /opt/tesserae-api/backups/backup.log 2>&1"
  ) | crontab -
'
```

Alternatively use the systemd units in `systemd/` (runs as root, daily 03:00 UTC):

```bash
scp systemd/tesserae-api-backup.* deploy@<vps>:/tmp/
ssh -t <sudo-user>@<vps> 'sudo mv /tmp/tesserae-api-backup.* /etc/systemd/system/ && \
  sudo systemctl daemon-reload && sudo systemctl enable --now tesserae-api-backup.timer'
```

Restore a dump into the running database:

```bash
gunzip -c /opt/tesserae-api/backups/tesserae-<stamp>.sql.gz | \
  docker exec -i tesserae-postgres psql -U tesserae -d tesserae
```

## GitHub secrets to add first

In the `tesserae-api` repo: Settings -> Secrets and variables -> Actions.

| Secret               | Value                                                                        |
| -------------------- | ---------------------------------------------------------------------------- |
| `DEPLOY_HOST`        | VPS hostname or IP.                                                           |
| `DEPLOY_USER`        | `deploy`                                                                      |
| `DEPLOY_SSH_KEY`     | Private half of a dedicated deploy keypair. Generate `ssh-keygen -t ed25519 -f deploy_key -N ""`, add `deploy_key.pub` to `/home/deploy/.ssh/authorized_keys` (step 1), paste `deploy_key` here. |
| `MAXMIND_LICENSE_KEY`| Free MaxMind account license key (Account -> Manage License Keys).           |

Once the secrets exist and the VPS is up, pushing a `v0.1.0` tag runs the release
workflow end to end: test, build the image with a fresh GeoLite2 database, push to
GHCR, deploy over SSH, and smoke-test `https://api.tesserae.ink/version/latest`.

```bash
git tag v0.1.0
git push origin v0.1.0
```
