import logging
import time
import uuid
import yaml

from juju_okeanos import constraints
from juju_okeanos.exceptions import ConfigError, PrecheckError
from juju_okeanos import ops
from juju_okeanos.runner import Runner


log = logging.getLogger("juju.okeanos")


class BaseCommand(object):

    def __init__(self, config, provider, environment):
        self.config = config
        self.provider = provider
        self.env = environment
        self.runner = Runner()

    def solve_constraints(self):
        size, region = constraints.solve_constraints(self.config.constraints)
        t = time.time()
        image_map = constraints.get_images(self.provider.client)
        log.debug("Looked up docean images in %0.2f seconds", time.time() - t)
        return image_map[self.config.series], size, region

    def get_do_ssh_keys(self):
        return [k.id for k in self.provider.get_ssh_keys()]

    def check_preconditions(self):
        """Check for provider ssh key, and configured environments.yaml.
        """
        env_name = self.config.get_env_name()
        with open(self.config.get_env_conf()) as fh:
            conf = yaml.safe_load(fh.read())
            if not 'environments' in conf:
                raise ConfigError(
                    "Invalid environments.yaml, no 'environments' section")
            if not env_name in conf['environments']:
                raise ConfigError(
                    "Environment %r not in environments.yaml" % env_name)
            env = conf['environments'][env_name]
            if not env['type'] in ('null', 'manual'):
                raise ConfigError(
                    "Environment %r provider type is %r must be 'null'" % (
                        env_name, env['type']))
            if env['bootstrap-host']:
                raise ConfigError(
                    "Environment %r already has a bootstrap-host" % (
                        env_name))
        return True


class Bootstrap(BaseCommand):
    """
    Actions:
    - Launch an instance
    - Wait for it to reach running state
    - Update environment in environments.yaml with bootstrap-host address.
    - Bootstrap juju environment

    Preconditions:
    - named environment found in environments.yaml
    - environment provider type is null
    - bootstrap-host must be null
    - at least one ssh key must exist.
    - ? existing digital ocean with matching env name does not exist.
    """
    def run(self):
        self.check_preconditions()
        #TODO: fix constraints
        #image, size, region = self.solve_constraints()
        log.info("Launching bootstrap host (eta 5m)...")        
        params = dict(
            name="%s-0" % self.config.get_env_name())
        net = self.provider.add_private_network(recreate=False)
        instance = self.provider.add_machine(params)
        self.provider.attach_public_ip_to_machine(instance)
        self.provider.attach_private_ip_to_machine(net, instance)
        self.provider.set_nat(instance)

        log.info("Bootstrapping environment...")
        try:
            self.env.bootstrap_jenv(instance['fqdn'])
        except:
            self.provider.terminate_instance(instance['id'])
            raise
        log.info("Bootstrap complete.")
        

    def check_preconditions(self):
        result = super(Bootstrap, self).check_preconditions()
        if self.env.is_running():
            raise PrecheckError(
                "Environment %s is already bootstrapped" % (
                self.config.get_env_name()))
        return result


class ListMachines(BaseCommand):

    def run(self):
        env_name = self.config.get_env_name()
        header = "{:<8} {:<18} {:<5} {:<8} {:<12} {:<6} {:<10}".format(
            "Id", "Name", "Size", "Status", "Created", "Region", "Address")

        allmachines = self.config.options.all
        for m in self.provider.get_instances():
            if not allmachines and not m.name.startswith('%s-' % env_name):
                continue

            if header:
                print(header)
                header = None

            for r in constraints.REGIONS:
                if m.region_id == r.id:
                    break
            name = m.name
            if len(name) > 18:
                name = name[:15] + "..."
            size = constraints.SIZE_MAP.get(m.size_id)
            if size is None:
                size_name = "Unknown"
            else:
                size_name = getattr(size, 'name', "Unknown")
            print("{:<8} {:<18} {:<5} {:<8} {:<12} {:<6} {:<10}".format(
                m.id,
                name,
                size_name,
                m.status,
                m.created_at[:-10],
                r.slug,
                m.ip_address).strip())


