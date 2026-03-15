#!/usr/bin/env bash
# setup_postgres.sh — Run once on a fresh EC2 instance to provision PostgreSQL for TruckWys.
# Usage: sudo bash scripts/setup_postgres.sh [DB_PASSWORD]
set -euo pipefail

DB_USER="truckwys"
DB_NAME="truckwys_db"
DB_PASSWORD="${1:-$(openssl rand -base64 24)}"

echo "==> Installing PostgreSQL and dependencies..."
sudo apt-get update -y
sudo apt-get install -y postgresql postgresql-contrib libpq-dev

echo "==> Starting PostgreSQL service..."
sudo systemctl enable postgresql
sudo systemctl start postgresql

echo "==> Creating database user '${DB_USER}'..."
sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';" 2>/dev/null \
    || sudo -u postgres psql -c "ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';"

echo "==> Creating database '${DB_NAME}'..."
sudo -u postgres createdb "${DB_NAME}" -O "${DB_USER}" 2>/dev/null \
    || echo "    Database '${DB_NAME}' already exists — skipping."

echo "==> Setting password for '${DB_USER}'..."
sudo -u postgres psql -c "ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';"

echo ""
echo "========================================================"
echo "  PostgreSQL provisioning complete!"
echo "========================================================"
echo ""
echo "  Add the following to your .env file on the server:"
echo ""
echo "  DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}"
echo ""
echo "  IMPORTANT: Save the password above — it will not be shown again."
echo "========================================================"
