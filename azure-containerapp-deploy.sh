#!/bin/bash

# Azure Container Apps Deployment Script with HTTPS Support
set -e

# Configuration from existing azure-deploy.sh
ACR_NAME="trackdownmvpserverregistry"
RESOURCE_GROUP="rg-brushless"
LOCATION="eastus"
CONTAINER_APP_NAME="trackdown-market-data-app"
CONTAINER_APP_ENV="trackdown-env"
IMAGE_NAME="trackdown-market-data-server"
TAG="latest"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🚀 Starting Azure Container Apps deployment with HTTPS support${NC}"

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

# Register Container Apps provider if not already registered
echo -e "${BLUE}📝 Registering Container Apps provider...${NC}"
az provider register --namespace Microsoft.App --wait

# Install Container Apps extension if not already installed
echo -e "${BLUE}🔧 Installing/updating Container Apps extension...${NC}"
az extension add --name containerapp --upgrade

# Create resource group if it doesn't exist
echo -e "${BLUE}🏗️  Creating resource group: $RESOURCE_GROUP${NC}"
az group create --name $RESOURCE_GROUP --location $LOCATION

# Create Azure Container Registry if it doesn't exist
echo -e "${BLUE}🏗️  Creating Azure Container Registry: $ACR_NAME${NC}"
az acr create --resource-group $RESOURCE_GROUP --name $ACR_NAME --sku Basic --admin-enabled true

# Get ACR login server
ACR_LOGIN_SERVER=$(az acr show --name $ACR_NAME --query loginServer --output tsv)
echo -e "${GREEN}✅ ACR Login Server: $ACR_LOGIN_SERVER${NC}"

# Build and push Docker image
echo -e "${BLUE}🏗️  Building and pushing Docker image...${NC}"
az acr build --registry $ACR_NAME --image $IMAGE_NAME:$TAG .

# Create Container Apps environment
echo -e "${BLUE}🌍 Creating Container Apps environment: $CONTAINER_APP_ENV${NC}"
az containerapp env create \
    --name $CONTAINER_APP_ENV \
    --resource-group $RESOURCE_GROUP \
    --location $LOCATION

# Get ACR credentials
ACR_USERNAME=$(az acr credential show --name $ACR_NAME --query username --output tsv)
ACR_PASSWORD=$(az acr credential show --name $ACR_NAME --query passwords[0].value --output tsv)

# Create Container App with optimized configuration for SSE
echo -e "${BLUE}🚀 Creating Container App: $CONTAINER_APP_NAME${NC}"
az containerapp create \
    --name $CONTAINER_APP_NAME \
    --resource-group $RESOURCE_GROUP \
    --environment $CONTAINER_APP_ENV \
    --image $ACR_LOGIN_SERVER/$IMAGE_NAME:$TAG \
    --registry-server $ACR_LOGIN_SERVER \
    --registry-username $ACR_USERNAME \
    --registry-password $ACR_PASSWORD \
    --target-port 8000 \
    --ingress external \
    --min-replicas 1 \
    --max-replicas 3 \
    --cpu 2.0 \
    --memory 4.0Gi \
    --env-vars \
        HOST=0.0.0.0 \
        PORT=8000 \
        DEBUG=false \
        LOG_LEVEL=info

# Configure ingress for long-running SSE connections
echo -e "${BLUE}⚙️  Configuring ingress for SSE connections...${NC}"
az containerapp ingress update \
    --name $CONTAINER_APP_NAME \
    --resource-group $RESOURCE_GROUP \
    --transport auto

# Get the HTTPS URL
FQDN=$(az containerapp show --name $CONTAINER_APP_NAME --resource-group $RESOURCE_GROUP --query properties.configuration.ingress.fqdn --output tsv)

echo -e "${GREEN}🎉 Deployment completed successfully!${NC}"
echo -e "${GREEN}🔒 Your API is available at: https://$FQDN${NC}"
echo -e "${GREEN}🏥 Health check: https://$FQDN/health${NC}"
echo -e "${GREEN}📊 WebSocket endpoint: wss://$FQDN/ws${NC}"

echo -e "${BLUE}📋 Useful commands:${NC}"
echo "  View logs: az containerapp logs show --name $CONTAINER_APP_NAME --resource-group $RESOURCE_GROUP"
echo "  Scale:     az containerapp update --name $CONTAINER_APP_NAME --resource-group $RESOURCE_GROUP --min-replicas 2 --max-replicas 5"
echo "  Delete:    az containerapp delete --name $CONTAINER_APP_NAME --resource-group $RESOURCE_GROUP"
echo "  Environment: az containerapp env delete --name $CONTAINER_APP_ENV --resource-group $RESOURCE_GROUP"