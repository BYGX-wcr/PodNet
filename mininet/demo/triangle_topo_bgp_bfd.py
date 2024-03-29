#!/usr/bin/python
"""
A topology of three ASs connected to form a triangle, using BFD to detect link failures.
"""
import sys
sys.path.append('/m/local2/wcr/Mininet-Emulab')

from mininet.net import Containernet
from mininet.node import * #Controller, Docker, DockerRouter, DockerP4Router
from mininet.nodelib import LinuxBridge
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.config import Subnet, NodeList
import os
setLogLevel('info')

net = Containernet(controller=Controller)
numOfAS = 3
sizeOfAS = 3
nodes = NodeList() # used for generating topology file
adminIP = ""
faultReportCollectionPort = 9024

info('*** Adding docker containers\n')

host_list = list()
for i in range(0, numOfAS * (sizeOfAS - 1)):
    new_host = net.addDocker('d{}'.format(i), dimage="localhost/ubuntu:trusty_v2")
    host_list.append(new_host)

admin_host = net.addDocker('admin', dimage="localhost/p4switch-frr:v7")
host_list.append(admin_host)

info('*** Adding switches\n')

switch_list = list()
for i in range(0, numOfAS * sizeOfAS):
    new_switch = net.addDocker('s{}'.format(i), cls=DockerP4Router, 
                         dimage="localhost/p4switch-frr:v7",
                         software="frr",
                         json_path="/m/local2/wcr/P4-Switches/diagnosable_switch_v0.json", 
                         pcap_dump="/tmp",
                         log_console=True,
                         log_level="info",
                         rt_mediator= "/m/local2/wcr/P4-Switches/rt_mediator.py",
                         runtime_api= "/m/local2/wcr/P4-Switches/runtime_API.py",
                         switch_agent= "/m/local2/wcr/P4-Switches/switch_agent.py",
                         bgpd='yes',
                         ospfd='yes',
                         bfdd='yes')
    switch_list.append(new_switch)
    new_switch.addRoutingConfig(configStr="log file /tmp/frr.log debugging")
    new_switch.addRoutingConfig(configStr="debug bgp neighbor-events")
    new_switch.addRoutingConfig(configStr="debug bgp bfd")
    new_switch.addRoutingConfig(configStr="debug bgp nht")
    new_switch.addRoutingConfig(configStr="debug bfd network")
    new_switch.addRoutingConfig(configStr="debug bfd peer")
    new_switch.addRoutingConfig(configStr="debug bfd zebra")
    new_switch.addRoutingConfig("bgpd", "router bgp {asn}".format(asn=int(i / sizeOfAS + 1)))
    new_switch.addRoutingConfig("bgpd", "bgp router-id " + new_switch.getLoopbackIP())
    new_switch.addRoutingConfig("bgpd", "no bgp ebgp-requires-policy")
    new_switch.addRoutingConfig("ospfd", "router ospf")
    new_switch.addRoutingConfig("ospfd", "ospf router-id " + new_switch.getLoopbackIP())
    new_switch.addRoutingConfig("bfdd", "bfd")

info('*** Adding subnets\n')
snet_list = list()
for i in range(0, 100):
    new_snet = Subnet(ipStr="10.{}.0.0".format(i), prefixLen=24)
    snet_list.append(new_snet)

info('*** Creating links & Configure routes\n')

snet_counter = 0