class AddMachine(BaseCommand):

    def run(self):
        self.check_preconditions()
        #TODO: fix constraints
        #image, size, region = self.solve_constraints()
        log.info("Launching %d instances...", self.config.num_machines)


        #op_class = self.provider.version == 2.0 and \
        #    ops.MachineUserDataRegister or ops.MachineRegister

        net = self.provider.get_private_network()
        for n in range(self.config.num_machines):
            machine_name="%s-%s" % (self.config.get_env_name(), uuid.uuid4().hex)
            params = dict(
                      name=machine_name
                     )

            # This should be flagged in case we use only ipv6
            instance = self.provider.add_machine(params, private_net=net)
            self.provider.set_internal_gw(instance)
            self.env.add_machine("ssh:root@%s" % instance['fqdn'])
            # TODO if the above fail kill the machine.
            log.info("Registered id:%s name:%s %s as juju machine",
                     instance['fqdn'], machine_name, instance['ip_address'][0])


class TerminateMachine(BaseCommand):

    def run(self):
        """Terminate machine in environment.
        """
        self.check_preconditions()
        self._terminate_machines()

    def _machine_filter(self, mid, m):
        return any([
            spec == mid for spec in
            self.config.options.machines if mid != '0'])

    def _terminate_machines(self, machine_filter=None):
        status = self.env.status()
        machines = status.get('machines', {})

        machine_filter = machine_filter or self._machine_filter
        # Using the api instance-id can be the provider id, but
        # else it defaults to ip, and we have to disambiguate.
        remove = []
        for m in machines:
            if machine_filter(m, machines[m]):
                remove.append(
                    {'address': machines[m].get('dns-name'),
                     'instance_id': machines[m]['instance-id'],
                     'machine_id': m})

        address_map = dict([(d.ip_address, d) for
                            d in self.provider.get_instances()])
        if not remove:
            return status, address_map

        log.info("Terminating machines %s",
                 " ".join([m['machine_id'] for m in remove]))

        for m in remove:
            instance = None
            if m['address']:
                instance = address_map.get(m['address'])
            else:
                instances = [
                    i for i in address_map.values()
                    if m['instance_id'] == i.name]
                if len(instances) == 1:
                    instance = instances[0]
                    #instances['instance'] =
            env_only = False  # Remove from only env or also provider.
            if instance is None:
                log.warning(
                    "Couldn't resolve machine %s's address %s to instance" % (
                        m['machine_id'], m['address']))
                # We have a machine in juju state that we couldn't
                # find in provider. Remove it from state so destroy
                # can proceed.
                env_only = True
                instance_id = None
            else:
                instance_id = instance.id
            self.runner.queue_op(
                ops.MachineDestroy(
                    self.provider, self.env, {
                        'machine_id': m['machine_id'],
                        'instance_id': instance_id},
                    env_only=env_only))
        for result in self.runner.iter_results():
            pass

        return status, address_map


class DestroyEnvironment(TerminateMachine):

    def run(self):
        """Destroy environment.
        """
        self.check_preconditions()
        force = self.config.options.force

        # Manual provider needs machines removed prior to env destroy.
        def state_service_filter(mid, m):
            if mid == "0":
                return False
            return True

        if force:
            return self.force_environment_destroy()

        env_status, instance_map = self._terminate_machines(
            state_service_filter)

        # sadness, machines are marked dead, but juju is async to
        # reality. either sleep (racy) or retry loop, 10s seems to
        # plenty of time.
        time.sleep(10)

        log.info("Destroying environment")
        self.env.destroy_environment()

        # Remove the state server.
        bootstrap_host = env_status.get(
            'machines', {}).get('0', {}).get('dns-name')
        instance = instance_map.get(bootstrap_host)
        if instance:
            log.info("Terminating state server")
            self.provider.terminate_instance(instance.id)
        log.info("Environment Destroyed")

    def force_environment_destroy(self):
        env_name = self.config.get_env_name()
        env_machines = [m for m in self.provider.get_instances()
                        if m.name.startswith("%s-" % env_name)]

        log.info("Destroying environment")
        for m in env_machines:
            self.runner.queue_op(
                ops.MachineDestroy(
                    self.provider, self.env, {'instance_id': m.id},
                    iaas_only=True))

        for result in self.runner.iter_results():
            pass

        # Fast destroy the client cache by removing the jenv file.
        self.env.destroy_environment_jenv()
        log.info("Environment Destroyed")
