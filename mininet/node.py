"""
Node objects for Mininet.

Nodes provide a simple abstraction for interacting with hosts, switches
and controllers. Local nodes are simply one or more processes on the local
machine.

Node: superclass for all (primarily local) network nodes.

Host: a virtual host. By default, a host is simply a shell; commands
    may be sent using Cmd (which waits for output), or using sendCmd(),
    which returns immediately, allowing subsequent monitoring using
    monitor(). Examples of how to run experiments using this
    functionality are provided in the examples/ directory. By default,
    hosts share the root file system, but they may also specify private
    directories.

CPULimitedHost: a virtual host whose CPU bandwidth is limited by
    RT or CFS bandwidth limiting.

Switch: superclass for switch nodes.

UserSwitch: a switch using the user-space switch from the OpenFlow
    reference implementation.

OVSSwitch: a switch using the Open vSwitch OpenFlow-compatible switch
    implementation (openvswitch.org).

OVSBridge: an Ethernet bridge implemented using Open vSwitch.
    Supports STP.

IVSSwitch: OpenFlow switch using the Indigo Virtual Switch.

Controller: superclass for OpenFlow controllers. The default controller
    is controller(8) from the reference implementation.

OVSController: The test controller from Open vSwitch.

NOXController: a controller node using NOX (noxrepo.org).

Ryu: The Ryu controller (https://osrg.github.io/ryu/)

RemoteController: a remote controller node, which may use any
    arbitrary OpenFlow-compatible controller, and which is not
    created or managed by Mininet.

Future enhancements:

- Possibly make Node, Switch and Controller more abstract so that
  they can be used for both local and remote nodes

- Create proxy objects for remote nodes (Mininet: Cluster Edition)
"""
import errno
import os
import pty
import re
import signal
import select
import docker
import json
import time
from subprocess import Popen, PIPE, check_output
from time import sleep

from mininet.log import info, error, warn, debug
from mininet.util import ( quietRun, errRun, errFail, moveIntf, isShellBuiltin,
                           numCores, retry, mountCgroups, BaseString, decode,
                           encode, Python3, which, makeIntfPair )
from mininet.moduledeps import moduleDeps, pathCheck, TUN
from mininet.link import Link, Intf, TCIntf, OVSIntf
from mininet.config import Subnet
from re import findall
from distutils.version import StrictVersion

class Node( object ):
    """A virtual network node is simply a shell in a network namespace.
       We communicate with it using pipes."""

    portBase = 0  # Nodes always start with eth0/port0, even in OF 1.0

    def __init__( self, name, inNamespace=True, **params ):
        """name: name of node
           inNamespace: in network namespace?
           privateDirs: list of private directory strings or tuples
           params: Node parameters (see config() for details)"""

        # Make sure class actually works
        self.checkSetup()

        self.name = params.get( 'name', name )
        self.privateDirs = params.get( 'privateDirs', [] )
        self.inNamespace = params.get( 'inNamespace', inNamespace )

        # Python 3 complains if we don't wait for shell exit
        self.waitExited = params.get( 'waitExited', Python3 )

        # Stash configuration parameters for future reference
        self.params = params

        self.intfs = {}  # dict of port numbers to interfaces
        self.ports = {}  # dict of interfaces to port numbers
                         # replace with Port objects, eventually ?
        self.nameToIntf = {}  # dict of interface names to Intfs

        # Make pylint happy
        ( self.shell, self.execed, self.pid, self.stdin, self.stdout,
            self.lastPid, self.lastCmd, self.pollOut ) = (
                None, None, None, None, None, None, None, None )
        self.waiting = False
        self.readbuf = ''

        # Start command interpreter shell
        self.master, self.slave = None, None  # pylint
        self.startShell()
        self.mountPrivateDirs()

    # File descriptor to node mapping support
    # Class variables and methods

    inToNode = {}  # mapping of input fds to nodes
    outToNode = {}  # mapping of output fds to nodes

    @classmethod
    def fdToNode( cls, fd ):
        """Return node corresponding to given file descriptor.
           fd: file descriptor
           returns: node"""
        node = cls.outToNode.get( fd )
        return node or cls.inToNode.get( fd )

    # Command support via shell process in namespace
    def startShell( self, mnopts=None ):
        "Start a shell process for running commands"
        if self.shell:
            error( "%s: shell is already running\n" % self.name )
            return
        # mnexec: (c)lose descriptors, (d)etach from tty,
        # (p)rint pid, and run in (n)amespace
        opts = '-cd' if mnopts is None else mnopts
        if self.inNamespace:
            opts += 'n'
        # bash -i: force interactive
        # -s: pass $* to shell, and make process easy to find in ps
        # prompt is set to sentinel chr( 127 )
        cmd = [ 'mnexec', opts, 'env', 'PS1=' + chr( 127 ),
                'bash', '--norc', '--noediting',
                '-is', 'mininet:' + self.name ]

        # Spawn a shell subprocess in a pseudo-tty, to disable buffering
        # in the subprocess and insulate it from signals (e.g. SIGINT)
        # received by the parent
        self.master, self.slave = pty.openpty()
        self.shell = self._popen( cmd, stdin=self.slave, stdout=self.slave,
                                  stderr=self.slave, close_fds=False )
        # XXX BL: This doesn't seem right, and we should also probably
        # close our files when we exit...
        self.stdin = os.fdopen( self.master, 'r' )
        self.stdout = self.stdin
        self.pid = self.shell.pid
        self.pollOut = select.poll()
        self.pollOut.register( self.stdout )
        # Maintain mapping between file descriptors and nodes
        # This is useful for monitoring multiple nodes
        # using select.poll()
        self.outToNode[ self.stdout.fileno() ] = self
        self.inToNode[ self.stdin.fileno() ] = self
        self.execed = False
        self.lastCmd = None
        self.lastPid = None
        self.readbuf = ''
        # Wait for prompt
        while True:
            data = self.read( 1024 )
            if data[ -1 ] == chr( 127 ):
                break
            self.pollOut.poll()
        self.waiting = False
        # +m: disable job control notification
        self.cmd( 'unset HISTFILE; stty -echo; set +m' )

    def mountPrivateDirs( self ):
        "mount private directories"
        # Avoid expanding a string into a list of chars
        assert not isinstance( self.privateDirs, BaseString )
        for directory in self.privateDirs:
            if isinstance( directory, tuple ):
                # mount given private directory
                privateDir = directory[ 1 ] % self.__dict__
                mountPoint = directory[ 0 ]
                self.cmd( 'mkdir -p %s' % privateDir )
                self.cmd( 'mkdir -p %s' % mountPoint )
                self.cmd( 'mount --bind %s %s' %
                               ( privateDir, mountPoint ) )
            else:
                # mount temporary filesystem on directory
                self.cmd( 'mkdir -p %s' % directory )
                self.cmd( 'mount -n -t tmpfs tmpfs %s' % directory )

    def unmountPrivateDirs( self ):
        "mount private directories"
        for directory in self.privateDirs:
            if isinstance( directory, tuple ):
                self.cmd( 'umount ', directory[ 0 ] )
            else:
                self.cmd( 'umount ', directory )

    def _popen( self, cmd, **params ):
        """Internal method: spawn and return a process
            cmd: command to run (list)
            params: parameters to Popen()"""
        # Leave this is as an instance method for now
        assert self
        popen = Popen( cmd, **params )
        debug( '_popen', cmd, popen.pid )
        return popen

    def cleanup( self ):
        "Help python collect its garbage."
        # We used to do this, but it slows us down:
        # Intfs may end up in root NS
        # for intfName in self.intfNames():
        # if self.name in intfName:
        # quietRun( 'ip link del ' + intfName )
        if self.shell:
            # Close ptys
            self.stdin.close()
            # os.close(self.slave)
            if self.waitExited:
                debug( 'waiting for', self.pid, 'to terminate\n' )
                self.shell.wait()
        self.shell = None
        if self.master:
            self.stdin.close()
            self.master = None
            self.stdin = None
            self.stdout = None
        if self.slave:
            os.close(self.slave)
            self.slave = None

    # Subshell I/O, commands and control

    def read( self, maxbytes=1024 ):
        """Buffered read from node, potentially blocking.
           maxbytes: maximum number of bytes to return"""
        count = len( self.readbuf )
        if count < maxbytes:
            data = decode( os.read( self.stdout.fileno(), maxbytes - count ) )
            self.readbuf += data
        if maxbytes >= len( self.readbuf ):
            result = self.readbuf
            self.readbuf = ''
        else:
            result = self.readbuf[ :maxbytes ]
            self.readbuf = self.readbuf[ maxbytes: ]
        return result

    def readline( self ):
        """Buffered readline from node, potentially blocking.
           returns: line (minus newline) or None"""
        self.readbuf += self.read( 1024 )
        if '\n' not in self.readbuf:
            return None
        pos = self.readbuf.find( '\n' )
        line = self.readbuf[ 0: pos ]
        self.readbuf = self.readbuf[ pos + 1: ]
        return line

    def write( self, data ):
        """Write data to node.
           data: string"""
        os.write( self.stdin.fileno(), encode( data ) )

    def terminate( self ):
        "Send kill signal to Node and clean up after it."
        self.unmountPrivateDirs()
        if self.shell:
            if self.shell.poll() is None:
                os.killpg( self.shell.pid, signal.SIGHUP )
        self.cleanup()

    def stop( self, deleteIntfs=False ):
        """Stop node.
           deleteIntfs: delete interfaces? (False)"""
        if deleteIntfs:
            self.deleteIntfs()
        self.terminate()

    def waitReadable( self, timeoutms=None ):
        """Wait until node's output is readable.
           timeoutms: timeout in ms or None to wait indefinitely.
           returns: result of poll()"""
        if len( self.readbuf ) == 0:
            return self.pollOut.poll( timeoutms )

    def sendCmd( self, *args, **kwargs ):
        """Send a command, followed by a command to echo a sentinel,
           and return without waiting for the command to complete.
           args: command and arguments, or string
           printPid: print command's PID? (False)"""
        # be a bit more relaxed here and allow to wait 120s for the shell
        cnt = 0
        while (self.waiting and cnt < 5 * 120):
            debug("Waiting for shell to unblock...")
            time.sleep(.2)
            cnt += 1
        if cnt > 0:
            warn("Shell unblocked after {:.2f}s"
                 .format(float(cnt)/5))
        assert self.shell and not self.waiting
        printPid = kwargs.get( 'printPid', False )
        # Allow sendCmd( [ list ] )
        if len( args ) == 1 and isinstance( args[ 0 ], list ):
            cmd = args[ 0 ]
        # Allow sendCmd( cmd, arg1, arg2... )
        elif len( args ) > 0:
            cmd = args
        # Convert to string
        if not isinstance( cmd, str ):
            cmd = ' '.join( [ str( c ) for c in cmd ] )
        if not re.search( r'\w', cmd ):
            # Replace empty commands with something harmless
            cmd = 'echo -n'
        self.lastCmd = cmd
        # if a builtin command is backgrounded, it still yields a PID
        if len( cmd ) > 0 and cmd[ -1 ] == '&':
            # print ^A{pid}\n so monitor() can set lastPid
            cmd += ' printf "\\001%d\\012" $! '
        elif printPid and not isShellBuiltin( cmd ):
            cmd = 'mnexec -p ' + cmd
        #info('execute cmd: {0}'.format(cmd))
        self.write( cmd + '\n' )
        self.lastPid = None
        self.waiting = True

    def sendInt( self, intr=chr( 3 ) ):
        "Interrupt running command."
        debug( 'sendInt: writing chr(%d)\n' % ord( intr ) )
        self.write( intr )

    def monitor( self, timeoutms=None, findPid=True ):
        """Monitor and return the output of a command.
           Set self.waiting to False if command has completed.
           timeoutms: timeout in ms or None to wait indefinitely
           findPid: look for PID from mnexec -p"""
        ready = self.waitReadable( timeoutms )
        if not ready:
            return ''
        data = self.read( 1024 )
        pidre = r'\[\d+\] \d+\r\n'
        # Look for PID
        marker = chr( 1 ) + r'\d+\r\n'
        if findPid and chr( 1 ) in data:
            # suppress the job and PID of a backgrounded command
            if re.findall( pidre, data ):
                data = re.sub( pidre, '', data )
            # Marker can be read in chunks; continue until all of it is read
            while not re.findall( marker, data ):
                data += self.read( 1024 )
            markers = re.findall( marker, data )
            if markers:
                self.lastPid = int( markers[ 0 ][ 1: ] )
                data = re.sub( marker, '', data )
        # Look for sentinel/EOF
        if len( data ) > 0 and data[ -1 ] == chr( 127 ):
            self.waiting = False
            data = data[ :-1 ]
        elif chr( 127 ) in data:
            self.waiting = False
            data = data.replace( chr( 127 ), '' )
        return data

    def waitOutput( self, verbose=False, findPid=True ):
        """Wait for a command to complete.
           Completion is signaled by a sentinel character, ASCII(127)
           appearing in the output stream.  Wait for the sentinel and return
           the output, including trailing newline.
           verbose: print output interactively"""
        log = info if verbose else debug
        output = ''
        while self.waiting:
            data = self.monitor( findPid=findPid )
            output += data
            log( data )
        return output

    def cmd( self, *args, **kwargs ):
        """Send a command, wait for output, and return it.
           cmd: string"""
        verbose = kwargs.get( 'verbose', False )
        log = info if verbose else debug
        log( '*** %s : %s\n' % ( self.name, args ) )
        if self.shell:
            self.shell.poll()
            if self.shell.returncode is not None:
                print("shell died on ", self.name)
                self.shell = None
                self.startShell()
            self.sendCmd( *args, **kwargs )
            return self.waitOutput( verbose )
        else:
            warn( '(%s exited - ignoring cmd%s)\n' % ( self, args ) )

    def cmdPrint( self, *args):
        """Call cmd and printing its output
           cmd: string"""
        return self.cmd( *args, **{ 'verbose': True } )

    def popen( self, *args, **kwargs ):
        """Return a Popen() object in our namespace
           args: Popen() args, single list, or string
           kwargs: Popen() keyword args"""
        defaults = { 'stdout': PIPE, 'stderr': PIPE,
                     'mncmd':
                     [ 'mnexec', '-da', str( self.pid ) ] }
        defaults.update( kwargs )
        shell = defaults.pop( 'shell', False )
        if len( args ) == 1:
            if isinstance( args[ 0 ], list ):
                # popen([cmd, arg1, arg2...])
                cmd = args[ 0 ]
            elif isinstance( args[ 0 ], BaseString ):
                # popen("cmd arg1 arg2...")
                cmd = [ args[ 0 ] ] if shell else args[ 0 ].split()
            else:
                raise Exception( 'popen() requires a string or list' )
        elif len( args ) > 0:
            # popen( cmd, arg1, arg2... )
            cmd = list( args )
        if shell:
            cmd = [ os.environ[ 'SHELL' ], '-c' ] + [ ' '.join( cmd ) ]
        # Attach to our namespace  using mnexec -a
        cmd = defaults.pop( 'mncmd' ) + cmd
        ### Shell requires a string, not a list!
        # if defaults.get( 'shell', False ):
        #     cmd = ' '.join( cmd )
        popen = self._popen( cmd, **defaults )
        return popen

    def pexec( self, *args, **kwargs ):
        """Execute a command using popen
           returns: out, err, exitcode"""
        popen = self.popen( *args, stdin=PIPE, stdout=PIPE, stderr=PIPE,
                            **kwargs )
        # Warning: this can fail with large numbers of fds!
        out, err = popen.communicate()
        exitcode = popen.wait()
        return decode( out ), decode( err ), exitcode

    # Interface management, configuration, and routing

    # BL notes: This might be a bit redundant or over-complicated.
    # However, it does allow a bit of specialization, including
    # changing the canonical interface names. It's also tricky since
    # the real interfaces are created as veth pairs, so we can't
    # make a single interface at a time.

    def newPort( self ):
        "Return the next port number to allocate."
        if len( self.ports ) > 0:
            return max( self.ports.values() ) + 1
        return self.portBase

    def addIntf( self, intf, port=None, moveIntfFn=moveIntf ):
        """Add an interface.
           intf: interface
           port: port number (optional, typically OpenFlow port number)
           moveIntfFn: function to move interface (optional)"""
        if port is None:
            port = self.newPort()
        self.intfs[ port ] = intf
        self.ports[ intf ] = port
        self.nameToIntf[ intf.name ] = intf
        debug( '\n' )
        debug( 'added intf %s (%d) to node %s\n' % (
                intf, port, self.name ) )
        if self.inNamespace:
            debug( 'moving', intf, 'into namespace for', self.name, '\n' )
            moveIntfFn( intf.name, self  )

    def delIntf( self, intf ):
        """Remove interface from Node's known interfaces
           Note: to fully delete interface, call intf.delete() instead"""
        port = self.ports.get( intf )
        if port is not None:
            del self.intfs[ port ]
            del self.ports[ intf ]
            del self.nameToIntf[ intf.name ]

    def defaultIntf( self ):
        "Return interface for lowest port"
        ports = self.intfs.keys()
        if ports:
            return self.intfs[ min( ports ) ]
        else:
            warn( '*** defaultIntf: warning:', self.name,
                  'has no interfaces\n' )

    def intf( self, intf=None ):
        """Return our interface object with given string name,
           default intf if name is falsy (None, empty string, etc).
           or the input intf arg.

        Having this fcn return its arg for Intf objects makes it
        easier to construct functions with flexible input args for
        interfaces (those that accept both string names and Intf objects).
        """
        if not intf:
            return self.defaultIntf()
        elif isinstance( intf, BaseString):
            return self.nameToIntf[ intf ]
        else:
            return intf

    def connectionsTo( self, node):
        "Return [ intf1, intf2... ] for all intfs that connect self to node."
        # We could optimize this if it is important
        connections = []
        for intf in self.intfList():
            link = intf.link
            if link:
                node1, node2 = link.intf1.node, link.intf2.node
                if node1 == self and node2 == node:
                    connections += [ ( intf, link.intf2 ) ]
                elif node1 == node and node2 == self:
                    connections += [ ( intf, link.intf1 ) ]
        return connections

    def deleteIntfs( self, checkName=True ):
        """Delete all of our interfaces.
           checkName: only delete interfaces that contain our name"""
        # In theory the interfaces should go away after we shut down.
        # However, this takes time, so we're better off removing them
        # explicitly so that we won't get errors if we run before they
        # have been removed by the kernel. Unfortunately this is very slow,
        # at least with Linux kernels before 2.6.33
        for intf in list( self.intfs.values() ):
            # Protect against deleting hardware interfaces
            if ( self.name in intf.name ) or ( not checkName ):
                intf.delete()
                info( '.' )

    # Routing support

    def setARP( self, ip, mac ):
        """Add an ARP entry.
           ip: IP address as string
           mac: MAC address as string"""
        result = self.cmd( 'arp', '-s', ip, mac )
        return result

    def setHostRoute( self, ip, intf ):
        """Add route to host.
           ip: IP address as dotted decimal
           intf: string, interface name"""
        return self.cmd( 'route add -host', ip, 'dev', intf )

    def setDefaultRoute( self, intf=None ):
        """Set the default route to go through intf.
           intf: Intf or {dev <intfname> via <gw-ip> ...}"""
        # Note setParam won't call us if intf is none
        if isinstance( intf, BaseString ):
            params = intf
        else:
            params = 'dev %s' % intf
        # Do this in one line in case we're messing with the root namespace
        self.cmd( 'ip route del default; route add default ', params )

    # Convenience and configuration methods

    def setMAC( self, mac, intf=None ):
        """Set the MAC address for an interface.
           intf: intf or intf name
           mac: MAC address as string"""
        return self.intf( intf ).setMAC( mac )

    def setIP( self, ip, prefixLen=8, intf=None, **kwargs ):
        """Set the IP address for an interface.
           intf: intf or intf name
           ip: IP address as a string
           prefixLen: prefix length, e.g. 8 for /8 or 16M addrs
           kwargs: any additional arguments for intf.setIP"""
        return self.intf( intf ).setIP( ip, prefixLen, **kwargs )

    def IP( self, intf=None ):
        "Return IP address of a node or specific interface."
        return self.intf( intf ).IP()

    def MAC( self, intf=None ):
        "Return MAC address of a node or specific interface."
        return self.intf( intf ).MAC()

    def intfIsUp( self, intf=None ):
        "Check if an interface is up."
        return self.intf( intf ).isUp()

    # The reason why we configure things in this way is so
    # That the parameters can be listed and documented in
    # the config method.
    # Dealing with subclasses and superclasses is slightly
    # annoying, but at least the information is there!

    def setParam( self, results, method, **param ):
        """Internal method: configure a *single* parameter
           results: dict of results to update
           method: config method name
           param: arg=value (ignore if value=None)
           value may also be list or dict"""
        name, value = list( param.items() )[ 0 ]
        if value is None:
            return
        f = getattr( self, method, None )
        if not f:
            return
        if isinstance( value, list ):
            result = f( *value )
        elif isinstance( value, dict ):
            result = f( **value )
        else:
            result = f( value )
        results[ name ] = result
        return result

    def config( self, mac=None, ip=None,
                defaultRoute=None, lo='up', **_params ):
        """Configure Node according to (optional) parameters:
           mac: MAC address for default interface
           ip: IP address for default interface
           ifconfig: arbitrary interface configuration
           Subclasses should override this method and call
           the parent class's config(**params)"""
        # If we were overriding this method, we would call
        # the superclass config method here as follows:
        # r = Parent.config( **_params )
        r = {}
        self.setParam( r, 'setMAC', mac=mac )
        self.setParam( r, 'setIP', ip=ip )
        self.setParam( r, 'setDefaultRoute', defaultRoute=defaultRoute )
        # This should be examined
        self.cmd( 'ifconfig lo ' + lo )
        return r

    def configDefault( self, **moreParams ):
        "Configure with default parameters"
        self.params.update( moreParams )
        self.config( **self.params )

    # This is here for backward compatibility
    def linkTo( self, node, link=Link ):
        """(Deprecated) Link to another node
           replace with Link( node1, node2)"""
        return link( self, node )

    # Other methods

    def intfList( self ):
        "List of our interfaces sorted by port number"
        return [ self.intfs[ p ] for p in sorted( self.intfs.keys() ) ]

    def intfNames( self ):
        "The names of our interfaces sorted by port number"
        return [ str( i ) for i in self.intfList() ]

    def __repr__( self ):
        "More informative string representation"
        intfs = ( ','.join( [ '%s:%s' % ( i.name, i.IP() )
                              for i in self.intfList() ] ) )
        return '<%s %s: %s pid=%s> ' % (
            self.__class__.__name__, self.name, intfs, self.pid )

    def __str__( self ):
        "Abbreviated string representation"
        return self.name

    # Automatic class setup support

    isSetup = False

    @classmethod
    def checkSetup( cls ):
        "Make sure our class and superclasses are set up"
        while cls and not getattr( cls, 'isSetup', True ):
            cls.setup()
            cls.isSetup = True
            # Make pylint happy
            cls = getattr( type( cls ), '__base__', None )

    @classmethod
    def setup( cls ):
        "Make sure our class dependencies are available"
        pathCheck( 'mnexec', 'ifconfig', moduleName='Mininet')


