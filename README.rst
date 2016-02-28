Juju Okeanos Provider
---------------------

This is a refactored copy of Juju Digital Ocean Provider.

Some config instructions apply here as well:

Juju Config
+++++++++++

Next let's configure a juju environment for digital ocean, add
a manual provider environment to 'environments.yaml', for example::

 environments:
   digitalocean:
      type: manual
      bootstrap-host: null
      bootstrap-user: root


Env variable you need to set
++++++++++++++++++++++++++++

OKEANOS_KAMAKIRC the kamaki rc
OKEANOS_SSH_KEY the ssh key path
OKEANOS_PROJECT the project that you have resources on


