#!/usr/bin/env bash
# The live Temporal store is Postgres (temporal.service). Do not restart start-dev.
echo "temporal_vacuum: skipped; live server is temporal.service on Postgres"
exit 0
