#!/bin/bash

# Build and Push to Azure Container Registry
set -e

# Configuration - CUSTOMIZE THESE VALUES
ACR_NAME="trackdownmvpserverregistry"  # Your ACR name
IMAGE_NAME="trackdown-market-data-server"
TAG=${1:-"latest"}

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🏗️  Building and pushing to Azure Container Registry${NC}"

# Check if Azure CLI is installed
if ! command -v az &> /dev/null; then
    echo -e "${RED}❌ Azure CLI is not installed. Please install it first.${NC}"
    exit 1
fi

# Check if user is logged in to Azure
if ! az account show &> /dev/null; then
    echo -e "${YELLOW}⚠️  You are not logged in to Azure. Please run: az login${NC}"
    exit 1
fi

# Get ACR login server
ACR_LOGIN_SERVER=$(az acr show --name $ACR_NAME --query loginServer --output tsv 2>/dev/null)
if [ -z "$ACR_LOGIN_SERVER" ]; then
    echo -e "${RED}❌ ACR '$ACR_NAME' not found. Please create it first or check the name.${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Found ACR: $ACR_LOGIN_SERVER${NC}"

# Build and push using ACR Build (recommended)
echo -e "${BLUE}🏗️  Building image with tag: $IMAGE_NAME:$TAG${NC}"
az acr build --registry $ACR_NAME --image $IMAGE_NAME:$TAG .

echo -e "${GREEN}🎉 Successfully built and pushed: $ACR_LOGIN_SERVER/$IMAGE_NAME:$TAG${NC}"

# Show recent images in the registry
echo -e "${BLUE}📋 Recent images in registry:${NC}"
az acr repository show-tags --name $ACR_NAME --repository $IMAGE_NAME --orderby time_desc --top 5 -o table

echo -e "${BLUE}💡 To deploy this image:${NC}"
echo "   Run: ./azure-deploy.sh"
echo "   Or update existing container instance with this new image."