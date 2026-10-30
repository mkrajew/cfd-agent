// The research VM: Ubuntu 24.04, SSH key only, system-assigned identity.
// cloud-init installs Docker, the idle-shutdown timer, the daily-shutdown timer, the metadata
// firewall and the SSH limits (infra/vm). The same files are applied to a running VM by
// scripts/vm-host-setup.sh, because customData cannot change after the VM exists.
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

// The daily shutdown fires at shutdownTime and then every 10 minutes for 4 hours (25 attempts), so
// a shutdown that `cfdagent-idle-shutdown --daily` deferred for an active run is retried. Each entry
// is one OnCalendar line; scripts/vm-host-setup.sh renders the same list for a running VM.
var shutdownStartMinute = int(substring(shutdownTime, 0, 2)) * 60 + int(substring(shutdownTime, 2, 2))
var attemptMinutes = map(range(0, 25), attempt => (shutdownStartMinute + attempt * 10) % 1440)
var dailyCalendar = join(
  map(
    attemptMinutes,
    minute => '*-*-* ${padLeft(string(minute / 60), 2, '0')}:${padLeft(string(minute % 60), 2, '0')}:00 ${shutdownTimeZone}'
  ),
  '\nOnCalendar='
)
var dailyTimer = replace(loadTextContent('../vm/daily-shutdown.timer'), '__ON_CALENDAR__', dailyCalendar)
// The VM files are embedded in cloud-init as base64 so that they are written
// byte for byte (LF endings, no YAML indentation issues).
var cloudInitSteps = [
  { placeholder: '__ADMIN_USER__', value: adminUsername }
  { placeholder: '__CONTAINER_IMAGE__', value: containerImage }
  { placeholder: '__B64_SSH_EXTRA__', value: base64(replace(loadTextContent('../vm/20-cfdagent-ssh-extra.conf'), '__ADMIN_USER__', adminUsername)) }
  { placeholder: '__B64_IPTABLES_SCRIPT__', value: base64(loadTextContent('../vm/cfdagent-iptables.sh')) }
  { placeholder: '__B64_IPTABLES_SERVICE__', value: base64(loadTextContent('../vm/cfdagent-iptables.service')) }
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
    // Supported by Standard_D4as_v4 (AcceleratedNetworkingEnabled capability), no extra charge.
    enableAcceleratedNetworking: true
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
