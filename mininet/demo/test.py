#!/usr/bin/python
"""
A topology of five ASs, meant to be generic for fault injection.
"""

import sys
sys.path.append('/m/local2/wcr/Mininet-Emulab')

from mininet.net import Containernet
from mininet.node import *
from mininet.cli import CLI
from mininet.log import info, setLogLevel
from mininet.config import Subnet, NodeList
import os
setLogLevel('info')

net = Containernet(controller=Controller)
nodes = NodeList() # used for generating topology file
admin_ip = ""
fault_report_collection_port = 9024

info('*** Adding docker containers\n')
host_image = "localhost/rockylinux:v2"
host_dict = dict()
host_count = 0

switch_image = "localhost/p4switch-frr:v9"
switch_dict = dict()
switch_count = 0
as_map = dict()

# d0 d1
for i in range(0, 2):
    new_host = net.addDocker('d{}'.format(host_count), dimage=host_image)
    as_map['d{}'.format(host_count)] = 1
    host_dict['d{}'.format(host_count)] = new_host
    host_count += 1

# admin
host_dict['admin'] = net.addDocker('admin', dimage=host_image)

# s0 s1 s2
for i in range(0, 3):
    new_switch = net.addDocker('s{}'.format(switch_count), cls=DockerP4Router, 
                         dimage=switch_image,
                         software="frr",
                         json_path="/m/local2/wcr/P4-Switches/diagnosable_switch_v1.json", 
                         pcap_dump="/tmp",
                         log_console=True,
                         log_level="info",
                         rt_mediator= "/m/local2/wcr/P4-Switches/rt_mediator.py",
                         runtime_api= "/m/local2/wcr/P4-Switches/runtime_API.py",
                         switch_agent= "/m/local2/wcr/P4-Switches/switch_agent.py",
                         packet_injector= "/m/local2/wcr/P4-Switches/packet_injector.py",
                         bgpd='yes',
                         ospfd='yes')
    as_map['s{}'.format(switch_count)] = i + 1
    switch_dict['s{}'.format(switch_count)] = new_switch
    switch_count += 1

    new_switch.addRoutingConfig(configStr="log file /tmp/frr.log debugging")
    new_switch.addRoutingConfig(configStr="debug bgp neighbor-events")
    new_switch.addRoutingConfig(configStr="debug bgp bfd")
    new_switch.addRoutingConfig(configStr="debug bgp nht")
    new_switch.addRoutingConfig(configStr="debug zebra dplane detailed")
    new_switch.addRoutingConfig("bgpd", "router bgp {asn}".format(asn=i + 1))
    new_switch.addRoutingConfig("bgpd", "bgp router-id " + new_switch.getLoopbackIP())
    # new_switch.addRoutingConfig("bgpd", "no bgp ebgp-requires-policy")
    new_switch.addRoutingConfig("ospfd", "router ospf")
    new_switch.addRoutingConfig("ospfd", "ospf router-id " + new_switch.getLoopbackIP())

info('*** Creating links & Configure routes\n')

info('*** Global\n')

snet_list = list()
for i in range(0, 20):
    new_snet = Subnet(ipStr="10.0.{}.0".format(i), prefixLen=24)
    snet_list.append(new_snet)
snet_counter = 0

switch_pairs = [("s0", "s1"), ("s0", "s2"), ("s1", "s2")]

for t in switch_pairs:
    ip1 = snet_list[snet_counter].allocateIPAddr()
    ip2 = snet_list[snet_counter].allocateIPAddr()

    index1 = t[0]
    index2 = t[1]

    # configure links
    link = net.addLink(switch_dict[index1], switch_dict[index2], ip1=ip1, ip2=ip2, addr1=Subnet.ipToMac(ip1), addr2=Subnet.ipToMac(ip2))
    snet_list[snet_counter].addNode(switch_dict[index1], switch_dict[index2])

    nodes.addNode(switch_dict[index1].name, ip=switch_dict[index1].getLoopbackIP(), nodeType="switch")
    nodes.addNode(switch_dict[index2].name, ip=switch_dict[index2].getLoopbackIP(), nodeType="switch")
    nodes.addLink(switch_dict[index1].name, switch_dict[index2].name, ip1=ip1, ip2=ip2)

    # configure eBGP peers
    switch_dict[index1].addRoutingConfig("bgpd", "neighbor {} remote-as {}".format(ip2.split("/")[0], as_map[index2]))
    switch_dict[index1].addRoutingConfig("bgpd", "neighbor {} soft-reconfiguration inbound".format(ip2.split("/")[0]))
    switch_dict[index1].addRoutingConfig("bgpd", "neighbor {} route-map OUT_AS_RMAP out".format(ip2.split("/")[0]))
    switch_dict[index1].addRoutingConfig("bgpd", "neighbor {} route-map IN_AS_RMAP in".format(ip2.split("/")[0]))

    switch_dict[index2].addRoutingConfig("bgpd", "neighbor {} remote-as {}".format(ip1.split("/")[0], as_map[index1]))
    switch_dict[index2].addRoutingConfig("bgpd", "neighbor {} soft-reconfiguration inbound".format(ip1.split("/")[0]))
    switch_dict[index2].addRoutingConfig("bgpd", "neighbor {} route-map OUT_AS_RMAP out".format(ip1.split("/")[0]))
    switch_dict[index2].addRoutingConfig("bgpd", "neighbor {} route-map IN_AS_RMAP in".format(ip1.split("/")[0]))

    # add new advertised network prefix
    switch_dict[index1].addRoutingConfig("bgpd", "network " + snet_list[snet_counter].getNetworkPrefix())
    switch_dict[index2].addRoutingConfig("bgpd", "network " + snet_list[snet_counter].getNetworkPrefix())

    snet_counter += 1

