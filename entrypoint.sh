#!/bin/bash
set -e

# Download and process sheet data (downloads fresh by default)
echo "Updating tracker data from Google Sheets..."
python main.py

echo "Starting player server..."
python player.py