# configure inter-AS switch-switch links
for i in range(0, numOfAS):
    for j in range(i + 1, numOfAS):
        index1 = i * sizeOfAS
        index2 = j * sizeOfAS

        if i != j:
            ip1 = snet_list[snet_counter].allocateIPAddr()
            ip2 = snet_list[snet_counter].allocateIPAddr()

            # configure links
            link = net.addLink(switch_list[index1], switch_list[index2], ip1=ip1, ip2=ip2, addr1=Subnet.ipToMac(ip1), addr2=Subnet.ipToMac(ip2))
            snet_list[snet_counter].addNode(switch_list[index1], switch_list[index2])

            nodes.addNode(switch_list[index1].name, ip=ip1, nodeType="switch")
            nodes.addNode(switch_list[index2].name, ip=ip2, nodeType="switch")
            nodes.addLink(switch_list[index1].name, switch_list[index2].name, ip1=ip1, ip2=ip2)

            # configure eBGP peers with BFD enabled
            # --- switch1
            switch_list[index1].addRoutingConfig("bgpd", "neighbor {} remote-as {}".format(ip2.split("/")[0], j + 1))
            switch_list[index1].addRoutingConfig("bgpd", "neighbor {} bfd".format(ip2.split("/")[0]))
            switch_list[index1].addRoutingConfig("bfdd", "peer {}".format(ip2.split("/")[0]))
            switch_list[index1].addRoutingConfig("bfdd", "no shutdown")
            switch_list[index1].addRoutingConfig("bfdd", "receive-interval 100")
            switch_list[index1].addRoutingConfig("bfdd", "transmit-interval 100")
            # --- switch2
            switch_list[index2].addRoutingConfig("bgpd", "neighbor {} remote-as {}".format(ip1.split("/")[0], i + 1))
            switch_list[index2].addRoutingConfig("bgpd", "neighbor {} bfd".format(ip1.split("/")[0]))
            switch_list[index2].addRoutingConfig("bfdd", "peer {}".format(ip1.split("/")[0]))
            switch_list[index2].addRoutingConfig("bfdd", "no shutdown")
            switch_list[index2].addRoutingConfig("bfdd", "receive-interval 100")
            switch_list[index2].addRoutingConfig("bfdd", "transmit-interval 100")

            # add new advertised network prefix
            switch_list[index1].addRoutingConfig("bgpd", "network " + snet_list[snet_counter].getNetworkPrefix())
            switch_list[index2].addRoutingConfig("bgpd", "network " + snet_list[snet_counter].getNetworkPrefix())

            snet_counter += 1

# configure intra-AS switch-switch links
for i in range(0, numOfAS):
    edgeRouter = i * sizeOfAS
    edgeRouterIp = ""

    # configure a single AS
    bgp_network_list = []
    for j in range(0, sizeOfAS):
        index1 = i * sizeOfAS + j
        index2 = i * sizeOfAS + (j + 1) % sizeOfAS

        # configure links
        ip1 = snet_list[snet_counter].allocateIPAddr()
        ip2 = snet_list[snet_counter].allocateIPAddr()
        link = net.addLink(switch_list[index1], switch_list[index2], ip1=ip1, ip2=ip2, addr1=Subnet.ipToMac(ip1), addr2=Subnet.ipToMac(ip2))
        snet_list[snet_counter].addNode(switch_list[index1], switch_list[index2])

        # config IGP routing, using OSPF
        switch_list[index1].addRoutingConfig("ospfd", "network " + snet_list[snet_counter].getNetworkPrefix() + " area {}".format(0))
        switch_list[index1].addRoutingConfig("ospfd", "network " + switch_list[index1].getLoopbackIP() + "/32" + " area {}".format(0))
        switch_list[index2].addRoutingConfig("ospfd", "network " + snet_list[snet_counter].getNetworkPrefix() + " area {}".format(0))

        # get the edge router's loopback ip
        if index1 == edgeRouter:
            edgeRouterIp = switch_list[index1].getLoopbackIP()

        # config iBGP peers
        if index1 != edgeRouter:
            loopbackIP1 = switch_list[index1].getLoopbackIP()
            # --- edge/border router
            switch_list[edgeRouter].addRoutingConfig("bgpd", "neighbor {} remote-as {}".format(loopbackIP1, i + 1))
            switch_list[edgeRouter].addRoutingConfig("bgpd", "neighbor {} update-source {}".format(loopbackIP1, edgeRouterIp))
            switch_list[edgeRouter].addRoutingConfig("bgpd", "neighbor {} bfd".format(loopbackIP1))
            switch_list[edgeRouter].addRoutingConfig("bfdd", "peer {} multihop local-address {}".format(loopbackIP1, edgeRouterIp))
            switch_list[edgeRouter].addRoutingConfig("bfdd", "no shutdown")
            switch_list[edgeRouter].addRoutingConfig("bfdd", "receive-interval 100")
            switch_list[edgeRouter].addRoutingConfig("bfdd", "transmit-interval 100")

            # --- non-edge router
            switch_list[index1].addRoutingConfig("bgpd", "neighbor {} remote-as {}".format(edgeRouterIp, i + 1))
            switch_list[index1].addRoutingConfig("bgpd", "neighbor {} update-source {}".format(edgeRouterIp, loopbackIP1))
            switch_list[index1].addRoutingConfig("bgpd", "neighbor {} bfd".format(edgeRouterIp))
            switch_list[index1].addRoutingConfig("bfdd", "peer {} multihop local-address {}".format(edgeRouterIp, loopbackIP1))
            switch_list[index1].addRoutingConfig("bfdd", "no shutdown")
            switch_list[index1].addRoutingConfig("bfdd", "receive-interval 100")
            switch_list[index1].addRoutingConfig("bfdd", "transmit-interval 100")

        # add new bgp advertised network prefix
        bgp_network_list.append(snet_list[snet_counter].getNetworkPrefix())

        nodes.addNode(switch_list[index1].name, ip=ip1, nodeType="switch")
        nodes.addNode(switch_list[index2].name, ip=ip2, nodeType="switch")
        nodes.addLink(switch_list[index1].name, switch_list[index2].name, ip1=ip1, ip2=ip2)

        snet_counter += 1

    # configure the advertised network prefixes for the AS
    for bgpNetwork in bgp_network_list:
        switch_list[edgeRouter].addRoutingConfig("bgpd", "network " + bgpNetwork)