class Host( Node ):
    "A host is simply a Node"
    pass


class Docker ( Node ):
    """Node that represents a docker container.
    This part is inspired by:
    http://techandtrains.com/2014/08/21/docker-container-as-mininet-host/
    We use the docker-py client library to control docker.
    """

    def __init__(self, name, dimage=None, dcmd=None, build_params={},
                 **kwargs):
        """
        Creates a Docker container as Mininet host.

        Resource limitations based on CFS scheduler:
        * cpu.cfs_quota_us: the total available run-time within a period (in microseconds)
        * cpu.cfs_period_us: the length of a period (in microseconds)
        (https://www.kernel.org/doc/Documentation/scheduler/sched-bwc.txt)

        Default Docker resource limitations:
        * cpu_shares: Relative amount of max. avail CPU for container
            (not a hard limit, e.g. if only one container is busy and the rest idle)
            e.g. usage: d1=4 d2=6 <=> 40% 60% CPU
        * cpuset_cpus: Bind containers to CPU 0 = cpu_1 ... n-1 = cpu_n (string: '0,2')
        * mem_limit: Memory limit (format: <number>[<unit>], where unit = b, k, m or g)
        * memswap_limit: Total limit = memory + swap

        All resource limits can be updated at runtime! Use:
        * updateCpuLimits(...)
        * updateMemoryLimits(...)
        """
        self.dimage = dimage
        self.dnameprefix = "mn"
        self.dcmd = dcmd if dcmd is not None else "/bin/bash"
        self.dc = None  # pointer to the dict containing 'Id' and 'Warnings' keys of the container
        self.dcinfo = None
        self.did = None # Id of running container
        #  let's store our resource limits to have them available through the
        #  Mininet API later on
        defaults = { 'cpu_quota': -1,
                     'cpu_period': None,
                     'cpu_shares': None,
                     'cpuset_cpus': None,
                     'mem_limit': None,
                     'memswap_limit': None,
                     'environment': {},
                     'volumes': [],  # use ["/home/user1/:/mnt/vol2:rw"]
                     'tmpfs': [], # use ["/home/vol1/:size=3G,uid=1000"]
                     'network_mode': None,
                     'publish_all_ports': True,
                     'port_bindings': {},
                     'ports': [],
                     'dns': [],
                     'ipc_mode': None,
                     'devices': [],
                     'cap_add': [],
                     'sysctls': {}
                     }
        defaults.update( kwargs )

        # keep resource in a dict for easy update during container lifetime
        self.resources = dict(
            cpu_quota=defaults['cpu_quota'],
            cpu_period=defaults['cpu_period'],
            cpu_shares=defaults['cpu_shares'],
            cpuset_cpus=defaults['cpuset_cpus'],
            mem_limit=defaults['mem_limit'],
            memswap_limit=defaults['memswap_limit']
        )

        self.volumes = defaults['volumes']
        self.tmpfs = defaults['tmpfs']
        self.environment = {} if defaults['environment'] is None else defaults['environment']
        # setting PS1 at "docker run" may break the python docker api (update_container hangs...)
        # self.environment.update({"PS1": chr(127)})  # CLI support
        self.network_mode = defaults['network_mode']
        self.publish_all_ports = defaults['publish_all_ports']
        self.port_bindings = defaults['port_bindings']
        self.dns = defaults['dns']
        self.ipc_mode = defaults['ipc_mode']
        self.devices = defaults['devices']
        self.cap_add = defaults['cap_add']
        self.sysctls = defaults['sysctls']

        # setup docker client
        # self.dcli = docker.APIClient(base_url='unix://var/run/docker.sock')
        self.d_client = docker.from_env()
        self.dcli = self.d_client.api

        _id = None
        if build_params.get("path", None):
            if not build_params.get("tag", None):
                if dimage:
                    build_params["tag"] = dimage
            _id, output = self.build(**build_params)
            dimage = _id
            self.dimage = _id
            info("Docker image built: id: {},  {}. Output:\n".format(
                _id, build_params.get("tag", None)))
            info(output)

        # pull image if it does not exist
        self._check_image_exists(dimage, True, _id=None)

        # for DEBUG
        debug("Created docker container object %s\n" % name)
        debug("image: %s\n" % str(self.dimage))
        debug("dcmd: %s\n" % str(self.dcmd))
        info("%s: kwargs %s\n" % (name, str(kwargs)))

        # creats host config for container
        # see: https://docker-py.readthedocs.io/en/2.0.2/api.html#module-docker.api.container
        hc = self.dcli.create_host_config(
            network_mode=self.network_mode,
            privileged=True,  # we need this to allow mininet network setup
            binds=self.volumes,
            tmpfs=self.tmpfs,
            publish_all_ports=self.publish_all_ports,
            port_bindings=self.port_bindings,
            mem_limit=self.resources.get('mem_limit'),
            cpuset_cpus=self.resources.get('cpuset_cpus'),
            dns=self.dns,
            ipc_mode=self.ipc_mode,  # string
            devices=self.devices,  # see docker-py docu
            cap_add=self.cap_add,  # see docker-py docu
            sysctls=self.sysctls   # see docker-py docu
            
        )

        if kwargs.get("rm", False):
            container_list = self.dcli.containers(all=True)
            for container in container_list:
                for container_name in container.get("Names", []):
                    if "%s.%s" % (self.dnameprefix, name) in container_name:
                        self.dcli.remove_container(container="%s.%s" % (self.dnameprefix, name), force=True)
                        break

        # create new docker container
        self.dc = self.dcli.create_container(
            name="%s.%s" % (self.dnameprefix, name),
            image=self.dimage,
            command=self.dcmd,
            entrypoint=list(),  # overwrite (will be executed manually at the end)
            stdin_open=True,  # keep container open
            tty=True,  # allocate pseudo tty
            environment=self.environment,
            #network_disabled=True,  # docker stats breaks if we disable the default network
            networking_config=self.dcli.create_networking_config({'bridge': self.dcli.create_endpoint_config()}),
            host_config=hc,
            ports=defaults['ports'],
            labels=['com.containernet'],
            volumes=[self._get_volume_mount_name(v) for v in self.volumes if self._get_volume_mount_name(v) is not None],
            hostname=name
        )

        # start the container
        self.dcli.start(self.dc)
        debug("Docker container %s started\n" % name)

        # fetch information about new container
        self.dcinfo = self.dcli.inspect_container(self.dc)
        self.did = self.dcinfo.get("Id")

        # call original Node.__init__
        Host.__init__(self, name, **kwargs)

        # let's initially set our resource limits
        # self.update_resources(**self.resources)

        self.master = None
        self.slave = None

    def build(self, **kwargs):
        image, output = self.d_client.images.build(**kwargs)
        output_str = parse_build_output(output)
        return image.id, output_str

    def start(self):
        # start ssh service
        self.cmd("service ssh start")

        # disable NIC offloading for all features
        for port, intf in self.intfs.items():
            # disable all NIC offloading for features
            self.cmd("ethtool --offload {} rx off".format(intf.name))
            self.cmd("ethtool --offload {} tx off".format(intf.name))

        self.cmd("iptables -t mangle -A OUTPUT -p icmp -j TOS --set-tos 0x00")

    # Command support via shell process in namespace
    def startShell( self, *args, **kwargs ):
        "Start a shell process for running commands"
        if self.shell:
            error( "%s: shell is already running\n" % self.name )
            return
        # mnexec: (c)lose descriptors, (d)etach from tty,
        # (p)rint pid, and run in (n)amespace
        # opts = '-cd' if mnopts is None else mnopts
        # if self.inNamespace:
        #     opts += 'n'
        # bash -i: force interactive
        # -s: pass $* to shell, and make process easy to find in ps
        # prompt is set to sentinel chr( 127 )
        cmd = [ 'docker', 'exec', '-it',  '%s.%s' % ( self.dnameprefix, self.name ), 'env', 'PS1=' + chr( 127 ),
                'bash', '--norc', '-is', 'mininet:' + self.name ]
        # Spawn a shell subprocess in a pseudo-tty, to disable buffering
        # in the subprocess and insulate it from signals (e.g. SIGINT)
        # received by the parent
        self.master, self.slave = pty.openpty()
        self.shell = self._popen( cmd, stdin=self.slave, stdout=self.slave, stderr=self.slave,
                                  close_fds=False )
        self.stdin = os.fdopen( self.master, 'r' )
        self.stdout = self.stdin
        self.pid = self._get_pid()
        self.pollOut = select.poll()
        self.pollOut.register( self.stdout )
        # Maintain mapping between file descriptors and nodes
        # This is useful for monitoring multiple nodes
        # using select.poll()
        self.outToNode[ self.stdout.fileno() ] = self
        self.inToNode[ self.stdin.fileno() ] = self
        self.execed = False
        self.lastCmd = None
        self.lastPid = None
        self.readbuf = ''
        # Wait for prompt
        while True:
            data = self.read( 1024 )
            if data[ -1 ] == chr( 127 ):
                break
            self.pollOut.poll()
        self.waiting = False
        # +m: disable job control notification
        self.cmd( 'unset HISTFILE; stty -echo; set +m' )

    def _get_volume_mount_name(self, volume_str):
        """ Helper to extract mount names from volume specification strings """
        parts = volume_str.split(":")
        if len(parts) < 3:
            return None
        return parts[1]

    def terminate( self ):
        """ Stop docker container """
        if not self._is_container_running():
            return
        try:
            self.dcli.remove_container(self.dc, force=True, v=True)
        except docker.errors.APIError as e:
            warn("Warning: API error during container removal.\n")

        self.cleanup()

    def sendCmd( self, *args, **kwargs ):
        """Send a command, followed by a command to echo a sentinel,
           and return without waiting for the command to complete."""
        self._check_shell()
        if not self.shell:
            return
        Host.sendCmd( self, *args, **kwargs )

    def popen( self, *args, **kwargs ):
        """Return a Popen() object in node's namespace
           args: Popen() args, single list, or string
           kwargs: Popen() keyword args"""
        if not self._is_container_running():
            error( "ERROR: Can't connect to Container \'%s\'' for docker host \'%s\'!\n" % (self.did, self.name) )
            return
        mncmd = ["docker", "exec", "-t", "%s.%s" % (self.dnameprefix, self.name)]
        return Host.popen( self, *args, mncmd=mncmd, **kwargs )

    def cmd(self, *args, **kwargs ):
        """Send a command, wait for output, and return it.
           cmd: string"""
        verbose = kwargs.get( 'verbose', False )
        log = info if verbose else debug
        log( '*** %s : %s\n' % ( self.name, args ) )
        self.sendCmd( *args, **kwargs )
        return self.waitOutput( verbose )

    def _get_pid(self):
        state = self.dcinfo.get("State", None)
        if state:
            return state.get("Pid", -1)
        return -1

    def _check_shell(self):
        """Verify if shell is alive and
           try to restart if needed"""
        if self._is_container_running():
            if self.shell:
                self.shell.poll()
                if self.shell.returncode is not None:
                    debug("*** Shell died for docker host \'%s\'!\n" % self.name )
                    self.shell = None
                    debug("*** Restarting Shell of docker host \'%s\'!\n" % self.name )
                    self.startShell()
            else:
                debug("*** Restarting Shell of docker host \'%s\'!\n" % self.name )
                self.startShell()
        else:
            error( "ERROR: Can't connect to Container \'%s\'' for docker host \'%s\'!\n" % (self.did, self.name) )
            if self.shell:
                self.shell = None

    def _is_container_running(self):
        """Verify if container is alive"""
        container_list = self.dcli.containers(filters={"id": self.did, "status": "running"})
        if len(container_list) == 0:
            return False
        return True

    def _check_image_exists(self, imagename=None, pullImage=False, _id=None):
        # split tag from repository if a tag is specified
        if imagename:
            if ":" in imagename:
                #If two :, then the first is to specify a port. Otherwise, it must be a tag
                slices = imagename.split(":")
                repo = ":".join(slices[0:-1])
                tag = slices[-1]
            else:
                repo = imagename
                tag = "latest"
        else:
            repo, tag = "None", "None"

        if self._image_exists(repo, tag, _id):
            return True

        # image not found
        if pullImage:
            if self._pull_image(repo, tag):
                info('*** Download of "%s:%s" successful\n' % (repo, tag))
                return True
        # we couldn't find the image
        return False

    def _image_exists(self, repo, tag, _id=None):
        """
        Checks if the repo:tag image exists locally
        :return: True if the image exists locally. Else false.
        """
        print("1: ")
        images = self.dcli.images()
        imageTag = "%s:%s" % (repo, tag)
        for image in images:
            if image.get("RepoTags", None):
                if imageTag in image.get("RepoTags", []):
                    debug("Image '{}' exists.\n".format(imageTag))
                    return True
            if image.get("Id", None):
                print("; ".join([str(repo), str(tag), str(_id), str(image.get("Id"))]))
                if image.get("Id") == _id:
                    return True
        return False

    def _pull_image(self, repository, tag):
        """
        :return: True if pull was successful. Else false.
        """
        try:
            info('*** Image "%s:%s" not found. Trying to load the image. \n' % (repository, tag))
            info('*** This can take some minutes...\n')

            message = ""
            for line in self.dcli.pull(repository, tag, stream=True):
                # Collect output of the log for enhanced error feedback
                message = message + json.dumps(json.loads(line), indent=4)

        except BaseException as ex:
            error('*** error: _pull_image: %s:%s failed.' % (repository, tag)
                  + message)
        #if not self._image_exists(repository, tag):
        #    error('*** error: _pull_image: %s:%s failed.' % (repository, tag)
        #          + message)
        #    return False
        return True

    def update_resources(self, **kwargs):
        """
        Update the container's resources using the docker.update function
        re-using the same parameters:
        Args:
           blkio_weight
           cpu_period, cpu_quota, cpu_shares
           cpuset_cpus
           cpuset_mems
           mem_limit
           mem_reservation
           memswap_limit
           kernel_memory
           restart_policy
        see https://docs.docker.com/engine/reference/commandline/update/
        or API docs: https://docker-py.readthedocs.io/en/stable/api.html#module-docker.api.container
        :return:
        """

        self.resources.update(kwargs)
        # filter out None values to avoid errors
        resources_filtered = {res:self.resources[res] for res in self.resources if self.resources[res] is not None}
        info("{1}: update resources {0}\n".format(resources_filtered, self.name))
        self.dcli.update_container(self.dc, **resources_filtered)


    def updateCpuLimit(self, cpu_quota=-1, cpu_period=-1, cpu_shares=-1, cores=None):
        """
        Update CPU resource limitations.
        This method allows to update resource limitations at runtime by bypassing the Docker API
        and directly manipulating the cgroup options.
        Args:
            cpu_quota: cfs quota us
            cpu_period: cfs period us
            cpu_shares: cpu shares
            cores: specifies which cores should be accessible for the container e.g. "0-2,16" represents
                Cores 0, 1, 2, and 16
        """
        # see https://www.kernel.org/doc/Documentation/scheduler/sched-bwc.txt

        # also negative values can be set for cpu_quota (uncontrained setting)
        # just check if value is a valid integer
        if isinstance(cpu_quota, int):
            self.resources['cpu_quota'] = self.cgroupSet("cfs_quota_us", cpu_quota)
        if cpu_period >= 0:
            self.resources['cpu_period'] = self.cgroupSet("cfs_period_us", cpu_period)
        if cpu_shares >= 0:
            self.resources['cpu_shares'] = self.cgroupSet("shares", cpu_shares)
        if cores:
            self.dcli.update_container(self.dc, cpuset_cpus=cores)
            # quota, period ad shares can also be set by this line. Usable for future work.

    def updateMemoryLimit(self, mem_limit=-1, memswap_limit=-1):
        """
        Update Memory resource limitations.
        This method allows to update resource limitations at runtime by bypassing the Docker API
        and directly manipulating the cgroup options.

        Args:
            mem_limit: memory limit in bytes
            memswap_limit: swap limit in bytes

        """
        # see https://www.kernel.org/doc/Documentation/scheduler/sched-bwc.txt
        if mem_limit >= 0:
            self.resources['mem_limit'] = self.cgroupSet("limit_in_bytes", mem_limit, resource="memory")
        if memswap_limit >= 0:
            self.resources['memswap_limit'] = self.cgroupSet("memsw.limit_in_bytes", memswap_limit, resource="memory")


    def cgroupSet(self, param, value, resource='cpu'):
        """
        Directly manipulate the resource settings of the Docker container's cgrpup.
        Args:
            param: parameter to set, e.g., cfs_quota_us
            value: value to set
            resource: resource name: cpu

        Returns: value that was set

        """
        cmd = 'cgset -r %s.%s=%s docker/%s' % (
            resource, param, value, self.did)
        debug(cmd + "\n")
        try:
            check_output(cmd, shell=True)
        except:
            error("Problem writing cgroup setting %r\n" % cmd)
            return
        nvalue = int(self.cgroupGet(param, resource))
        if nvalue != value:
            error('*** error: cgroupSet: %s set to %s instead of %s\n'
                  % (param, nvalue, value))
        return nvalue

    def cgroupGet(self, param, resource='cpu'):
        """
        Read cgroup values.
        Args:
            param: parameter to read, e.g., cfs_quota_us
            resource: resource name: cpu / memory

        Returns: value

        """
        cmd = 'cgget -r %s.%s docker/%s' % (
            resource, param, self.did)
        try:
            return int(check_output(cmd, shell=True).split()[-1])
        except:
            error("Problem reading cgroup info: %r\n" % cmd)
            return -1

    def getLANIp(self):
        return self.cmd("hostname -i").strip()

