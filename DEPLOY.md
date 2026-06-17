# TruckWys — Deployment Guide

World-class, SA-first target: **AWS `af-south-1` (Cape Town)** for POPIA data residency + low latency.

```
Carriers ──▶ CloudFront/Amplify (React) ──▶ App Runner (Django API) ──▶ RDS Postgres (Multi-AZ)
                                                     │
                                                     ├─▶ ElastiCache Redis ──▶ Celery worker (App Runner/ECS)
                                                     ├─▶ Secrets Manager (SECRET_KEY, ANTHROPIC_API_KEY, ...)
                                                     └─▶ S3 (logos, invoice PDFs, PODs)
```

## 0. Local prod-shaped stack (verify before cloud)

```sh
docker compose up --build       # web + Postgres + Redis + Celery worker
# API at http://localhost:8000 — migrations + collectstatic run automatically.
```

## 1. Database — RDS PostgreSQL
- Engine **PostgreSQL 16**, Multi-AZ, encrypted (KMS), in a **private subnet**, automated backups on.
- Build the connection string → store as the `DATABASE_URL` secret (see step 3).
  `postgresql://USER:PASSWORD@HOST:5432/truckwys`
- The app reads it via `dj-database-url`; no code change — it falls back to SQLite only when unset.

## 2. Container image → ECR
```sh
aws ecr create-repository --repository-name truckwys-backend --region af-south-1
docker build -t truckwys-backend .
aws ecr get-login-password --region af-south-1 | docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.af-south-1.amazonaws.com
docker tag truckwys-backend <ACCOUNT>.dkr.ecr.af-south-1.amazonaws.com/truckwys-backend:latest
docker push <ACCOUNT>.dkr.ecr.af-south-1.amazonaws.com/truckwys-backend:latest
```

## 3. Secrets — AWS Secrets Manager
Create one secret per value (or a single JSON secret) and reference them in App Runner:
`SECRET_KEY` (generate a real one), `DATABASE_URL`, `REDIS_URL`, `ANTHROPIC_API_KEY`,
`TOMTOM_API_KEY`, `RESEND_API_KEY`, `PAYFAST_MERCHANT_ID/KEY/PASSPHRASE`.
> The app **fails fast** if `DEBUG=False` and `SECRET_KEY` is the dev default — so a real key is mandatory in prod.

## 4. Backend — App Runner (fast path)
- Create an App Runner service from the ECR image, port **8000**, health check path **`/api/`**.
- Env: `DEBUG=False`, `ALLOWED_HOSTS=api.truckwys.co.za,.awsapprunner.com`,
  `CORS_ALLOWED_ORIGINS=https://app.truckwys.co.za`, `FRONTEND_URL=https://app.truckwys.co.za`.
- Secrets: wire the step-3 secrets (App Runner → Configure → Secrets).
- The image entrypoint runs `migrate` + `collectstatic` on start. For zero-race migrations at scale,
  run a one-off `docker-entrypoint.sh migrate` task before scaling out.
- Custom domain `api.truckwys.co.za` via App Runner custom domains (ACM cert auto-managed).

> Scale-up path: move to **ECS Fargate + ALB** (more control, WAF, blue/green) when traffic grows — same image.

## 5. Async — Celery worker + Redis
- **ElastiCache Redis** (single node to start) → `REDIS_URL` secret.
- Run the worker as a second App Runner service (or ECS task) using the **same image** with command
  `./docker-entrypoint.sh worker` (and `beat` for schedules).

## 6. Frontend — React/Vite
- **Amplify Hosting** (connect the repo, build `npm run build`, output `dist/`) **or** S3 + CloudFront.
- Set `VITE_API_URL=https://api.truckwys.co.za/` at build time.
- Custom domain `app.truckwys.co.za` + ACM TLS + (optional) WAF.

## 7. CI/CD
- GitHub Actions: on push to `main` → build image → push to ECR → App Runner auto-deploys (or `aws apprunner start-deployment`).
- Frontend: build → deploy to Amplify/S3 + CloudFront invalidation.

## 8. Observability & security
- **Sentry** (`SENTRY_DSN`) — already wired in `settings_prod.py`; **CloudWatch** logs from App Runner.
- RDS/Redis in private subnets; security groups least-privilege; **WAF** on CloudFront + App Runner.
- Rotate the partner/lender API keys onto the `IntegrationAPIKey` model (P3) before exposing the public API.

## Compliance (SA fintech)
- Data residency in `af-south-1`; encryption at rest (KMS) + in transit (TLS); audit logging; POPIA-aware retention.
