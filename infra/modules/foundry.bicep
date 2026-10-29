// Azure AI Services account (Foundry) with serverless model deployments. Billing is per token.
param location string
param tags object

@description('Items: name, format, version, sku, capacity. Values come from `az cognitiveservices model list`.')
param modelDeployments array

@description('Disable key authentication (Entra ID only).')
param disableLocalAuth bool

// Custom subdomain must be globally unique; it is also the Entra audience host.
var accountName = 'cfdagent-ai-${uniqueString(resourceGroup().id)}'

resource ai 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: disableLocalAuth
  }
}

// Deployments on one account must be created one after another.
@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [
  for d in modelDeployments: {
    parent: ai
    name: d.name
    sku: {
      name: d.sku
      capacity: d.capacity
    }
    properties: {
      model: {
        format: d.format
        name: d.name
        version: d.version
      }
    }
  }
]

output aiAccountName string = ai.name
// OpenAI v1-compatible base URL of the Foundry resource endpoint.
output aiEndpoint string = 'https://${accountName}.services.ai.azure.com/openai/v1/'