class DockerPingHost( Docker ):
    """
    Noe that represents a host that runs pingmesh
    """
    def __init__(self, name, monitor, **kwargs):
        super().__init__(name, **kwargs)
        self.pingmesh_client = monitor
        self.hosts = "hosts.txt"

    def setAdminConfig(self, adminIP, troubleReportCollectionPort):
        self.adminIP = adminIP
        self.adminPort = troubleReportCollectionPort

    def setHostsFile(self, hosts):
        self.hosts = hosts

    def start(self):
        super().start()
        os.system("docker cp {} mn.{}:/pingmesh_client".format(self.pingmesh_client, self.name))
        os.system("docker cp {} mn.{}:/hosts".format(self.hosts, self.name))
        self.cmd("python3 /pingmesh_client --hosts /hosts --admin-ip {} --admin-port {} 2>&1 > pingmesh_client.log &".format(self.adminIP, self.adminPort))

class DockerRouter( Docker ):
    """
    Node that represents a router running in an indepedent container
    """
    def __init__(self, name, software="quagga", **kwargs):
        Docker.__init__(self, name, **kwargs)

        self.software = software
        self.daemonsOptions = {
            "zebra" : "yes",
            "bgpd" : "no",
            "ospfd" : "no",
            "ospf6d" : "no",
            "ripd" : "no",
            "ripngd" : "no",
            "isisd" : "no",
            "bfdd" : "no"
        }
        self.daemonsOptions.update(kwargs)
        self.daemonConfigs = {
            "zebra" : [], 
            "bgpd" : [],
            "ospfd" : [],
            "ospf6d" : [],
            "ripd" : [],
            "ripngd" : [],
            "isisd" : [],
            "bfdd" : []
        }
        self.generalConfig = [] # general configurations, not specific to any particular daemon

        # configure the loopback interface with a unique IP address
        self.loopbackIP = "192.168.19.{}".format(int(self.name[1:]) + 1)
        self.cmd("ifconfig lo {} netmask 255.255.255.255 up".format(self.loopbackIP))
        self.cmd("ip route add {} dev lo".format(self.loopbackIP))

        # VRF Config
        self.vrfDict = dict() # map VRF names to IDs
        self.vrfDict["default"] = 0 # Set the ID of default VRF to be 0
        self.tableVrfDict = dict() # map Linux routing tables to VRF IDs
        self.tableVrfDict[254] = 0
        self.tableVrfDict[255] = 0
        self.vrfVniDict = dict() # the first vni is the l3 vni, all following vni are l2 vni attached to this vrf
        self.vrfVniDict['default'] = [None, []]

        # BD config
        self.bdIntfDict = dict() # keep info about which interfaces belong to a broadcast domain (equivalent to a L2 VNI)

        # effective interface lists
        self.effIntfs = dict()

    def start(self):
        super().start()

        # set daemon configuration file
        self.configDaemons()

        # set the general configuration
        self.setupGeneralConfig()

        # if any routing daemon is enabled, write the corresponding config file
        for protocol in self.daemonConfigs.keys():
            if self.daemonsOptions[protocol] == "yes":
                self.setupRoutingConfigByProtocol(protocol)

        # disable all reverse path filters
        self.cmd("sysctl net.ipv4.conf.all.rp_filter=0")

        # start quagga
        info("Start {} Daemons\n".format(self.software))
        self.cmd("route del default")
        self.cmd("/etc/init.d/{} start".format(self.software))

    def configDaemons(self):
        configStr = ""
        configStr += "zebra=" + self.daemonsOptions["zebra"] + "\\n"
        configStr += "bgpd=" + self.daemonsOptions["bgpd"] + "\\n"
        configStr += "ospfd=" + self.daemonsOptions["ospfd"] + "\\n"
        configStr += "ospf6d=" + self.daemonsOptions["ospf6d"] + "\\n"
        configStr += "ripd=" + self.daemonsOptions["ripd"] + "\\n"
        configStr += "ripngd=" + self.daemonsOptions["ripngd"] + "\\n"
        configStr += "isisd=" + self.daemonsOptions["isisd"] + "\\n"
        configStr += "bfdd=" + self.daemonsOptions["bfdd"] + "\\n"
        configStr += "zebra_options=\\\"-f /etc/{software}/zebra.conf\\\"\\n".format(software=self.software)
        configStr += "bgpd_options=\\\"-f /etc/{software}/bgpd.conf\\\"\\n".format(software=self.software)
        configStr += "ospfd_options=\\\"-f /etc/{software}/ospfd.conf\\\"\\n".format(software=self.software)
        configStr += "ospf6d_options=\\\"-f /etc/{software}/ospf6d.conf\\\"\\n".format(software=self.software)
        configStr += "ripd_options=\\\"-f /etc/{software}/ripd.conf\\\"\\n".format(software=self.software)
        configStr += "ripngd_options=\\\"-f /etc/{software}/ripngd.conf\\\"\\n".format(software=self.software)
        configStr += "isisd_options=\\\"-f /etc/{software}/isisd.conf\\\"\\n".format(software=self.software)
        configStr += "bfdd_options=\\\"-f /etc/{software}/bfdd.conf\\\"\\n".format(software=self.software)

        self.cmd("echo -e \"{config}\" > /etc/{software}/daemons".format(config=configStr, software=self.software))

    def setupRoutingConfigIntegratedly(self):
        configStr = "hostname {}\\n".format(self.name) + "password zebra\\n\\n"

        for line in self.generalConfig:
            configStr += line + "\\n"

        for protocol, options in self.daemonConfigs:
            for i in options:
                configStr += i + "\\n"

            configStr += "\\n"

        self.cmd("echo -e \"{}\" > /etc/{}/{}.conf".format(configStr, self.software, self.software))

    def setupGeneralConfig(self):
        """ Only used with setupRoutingConfigByProtocol because the integrated configuration file will always incorporate these general configurations """
        configStr = "hostname {}\\n".format(self.name) + "password zebra\\n\\n"

        for line in self.generalConfig:
            configStr += line + "\\n"

        self.cmd("echo -e \"{}\" > /etc/{}/{}.conf".format(configStr, self.software, self.software))

    def setupRoutingConfigByProtocol(self, protocol):
        configStr = ""
        
        # append every optional configuration command
        for i in self.daemonConfigs[protocol]:
            configStr += i + "\\n"

        self.cmd("echo -e \"{}\" > /etc/{}/{}.conf".format(configStr, self.software, protocol))

    def addRoutingConfig(self, protocol = None, configStr = ""):
        if protocol != None and configStr != "":
            self.daemonConfigs[protocol].append(configStr)
        else:
            self.generalConfig.append(configStr)

    def getLoopbackIP(self) -> str:
        return self.loopbackIP

    def addVRF(self, name, tableId):
        self.vrfDict[name] = tableId
        self.cmd("ip link add {} type vrf table {}".format(name, tableId))
        self.cmd("ip link set {} up".format(name))
        self.cmd("ip link add {}-br type bridge".format(name))
        self.cmd("ip link set {}-br master {} addrgenmode none".format(name, name))
        self.cmd("ip link set {}-br up".format(name))
        self.tableVrfDict[int(tableId)] = int(tableId)
        self.vrfVniDict[name] = [None, []]

    def attachIntfToL2VNI(self, intfName, vni, brname=None):
        """Attach an interface to a L2 virtual network domain specified by arg:vni"""
        if brname == None:
            brname = "br" + str(vni)
        # Set VRF for Intf to be the VRF of the bridge
        intf = self.nameToIntf[intfName]
        brIntf = self.nameToIntf[brname]
        intf.setVRF(brIntf.VRF())
        intf.setBDI(brIntf.BDI())
        self.cmd("ip link set {} down".format(intfName))
        self.cmd("ip link set {} master {} addrgenmode none".format(intfName, brname))
        self.cmd("ip link set {} up".format(intfName))
        self.bdIntfDict[brIntf.BDI()].append(intf)

    def addIntf(self, intf, port=None, moveIntfFn=moveIntf):
        """Override the super's addIntf and register effective interfaces"""
        super().addIntf(intf, port, moveIntfFn)
        self.effIntfs[intf.name] = self.ports[intf]

    def addL2VNI(self, vni, brip, devname=None, brname=None, vrf="default"):
        """Add a L2 VNI to the router, arg:brip specifies the IP address of the bridge of this VNI, arg:devname specifies the name of the vxlan device, arg:vrf specifies which vrf the VNI is attached to"""
        if devname == None:
            devname = "vxlan" + str(vni)
        if brname == None:
            brname = "br" + str(vni)
        self.cmd("ip link add {} type bridge".format(brname))
        self.cmd("ip link set {} addrgenmode none".format(brname))
        intf = Intf(brname, node=self, moveIntfFn=lambda intf, dstNode: None)
        intf.setMAC(Subnet.ipToMac(brip))
        intf.setIP(brip)
        intf.setVRF(vrf)
        intf.setBDI(vni)
        self.cmd("ebtables -t filter -A INPUT  -i {} -j DROP".format(devname))
        self.cmd("ebtables -t filter -A INPUT  -i {} -j DROP".format(brname))

        self.cmd("ip link add {} type vxlan local {} dstport 4789 id {} nolearning".format(devname, self.getLoopbackIP(), vni))
        self.cmd("ip link set {} master {} addrgenmode none".format(devname, brname))
        self.cmd("ip link set {} type bridge_slave neigh_suppress off learning off".format(devname))
        self.cmd("ip link set {} up".format(devname))
        self.cmd("ip link set {} up".format(brname))

        # add l2 vni to vrf
        self.vrfVniDict[vrf][1].append(vni)

        # init BD interface list
        self.bdIntfDict[intf.BDI()] = []

    def addL3VNI(self, vni, devname=None, vrf="default"):
        """Add a L3 VNI to the router, arg:devname specifies the name of the vxlan device, arg:vrf specifies which vrf the VNI is attached to"""
        if devname == None:
            devname = "vxlan" + str(vni)
        self.cmd("ip link add {} type vxlan local {} id {} dstport 4789 nolearning".format(devname, self.getLoopbackIP(), vni))
        self.cmd("ip link set {} master {}-br addrgenmode none".format(devname, vrf))
        self.cmd("ip link set {} type bridge_slave neigh_suppress on learning off".format(devname, vrf))
        self.cmd("ip link set {} up".format(devname))
        self.vrfVniDict[vrf][0] = vni

