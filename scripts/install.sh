#!/bin/bash
set -e
echo "Setting up AI Clipper..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "Dependencies installed. Don't forget to create your .env file."

