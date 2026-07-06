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
sudo chown -R deploy:deploy /opt/tesserae-api /var/lib/tesserae-api
sudo chown -R caddy:caddy /var/log/caddy
```

## 5. Install the compose file and Caddyfile

Fetch them from the repo (or scp them up):

```bash
sudo -u deploy curl -fsSL https://raw.githubusercontent.com/dmellok/tesserae-api/main/docker-compose.yml -o /opt/tesserae-api/docker-compose.yml
sudo curl -fsSL https://raw.githubusercontent.com/dmellok/tesserae-api/main/Caddyfile -o /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Point the DNS A record for `api.tesserae.ink` at this VPS now. Caddy will obtain a
Let's Encrypt certificate automatically on the first request once DNS resolves.

## 6. Log in to GHCR as the deploy user (once)

Create a GitHub Personal Access Token with the `read:packages` scope, then:

```bash
sudo -u deploy bash -c 'echo <GHCR_TOKEN> | docker login ghcr.io -u dmellok --password-stdin'
```

The image is public, so this is only needed if pulls are rate-limited or the
package is later made private. It is safe to run regardless.

## 7. Install the systemd poll timer

```bash
sudo curl -fsSL https://raw.githubusercontent.com/dmellok/tesserae-api/main/systemd/tesserae-api-poll.service -o /etc/systemd/system/tesserae-api-poll.service
sudo curl -fsSL https://raw.githubusercontent.com/dmellok/tesserae-api/main/systemd/tesserae-api-poll.timer -o /etc/systemd/system/tesserae-api-poll.timer
sudo systemctl daemon-reload
sudo systemctl enable --now tesserae-api-poll.timer
```

## 8. Start the service

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