class DockerP4Router( DockerRouter ):
    """
    P4 router running in an indepedent container
    """
    def __init__(self, name,
                 json_path,
                 target_path = 'simple_switch',
                 thrift_port = None,
                 cpu_input_port = 80,
                 cpu_output_port = 81,
                 pcap_dump = None,
                 log_console = False,
                 log_level = "trace",
                 verbose = False,
                 enable_debugger = False,
                 rt_mediator = None,
                 runtime_api = None,
                 switch_agent = None,
                 packet_injector = None,
                 bgp_adv_modifier = None,
                 **kwargs):
        DockerRouter.__init__(self, name, **kwargs)
        assert(json_path)
        # make sure that the provided JSON file exists
        if not os.path.isfile(json_path):
            error("Invalid JSON file: {}.\n".format(json_path))
            exit(1)
        self.json_path = json_path
        self.target_path = target_path
        self.verbose = verbose
        self.thrift_port = thrift_port
        self.pcap_dump = pcap_dump
        self.enable_debugger = enable_debugger
        self.log_console = log_console
        self.log_level = log_level
        self.nanomsg = "ipc:///tmp/bm-log.ipc"
        self.rt_mediator = rt_mediator
        self.runtime_api = runtime_api
        self.bgp_adv_modifier = bgp_adv_modifier
        self.switch_agent = switch_agent
        self.packet_injector = packet_injector
        self.cpu_input_port = cpu_input_port
        self.cpu_output_port = cpu_output_port

        # ACL(access control list) configuration
        self.aclConfig = []

        self.adminIP = None
        self.faultReportCollectionPort = None

    def setAdminConfig(self, adminIP, faultReportCollectionPort):
        self.adminIP = adminIP
        self.faultReportCollectionPort = faultReportCollectionPort

    def addACLConfig(self, dstAddr = None, srcAddr = None, protocol = None, dstPort = None, srcPort = None) -> str:
        acl_entry = ["0&&&0"] * 7
        if dstAddr != None:
            acl_entry[0] = dstAddr
        if srcAddr != None:
            acl_entry[1] = srcAddr
        if protocol != None:
            acl_entry[2] = protocol if isinstance(protocol, str) else "{}&&&0xff".format(protocol)

        if protocol == 6:
            # TCP
            if dstPort != None:
                acl_entry[3] = dstPort if isinstance(dstPort, str) else "{}&&&0xffff".format(dstPort)
            if srcPort != None:
                acl_entry[4] = srcPort if isinstance(srcPort, str) else "{}&&&0xffff".format(srcPort)
        elif protocol == 17:
            # UDP
            if dstPort != None:
                acl_entry[5] = dstPort if isinstance(dstPort, str) else "{}&&&0xffff".format(dstPort)
            if srcPort != None:
                acl_entry[6] = srcPort if isinstance(srcPort, str) else "{}&&&0xffff".format(srcPort)

        self.aclConfig.append(acl_entry)

    def setupACL(self):
        """
        Prepare ACL entries of the ACL filter table.
        """
        filename = "./ACL-{}.txt".format(self.name)
        with open(filename, "w", encoding="utf-8") as file:
            # write ACL commands into the file
            print("ACL on {}: {}".format(self.name, self.aclConfig))
            for entry in self.aclConfig:
                file.write("table_add Filter_ACL acl_drop " + " ".join(entry) + " => 1\n")

    def setupSubnetTable(self, macTable, subnet, DMTName="DstMac_FIB", DMTAction="set_dmac"):
        """
        Prepare subnet entries of the Dst MAC table and Ipv4 LPM table.
        """
        filename = "./Subnet-{}.txt".format(self.name)
        with open(filename, "a", encoding="utf-8") as file:
            # write DMT commands into the file
            for entry in macTable:
                file.write("table_add {} {} {} => {}\n".format(DMTName, DMTAction, entry[0], entry[1]))

    def combineIpAndVrfToHex(self, ip, vrf):
        addrBytes = ip.split(".")
        field = int(vrf)
        for i in range(len(addrBytes)):
            field = field << 8
            field += int(addrBytes[i])
        return "0x{:x}".format(field)

    def setupStartupTables(self, SMTName="SrcMac_RW", SMTAction="set_smac", DMTName="DstMac_FIB", DMTAction="set_dmac", VDVName="VxlanDecap_Virtual", L3VDVAction="l3vxlan_decap", L2VDVAction="l2vxlan_decap", SVRFName="SetVrf_Virtual", SVRFAction="set_vrf", SBDName="SetBD_Virtual", SBDAction="set_broadcast_domain", EMCASTName="EthernetMcast_FIB", EMCASTAction="l2mcast_forward"):
        """
        Prepare entries for the local switch interfaces including data ports, control-plane ports and data-plane ports.
        SMT -> Source Mac Table
        DMT -> Destination Mac Table
        """
        filename = "./Startup-{}.txt".format(self.name)
        with open(filename, "w", encoding="utf-8") as file:
            # write commands into the file

            # prepare dp-egress and local data port entries for src mac table
            file.write("table_add {} {} {} => {}\n".format(SMTName, SMTAction, self.cpu_input_port, "aa:00:00:00:00:01"))
            for intfName, port in self.effIntfs.items():
                intf = self.intfs[port]
                mac = intf.MAC()
                file.write("table_add {} {} {} => {}\n".format(SMTName, SMTAction, port, mac))
            
            # prepare the cp-ingress entry for dst mac table
            file.write("table_add {} {} {} => {}\n".format(DMTName, DMTAction, "0.0.0.0", "aa:00:00:00:00:02"))

            # configure mirroring_session for potential usage
            file.write("mirroring_add 819 80\n")
            file.write("mirroring_add 114 80\n")

            # prepare entries for VXLANDecap
            for vrf, vni_vec in self.vrfVniDict.items():
                l3vni = vni_vec[0]
                file.write("table_add {tname} {aname} {lo_ip} {vni} =>  {vni} \n".format(tname=VDVName, aname=L3VDVAction, lo_ip=self.loopbackIP, vni=l3vni))

                for l2vni in vni_vec[1]:
                    file.write("table_add {tname} {aname} {lo_ip} {vni} => {vni} \n".format(tname=VDVName, aname=L2VDVAction, lo_ip=self.loopbackIP, vni=l2vni))

            # prepare entries for SetVrf
            for intfName, port in self.effIntfs.items():
                intf = self.intfs[port]
                vrf_id = self.vrfDict[intf.VRF()]
                file.write("table_add {} {} {} => {}\n".format(SVRFName, SVRFAction, port, vrf_id))
            # prepare the SetVrf for dp-ingress interface
            file.write("table_add {} {} {} => {} \n".format(SVRFName, SVRFAction, self.cpu_output_port, 0))

            # prepare entries for SetBD
            for intfName, port in self.effIntfs.items():
                intf = self.intfs[port]
                bdi = intf.BDI()
                file.write("table_add {} {} {} => {}\n".format(SBDName, SBDAction, port, bdi))

            # prepare entries for Ethernet Mcast
            for bdi, intfs in self.bdIntfDict.items():
                file.write("table_add {} {} {} => {}\n".format(EMCASTName, EMCASTAction, bdi, bdi)) # assume that vni = bdi

                # create mcast node
                ports = ""
                for intf in intfs:
                    ports += str(self.ports[intf]) + " "
                file.write("mc_mgrp_add {gid} {port_list}\n".format(gid=bdi, port_list=ports))

    def setupIntfTable(self):
        """
        Prepare the table entries used by the rt_mediator to map interfaces to P4 BMv2 names
        """
        filename = "./IntfPortDict-{}.txt".format(self.name)
        with open(filename, "w", encoding="utf-8") as file:
            for intfName, port in self.effIntfs.items():
                file.write("{} {}\n".format(intfName, port))

    def setupVrfTable(self):
        """
        Prepare the table entries used by the rt_mediator to map routing tables to VRFs
        """
        filename = "./TableVrfDict-{}.txt".format(self.name)
        with open(filename, "w", encoding="utf-8") as file:
            for tableId, vrfId in self.tableVrfDict.items():
                file.write("{} {}\n".format(tableId, vrfId))

    def installTables(self):
        # copy the startup table file into the tmp directory of docker container
        filename = "./Startup-{}.txt".format(self.name)
        os.system("docker cp " + filename + " " + self.dc['Id'] + ":/tmp/Startup_cmds")

        # remove the file
        try:
            os.remove(filename)
        except:
            print("No Startup-{}.txt! ".format(self.name))

        # copy the subnet file into the tmp directory of docker container
        filename = "./Subnet-{}.txt".format(self.name)
        os.system("docker cp " + filename + " " + self.dc['Id'] + ":/tmp/Subnet_cmds")

        # remove the subnet file
        try:
            os.remove(filename)
        except:
            print("No Subnet-{}.txt! ".format(self.name))

        # copy the ACL file into the tmp directory of docker container
        print("ACL: ", self.aclConfig)
        filename = "./ACL-{}.txt".format(self.name)
        os.system("docker cp " + filename + " " + self.dc['Id'] + ":/tmp/ACL_cmds")

        # remove the subnet file
        try:
            os.remove(filename)
        except:
            print("No ACL-{}.txt! ".format(self.name))

        # copy the table-vrf file into the tmp directory of docker container
        filename = "./TableVrfDict-{}.txt".format(self.name)
        os.system("docker cp " + filename + " " + self.dc['Id'] + ":/tmp/TableVrfDict")

        # remove the table-vrf file
        try:
            os.remove(filename)
        except:
            print("No TableVrfDict-{}.txt! ".format(self.name))

        # copy the intf-port file into the tmp directory of docker container
        filename = "./IntfPortDict-{}.txt".format(self.name)
        os.system("docker cp " + filename + " " + self.dc['Id'] + ":/tmp/IntfPortDict")

        # remove the intf-port file
        try:
            os.remove(filename)
        except:
            print("No IntfPortDict-{}.txt! ".format(self.name))

    def start(self, debug = False):
        """Start up a new P4 switch"""
        # create & start a veth pair for CPU(Control-plane) input port
        makeIntfPair("dp-egress", "cp-ingress", node1=self, node2=self, addr1="aa:00:00:00:00:01", addr2="aa:00:00:00:00:02")     
        self.cmd("ifconfig dp-egress up 127.0.1.1/24")
        self.cmd("ifconfig cp-ingress up 127.0.1.2/24")
        self.cmd("iptables -t filter -A OUTPUT -p all -o dp-egress -j DROP")
        self.cmd("iptables -t filter -A OUTPUT -p all -o cp-ingress -j DROP")
        self.cmd("ethtool --offload dp-egress rx off")
        self.cmd("ethtool --offload dp-egress tx off")
        self.cmd("ethtool --offload cp-ingress rx off")
        self.cmd("ethtool --offload cp-ingress tx off")

        # create & start a veth pair for CPU(Control-plane) output port
        makeIntfPair("dp-ingress", "cp-egress", node1=self, node2=self, addr1="aa:00:00:00:00:03", addr2="aa:00:00:00:00:04")
        self.cmd("ifconfig dp-ingress up 127.0.1.3/24")
        self.cmd("ifconfig cp-egress up 127.0.1.4/24")
        self.cmd("iptables -t filter -A INPUT -p all -i dp-ingress -j DROP")
        self.cmd("iptables -t filter -A INPUT -p all -i cp-egress -j DROP")
        self.cmd("ethtool --offload dp-ingress rx off")
        self.cmd("ethtool --offload dp-ingress tx off")
        self.cmd("ethtool --offload cp-egress rx off")
        self.cmd("ethtool --offload cp-egress tx off")

        # disable rp_filter for cp-ingress
        self.cmd("sysctl net.ipv4.conf.all.rp_filter=0")
        self.cmd("sysctl net.ipv4.conf.cp-ingress.rp_filter=0")
        self.cmd("ifconfig cp-ingress down; ifconfig cp-ingress up")

        # disable linux routing
        self.cmd("sysctl net.ipv4.ip_forward=0")

        # setup route table 1
        self.cmd("ip route add default via 127.0.1.3 dev cp-egress table 252")
        # Configure Policy Routing
        self.cmd("ip rule add fwmark 0x8 table 252")
        # setup arp entry for dp-ingress interface
        self.cmd("arp -s -i cp-egress 127.0.1.3 aa:00:00:00:00:03")

        # merge arguments
        args = [self.target_path]
        for intfName, port in self.effIntfs.items():
            args.extend(['-i', str(port) + "@" + intfName])

            # add iptables entries to block input packets
            self.cmd("iptables -t filter -A INPUT -p ospf -i {} -j ACCEPT".format(intfName)) # exclude OSPF packets
            self.cmd("iptables -t filter -A INPUT -p all ! -d 224.0.0.0/4 -i {} -j DROP".format(intfName)) # exclude multicast packets for reserved addresses
            self.cmd("iptables -t filter -A FORWARD -p all -i {} -j DROP".format(intfName)) # exclude multicast packets for reserved addresses

            # add iptables entries to mark output packets
            self.cmd("iptables -t mangle -A OUTPUT -p all -o {} -j MARK --set-mark 0x8".format(intfName))

         # bind data-plane ports
        args.extend(['-i', str(self.cpu_input_port) + '@' + "dp-egress"])
        args.extend(['-i', str(self.cpu_output_port) + '@' + "dp-ingress"])

        if self.pcap_dump:
            args.extend(["--pcap", self.pcap_dump])
        if self.thrift_port:
            args.extend(['--thrift-port', str(self.thrift_port)])
        if self.nanomsg:
            args.extend(['--nanolog', self.nanomsg])
        if self.enable_debugger:
            args.append("--debugger")
        if self.log_console:
            args.append("--log-console")
        args.extend(['--log-level', self.log_level])

        # import json file & merge arguments
        os.system("docker cp " + self.json_path + " " + self.dc['Id'] + ":/tmp/running.json")
        args.append("/tmp/running.json")
        info("Starting P4 switch {} with cmd: ".format(self.name) + ' '.join(args) + "\n")

        # import rt_mediator if path is not null
        if self.rt_mediator:
            os.system("docker cp " + self.rt_mediator + " " + self.dc['Id'] + ":/tmp/rt_mediator")

        # import runtime api if path is not null
        if self.runtime_api:
            os.system("docker cp " + self.runtime_api + " " + self.dc['Id'] + ":/tmp/runtime_API.py")

        # import switch_agent if path is not null
        if self.switch_agent:
            os.system("docker cp " + self.switch_agent + " " + self.dc['Id'] + ":/tmp/switch_agent")

        # import packet_injector if path is not null
        if self.packet_injector:
            os.system("docker cp " + self.packet_injector + " " + self.dc['Id'] + ":/tmp/packet_injector")

        # import bgp_adv_modifier if path is not null
        if self.bgp_adv_modifier:
            os.system("docker cp " + self.bgp_adv_modifier + " " + self.dc['Id'] + ":/bgp_adv_modifier")

        # start bmv2
        self.cmd(' '.join(args) + ' >/tmp/p4bm.log 2>&1 &') # p4 bmv2
        # time.sleep(1) # wait for starting switch

        self.setupStartupTables()
        self.setupACL()
        self.setupVrfTable()
        self.setupIntfTable()
        self.installTables()
        # install initial table entries
        check_point1 = "init"
        check_point2 = "init"
        check_point3 = "init"
        while "RuntimeCmd" not in check_point1 or "RuntimeCmd" not in check_point2 or "RuntimeCmd" not in check_point3:
            check_point1 = self.cmd("python3 /tmp/runtime_API.py < /tmp/Startup_cmds")
            check_point2 = self.cmd("python3 /tmp/runtime_API.py < /tmp/Subnet_cmds")
            check_point3 = self.cmd("python3 /tmp/runtime_API.py < /tmp/ACL_cmds")

        # start rt_mediator
        if self.rt_mediator != None:
            if debug == True:
                self.cmd("strace -f -o /rt_mediator.strace python3 /tmp/rt_mediator --log-file /tmp/rt_mediator.log > /rt_mediator.out &")
            else:
                print("Start rt_mediator on {} with {}".format(self.name, "python3 /tmp/rt_mediator --log-file /tmp/rt_mediator.log --report-server-ip {} --report-server-port {} --table-vrf-file /tmp/TableVrfDict --intf-port-file /tmp/IntfPortDict > /rt_mediator.out &".format(self.adminIP, self.faultReportCollectionPort)))
                self.cmd("python3 /tmp/rt_mediator --log-file /tmp/rt_mediator.log --report-server-ip {} --report-server-port {} --table-vrf-file /tmp/TableVrfDict --intf-port-file /tmp/IntfPortDict > /rt_mediator.out &".format(self.adminIP, self.faultReportCollectionPort))

            while self.cmd("cat /rt_mediator_init_succ") != "1":
                time.sleep(1)

        # start switch_agent
        if self.switch_agent != None:
            if debug == True:
                self.cmd("strace -f -o /switch_agent.strace python3 /tmp/switch_agent --log-file /tmp/switch_agent.log --report-server-ip {} --report-server-port {} --port-num {} > /switch_agent.out &".format(self.adminIP, self.faultReportCollectionPort, len(self.intfs.keys())))
            else:
                print("Start switch_agent on {} with {}".format(self.name, "python3 /tmp/switch_agent --log-file /tmp/switch_agent.log --report-server-ip {} --report-server-port {} --port-num {} > /switch_agent.out &".format(self.adminIP, self.faultReportCollectionPort, len(self.intfs.keys()))))
                self.cmd("python3 /tmp/switch_agent --log-file /tmp/switch_agent.log --report-server-ip {} --report-server-port {} --port-num {} > /switch_agent.out &".format(self.adminIP, self.faultReportCollectionPort, len(self.intfs.keys())))

        super().start()

    def addL2VNI(self, vni, brip, devname=None, brname=None, vrf="default"):
        if devname == None:
            devname = "vxlan" + str(vni)
        if brname == None:
            brname = "br" + str(vni)

        super().addL2VNI(vni, brip, devname, brname, vrf)
        self.effIntfs.pop(brname)

    def attachIntfToL2VNI(self, intfName, vni, brname=None):
        super().attachIntfToL2VNI(intfName, vni, brname)
        self.cmd("ebtables -t filter -A FORWARD  -i {} -p ip -j DROP".format(intfName))
        self.cmd("ebtables -t filter -A INPUT  -i {} -p ip -j DROP".format(intfName))

