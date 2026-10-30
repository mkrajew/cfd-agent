// Role assignments (least privilege). The VM identity gets data access to the `runs` blob container
// (not the storage account) and to Foundry, plus a custom role that can only deallocate its own VM;
// the owner gets read-only browsing roles.
param vmName string
param vmPrincipalId string
param storageAccountName string
param aiAccountName string

@description('Entra object id of the owner.')
param ownerObjectId string

// Built-in role definition ids.
var storageBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var storageBlobDataReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var cognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' existing = {
  name: vmName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

// Created by storage.bicep. The VM identity may write only here.
resource runsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  parent: blobService
  name: 'runs'
}

resource ai 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: aiAccountName
}

// Custom roles cannot list a single resource as assignable scope, so the definition is
// assignable in the resource group and the assignment below is scoped to the VM only.
resource deallocateRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'cfdagent-vm-deallocate')
  properties: {
    roleName: 'cfdagent VM Deallocate'
    description: 'Read a virtual machine and deallocate it. Nothing else.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Compute/virtualMachines/read'
          'Microsoft.Compute/virtualMachines/deallocate/action'
        ]
        notActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource vmDeallocate 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vm
  name: guid(vm.id, vmName, deallocateRole.id)
  properties: {
    roleDefinitionId: deallocateRole.id
    principalId: vmPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource vmBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: runsContainer
  name: guid(runsContainer.id, vmName, storageBlobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataContributor
    )
    principalId: vmPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource vmOpenAI 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: ai
  name: guid(ai.id, vmName, cognitiveServicesOpenAIUser)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAIUser
    )
    principalId: vmPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource ownerBlobReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, ownerObjectId, storageBlobDataReader)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataReader
    )
    principalId: ownerObjectId
    principalType: 'User'
  }
}

resource ownerOpenAI 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: ai
  name: guid(ai.id, ownerObjectId, cognitiveServicesOpenAIUser)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAIUser
    )
    principalId: ownerObjectId
    principalType: 'User'
  }
}
