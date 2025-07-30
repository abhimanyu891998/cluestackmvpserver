# Azure Container Registry & Container Instance Deployment Guide

## Prerequisites

1. **Azure CLI**: Install from https://docs.microsoft.com/en-us/cli/azure/install-azure-cli
2. **Docker**: Make sure Docker is running locally for testing
3. **Azure Subscription**: Active Azure subscription

## Quick Setup Steps

### 1. Login to Azure
```bash
az login
```

### 2. Customize Configuration
Edit the following values in `azure-deploy.sh`:
```bash
ACR_NAME="bddmvpregistry"        # Must be globally unique
RESOURCE_GROUP="bdd-mvp-rg"      # Your resource group name
LOCATION="eastus"                # Azure region
CONTAINER_NAME="bdd-market-data-api"  # Container instance name
```

### 3. Deploy Everything (One Command)
```bash
./azure-deploy.sh
```

This script will:
- Create a resource group
- Create an Azure Container Registry
- Build and push your Docker image
- Create a Container Instance
- Provide you with the public URL

## Alternative: Step-by-Step Deployment

### Step 1: Create Resources
```bash
# Create resource group
az group create --name bdd-mvp-rg --location eastus

# Create container registry
az acr create --resource-group bdd-mvp-rg --name bddmvpregistry --sku Basic --admin-enabled true
```

### Step 2: Build and Push Image
```bash
./build-and-push-acr.sh
```

### Step 3: Deploy Container Instance
```bash
# Get ACR credentials
ACR_LOGIN_SERVER=$(az acr show --name bddmvpregistry --query loginServer --output tsv)
ACR_USERNAME=$(az acr credential show --name bddmvpregistry --query username --output tsv)
ACR_PASSWORD=$(az acr credential show --name bddmvpregistry --query passwords[0].value --output tsv)

# Create container instance
az container create \
    --resource-group bdd-mvp-rg \
    --name bdd-market-data-api \
    --image $ACR_LOGIN_SERVER/bdd-market-data-server:latest \
    --registry-login-server $ACR_LOGIN_SERVER \
    --registry-username $ACR_USERNAME \
    --registry-password $ACR_PASSWORD \
    --dns-name-label bdd-market-data-api \
    --ports 8000 \
    --environment-variables HOST=0.0.0.0 PORT=8000 DEBUG=false \
    --cpu 2 --memory 4 \
    --restart-policy Always
```

## Configuration

### Environment Variables
Update `.env.production` with your production settings:
- `CORS_ORIGINS`: Add your frontend domain
- Grafana/Prometheus settings if needed

### Custom Domain (Optional)
To use a custom domain:
1. Get the container's IP address
2. Create a DNS A record pointing to that IP
3. Update CORS_ORIGINS in your container

## Monitoring & Management

### View Logs
```bash
az container logs --resource-group bdd-mvp-rg --name bdd-market-data-api --follow
```

### Restart Container
```bash
az container restart --resource-group bdd-mvp-rg --name bdd-market-data-api
```

### Update Container with New Image
```bash
# First, build and push new image
./build-and-push-acr.sh v1.1

# Then update the container
az container create \
    --resource-group bdd-mvp-rg \
    --name bdd-market-data-api \
    --image bddmvpregistry.azurecr.io/bdd-market-data-server:v1.1 \
    # ... other parameters same as above
```

### Scale Resources
```bash
# Update CPU/memory
az container create \
    --resource-group bdd-mvp-rg \
    --name bdd-market-data-api \
    --cpu 4 --memory 8 \
    # ... other parameters
```

## API Endpoints

After deployment, your API will be available at:
- **Base URL**: `http://{container-name}.{region}.azurecontainer.io:8000`
- **Health Check**: `http://{url}/health`
- **WebSocket**: `ws://{url}/ws`
- **Metrics**: `http://{url}/metrics`

## Costs

Container Instance costs (approximate):
- 2 CPU, 4GB RAM: ~$60/month
- 1 CPU, 2GB RAM: ~$30/month
- Container Registry Basic: ~$5/month

## Troubleshooting

### Container Won't Start
```bash
# Check container logs
az container logs --resource-group bdd-mvp-rg --name bdd-market-data-api

# Check container events
az container show --resource-group bdd-mvp-rg --name bdd-market-data-api
```

### Image Pull Errors
- Verify ACR credentials
- Check image name and tag
- Ensure admin user is enabled on ACR

### Network Issues
- Check security group rules
- Verify port 8000 is exposed
- Check CORS settings

## Cleanup

To delete all resources:
```bash
az group delete --name bdd-mvp-rg --yes --no-wait
```