class CPULimitedHost( Host ):

    "CPU limited host"

    def __init__( self, name, sched='cfs', **kwargs ):
        Host.__init__( self, name, **kwargs )
        # Initialize class if necessary
        if not CPULimitedHost.inited:
            CPULimitedHost.init()
        # Create a cgroup and move shell into it
        self.cgroup = 'cpu,cpuacct,cpuset:/' + self.name
        errFail( 'cgcreate -g ' + self.cgroup )
        # We don't add ourselves to a cpuset because you must
        # specify the cpu and memory placement first
        errFail( 'cgclassify -g cpu,cpuacct:/%s %s' % ( self.name, self.pid ) )
        # BL: Setting the correct period/quota is tricky, particularly
        # for RT. RT allows very small quotas, but the overhead
        # seems to be high. CFS has a mininimum quota of 1 ms, but
        # still does better with larger period values.
        self.period_us = kwargs.get( 'period_us', 100000 )
        self.sched = sched
        if sched == 'rt':
            self.checkRtGroupSched()
            self.rtprio = 20

    def cgroupSet( self, param, value, resource='cpu' ):
        "Set a cgroup parameter and return its value"
        cmd = 'cgset -r %s.%s=%s /%s' % (
            resource, param, value, self.name )
        quietRun( cmd )
        nvalue = int( self.cgroupGet( param, resource ) )
        if nvalue != value:
            error( '*** error: cgroupSet: %s set to %s instead of %s\n'
                   % ( param, nvalue, value ) )
        return nvalue

    def cgroupGet( self, param, resource='cpu' ):
        "Return value of cgroup parameter"
        cmd = 'cgget -r %s.%s /%s' % (
            resource, param, self.name )
        return int( quietRun( cmd ).split()[ -1 ] )

    def cgroupDel( self ):
        "Clean up our cgroup"
        # info( '*** deleting cgroup', self.cgroup, '\n' )
        _out, _err, exitcode = errRun( 'cgdelete -r ' + self.cgroup )
        # Sometimes cgdelete returns a resource busy error but still
        # deletes the group; next attempt will give "no such file"
        return exitcode == 0 or ( 'no such file' in _err.lower() )

    def popen( self, *args, **kwargs ):
        """Return a Popen() object in node's namespace
           args: Popen() args, single list, or string
           kwargs: Popen() keyword args"""
        # Tell mnexec to execute command in our cgroup
        mncmd = kwargs.pop( 'mncmd', [ 'mnexec', '-g', self.name,
                                       '-da', str( self.pid ) ] )
        # if our cgroup is not given any cpu time,
        # we cannot assign the RR Scheduler.
        if self.sched == 'rt':
            if int( self.cgroupGet( 'rt_runtime_us', 'cpu' ) ) <= 0:
                mncmd += [ '-r', str( self.rtprio ) ]
            else:
                debug( '*** error: not enough cpu time available for %s.' %
                       self.name, 'Using cfs scheduler for subprocess\n' )
        return Host.popen( self, *args, mncmd=mncmd, **kwargs )

    def cleanup( self ):
        "Clean up Node, then clean up our cgroup"
        super( CPULimitedHost, self ).cleanup()
        retry( retries=3, delaySecs=.1, fn=self.cgroupDel )

    _rtGroupSched = False   # internal class var: Is CONFIG_RT_GROUP_SCHED set?

    @classmethod
    def checkRtGroupSched( cls ):
        "Check (Ubuntu,Debian) kernel config for CONFIG_RT_GROUP_SCHED for RT"
        if not cls._rtGroupSched:
            release = quietRun( 'uname -r' ).strip('\r\n')
            output = quietRun( 'grep CONFIG_RT_GROUP_SCHED /boot/config-%s' %
                               release )
            if output == '# CONFIG_RT_GROUP_SCHED is not set\n':
                error( '\n*** error: please enable RT_GROUP_SCHED '
                       'in your kernel\n' )
                exit( 1 )
            cls._rtGroupSched = True

    def chrt( self ):
        "Set RT scheduling priority"
        quietRun( 'chrt -p %s %s' % ( self.rtprio, self.pid ) )
        result = quietRun( 'chrt -p %s' % self.pid )
        firstline = result.split( '\n' )[ 0 ]
        lastword = firstline.split( ' ' )[ -1 ]
        if lastword != 'SCHED_RR':
            error( '*** error: could not assign SCHED_RR to %s\n' % self.name )
        return lastword

    def rtInfo( self, f ):
        "Internal method: return parameters for RT bandwidth"
        pstr, qstr = 'rt_period_us', 'rt_runtime_us'
        # RT uses wall clock time for period and quota
        quota = int( self.period_us * f )
        return pstr, qstr, self.period_us, quota

    def cfsInfo( self, f ):
        "Internal method: return parameters for CFS bandwidth"
        pstr, qstr = 'cfs_period_us', 'cfs_quota_us'
        # CFS uses wall clock time for period and CPU time for quota.
        quota = int( self.period_us * f * numCores() )
        period = self.period_us
        if f > 0 and quota < 1000:
            debug( '(cfsInfo: increasing default period) ' )
            quota = 1000
            period = int( quota / f / numCores() )
        # Reset to unlimited on negative quota
        if quota < 0:
            quota = -1
        return pstr, qstr, period, quota

    # BL comment:
    # This may not be the right API,
    # since it doesn't specify CPU bandwidth in "absolute"
    # units the way link bandwidth is specified.
    # We should use MIPS or SPECINT or something instead.
    # Alternatively, we should change from system fraction
    # to CPU seconds per second, essentially assuming that
    # all CPUs are the same.

    def setCPUFrac( self, f, sched=None ):
        """Set overall CPU fraction for this host
           f: CPU bandwidth limit (positive fraction, or -1 for cfs unlimited)
           sched: 'rt' or 'cfs'
           Note 'cfs' requires CONFIG_CFS_BANDWIDTH,
           and 'rt' requires CONFIG_RT_GROUP_SCHED"""
        if not sched:
            sched = self.sched
        if sched == 'rt':
            if not f or f < 0:
                raise Exception( 'Please set a positive CPU fraction'
                                 ' for sched=rt\n' )
            pstr, qstr, period, quota = self.rtInfo( f )
        elif sched == 'cfs':
            pstr, qstr, period, quota = self.cfsInfo( f )
        else:
            return
        # Set cgroup's period and quota
        setPeriod = self.cgroupSet( pstr, period )
        setQuota = self.cgroupSet( qstr, quota )
        if sched == 'rt':
            # Set RT priority if necessary
            sched = self.chrt()
        info( '(%s %d/%dus) ' % ( sched, setQuota, setPeriod ) )

    def setCPUs( self, cores, mems=0 ):
        "Specify (real) cores that our cgroup can run on"
        if not cores:
            return
        if isinstance( cores, list ):
            cores = ','.join( [ str( c ) for c in cores ] )
        self.cgroupSet( resource='cpuset', param='cpus',
                        value=cores )
        # Memory placement is probably not relevant, but we
        # must specify it anyway
        self.cgroupSet( resource='cpuset', param='mems',
                        value=mems)
        # We have to do this here after we've specified
        # cpus and mems
        errFail( 'cgclassify -g cpuset:/%s %s' % (
                 self.name, self.pid ) )

    def config( self, cpu=-1, cores=None, **params ):
        """cpu: desired overall system CPU fraction
           cores: (real) core(s) this host can run on
           params: parameters for Node.config()"""
        r = Node.config( self, **params )
        # Was considering cpu={'cpu': cpu , 'sched': sched}, but
        # that seems redundant
        self.setParam( r, 'setCPUFrac', cpu=cpu )
        self.setParam( r, 'setCPUs', cores=cores )
        return r

    inited = False

    @classmethod
    def init( cls ):
        "Initialization for CPULimitedHost class"
        mountCgroups()
        cls.inited = True


