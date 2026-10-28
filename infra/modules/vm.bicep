// The research VM: Ubuntu 24.04, SSH key only, system-assigned identity.
// cloud-init installs Docker, the idle-shutdown timer and the daily-shutdown timer (infra/vm).
// The daily shutdown runs on the VM because Microsoft.DevTestLab/schedules is not offered in any
// region this subscription may use.
param location string
param tags object
param vmSize string
param adminUsername string
param sshPublicKey string
param osDiskSizeGB int
param osDiskSku string
param subnetId string
param publicIpId string
param shutdownTime string
param shutdownTimeZone string
param containerImage string

var vmName = 'vm-cfdagent'

// The idle-shutdown files are embedded in cloud-init as base64 so that they are written
// byte for byte (LF endings, no YAML indentation issues).
var dailyTimer = replace(
  loadTextContent('../vm/daily-shutdown.timer'),
  '__ON_CALENDAR__',
  '*-*-* ${substring(shutdownTime, 0, 2)}:${substring(shutdownTime, 2, 2)}:00 ${shutdownTimeZone}'
)
var cloudInitSteps = [
  { placeholder: '__ADMIN_USER__', value: adminUsername }
  { placeholder: '__CONTAINER_IMAGE__', value: containerImage }
  { placeholder: '__B64_IDLE_SCRIPT__', value: base64(loadTextContent('../vm/idle-shutdown.sh')) }
  { placeholder: '__B64_IDLE_SERVICE__', value: base64(loadTextContent('../vm/idle-shutdown.service')) }
  { placeholder: '__B64_IDLE_TIMER__', value: base64(loadTextContent('../vm/idle-shutdown.timer')) }
  { placeholder: '__B64_DAILY_SERVICE__', value: base64(loadTextContent('../vm/daily-shutdown.service')) }
  { placeholder: '__B64_DAILY_TIMER__', value: base64(dailyTimer) }
]
var cloudInit = reduce(
  cloudInitSteps,
  loadTextContent('../vm/cloud-init.yaml'),
  (text, step) => replace(text, step.placeholder, step.value)
)

resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: 'nic-cfdagent'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: subnetId
          }
          publicIPAddress: {
            id: publicIpId
          }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: vmName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        name: 'osdisk-cfdagent'
        createOption: 'FromImage'
        caching: 'ReadWrite'
        diskSizeGB: osDiskSizeGB
        deleteOption: 'Delete'
        managedDisk: {
          storageAccountType: osDiskSku
        }
      }
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      customData: base64(cloudInit)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        provisionVMAgent: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
          properties: {
            deleteOption: 'Delete'
          }
        }
      ]
    }
  }
}

output vmName string = vm.name
output principalId string = vm.identity.principalId
