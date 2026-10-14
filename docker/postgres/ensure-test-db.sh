#!/bin/sh
set -eu
if psql "postgresql://asts:asts@db:5432/asts" -tAc "SELECT 1 FROM pg_database WHERE datname = 'asts_test'" | grep -q 1; then
  echo "asts_test already exists"
else
  psql "postgresql://asts:asts@db:5432/asts" -v ON_ERROR_STOP=1 -c "CREATE DATABASE asts_test"
fi