# configure host-switch links
for i in range(0, numOfAS):
    edgeRouter = i * sizeOfAS

    # configure a single AS
    for j in range(0, sizeOfAS - 1):
        sid = i * sizeOfAS + 1 + j
        hid = i * (sizeOfAS - 1) + j

        ip1 = snet_list[snet_counter].allocateIPAddr()
        ip2 = snet_list[snet_counter].allocateIPAddr()
        net.addLink(switch_list[sid], host_list[hid], ip1=ip1, ip2=ip2, addr1=Subnet.ipToMac(ip1), addr2=Subnet.ipToMac(ip2))
        snet_list[snet_counter].addNode(switch_list[sid])
        switch_list[sid].addRoutingConfig("ospfd", "network " + snet_list[snet_counter].getNetworkPrefix() + " area {}".format(0))

        host_list[hid].setDefaultRoute("gw {}".format(ip1.split("/")[0]))

        nodes.addNode(host_list[hid].name, ip=ip2, nodeType="host")
        nodes.addLink(switch_list[sid].name, host_list[hid].name, ip1=ip1, ip2=ip2)

        # add a new advertised network prefix for the AS
        switch_list[edgeRouter].addRoutingConfig("bgpd", "network " + snet_list[snet_counter].getNetworkPrefix())

        snet_counter += 1

# configure the link between admin host
ip1 = snet_list[snet_counter].allocateIPAddr()
ip2 = snet_list[snet_counter].allocateIPAddr()
net.addLink(switch_list[0], admin_host, ip1=ip1, ip2=ip2, addr1=Subnet.ipToMac(ip1), addr2=Subnet.ipToMac(ip2))
snet_list[snet_counter].addNode(switch_list[0])
switch_list[0].addRoutingConfig("ospfd", "network " + snet_list[snet_counter].getNetworkPrefix() + " area {}".format(0))
admin_host.setDefaultRoute("gw {}".format(ip1.split("/")[0]))
nodes.addNode(admin_host.name, ip=ip2, nodeType="host")
nodes.addLink(switch_list[0].name, admin_host.name, ip1=ip1, ip2=ip2)
switch_list[0].addRoutingConfig("bgpd", "network " + snet_list[snet_counter].getNetworkPrefix())
snet_counter += 1
adminIP = ip2.split("/")[0]

for snet in snet_list:
    snet.installSubnetTable()

info('*** Exp Setup\n')

nodes.writeFile("topo.txt")
os.system("docker cp /m/local2/wcr/Diagnosis-driver/driver.tar.bz mn.admin:/")
os.system("docker cp /m/local2/wcr/Mininet-Emulab/topo.txt mn.admin:/")

info('*** Starting network\n')

for host in host_list:
    host.start()

for switch in switch_list:
    switch.setAdminConfig(adminIP, faultReportCollectionPort)
    switch.start()

net.start()

info('*** Running CLI\n')

CLI(net)

info('*** Stopping network')

net.stop()
