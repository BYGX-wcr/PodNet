#!/usr/bin/python3
import sys

sys.path.append('/m/local2/wcr/Mininet-Emulab')
from mininet.net import Containernet
import mininet.node
from mininet.node import * #Controller, Docker, DockerRouter, DockerP4Router
from mininet.nodelib import LinuxBridge
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.config import Subnet
import os
setLogLevel('info')

net = Containernet(controller=Controller)

info('*** Adding docker containers\n')

d1 = net.addDocker('d1', dimage="ubuntu:trusty_v2")
d2 = net.addDocker('d2', dimage="ubuntu:trusty_v2")
d3 = net.addDocker('d3', dimage="ubuntu:trusty_v2")
d4 = net.addDocker('d4', dimage="ubuntu:trusty_v2")

info('*** Adding switches\n')

s1 = net.addDocker('s1', cls=DockerP4Router, 
                         dimage="p4switch:v9",
                         json_path="/m/local2/wcr/P4-Switches/ecmp_switch.json", 
                         pcap_dump="/tmp",
                         controller="/m/local2/wcr/P4-Switches/rt_mediator.py",
                         ospfd='yes')
s2 = net.addDocker('s2', cls=DockerP4Router, 
                         dimage="p4switch:v9", 
                         json_path="/m/local2/wcr/P4-Switches/ecmp_switch.json", 
                         pcap_dump="/tmp",
                         controller="/m/local2/wcr/P4-Switches/rt_mediator.py",
                         ospfd='yes')

info('*** Adding subnets\n')
snet1 = Subnet(ipStr="10.0.0.0", prefixLen=24)
snet2 = Subnet(ipStr="10.1.0.0", prefixLen=24)
snet3 = Subnet(ipStr="10.2.0.0", prefixLen=24)
snet4 = Subnet(ipStr="10.3.0.0", prefixLen=24)
snet5 = Subnet(ipStr="10.4.0.0", prefixLen=24)

info('*** Creating links\n')

ip1 = snet1.assignIpAddr("10.0.0.2")
ip2 = snet1.assignIpAddr("10.0.0.1")
net.addLink(s2, s1, ip1=ip1, ip2=ip2, addr1=snet1.ipToMac(ip1), addr2=snet1.ipToMac(ip2))
snet1.addNode(s2, s1)

ip1 = snet2.assignIpAddr("10.1.0.1")
ip2 = snet2.allocateIPAddr()
net.addLink(s1, d1, ip1=ip1, ip2=ip2, addr1=snet2.ipToMac(ip1), addr2=snet2.ipToMac(ip2))
snet2.addNode(s1)

ip1 = snet3.assignIpAddr("10.2.0.1")
ip2 = snet3.allocateIPAddr()
net.addLink(s1, d2, ip1=ip1, ip2=ip2, addr1=snet3.ipToMac(ip1), addr2=snet3.ipToMac(ip2))
snet3.addNode(s1)

ip1 = snet4.allocateIPAddr()
ip2 = snet4.allocateIPAddr()
net.addLink(s2, d3, ip1=ip1, ip2=ip2, addr1=snet4.ipToMac(ip1), addr2=snet4.ipToMac(ip2))
snet4.addNode(s2, d3)

ip1 = snet5.allocateIPAddr()
ip2 = snet5.allocateIPAddr()
net.addLink(s2, d4, ip1=ip1, ip2=ip2, addr1=snet4.ipToMac(ip1), addr2=snet4.ipToMac(ip2))
snet5.addNode(s2, d4)


info('*** Configuring routes\n')
snet1.installSubnetTable()
snet2.installSubnetTable()
snet3.installSubnetTable()
snet4.installSubnetTable()
snet5.installSubnetTable()

s1.addRoutingConfig("ospfd", "router ospf")
s1.addRoutingConfig("ospfd", "router-id 10.0.0.1")
s1.addRoutingConfig("ospfd", "network " + snet1.getNetworkPrefix() + " area 0")
s1.addRoutingConfig("ospfd", "network " + snet2.getNetworkPrefix() + " area 1")
s1.addRoutingConfig("ospfd", "network " + snet3.getNetworkPrefix() + " area 2")
s1.addRoutingConfig("ospfd", "log file tmp/quagga.log")
s1.start()

s2.addRoutingConfig("ospfd", "router ospf")
s2.addRoutingConfig("ospfd", "router-id 10.0.0.2")
s2.addRoutingConfig("ospfd", "network " + snet1.getNetworkPrefix() + " area 0")
s2.addRoutingConfig("ospfd", "network " + snet4.getNetworkPrefix() + " area 1")
s2.addRoutingConfig("ospfd", "network " + snet5.getNetworkPrefix() + " area 2")
s2.addRoutingConfig("ospfd", "log file tmp/quagga.log")
s2.start()

d1.setDefaultRoute("gw 10.1.0.1")
d2.setDefaultRoute("gw 10.2.0.1")
d3.setDefaultRoute("gw 10.3.0.1")
d4.setDefaultRoute("gw 10.4.0.1")

info('*** Exp Setup\n')


info('*** Starting network\n')

net.start()

info('*** Running CLI\n')

CLI(net)

info('*** Stopping network')

net.stop()