for snet in snet_list:
    snet.installSubnetTable()

# configure policy for edge routers
for s in {"s0", "s1", "s2"}:
    switch_dict[s].addRoutingConfig(configStr="route-map OUT_AS_RMAP permit 10\nmatch ip address prefix-list AS_PREFIX_LIST\nset community {}:1".format(as_map[s]))
    switch_dict[s].addRoutingConfig(configStr="route-map OUT_AS_RMAP permit 20\nmatch ip address prefix-list INTERNET_PREFIX_LIST\nset community 0:1")
    switch_dict[s].addRoutingConfig(configStr="route-map OUT_AS_RMAP permit 30\nmatch community OUT_AS")
    switch_dict[s].addRoutingConfig(configStr="route-map IN_AS_RMAP permit 20\nmatch community IN_AS\nset community {}:1".format(as_map[s]))
    switch_dict[s].addRoutingConfig(configStr="bgp community-list standard IN_AS permit 0:1")
    switch_dict[s].addRoutingConfig(configStr="bgp community-list standard OUT_AS permit 0:1")
    switch_dict[s].addRoutingConfig(configStr="ip prefix-list INTERNET_PREFIX_LIST permit 10.0.0.0/16 ge 16")

# s0 advertise and accept routes from all AS
for i in {1, 2, 3}:
    switch_dict["s0"].addRoutingConfig(configStr="bgp community-list standard OUT_AS permit {}:1".format(i))
for i in {1, 2, 3}:
    switch_dict["s0"].addRoutingConfig(configStr="bgp community-list standard IN_AS permit {}:1".format(i))

# s1 advertise and accept routes from all AS
for i in {1, 2, 3}:
    switch_dict["s1"].addRoutingConfig(configStr="bgp community-list standard OUT_AS permit {}:1".format(i))
for i in {1, 2, 3}:
    switch_dict["s1"].addRoutingConfig(configStr="bgp community-list standard IN_AS permit {}:1".format(i))

# s2 advertise and accept routes from all AS
for i in {1, 2, 3}:
    switch_dict["s2"].addRoutingConfig(configStr="bgp community-list standard OUT_AS permit {}:1".format(i))
for i in {1, 3}:
    switch_dict["s2"].addRoutingConfig(configStr="bgp community-list standard IN_AS permit {}:1".format(i))

info('*** AS1\n')

snet_list = list()
for i in range(0, 20):
    new_snet = Subnet(ipStr="10.1.{}.0".format(i), prefixLen=24)
    snet_list.append(new_snet)

snet_counter = 0

# configure intra-AS switch-switch links

# configure host-switch links

hs_pairs = [("s0", "d0")]
for t in hs_pairs:
    sid = t[0]
    hid = t[1]

    ip1 = snet_list[snet_counter].allocateIPAddr()
    ip2 = snet_list[snet_counter].allocateIPAddr()
    net.addLink(switch_dict[sid], host_dict[hid], ip1=ip1, ip2=ip2, addr1=Subnet.ipToMac(ip1), addr2=Subnet.ipToMac(ip2))
    snet_list[snet_counter].addNode(switch_dict[sid])
    switch_dict[sid].addRoutingConfig("ospfd", "network " + snet_list[snet_counter].getNetworkPrefix() + " area {}".format(0))

    host_dict[hid].setDefaultRoute("gw {}".format(ip1.split("/")[0]))

    nodes.addNode(host_dict[hid].name, ip=ip2, nodeType="host")
    nodes.addLink(switch_dict[sid].name, host_dict[hid].name, ip1, ip2)

    # add a new advertised network prefix for the AS
    switch_dict[sid].addRoutingConfig("bgpd", "network " + snet_list[snet_counter].getNetworkPrefix())
    switch_dict[sid].addRoutingConfig(configStr="ip prefix-list AS_PREFIX_LIST permit " + snet_list[snet_counter].getNetworkPrefix())

    snet_counter += 1

