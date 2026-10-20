# Deploying MAW on a small VM (no domain needed)

One VM runs the whole stack with Docker Compose: Caddy (HTTPS) → MAW → Postgres + pgvector, plus the
mock MCP/A2A services so the catalog works out of the box. HTTPS uses a free
`<your-ip>.sslip.io` hostname, which resolves to your VM's IP, so Let's Encrypt issues a real
certificate without you owning a domain.

```
Internet ──443/80──► Caddy ──► maw:8000 ──► postgres (pgvector)
                                   └──────► mock-mcp, mock-a2a, LLM APIs
```

## 1. Choose a VM

| Provider | Plan | Cost | Notes |
|---|---|---|---|
| **Hetzner Cloud** (recommended) | CX22: 2 vCPU, 4 GB RAM, 40 GB | ≈ €4–5 / month | Reliable, simple; EU/US locations |
| Oracle Cloud Always Free | Ampere A1, up to 4 OCPU / 24 GB | free | Free capacity is often unavailable; open ports in the VCN security list too |
| DigitalOcean / Vultr / Linode | 2 GB droplet | ≈ $12 / month | Fine; 1 GB is too small for the image build |

Pick **Ubuntu 24.04** (or 22.04), add your SSH key, and note the public IPv4 address. 2 GB RAM is
the minimum (the first image build needs it); 4 GB is comfortable.

## 2. Install

From your machine:

```bash
ssh root@<VM_IP>
curl -fsSL https://raw.githubusercontent.com/dinup07/maw-api/claude/festive-feynman-ibsp9e/deploy/setup-vm.sh -o setup-vm.sh
sudo BRANCH=claude/festive-feynman-ibsp9e bash setup-vm.sh      # use BRANCH=main once merged
```

The script:

1. installs Docker, git and ufw, and allows only SSH, HTTP and HTTPS;
2. clones the repo to `/opt/maw`;
3. writes `/opt/maw/.env` **once**, with a random Postgres password, a random API key,
   `MAW_HOSTNAME=<ip-with-dashes>.sslip.io` and an outbound-host allowlist;
4. builds and starts the stack with `deploy/docker-compose.prod.yml` (production mode: MAW refuses
   to start without API keys, an allowlist and a database);
5. installs a nightly database backup (02:30, kept 14 days, in `/opt/maw/backups`);
6. prints the URL and the API key.

If your repository is private, clone it yourself first (with a deploy key or token) into
`/opt/maw`, then run `sudo bash /opt/maw/deploy/setup-vm.sh` — it reuses the existing checkout.

## 3. Add your API keys

```bash
sudo nano /opt/maw/.env          # uncomment OPENAI_API_KEY / GOOGLE_API_KEY
sudo bash /opt/maw/deploy/update.sh
```

To use the real-embedding stores, ingest documents over the API, from the VM or from your own
machine (the script needs only Python 3):

```bash
python3 scripts/ingest_remote.py docs_gemini examples/docs/*.md \
  --base https://<ip-with-dashes>.sslip.io --api-key <key>
```

## 4. Check it

```bash
BASE=https://<ip-with-dashes>.sslip.io API_KEY=<key> REAL=1 ./scripts/smoke.sh     # from a clone, anywhere
```

or manually:

```bash
curl -s https://<host>/ready
curl -s https://<host>/v1/runs -H "X-API-Key: <key>" -H 'content-type: application/json' \
  -d '{"workflow_id": "support_triage", "input": "Was INV-1001 charged twice?"}'
```

Public without a key: `/health`, `/ready`, `/docs` and A2A agent cards. `/metrics` is blocked at
Caddy; read it on the VM with
`docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml exec maw python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())"`.

## Operations

All commands run on the VM, in `/opt/maw`. Shorthand used below:

```bash
alias mawc='docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml'
```

| Task | Command |
|---|---|
| Status | `mawc ps` |
| Logs | `mawc logs -f maw` (JSON; rotated at 5 × 20 MB) |
| Update to the latest code | `sudo bash deploy/update.sh` (prints the previous commit) |
| Roll back | `sudo bash deploy/rollback.sh <commit>` |
| Restart | `mawc restart maw` |
| Back up now | `sudo APP_DIR=/opt/maw deploy/backup.sh` |
| Restore | `sudo bash deploy/restore.sh backups/maw-<timestamp>.sql.gz` (asks for confirmation) |
| Rotate the API key | Edit `MAW_API_KEYS` in `.env` (a JSON list: keep the old key during the switch, e.g. `["new","old"]`), then `mawc up -d maw` |
| Rotate a provider key | Edit `.env`, then `mawc up -d maw` |
| Allow a new outbound host | Add it to `MAW_ALLOWED_OUTBOUND_HOSTS` in `.env`, then `mawc up -d maw` |
| Database shell | `mawc exec postgres psql -U maw -d maw` |

**Copy backups off the VM.** The nightly dump protects against mistakes, not against losing the VM.
Simplest: enable your provider's automatic VM snapshots (Hetzner: about 20% of the VM price), and/or
periodically `scp root@<VM_IP>:/opt/maw/backups/*.sql.gz .`.

**Durability:** `docker compose up -d` and VM reboots are safe. Runs in progress continue from their
last checkpoint after a restart, and paused (human-in-the-loop) runs survive indefinitely.

## Security notes

- Only ports 22, 80 and 443 are open. Postgres, the mocks and MAW itself are not published; Docker
  publishes only Caddy. (Docker-published ports bypass ufw, which is why nothing else is published.)
- `/opt/maw/.env` holds every secret; it is `chmod 600`. Don't commit it.
- Keep `MAW_ALLOWED_OUTBOUND_HOSTS` tight: it is what stops a submitted workflow from making MAW call
  arbitrary hosts. Add each MCP server, A2A agent or vector database you configure.
- Disable `MAW_ALLOW_WORKFLOW_REGISTRATION` if workflows should only come from files in the repo.
- Use SSH keys only (`PasswordAuthentication no` in `/etc/ssh/sshd_config`) and keep the OS patched
  (`apt install unattended-upgrades`).
- When you get a domain: point an A record at the VM, set `MAW_HOSTNAME=maw.example.com` in `.env`
  and run `mawc up -d` — Caddy fetches a certificate for the new name.

## Scaling notes

This is a single-replica deployment: live event streams and cancellation are tracked in the MAW
process. That comfortably handles a small team. Beyond one replica, MAW needs a shared event bus
(Redis) — see *Future work* in [ARCHITECTURE.md](ARCHITECTURE.md). A bigger VM (or
`MAW_MAX_CONCURRENT_RUNS`) is the first lever; the LLM providers are usually the bottleneck.