# Some important things to note:
#
# The "IP" address which setIP() assigns to the switch is not
# an "IP address for the switch" in the sense of IP routing.
# Rather, it is the IP address for the control interface,
# on the control network, and it is only relevant to the
# controller. If you are running in the root namespace
# (which is the only way to run OVS at the moment), the
# control interface is the loopback interface, and you
# normally never want to change its IP address!
#
# In general, you NEVER want to attempt to use Linux's
# network stack (i.e. ifconfig) to "assign" an IP address or
# MAC address to a switch data port. Instead, you "assign"
# the IP and MAC addresses in the controller by specifying
# packets that you want to receive or send. The "MAC" address
# reported by ifconfig for a switch data port is essentially
# meaningless. It is important to understand this if you
# want to create a functional router using OpenFlow.

class Switch( Node ):
    """A Switch is a Node that is running (or has execed?)
       an OpenFlow switch."""

    portBase = 1  # Switches start with port 1 in OpenFlow
    dpidLen = 16  # digits in dpid passed to switch

    def __init__( self, name, dpid=None, opts='', listenPort=None, **params):
        """dpid: dpid hex string (or None to derive from name, e.g. s1 -> 1)
           opts: additional switch options
           listenPort: port to listen on for dpctl connections"""
        Node.__init__( self, name, **params )
        self.dpid = self.defaultDpid( dpid )
        self.opts = opts
        self.listenPort = listenPort
        if not self.inNamespace:
            self.controlIntf = Intf( 'lo', self, port=0 )

    def defaultDpid( self, dpid=None ):
        "Return correctly formatted dpid from dpid or switch name (s1 -> 1)"
        if dpid:
            # Remove any colons and make sure it's a good hex number
            dpid = dpid.replace( ':', '' )
            assert len( dpid ) <= self.dpidLen and int( dpid, 16 ) >= 0
        else:
            # Use hex of the first number in the switch name
            nums = re.findall( r'\d+', self.name )
            if nums:
                dpid = hex( int( nums[ 0 ] ) )[ 2: ]
            else:
                self.terminate()  # Python 3.6 crash workaround
                raise Exception( 'Unable to derive default datapath ID - '
                                 'please either specify a dpid or use a '
                                 'canonical switch name such as s23.' )
        return '0' * ( self.dpidLen - len( dpid ) ) + dpid

    def defaultIntf( self ):
        "Return control interface"
        if self.controlIntf:
            return self.controlIntf
        else:
            return Node.defaultIntf( self )

    def sendCmd( self, *cmd, **kwargs ):
        """Send command to Node.
           cmd: string"""
        kwargs.setdefault( 'printPid', False )
        if not self.execed:
            return Node.sendCmd( self, *cmd, **kwargs )
        else:
            error( '*** Error: %s has execed and cannot accept commands' %
                   self.name )

    def connected( self ):
        "Is the switch connected to a controller? (override this method)"
        # Assume that we are connected by default to whatever we need to
        # be connected to. This should be overridden by any OpenFlow
        # switch, but not by a standalone bridge.
        debug( 'Assuming', repr( self ), 'is connected to a controller\n' )
        return True

    def stop( self, deleteIntfs=True ):
        """Stop switch
           deleteIntfs: delete interfaces? (True)"""
        if deleteIntfs:
            self.deleteIntfs()
        self.terminate()

    def __repr__( self ):
        "More informative string representation"
        intfs = ( ','.join( [ '%s:%s' % ( i.name, i.IP() )
                              for i in self.intfList() ] ) )
        return '<%s %s: %s pid=%s> ' % (
            self.__class__.__name__, self.name, intfs, self.pid )


