#!/bin/bash
set -e
source venv/bin/activate
export PYTHONPATH=.
pytest tests/ -v

