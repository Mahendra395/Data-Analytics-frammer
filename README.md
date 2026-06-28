# Frammer EC2 Deployment Bundle

This bundle contains a minimal production deployment setup for a FastAPI backend on an AWS EC2 free-tier Ubuntu instance.

## Files
- `.env.example` - environment variable template
- `requirements-prod.txt` - production dependency list
- `frammer-backend.service` - systemd service file
- `nginx-frammer.conf` - Nginx reverse proxy config
- `deploy.sh` - idempotent deployment script used by GitHub Actions
- `.github/workflows/deploy.yml` - CI/CD workflow triggered on pushes to `main`

## Assumptions
- App entrypoint is `main:app`
- EC2 OS user is `ubuntu`
- Project is deployed under `/opt/frammer-backend`
- Nginx proxies to `127.0.0.1:8000`