class UserSwitch( Switch ):
    "User-space switch."

    dpidLen = 12

    def __init__( self, name, dpopts='--no-slicing', **kwargs ):
        """Init.
           name: name for the switch
           dpopts: additional arguments to ofdatapath (--no-slicing)"""
        Switch.__init__( self, name, **kwargs )
        pathCheck( 'ofdatapath', 'ofprotocol',
                   moduleName='the OpenFlow reference user switch' +
                              '(openflow.org)' )
        if self.listenPort:
            self.opts += ' --listen=ptcp:%i ' % self.listenPort
        else:
            self.opts += ' --listen=punix:/tmp/%s.listen' % self.name
        self.dpopts = dpopts

    @classmethod
    def setup( cls ):
        "Ensure any dependencies are loaded; if not, try to load them."
        if not os.path.exists( '/dev/net/tun' ):
            moduleDeps( add=TUN )

    def dpctl( self, *args ):
        "Run dpctl command"
        listenAddr = None
        if not self.listenPort:
            listenAddr = 'unix:/tmp/%s.listen' % self.name
        else:
            listenAddr = 'tcp:127.0.0.1:%i' % self.listenPort
        return self.cmd( 'dpctl ' + ' '.join( args ) +
                         ' ' + listenAddr )

    def connected( self ):
        "Is the switch connected to a controller?"
        status = self.dpctl( 'status' )
        return ( 'remote.is-connected=true' in status and
                 'local.is-connected=true' in status )

    @staticmethod
    def TCReapply( intf ):
        """Unfortunately user switch and Mininet are fighting
           over tc queuing disciplines. To resolve the conflict,
           we re-create the user switch's configuration, but as a
           leaf of the TCIntf-created configuration."""
        if isinstance( intf, TCIntf ):
            ifspeed = 10000000000  # 10 Gbps
            minspeed = ifspeed * 0.001

            res = intf.config( **intf.params )

            if res is None:  # link may not have TC parameters
                return

            # Re-add qdisc, root, and default classes user switch created, but
            # with new parent, as setup by Mininet's TCIntf
            parent = res['parent']
            intf.tc( "%s qdisc add dev %s " + parent +
                     " handle 1: htb default 0xfffe" )
            intf.tc( "%s class add dev %s classid 1:0xffff parent 1: htb rate "
                     + str(ifspeed) )
            intf.tc( "%s class add dev %s classid 1:0xfffe parent 1:0xffff " +
                     "htb rate " + str(minspeed) + " ceil " + str(ifspeed) )

    def start( self, controllers ):
        """Start OpenFlow reference user datapath.
           Log to /tmp/sN-{ofd,ofp}.log.
           controllers: list of controller objects"""
        # Add controllers
        clist = ','.join( [ 'tcp:%s:%d' % ( c.IP(), c.port )
                            for c in controllers ] )
        ofdlog = '/tmp/' + self.name + '-ofd.log'
        ofplog = '/tmp/' + self.name + '-ofp.log'
        intfs = [ str( i ) for i in self.intfList() if not i.IP() ]
        self.cmd( 'ofdatapath -i ' + ','.join( intfs ) +
                  ' punix:/tmp/' + self.name + ' -d %s ' % self.dpid +
                  self.dpopts +
                  ' 1> ' + ofdlog + ' 2> ' + ofdlog + ' &' )
        self.cmd( 'ofprotocol unix:/tmp/' + self.name +
                  ' ' + clist +
                  ' --fail=closed ' + self.opts +
                  ' 1> ' + ofplog + ' 2>' + ofplog + ' &' )
        if "no-slicing" not in self.dpopts:
            # Only TCReapply if slicing is enable
            sleep(1)  # Allow ofdatapath to start before re-arranging qdisc's
            for intf in self.intfList():
                if not intf.IP():
                    self.TCReapply( intf )

    def stop( self, deleteIntfs=True ):
        """Stop OpenFlow reference user datapath.
           deleteIntfs: delete interfaces? (True)"""
        self.cmd( 'kill %ofdatapath' )
        self.cmd( 'kill %ofprotocol' )
        super( UserSwitch, self ).stop( deleteIntfs )


class OVSSwitch( Switch ):
    "Open vSwitch switch. Depends on ovs-vsctl."

    def __init__( self, name, failMode='secure', datapath='kernel',
                  inband=False, protocols=None,
                  reconnectms=1000, stp=False, batch=False, **params ):
        """name: name for switch
           failMode: controller loss behavior (secure|standalone)
           datapath: userspace or kernel mode (kernel|user)
           inband: use in-band control (False)
           protocols: use specific OpenFlow version(s) (e.g. OpenFlow13)
                      Unspecified (or old OVS version) uses OVS default
           reconnectms: max reconnect timeout in ms (0/None for default)
           stp: enable STP (False, requires failMode=standalone)
           batch: enable batch startup (False)"""
        Switch.__init__( self, name, **params )
        self.failMode = failMode
        self.datapath = datapath
        self.inband = inband
        self.protocols = protocols
        self.reconnectms = reconnectms
        self.stp = stp
        self._uuids = []  # controller UUIDs
        self.batch = batch
        self.commands = []  # saved commands for batch startup

        # add a prefix to the name of the deployed switch, to find it back easier later
        prefix = params.get('prefix', '')
        self.deployed_name = prefix + name


    @classmethod
    def setup( cls ):
        "Make sure Open vSwitch is installed and working"
        pathCheck( 'ovs-vsctl',
                   moduleName='Open vSwitch (openvswitch.org)')
        # This should no longer be needed, and it breaks
        # with OVS 1.7 which has renamed the kernel module:
        #  moduleDeps( subtract=OF_KMOD, add=OVS_KMOD )
        out, err, exitcode = errRun( 'ovs-vsctl -t 1 show' )
        if exitcode:
            error( out + err +
                   'ovs-vsctl exited with code %d\n' % exitcode +
                   '*** Error connecting to ovs-db with ovs-vsctl\n'
                   'Make sure that Open vSwitch is installed, '
                   'that ovsdb-server is running, and that\n'
                   '"ovs-vsctl show" works correctly.\n'
                   'You may wish to try '
                   '"service openvswitch-switch start".\n' )
            exit( 1 )
        version = quietRun( 'ovs-vsctl --version' )
        cls.OVSVersion = findall( r'\d+\.\d+', version )[ 0 ]

    @classmethod
    def isOldOVS( cls ):
        "Is OVS ersion < 1.10?"
        return ( StrictVersion( cls.OVSVersion ) <
                 StrictVersion( '1.10' ) )

    def dpctl( self, *args ):
        "Run ovs-ofctl command"
        return self.cmd( 'ovs-ofctl', args[ 0 ], self.deployed_name, *args[ 1: ] )

    def vsctl( self, *args, **kwargs ):
        "Run ovs-vsctl command (or queue for later execution)"
        if self.batch:
            cmd = ' '.join( str( arg ).strip() for arg in args )
            self.commands.append( cmd )
        else:
            return self.cmd( 'ovs-vsctl', *args, **kwargs )

    @staticmethod
    def TCReapply( intf ):
        """Unfortunately OVS and Mininet are fighting
           over tc queuing disciplines. As a quick hack/
           workaround, we clear OVS's and reapply our own."""
        if isinstance( intf, TCIntf ):
            intf.config( **intf.params )

    def attach( self, intf ):
        "Connect a data port"
        self.vsctl( 'add-port', self.deployed_name, intf )
        self.cmd( 'ifconfig', intf, 'up' )
        self.TCReapply( intf )

    def attachInternalIntf(self, intf_name, net):
        """Add an interface of type:internal to the ovs switch
           and add routing entry to host"""
        self.vsctl('add-port', self.deployed_name, intf_name, '--', 'set', ' interface', intf_name, 'type=internal')
        int_intf = Intf(intf_name, node=self.deployed_name)
        #self.addIntf(int_intf, moveIntfFn=None)
        self.cmd('ip route add', net, 'dev', intf_name)

        return self.nameToIntf[intf_name]

    def detach( self, intf ):
        "Disconnect a data port"
        self.vsctl( 'del-port', self.deployed_name, intf )

    def controllerUUIDs( self, update=False ):
        """Return ovsdb UUIDs for our controllers
           update: update cached value"""
        if not self._uuids or update:
            controllers = self.cmd( 'ovs-vsctl -- get Bridge', self.deployed_name,
                                    'Controller' ).strip()
            if controllers.startswith( '[' ) and controllers.endswith( ']' ):
                controllers = controllers[ 1 : -1 ]
                if controllers:
                    self._uuids = [ c.strip()
                                    for c in controllers.split( ',' ) ]
        return self._uuids

    def connected( self ):
        "Are we connected to at least one of our controllers?"
        for uuid in self.controllerUUIDs():
            if 'true' in self.vsctl( '-- get Controller',
                                     uuid, 'is_connected' ):
                return True
        return self.failMode == 'standalone'

    def intfOpts( self, intf ):
        "Return OVS interface options for intf"
        opts = ''
        if not self.isOldOVS():
            # ofport_request is not supported on old OVS
            opts += ' ofport_request=%s' % self.ports[ intf ]
            # Patch ports don't work well with old OVS
            if isinstance( intf, OVSIntf ):
                intf1, intf2 = intf.link.intf1, intf.link.intf2
                peer = intf1 if intf1 != intf else intf2
                opts += ' type=patch options:peer=%s' % peer
        return '' if not opts else ' -- set Interface %s' % intf + opts

    def bridgeOpts( self ):
        "Return OVS bridge options"
        opts = ( ' other_config:datapath-id=%s' % self.dpid +
                 ' fail_mode=%s' % self.failMode )
        if not self.inband:
            opts += ' other-config:disable-in-band=true'
        if self.datapath == 'user':
            opts += ' datapath_type=netdev'
        if self.protocols and not self.isOldOVS():
            opts += ' protocols=%s' % self.protocols
        if self.stp and self.failMode == 'standalone':
            opts += ' stp_enable=true'
        opts += ' other-config:dp-desc=%s' % self.name
        return opts

    def start( self, controllers ):
        "Start up a new OVS OpenFlow switch using ovs-vsctl"
        if self.inNamespace:
            raise Exception(
                'OVS kernel switch does not work in a namespace' )
        int( self.dpid, 16 )  # DPID must be a hex string
        # Command to add interfaces
        intfs = ''.join( ' -- add-port %s %s' % ( self.deployed_name, intf ) +
                         self.intfOpts( intf )
                         for intf in self.intfList()
                         if self.ports[ intf ] and not intf.IP() )
        # Command to create controller entries
        clist = [ ( self.deployed_name + c.name, '%s:%s:%d' %
                  ( c.protocol, c.IP(), c.port ) )
                  for c in controllers ]
        if self.listenPort:
            clist.append( ( self.deployed_name + '-listen',
                            'ptcp:%s' % self.listenPort ) )
        ccmd = '-- --id=@%s create Controller target=\\"%s\\"'
        if self.reconnectms:
            ccmd += ' max_backoff=%d' % self.reconnectms
        cargs = ' '.join( ccmd % ( name, target )
                          for name, target in clist )
        # Controller ID list
        cids = ','.join( '@%s' % name for name, _target in clist )
        # Try to delete any existing bridges with the same name
        if not self.isOldOVS():
            cargs += ' -- --if-exists del-br %s' % self.deployed_name
        # One ovs-vsctl command to rule them all!
        self.vsctl( cargs +
                    ' -- add-br %s' % self.deployed_name +
                    ' -- set bridge %s controller=[%s]' % ( self.deployed_name, cids  ) +
                    self.bridgeOpts() +
                    intfs )
        # If necessary, restore TC config overwritten by OVS
        if not self.batch:
            for intf in self.intfList():
                self.TCReapply( intf )

    # This should be ~ int( quietRun( 'getconf ARG_MAX' ) ),
    # but the real limit seems to be much lower
    argmax = 128000

    @classmethod
    def batchStartup( cls, switches, run=errRun ):
        """Batch startup for OVS
           switches: switches to start up
           run: function to run commands (errRun)"""
        info( '...' )
        cmds = 'ovs-vsctl'
        for switch in switches:
            if switch.isOldOVS():
                # Ideally we'd optimize this also
                run( 'ovs-vsctl del-br %s' % switch )
            for cmd in switch.commands:
                cmd = cmd.strip()
                # Don't exceed ARG_MAX
                if len( cmds ) + len( cmd ) >= cls.argmax:
                    run( cmds, shell=True )
                    cmds = 'ovs-vsctl'
                cmds += ' ' + cmd
                switch.cmds = []
                switch.batch = False
        if cmds:
            run( cmds, shell=True )
        # Reapply link config if necessary...
        for switch in switches:
            for intf in switch.intfs.values():
                if isinstance( intf, TCIntf ):
                    intf.config( **intf.params )
        return switches

    def stop( self, deleteIntfs=True ):
        """Terminate OVS switch.
           deleteIntfs: delete interfaces? (True)"""
        self.cmd( 'ovs-vsctl del-br', self.deployed_name )
        if self.datapath == 'user':
            self.cmd( 'ip link del', self.deployed_name )
        super( OVSSwitch, self ).stop( deleteIntfs )

    @classmethod
    def batchShutdown( cls, switches, run=errRun ):
        "Shut down a list of OVS switches"
        delcmd = 'del-br %s'
        if switches and not switches[ 0 ].isOldOVS():
            delcmd = '--if-exists ' + delcmd
        # First, delete them all from ovsdb
        run( 'ovs-vsctl ' +
             ' -- '.join( delcmd % s.deployed_name for s in switches ), shell=True )
        # Next, shut down all of the processes
        pids = ' '.join( str( switch.pid ) for switch in switches )

        success = False
        while not success:
            try:
                run( 'kill -HUP ' + pids )
                success = True
            except select.error as e:
                # retry on interrupt
                if e != errno.EINTR:
                    raise
        for switch in switches:
            switch.terminate()
            switch.shell = None
        return switches