snet_counter += 1

for snet in snet_list:
    snet.installSubnetTable()

info('*** AS2\n')

edge_switches = {"s1"}

snet_list = list()
for i in range(0, 20):
    new_snet = Subnet(ipStr="10.2.{}.0".format(i), prefixLen=24)
    snet_list.append(new_snet)

snet_counter = 0

# configure host-switch links

hs_pairs = [("s1", "d1")]
for t in hs_pairs:
    sid = t[0]
    hid = t[1]

    ip1 = snet_list[snet_counter].allocateIPAddr()
    ip2 = snet_list[snet_counter].allocateIPAddr()
    net.addLink(switch_dict[sid], host_dict[hid], ip1=ip1, ip2=ip2, addr1=Subnet.ipToMac(ip1), addr2=Subnet.ipToMac(ip2))
    snet_list[snet_counter].addNode(switch_dict[sid])
    switch_dict[sid].addRoutingConfig("ospfd", "network " + snet_list[snet_counter].getNetworkPrefix() + " area {}".format(0))

    host_dict[hid].setDefaultRoute("gw {}".format(ip1.split("/")[0]))

    nodes.addNode(host_dict[hid].name, ip=ip2, nodeType="host")
    nodes.addLink(switch_dict[sid].name, host_dict[hid].name, ip1, ip2)

    # add a new advertised network prefix for the AS
    switch_dict[sid].addRoutingConfig("bgpd", "network " + snet_list[snet_counter].getNetworkPrefix())
    switch_dict[sid].addRoutingConfig(configStr="ip prefix-list AS_PREFIX_LIST permit " + snet_list[snet_counter].getNetworkPrefix())

    snet_counter += 1

for snet in snet_list:
    snet.installSubnetTable()

info('*** AS3\n')

edge_switches = {"s2"}

snet_list = list()
for i in range(0, 20):
    new_snet = Subnet(ipStr="10.3.{}.0".format(i), prefixLen=24)
    snet_list.append(new_snet)

snet_counter = 0

# configure link to admin host

sid = "s2"
hid = "admin"

ip1 = snet_list[snet_counter].allocateIPAddr()
ip2 = snet_list[snet_counter].allocateIPAddr()
net.addLink(switch_dict[sid], host_dict[hid], ip1=ip1, ip2=ip2, addr1=Subnet.ipToMac(ip1), addr2=Subnet.ipToMac(ip2))
snet_list[snet_counter].addNode(switch_dict[sid])
switch_dict[sid].addRoutingConfig("ospfd", "network " + snet_list[snet_counter].getNetworkPrefix() + " area {}".format(0))

nodes.addNode(host_dict[hid].name, ip=ip2, nodeType="host")
nodes.addLink(switch_dict[sid].name, host_dict[hid].name, ip1, ip2)

host_dict[hid].setDefaultRoute("gw {}".format(ip1.split("/")[0]))

# add a new advertised network prefix for the AS
switch_dict[sid].addRoutingConfig("bgpd", "network " + snet_list[snet_counter].getNetworkPrefix())
switch_dict[sid].addRoutingConfig(configStr="ip prefix-list AS_PREFIX_LIST permit " + snet_list[snet_counter].getNetworkPrefix())

# set up admin_ip
admin_ip = ip2.split("/")[0]

for snet in snet_list:
    snet.installSubnetTable()

info('*** Exp Setup\n')

nodes.writeFile("topo.txt")
os.system("docker cp /m/local2/wcr/Diagnosis-driver/driver.tar.bz mn.admin:/")
os.system("docker cp /m/local2/wcr/Mininet-Emulab/topo.txt mn.admin:/")

print("tar: ", host_dict["admin"].cmd("tar -xf /driver.tar.bz -C /"))
print("install dns: ", host_dict["admin"].cmd("python3 /network_graph.py /topo.txt"))

info('*** Starting network\n')

for host in host_dict.values():
    host.start()

for switch in switch_dict.keys():
    switch_dict[switch].setAdminConfig(admin_ip, fault_report_collection_port)
    switch_dict[switch].start()

net.start()

info('*** Running CLI\n')

CLI(net)

info('*** Stopping network')

net.stop()