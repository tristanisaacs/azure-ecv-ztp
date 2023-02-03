import requests
import re
import time
from netmiko import ConnectHandler
from pyedgeconnect import Orchestrator
from pyedgeconnect import EdgeConnect

"""

This script takes the URL of an EC-V in Azure,
identifies the primary interfaces and their MAC addresses,
connects to the EC-V via SSH key authentication,
creates a username/password that is allowed to login,
uses the EC-V REST API to assign the MAC addresses to the correct interfaces,
uses the Orchestrator REST API to pull registration information,
and uses the EC-V REST API to input registration information
Thus enabling Zero-Touch Provisioning for EC-Vs in Azure.

"""

# variables for the Azure REST API portion of the script
api_url = 'https://management.azure.com/subscriptions/subscriptionID/resourceGroups/resourceGroup/providers/Microsoft.Compute/virtualMachines/vmName?api-version=2022-08-01'
api_token = '' #enter your api token here
response = None
hostname = ''
interfaces = []
ifDict = {}

#variables for the ecv portion
username = ''
password = ''
url = '' #put EC-V IP address here
#important to set these to none by default because that is used in the main execution to determine which interfaces to configure
wan0MacAddress = None
wan1MacAddress = None
lan0MacAddress = None
mgmt0MacAddress = None

ecInterfaces = {}
#list of Dictionaries that all go into the edgeConnect.modify_network_interfaces api call
ifList =[]
#set value for Orchestrator URL/IP address
orchestrator_url = '' # enter your Orchestrator URL here
# set value for Orchestrator API key
orchestrator_api_key = ''

#variables for ssh portion
host = '' # input EC-V IP address here
sshUsername = ''
sshPassphrase = ''
myPubKey = '' # file path to your SSH public key
deviceType = 'aruba_os' # Netmiko doesn't support EC-V OS but this was reasonably close and worked
createUser = 'username {} password 0 {}'.format(username,password)
sessionLog = 'netmiko-session.log'

#function definitions

#this function looks at the NIC JSON file retrieved from Azure REST API and checks the subnet ID to determine what inteface it is
#note that the values used here are matched to a specific environment. would need to adjust patterns to fit different environments
def identifySubnet(interface):
    if re.search('ec-lan',interface.json()['properties']['ipConfigurations'][0]['properties']['subnet']['id']):
        interface = 'lan0'
    elif re.search('ec-wan0',interface.json()['properties']['ipConfigurations'][0]['properties']['subnet']['id']):
        interface = 'wan0'
    elif re.search('ec-wan1',interface.json()['properties']['ipConfigurations'][0]['properties']['subnet']['id']):
        interface = 'wan1'
    elif re.search('hub-mgmt',interface.json()['properties']['ipConfigurations'][0]['properties']['subnet']['id']):
        interface = 'mgmt0'
    else:
        interface = None
    return interface

#function to query Orchestrator for the account name and registration key
def getRegistration():
    orch = Orchestrator(orchestrator_url,api_key=orchestrator_api_key,log_console=True,verify_ssl=False,)
    portalConfig = orch.get_portal_registration_config()
    return portalConfig

#class definitions 
class AssignEcvMacs:
    wan0MacAddress = None
    wan1MacAddress = None
    lan0MacAddress = None
    mgmt0MacAddress = None

    def __init__(self, wan0=None,wan1=None,lan0=None,mgmt0=None):
        self.wan0MacAddress = wan0
        self.wan1MacAddress = wan1
        self.lan0MacAddress = lan0
        self.mgmt0MacAddress = mgmt0

    def display(self):
        print(self.wan0MacAddress)
        print(self.wan1MacAddress)
        print(self.lan0MacAddress)
        print(self.mgmt0MacAddress)

    def identifyInterface(self,ifName):
        match ifName:
            case 'wan0':
                return 'wan0'
            case 'wan1':
                return 'wan1'
            case 'lan0':
                return 'lan0'
            case 'mgmt0':
                return 'mgmt0'
            case _:
                return

    def findMacAddress(self,ifName):
        match ifName:
            case 'wan0':
                return self.wan0MacAddress
            case 'wan1':
                return self.wan1MacAddress
            case 'lan0':
                return self.lan0MacAddress
            case 'mgmt0':
                return self.mgmt0MacAddress
            case _:
                return

#main execution

if __name__ == "__main__":

    #polling Azure rest API
    response = requests.get(api_url, headers={'Authorization': api_token})
    hostname = response.json()['name']
    #print(response) #debug
    #populate the interface list
    for item in response.json()['properties']['networkProfile']['networkInterfaces']:
        #print(item) #debug
        interfaces.append(item['id'])
    #populate the interface name / mac address ifDict
    for item in interfaces:
        thisInterfaceUri = 'https://management.azure.com' + item + '?api-version=2022-05-01'
        #print(thisInterfaceUri) #debug
        thisInterface = requests.get(thisInterfaceUri, headers={'Authorization': api_token})
        #print(thisInterface.json()) #debug
        ifName = identifySubnet(thisInterface)
        ifMac = thisInterface.json()['properties']['macAddress']
        ifDict[ifName] = ifMac.replace('-',':')
    #print(ifDict) #debug
    #ifDict now has the interface:mac address mappings

    #ecv ssh portion
    net_connect = ConnectHandler(host=host,username=sshUsername,use_keys=True,key_file=myPubKey,passphrase=sshPassphrase,device_type=deviceType,secret='',session_log=sessionLog)
    net_connect.enable()
    net_connect.config_mode()
    net_connect.find_prompt()
    net_connect.send_command(createUser)
    #net_connect.send_command('exit')
    showUsernames = net_connect.send_command('show usernames')
    print(showUsernames)

    #ecv rest api portion
    obj1 = AssignEcvMacs(ifDict['wan0'],ifDict['wan1'],ifDict['lan0'])
    print(obj1.display()) #debug
    #connect to EC-V and GET all current interface configuration
    edgeConnect = EdgeConnect(url=url,log_console=True,verify_ssl=False)
    edgeConnect.login(user=username,password=password)
    ecInterfaces = edgeConnect.get_appliance_network_interfaces()
    #get portal registration info and configure on EC-V
    portalConfig = getRegistration()
    #print(portalConfig) #debug
    postResult = edgeConnect.register_sp_portal(account_key=portalConfig['registration']['key'],account=portalConfig['registration']['account'],site=hostname)
    #print(postResult) #debug

    for item in ecInterfaces['ifInfo']: #iterate over interfaces in ecInterfaces 
        #print(item['ifname']) #debug
        thisInterface = obj1.identifyInterface(item['ifname']) #identify the interfaces we want
        #print(item['mac']) #debug
        thisMacAddress = obj1.findMacAddress(thisInterface) #match interface to mac address
        #create a list of dictionaries that will be passed to edgeConnect.modify_network_interfaces()
        if thisMacAddress != None:
            item['mac'] = thisMacAddress
            ifList.append(item)
        else:
            continue
        #print(item['mac']) #debug

    #this section will update the interfaces with the MAC addresses given when running the command
    #it will not modify any interfaces for which a command line argument was not provided
    #note that the EC-V must be rebooted after these changes but we are not rebooting it here
    #because once this is done it should connect to Orchestrator, pull a preconfiguration,
    #and update software at which point it will reboot. 

    postResult = edgeConnect.modify_network_interfaces(if_info=ifList)
    print('waiting 30 seconds after modifying network interfaces...')
    time.sleep(30)
    if postResult:
        edgeConnect.save_changes()
    print(postResult)
















    
    
