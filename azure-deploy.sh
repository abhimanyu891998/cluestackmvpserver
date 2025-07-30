#!/bin/bash

# Azure Container Registry and Container Instance Deployment Script
set -e

# Configuration - CUSTOMIZE THESE VALUES
ACR_NAME="trackdownmvpserverregistry"  # Your ACR name (must be globally unique)
RESOURCE_GROUP="rg-brushless"
LOCATION="eastus"
CONTAINER_NAME="trackdown-market-data-server"
IMAGE_NAME="trackdown-market-data-server"
TAG="latest"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🚀 Starting Azure deployment for BDD Market Data Server${NC}"

# Check if Azure CLI is installed
if ! command -v az &> /dev/null; then
    echo -e "${RED}❌ Azure CLI is not installed. Please install it first:${NC}"
    echo "https://docs.microsoft.com/en-us/cli/azure/install-azure-cli"
    exit 1
fi

# Check if user is logged in to Azure
if ! az account show &> /dev/null; then
    echo -e "${YELLOW}⚠️  You are not logged in to Azure. Please login:${NC}"
    echo "az login"
    exit 1
fi

echo -e "${BLUE}📋 Current Azure subscription:${NC}"
az account show --query "{subscriptionId: id, name: name, user: user.name}" -o table

# Create resource group if it doesn't exist
echo -e "${BLUE}🏗️  Creating resource group: $RESOURCE_GROUP${NC}"
az group create --name $RESOURCE_GROUP --location $LOCATION

# Create Azure Container Registry
echo -e "${BLUE}🏗️  Creating Azure Container Registry: $ACR_NAME${NC}"
az acr create --resource-group $RESOURCE_GROUP --name $ACR_NAME --sku Basic --admin-enabled true

# Get ACR login server
ACR_LOGIN_SERVER=$(az acr show --name $ACR_NAME --query loginServer --output tsv)
echo -e "${GREEN}✅ ACR Login Server: $ACR_LOGIN_SERVER${NC}"

# Build and push Docker image
echo -e "${BLUE}🏗️  Building and pushing Docker image...${NC}"
az acr build --registry $ACR_NAME --image $IMAGE_NAME:$TAG .

# Get ACR credentials
ACR_USERNAME=$(az acr credential show --name $ACR_NAME --query username --output tsv)
ACR_PASSWORD=$(az acr credential show --name $ACR_NAME --query passwords[0].value --output tsv)

# Create Container Instance
echo -e "${BLUE}🚀 Creating Azure Container Instance: $CONTAINER_NAME${NC}"
az container create \
    --resource-group $RESOURCE_GROUP \
    --name $CONTAINER_NAME \
    --image $ACR_LOGIN_SERVER/$IMAGE_NAME:$TAG \
    --registry-login-server $ACR_LOGIN_SERVER \
    --registry-username $ACR_USERNAME \
    --registry-password $ACR_PASSWORD \
    --dns-name-label $CONTAINER_NAME \
    --ports 8000 \
    --os-type Linux \
    --environment-variables \
        HOST=0.0.0.0 \
        PORT=8000 \
        DEBUG=false \
        LOG_LEVEL=info \
    --cpu 2 \
    --memory 4 \
    --restart-policy Always

# Get the FQDN
FQDN=$(az container show --resource-group $RESOURCE_GROUP --name $CONTAINER_NAME --query ipAddress.fqdn --output tsv)

echo -e "${GREEN}🎉 Deployment completed successfully!${NC}"
echo -e "${GREEN}📍 Your API is available at: http://$FQDN:8000${NC}"
echo -e "${GREEN}🏥 Health check: http://$FQDN:8000/health${NC}"
echo -e "${GREEN}📊 WebSocket endpoint: ws://$FQDN:8000/ws${NC}"

echo -e "${BLUE}📋 Useful commands:${NC}"
echo "  View logs: az container logs --resource-group $RESOURCE_GROUP --name $CONTAINER_NAME"
echo "  Restart:   az container restart --resource-group $RESOURCE_GROUP --name $CONTAINER_NAME"
echo "  Delete:    az container delete --resource-group $RESOURCE_GROUP --name $CONTAINER_NAME"