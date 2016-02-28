import logging
import os
import time

from kamaki.clients import ClientError
from kamaki.clients.astakos import AstakosClient
from kamaki.clients.cyclades import CycladesComputeClient
from kamaki.clients.image import ImageClient
from kamaki import defaults
from kamaki.clients.utils import https
from kamaki.clients.cyclades import CycladesNetworkClient

from juju_okeanos.exceptions import ConfigError, ProviderError
from juju_okeanos.client import Client
from juju_okeanos.constraints import init
from kamaki.cli.config import Config
from base64 import b64encode
from time import sleep
import subprocess

log = logging.getLogger("juju.okeanos")


def factory():
    cfg = Okeanos.get_config()
    okeanos = Okeanos(cfg)
    return okeanos


def validate():
    Okeanos.get_config()


class Okeanos(object):

    def __init__(self, config):
        self.config = config
        cloud_name = self.config.get('global', 'default_cloud')
        self.auth_token = self.config.get_cloud(cloud_name, 'token')
        cacerts_path = self.config.get('global', 'ca_certs')
        https.patch_with_certs(cacerts_path)
        auth_url = self.config.get_cloud(cloud_name, 'url')
        auth = AstakosClient(auth_url, self.auth_token)
        self.endpoints = dict(
            astakos=auth_url,
            cyclades=auth.get_endpoint_url(CycladesComputeClient.service_type),
            network=auth.get_endpoint_url(CycladesNetworkClient.service_type),
            plankton=auth.get_endpoint_url(ImageClient.service_type)
            )
        self.user_id = auth.user_info['id']

    @property
    def version(self):
        return "0.1.0"


    @classmethod
    def get_config(cls):
        okeanos_ssh_key_path = os.environ.get('OKEANOS_SSH_KEY')
        if not okeanos_ssh_key_path:
            raise ConfigError("Please set the OKEANOS_SSH_KEY with the path to your public ssh key")

        kamakirc_path = os.environ.get('OKEANOS_KAMAKIRC')
        okeanos_config = Config(kamakirc_path)

        # This is debian specific... for now...
        okeanos_config.set('global', 'ca_certs', '/etc/ssl/certs/ca-certificates.crt')
        cloud_name = okeanos_config.get('global', 'default_cloud')
        auth_url = okeanos_config.get_cloud(cloud_name, 'url')
        auth_token = okeanos_config.get_cloud(cloud_name, 'token')

        if (not cloud_name or not auth_url or not auth_token):
            raise ConfigError("Wrong okeanos configuration")
        return okeanos_config

    def remote_run(self, vm, command, env=None, capture_err=False):
        if env is None:
            env = dict(os.environ)
        
        args = ['ssh', '-oStrictHostKeyChecking=no', 'root@{}'.format(vm['fqdn'])]
        args.extend(command)
        log.debug("Running juju command: %s", " ".join(args))
        try:
            if capture_err:
                return subprocess.check_call(
                    args, env=env, stderr=subprocess.STDOUT)
            return subprocess.check_output(
                args, env=env, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError, e:
            log.error(
                "Failed to run command %s\n%s",
                ' '.join(args), e.output)
            raise

    def get_image_client(self):
        astakos = AstakosClient(self.endpoints['astakos'], self.auth_token)
        image_url = astakos.get_endpoint_url(ImageClient.service_type)
        plankton = ImageClient(image_url, self.auth_token)
        return plankton

    def get_compute_client(self):
        astakos = AstakosClient(self.endpoints['astakos'], self.auth_token)
        cyclades_url = astakos.get_endpoint_url(CycladesComputeClient.service_type)
        compute_client = CycladesComputeClient(cyclades_url, self.auth_token)
        return compute_client
    
    def get_network_client(self):
        astakos = AstakosClient(self.endpoints['astakos'], self.auth_token)
        network_url = astakos.get_endpoint_url(CycladesNetworkClient.service_type)
        network_client = CycladesNetworkClient(network_url, self.auth_token)
        return network_client
    
    def get_identity_client(self):
        return AstakosClient(self.endpoints['astakos'], self.auth_token)

    def add_private_network(self, recreate=True):
        existing_net = self.get_private_network()
        if not recreate and existing_net:
            return existing_net
 
        # if get_private_network():
        #     clean network
        project = self.get_project_id()
        network = self.get_network_client()
        net = network.create_network(type='MAC_FILTERED', name='Juju-okeanos private network', project_id=project)
        network.create_subnet(net['id'], '192.168.1.0/24')
        #network.create_subnet(net['id'], '192.168.1.0/24' , gateway_ip='192.168.1.1', 
        #                      allocation_pools={"start": "192.168.1.2", "end": "192.168.1.254"},  enable_dhcp=True)
        sleep(10)
        return net

    def get_private_network(self):
        network = self.get_network_client()
        for net in network.list_networks(detail=True):
            if not net['public'] and net['name'] == 'Juju-okeanos private network':
                return net

        return None

    def attach_private_ip_to_machine(self, net, vm):
        project = self.get_project_id()
        network = self.get_network_client()
        port = network.create_port(net['id'], vm['id'])
        print("****** Private port for vm  with id {} *******".format(vm['id']))
        print(port)
        print("****** port *******")
        port['status'] = network.wait_port(port['id'], port['status'])
        sleep(10)
        return port

    def attach_public_ip_to_machine(self, vm):
        project = self.get_project_id()
        network = self.get_network_client()
        ip = network.create_floatingip(project_id=project)
        print('Reserved new IP {}'.format(ip['floating_ip_address']))
        port = network.create_port(
                   network_id=ip['floating_network_id'],
                   device_id=vm['id'],
                   fixed_ips=[dict(ip_address=ip['floating_ip_address']), ])
        print("****** Public port for vm  with id {} *******".format(vm['id']))
        print(port)
        print("****** port *******")
        port['status'] = network.wait_port(port['id'], port['status'])
        sleep(10)
        return port

    def set_internal_gw(self, vm):
        self.remote_run(vm, ["route del default"])
        # get this from param
        # make this permanent
        sleep(10)
        self.remote_run(vm, ["route add default gw 192.168.1.2 eth1"])
        sleep(10)

    def set_nat(self, vm):
        self.remote_run(vm, ["echo 1 > /proc/sys/net/ipv4/ip_forward"])
        sleep(10)
        self.remote_run(vm, ["iptables -F"])
        sleep(10)
        self.remote_run(vm, ["iptables -t nat -F"])
        sleep(10)
        # get this from param
        # make this permanent
        self.remote_run(vm, ["iptables -t nat -A POSTROUTING -o eth1 -j MASQUERADE"])
        sleep(10)


    def add_machine(self, params, private_net=None, is_gateway=False, public_net=None):
        print(self.config)
        compute_client = self.get_compute_client()

        #Prepare ssh keys
        okeanos_ssh_key_path = os.environ.get('OKEANOS_SSH_KEY')
        ssh_keys = dict(
                contents=b64encode(open(okeanos_ssh_key_path).read()),
                path='/root/.ssh/authorized_keys',
                owner='root', group='root', mode=0600)

        #TODO(get from params)
        constraints = dict(
               ram=2048,
               vcpus=1,
               max_disk=10,
               min_disk=1)

        flv = self.get_flavor(constraints)
        if not flv:
            raise ConfigError("No flavor found")

        img = self.get_ubuntu_image()

        project = self.get_project_id()
        
        networks = []
        if private_net:
            networks.append({'uuid':private_net['id']})
        if public_net:
            networks.append({'uuid':public_net['id']})

        srv = compute_client.create_server(
                       params['name'],
                       flavor_id=flv['id'], 
                       image_id=img['id'], 
                       personality=[ssh_keys],
                       project_id=project,
                       networks=networks)

        print("Waiting for server....")    
        compute_client.wait_server(srv['id'], srv['status'])

        conn_info = dict(fqdn=srv['SNF:fqdn'], ip_address=[], id=srv['id'])
        nics = compute_client.get_server_nics(srv['id'])
        for port in nics['attachments']:
            if port['ipv4']:
                conn_info['ip_address'].append(port['ipv4'])
            if port['ipv6']:
                conn_info['ip_address'].append(port['ipv6'])

        print(conn_info)
        print(srv)
        print(nics)
        sleep(30)
        return conn_info


    def get_project_id(self):
        okeanos_project_name = os.environ.get('OKEANOS_PROJECT')
        identity_client = self.get_identity_client()
        for p in identity_client.get_projects():
            if okeanos_project_name == p['name']:
                print(p)
                return p['id']
        
        raise ConfigError("No project found")


    def get_ubuntu_image(self):
        image_client = self.get_image_client()
        for img in image_client.list_public():
            image_path = img['name']
            if "Ubuntu Server LTS" in image_path:
                print 'Image %s' % img
                return img
 

    def get_flavor(self, constraints):
        compute_client = self.get_compute_client()

        for flv in compute_client.list_flavors(detail=True):
            if flv['ram'] == constraints['ram'] and flv['vcpus'] == constraints['vcpus'] and constraints['min_disk'] <= flv['disk'] <= constraints['max_disk']:
                print 'Flavor', flv, 'matches'
                return flv

        return None


    def get_instances(self):
        return self.client.get_droplets()

    def get_instance(self, instance_id):
        return self.client.get_droplet(instance_id)

    def launch_instance(self, params):
        if not 'virtio' in params:
            params['virtio'] = True
        if not 'private_networking' in params:
            params['private_networking'] = True
        if 'ssh_key_ids' in params:
            params['ssh_key_ids'] = map(str, params['ssh_key_ids'])
        return self.client.create_droplet(**params)

    def terminate_instance(self, instance_id):
        self.client.destroy_droplet(instance_id)

    def wait_on(self, instance):
        return self._wait_on(instance.event_id, instance.name)

    def _wait_on(self, event, name, event_type=1):
        loop_count = 0
        while 1:
            time.sleep(8)  # Takes on average 1m for a do instance.
            done, result = self.client.create_done(event, name)
            if done:
                log.debug("Instance %s ready", name)
                return
            else:
                log.debug("Waiting on instance %s", name)
            if loop_count > 8:
                # Its taking a long while (2m+), give the user some
                # diagnostics if in debug mode.
                log.debug("Diagnostics on instance %s event %s",
                          name, result)
            if loop_count > 25:
                # After 3.5m for instance, just bail as provider error.
                raise ProviderError(
                    "Failed to get running instance %s event: %s" % (
                        name, result))
            loop_count += 1

