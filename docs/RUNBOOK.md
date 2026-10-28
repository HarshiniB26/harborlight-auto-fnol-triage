# FNOL Runbook

## Start application

docker compose up --build

## Run tests

python -m unittest discover -s tests -v

## Review workflow

python tools/runtime.py pending

python tools/runtime.py approve REVIEW_ID --reviewer NAME

Approval automatically creates the FNOL record.

## Architecture

The active application uses workbench/ and tools/runtime.py.

The previous implementation is stored in legacy_archive/.
