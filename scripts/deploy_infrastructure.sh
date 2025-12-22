#!/bin/bash
# Deploy JARVIS Infrastructure using Terraform
# Usage: ./scripts/deploy_infrastructure.sh

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}🚀 JARVIS Infrastructure Deployment${NC}"
echo "========================================"

# Check if Terraform is installed
if ! command -v terraform &> /dev/null; then
    echo -e "${RED}❌ Terraform not found. Please install it first:${NC}"
    echo "   brew install terraform"
    exit 1
fi

# Check if gcloud is installed/auth
if ! command -v gcloud &> /dev/null; then
    echo -e "${RED}❌ gcloud CLI not found.${NC}"
    exit 1
fi

echo -e "${BLUE}📦 Initializing Terraform...${NC}"
cd terraform
terraform init

echo -e "${BLUE}🔍 Planning changes...${NC}"
terraform plan -out=tfplan

echo -e "${BLUE}🏗️  Applying Infrastructure (this may take 10-20 mins for Redis)...${NC}"
read -p "Do you want to proceed? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

terraform apply tfplan

echo -e "${BLUE}📝 Generating configuration...${NC}"

# Get outputs
REDIS_HOST=$(terraform output -raw redis_host)
REDIS_PORT=$(terraform output -raw redis_port)
VPC_ID=$(terraform output -raw vpc_id)

echo -e "${GREEN}✅ Infrastructure Deployed Successfully!${NC}"
echo ""
echo "Add these variables to your production .env or CI/CD secrets:"
echo "---------------------------------------------------"
echo "REDIS_HOST=$REDIS_HOST"
echo "REDIS_PORT=$REDIS_PORT"
echo "GCP_NETWORK=jarvis-vpc"
echo "GCP_SUBNETWORK=jarvis-subnet-01"
echo "---------------------------------------------------"
echo ""
echo "Monitor your dashboard at:"
echo "https://console.cloud.google.com/monitoring/dashboards"

