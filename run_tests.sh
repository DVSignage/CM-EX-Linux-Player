#!/bin/bash
# Test runner script for the player daemon

set -e

echo "================================"
echo "Digital Signage Player - Tests"
echo "================================"
echo ""

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo "pytest not found. Installing test dependencies..."
    pip3 install pytest pytest-asyncio pytest-cov
fi

# Run tests
echo "Running tests..."
echo ""

# Run with coverage
pytest -v --cov=. --cov-report=term-missing --cov-report=html tests/

echo ""
echo "================================"
echo "Tests complete!"
echo "Coverage report: htmlcov/index.html"
echo "================================"
