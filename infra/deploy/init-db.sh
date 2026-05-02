#!/usr/bin/env bash
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE aslan WITH LOGIN PASSWORD 'aslan' SUPERUSER;
    CREATE DATABASE aslan OWNER aslan;
EOSQL
