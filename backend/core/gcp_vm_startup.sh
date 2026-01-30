#!/bin/bash
#
# JARVIS GCP Spot VM Startup Script
# ==================================
#
# Automatically sets up a fresh GCP VM with JARVIS backend
# This script:
# 1. Installs system dependencies
# 2. Clones JARVIS repo (or uses pre-baked image)
# 3. Installs Python dependencies
# 4. Configures environment
# 5. Starts JARVIS backend on port 8010
#

set -e  # Exit on error
set -u  # Exit on undefined variable

echo "🚀 JARVIS GCP VM Startup Script"
echo "================================"
echo "Starting at: $(date)"
echo "Instance: $(hostname)"
echo "Zone: $(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/zone | cut -d/ -f4)"

# Get instance metadata
JARVIS_COMPONENTS=$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/attributes/jarvis-components || echo "")
JARVIS_TRIGGER=$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/attributes/jarvis-trigger || echo "")

echo "📦 Components to run: ${JARVIS_COMPONENTS:-all}"
echo "🎯 Trigger reason: ${JARVIS_TRIGGER:-manual}"

# Update system
echo "📥 Updating system packages..."
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3.10 \
    python3-pip \
    git \
    curl \
    wget \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3.10-dev \
    htop \
    screen

# Install Python 3.10 as default
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1
sudo update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1

# Upgrade pip
echo "📦 Upgrading pip..."
python3 -m pip install --upgrade pip setuptools wheel

# Clone JARVIS repository
echo "📥 Cloning JARVIS repository..."
cd /home

# v147.0: Get git repo URL from instance metadata (injected during VM creation)
JARVIS_REPO_URL=$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/attributes/jarvis-repo-url || echo "")

if [ ! -d "JARVIS-AI-Agent" ]; then
    if [ -n "$JARVIS_REPO_URL" ]; then
        echo "📥 Cloning from: $JARVIS_REPO_URL"
        git clone "$JARVIS_REPO_URL" JARVIS-AI-Agent || {
            echo "⚠️  Git clone failed, trying public repo..."
            git clone https://github.com/derekrussell/JARVIS-AI-Agent.git JARVIS-AI-Agent || {
                echo "⚠️  Fallback clone also failed, creating minimal structure..."
                mkdir -p JARVIS-AI-Agent/backend
            }
        }
    else
        echo "⚠️  No repo URL in metadata, trying public repo..."
        git clone https://github.com/derekrussell/JARVIS-AI-Agent.git JARVIS-AI-Agent || {
            echo "⚠️  Clone failed, creating minimal structure..."
            mkdir -p JARVIS-AI-Agent/backend
        }
    fi
fi

cd JARVIS-AI-Agent/backend

# Install Python dependencies
echo "📦 Installing Python dependencies..."
if [ -f "requirements.txt" ]; then
    python3 -m pip install -r requirements.txt
fi

# Install GCP-specific dependencies
python3 -m pip install \
    fastapi \
    uvicorn \
    google-cloud-storage \
    google-cloud-sql-connector \
    asyncpg \
    python-dotenv

# Set up environment variables
echo "⚙️  Configuring environment..."

# v147.0: Get port from metadata or default to 8000 (matches jarvis-prime)
BACKEND_PORT=$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/attributes/jarvis-port || echo "8000")

cat > /home/JARVIS-AI-Agent/backend/.env.gcp << EOF
# GCP VM Environment Configuration
GCP_PROJECT_ID=jarvis-473803
GCP_REGION=us-central1
GCP_ZONE=us-central1-a

# Backend Configuration (v147.0: Changed from 8010 to 8000 to match jarvis-prime)
BACKEND_PORT=${BACKEND_PORT}
BACKEND_HOST=0.0.0.0

# Component Configuration
JARVIS_COMPONENTS=${JARVIS_COMPONENTS:-VISION,CHATBOTS,ML_MODELS}

# Optimization
OPTIMIZE_STARTUP=true
LAZY_LOAD_MODELS=true
DYNAMIC_LOADING_ENABLED=true

# Disable local-only components
ENABLE_VOICE_UNLOCK=false
ENABLE_WAKE_WORD=false
ENABLE_MACOS_AUTOMATION=false

# Cloud SQL (use Cloud SQL Proxy)
DB_HOST=localhost
DB_PORT=5432
DB_NAME=jarvis_learning
DB_USER=jarvis
# DB_PASSWORD will be set via Cloud SQL Proxy auth

# Logging
LOG_LEVEL=INFO
ENABLE_ML_LOGGING=true
EOF

# Download Cloud SQL Proxy
echo "📥 Installing Cloud SQL Proxy..."
wget -q https://dl.google.com/cloudsql/cloud_sql_proxy.linux.amd64 -O cloud_sql_proxy
chmod +x cloud_sql_proxy

# Start Cloud SQL Proxy in background
echo "🔗 Starting Cloud SQL Proxy..."
./cloud_sql_proxy jarvis-473803:us-central1:jarvis-learning-db --port 5432 &
PROXY_PID=$!
echo "   Cloud SQL Proxy PID: $PROXY_PID"

# Wait for proxy to be ready
sleep 5

# Start JARVIS backend in screen session
echo "🚀 Starting JARVIS backend..."
cd /home/JARVIS-AI-Agent/backend

# Create startup log
mkdir -p /var/log/jarvis
LOG_FILE="/var/log/jarvis/backend.log"

# Self-Destruct Monitoring (Dead Man's Switch - VM Side)
# Checks if the JARVIS backend is still running. If it crashes or exits, shut down the VM.
screen -dmS self_destruct bash -c '
    echo "🛡️  Self-destruct monitor active"
    sleep 300  # Give backend 5 mins to start
    
    while true; do
        # Check if backend process is running (python3 main.py)
        if ! pgrep -f "python3 main.py" > /dev/null; then
            echo "❌ JARVIS backend not running! Shutting down VM to save money..."
            sudo shutdown -h now
            exit 0
        fi
        
        # Check for idle CPU (if CPU < 5% for 15 mins, shut down)
        # TODO: Add CPU idle check
        
        sleep 60
    done
'

# Start backend in screen session for easy access (v147.0: Use dynamic port)
screen -dmS jarvis bash -c "python3 main.py --port ${BACKEND_PORT} > $LOG_FILE 2>&1"

# Wait for backend to start
echo "⏳ Waiting for backend to start on port ${BACKEND_PORT}..."
sleep 10

# Health check
MAX_RETRIES=30
RETRY_COUNT=0
BACKEND_READY=false

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -s http://localhost:${BACKEND_PORT}/health > /dev/null; then
        BACKEND_READY=true
        break
    fi
    echo "   Waiting for backend... ($((RETRY_COUNT + 1))/$MAX_RETRIES)"
    sleep 2
    RETRY_COUNT=$((RETRY_COUNT + 1))
done

if [ "$BACKEND_READY" = true ]; then
    echo "✅ JARVIS backend is ready!"
    echo "   URL: http://$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip):${BACKEND_PORT}"
    echo "   Health: http://localhost:${BACKEND_PORT}/health"
    echo "   Logs: $LOG_FILE"
    echo "   Screen session: screen -r jarvis"
else
    echo "❌ Backend failed to start within timeout"
    echo "   Check logs: $LOG_FILE"
    exit 1
fi

# Log memory usage
echo "💾 Memory usage:"
free -h

# Log disk usage
echo "💿 Disk usage:"
df -h

echo "✅ Startup complete at: $(date)"
echo "================================"

# Keep script running to show in startup logs
tail -f $LOG_FILE
