#!/bin/bash
# Wrapper script to run fix_inactive_verified.py with the correct environment

# Determine the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Activate virtual environment if it exists
if [ -f "$PROJECT_ROOT/venv/bin/activate" ]; then
    source "$PROJECT_ROOT/venv/bin/activate"
elif [ -f "/opt/invasives/venv/bin/activate" ]; then
    source /opt/invasives/venv/bin/activate
else
    echo "Warning: No virtual environment found. Using system Python."
fi

# Run the Python script
python3 "$SCRIPT_DIR/fix_inactive_verified.py"