OVSKernelSwitch = OVSSwitch


class OVSBridge( OVSSwitch ):
    "OVSBridge is an OVSSwitch in standalone/bridge mode"

    def __init__( self, *args, **kwargs ):
        """stp: enable Spanning Tree Protocol (False)
           see OVSSwitch for other options"""
        kwargs.update( failMode='standalone' )
        OVSSwitch.__init__( self, *args, **kwargs )

        # ip address of this bridge (eg. 10.10.0.1/24)
        self.ip = kwargs.get('ip')

    def start( self, controllers ):
        "Start bridge, ignoring controllers argument"
        OVSSwitch.start( self, controllers=[] )

        # assign an ip address to this switch, so it can connect to the host
        if self.ip:
            self.cmd('ip address add', self.ip, 'dev', self.deployed_name)
            self.cmd('ip link set', self.deployed_name, 'up')

    def connected( self ):
        "Are we forwarding yet?"
        if self.stp:
            status = self.dpctl( 'show' )
            return 'STP_FORWARD' in status and not 'STP_LEARN' in status
        else:
            return True


class IVSSwitch( Switch ):
    "Indigo Virtual Switch"

    def __init__( self, name, verbose=False, **kwargs ):
        Switch.__init__( self, name, **kwargs )
        self.verbose = verbose

    @classmethod
    def setup( cls ):
        "Make sure IVS is installed"
        pathCheck( 'ivs-ctl', 'ivs',
                   moduleName="Indigo Virtual Switch (projectfloodlight.org)" )
        out, err, exitcode = errRun( 'ivs-ctl show' )
        if exitcode:
            error( out + err +
                   'ivs-ctl exited with code %d\n' % exitcode +
                   '*** The openvswitch kernel module might '
                   'not be loaded. Try modprobe openvswitch.\n' )
            exit( 1 )

    @classmethod
    def batchShutdown( cls, switches ):
        "Kill each IVS switch, to be waited on later in stop()"
        for switch in switches:
            switch.cmd( 'kill %ivs' )
        return switches

    def start( self, controllers ):
        "Start up a new IVS switch"
        args = ['ivs']
        args.extend( ['--name', self.name] )
        args.extend( ['--dpid', self.dpid] )
        if self.verbose:
            args.extend( ['--verbose'] )
        for intf in self.intfs.values():
            if not intf.IP():
                args.extend( ['-i', intf.name] )
        for c in controllers:
            args.extend( ['-c', '%s:%d' % (c.IP(), c.port)] )
        if self.listenPort:
            args.extend( ['--listen', '127.0.0.1:%i' % self.listenPort] )
        args.append( self.opts )

        logfile = '/tmp/ivs.%s.log' % self.name

        self.cmd( ' '.join(args) + ' >' + logfile + ' 2>&1 </dev/null &' )

    def stop( self, deleteIntfs=True ):
        """Terminate IVS switch.
           deleteIntfs: delete interfaces? (True)"""
        self.cmd( 'kill %ivs' )
        self.cmd( 'wait' )
        super( IVSSwitch, self ).stop( deleteIntfs )

    def attach( self, intf ):
        "Connect a data port"
        self.cmd( 'ivs-ctl', 'add-port', '--datapath', self.name, intf )

    def detach( self, intf ):
        "Disconnect a data port"
        self.cmd( 'ivs-ctl', 'del-port', '--datapath', self.name, intf )

    def dpctl( self, *args ):
        "Run dpctl command"
        if not self.listenPort:
            return "can't run dpctl without passive listening port"
        return self.cmd( 'ovs-ofctl ' + ' '.join( args ) +
                         ' tcp:127.0.0.1:%i' % self.listenPort )


class Controller( Node ):
    """A Controller is a Node that is running (or has execed?) an
       OpenFlow controller."""

    def __init__( self, name, inNamespace=False, command='controller',
                  cargs='-v ptcp:%d', cdir=None, ip="127.0.0.1",
                  port=6653, protocol='tcp', **params ):
        self.command = command
        self.cargs = cargs
        self.cdir = cdir
        # Accept 'ip:port' syntax as shorthand
        if ':' in ip:
            ip, port = ip.split( ':' )
            port = int( port )
        self.ip = ip
        self.port = port
        self.protocol = protocol
        Node.__init__( self, name, inNamespace=inNamespace,
                       ip=ip, **params  )
        self.checkListening()

    def checkListening( self ):
        "Make sure no controllers are running on our port"
        # Verify that Telnet is installed first:
        out, _err, returnCode = errRun( "which telnet" )
        if 'telnet' not in out or returnCode != 0:
            raise Exception( "Error running telnet to check for listening "
                             "controllers; please check that it is "
                             "installed." )
        listening = self.cmd( "echo A | telnet -e A %s %d" %
                              ( self.ip, self.port ) )
        if 'Connected' in listening:
            servers = self.cmd( 'netstat -natp' ).split( '\n' )
            pstr = ':%d ' % self.port
            clist = servers[ 0:1 ] + [ s for s in servers if pstr in s ]
            raise Exception( "Please shut down the controller which is"
                             " running on port %d:\n" % self.port +
                             '\n'.join( clist ) )

    def start( self ):
        """Start <controller> <args> on controller.
           Log to /tmp/cN.log"""
        pathCheck( self.command )
        cout = '/tmp/' + self.name + '.log'
        if self.cdir is not None:
            self.cmd( 'cd ' + self.cdir )
        self.cmd( self.command + ' ' + self.cargs % self.port +
                  ' 1>' + cout + ' 2>' + cout + ' &' )
        self.execed = False

    def stop( self, *args, **kwargs ):
        "Stop controller."
        self.cmd( 'kill %' + self.command )
        self.cmd( 'wait %' + self.command )
        super( Controller, self ).stop( *args, **kwargs )

    def IP( self, intf=None ):
        "Return IP address of the Controller"
        if self.intfs:
            ip = Node.IP( self, intf )
        else:
            ip = self.ip
        return ip

    def __repr__( self ):
        "More informative string representation"
        return '<%s %s: %s:%s pid=%s> ' % (
            self.__class__.__name__, self.name,
            self.IP(), self.port, self.pid )

    @classmethod
    def isAvailable( cls ):
        "Is controller available?"
        return which( 'controller' )


class OVSController( Controller ):
    "Open vSwitch controller"
    def __init__( self, name, **kwargs ):
        kwargs.setdefault( 'command', self.isAvailable() or
                           'ovs-controller' )
        Controller.__init__( self, name, **kwargs )

    @classmethod
    def isAvailable( cls ):
        return (which( 'ovs-controller' ) or
                which( 'test-controller' ) or
                which( 'ovs-testcontroller' ))

class NOX( Controller ):
    "Controller to run a NOX application."

    def __init__( self, name, *noxArgs, **kwargs ):
        """Init.
           name: name to give controller
           noxArgs: arguments (strings) to pass to NOX"""
        if not noxArgs:
            warn( 'warning: no NOX modules specified; '
                  'running packetdump only\n' )
            noxArgs = [ 'packetdump' ]
        elif type( noxArgs ) not in ( list, tuple ):
            noxArgs = [ noxArgs ]

        if 'NOX_CORE_DIR' not in os.environ:
            exit( 'exiting; please set missing NOX_CORE_DIR env var' )
        noxCoreDir = os.environ[ 'NOX_CORE_DIR' ]

        Controller.__init__( self, name,
                             command=noxCoreDir + '/nox_core',
                             cargs='--libdir=/usr/local/lib -v -i ptcp:%s ' +
                             ' '.join( noxArgs ),
                             cdir=noxCoreDir,
                             **kwargs )

class Ryu( Controller ):
    "Controller to run Ryu application"
    def __init__( self, name, *ryuArgs, **kwargs ):
        """Init.
        name: name to give controller.
        ryuArgs: arguments and modules to pass to Ryu"""
        homeDir = quietRun( 'printenv HOME' ).strip( '\r\n' )
        ryuCoreDir = '%s/ryu/ryu/app/' % homeDir
        if not ryuArgs:
            warn( 'warning: no Ryu modules specified; '
                  'running simple_switch only\n' )
            ryuArgs = [ ryuCoreDir + 'simple_switch.py' ]
        elif type( ryuArgs ) not in ( list, tuple ):
            ryuArgs = [ ryuArgs ]

        Controller.__init__( self, name,
                             command='ryu-manager',
                             cargs='--ofp-tcp-listen-port %s ' +
                             ' '.join( ryuArgs ),
                             cdir=ryuCoreDir,
                             **kwargs )


class RemoteController( Controller ):
    "Controller running outside of Mininet's control."

    def __init__( self, name, ip='127.0.0.1',
                  port=None, **kwargs):
        """Init.
           name: name to give controller
           ip: the IP address where the remote controller is
           listening
           port: the port where the remote controller is listening"""
        Controller.__init__( self, name, ip=ip, port=port, **kwargs )

    def start( self ):
        "Overridden to do nothing."
        return

    def stop( self ):
        "Overridden to do nothing."
        return

    def checkListening( self ):
        "Warn if remote controller is not accessible"
        if self.port is not None:
            self.isListening( self.ip, self.port )
        else:
            for port in 6653, 6633:
                if self.isListening( self.ip, port ):
                    self.port = port
                    info( "Connecting to remote controller"
                          " at %s:%d\n" % ( self.ip, self.port ))
                    break

        if self.port is None:
            self.port = 6653
            warn( "Setting remote controller"
                  " to %s:%d\n" % ( self.ip, self.port ))

    def isListening( self, ip, port ):
        "Check if a remote controller is listening at a specific ip and port"
        listening = self.cmd( "echo A | telnet -e A %s %d" % ( ip, port ) )
        if 'Connected' not in listening:
            warn( "Unable to contact the remote controller"
                  " at %s:%d\n" % ( ip, port ) )
            return False
        else:
            return True


DefaultControllers = ( Controller, OVSController )


def findController( controllers=DefaultControllers ):
    "Return first available controller from list, if any"
    for controller in controllers:
        if controller.isAvailable():
            return controller


def DefaultController( name, controllers=DefaultControllers, **kwargs ):
    "Find a controller that is available and instantiate it"
    controller = findController( controllers )
    if not controller:
        raise Exception( 'Could not find a default OpenFlow controller' )
    return controller( name, **kwargs )


def NullController( *_args, **_kwargs ):
    "Nonexistent controller - simply returns None"
    return None


def parse_build_output(output):
        output_str = ""
        for line in output:
            for item in line.values():
                output_str += str(item)
        return output_str
