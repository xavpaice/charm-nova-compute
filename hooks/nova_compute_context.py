import uuid
import os
import platform

from charmhelpers.core.unitdata import kv
from charmhelpers.contrib.openstack import context
from charmhelpers.core.host import (
    lsb_release,
    service_running,
    service_start,
)
from charmhelpers.fetch import apt_install, filter_installed_packages
from charmhelpers.core.hookenv import (
    config,
    log,
    relation_get,
    relation_ids,
    related_units,
    service_name,
    unit_get,
    ERROR,
)
from charmhelpers.contrib.openstack.utils import (
    get_host_ip,
    get_os_version_package,
    get_os_version_codename,
    is_unit_paused_set,
)
from charmhelpers.contrib.network.ovs import add_bridge

from charmhelpers.contrib.network.ip import (
    get_address_in_network,
    get_ipv6_addr,
)

# This is just a label and it must be consistent across
# nova-compute nodes to support live migration.
CEPH_SECRET_UUID = '514c9fca-8cbe-11e2-9c52-3bc8c7819472'

OVS_BRIDGE = 'br-int'

CEPH_CONF = '/etc/ceph/ceph.conf'
CHARM_CEPH_CONF = '/var/lib/charm/{}/ceph.conf'


def ceph_config_file():
    return CHARM_CEPH_CONF.format(service_name())


def _save_flag_file(path, data):
    '''
    Saves local state about plugin or manager to specified file.
    '''
    # Wonder if we can move away from this now?
    if data is None:
        return
    with open(path, 'wb') as out:
        out.write(data)


# compatability functions to help with quantum -> neutron transition
def _network_manager():
    from nova_compute_utils import network_manager as manager
    return manager()


def _neutron_security_groups():
        '''
        Inspects current cloud-compute relation and determine if nova-c-c has
        instructed us to use neutron security groups.
        '''
        for rid in relation_ids('cloud-compute'):
            for unit in related_units(rid):
                groups = [
                    relation_get('neutron_security_groups',
                                 rid=rid, unit=unit),
                    relation_get('quantum_security_groups',
                                 rid=rid, unit=unit)
                ]
                if ('yes' in groups or 'Yes' in groups):
                    return True
        return False


def _neutron_plugin():
        from nova_compute_utils import neutron_plugin
        return neutron_plugin()


def _neutron_url(rid, unit):
        # supports legacy relation settings.
        return (relation_get('neutron_url', rid=rid, unit=unit) or
                relation_get('quantum_url', rid=rid, unit=unit))


class NovaComputeLibvirtContext(context.OSContextGenerator):

    '''
    Determines various libvirt and nova options depending on live migration
    configuration.
    '''
    interfaces = []

    def __call__(self):
        # distro defaults
        ctxt = {
            # /etc/libvirt/libvirtd.conf (
            'listen_tls': 0
        }
        if lsb_release()['DISTRIB_CODENAME'].lower() < "wily":
            ctxt['libvirtd_opts'] = '-d'
        else:
            ctxt['libvirtd_opts'] = ''
        # get the processor architecture to use in the nova.conf template
        ctxt['arch'] = platform.machine()

        # enable tcp listening if configured for live migration.
        if config('enable-live-migration'):
            ctxt['libvirtd_opts'] += ' -l'

        if config('migration-auth-type') in ['none', 'None', 'ssh']:
            ctxt['listen_tls'] = 0

        if config('migration-auth-type') == 'ssh':
            # nova.conf
            ctxt['live_migration_uri'] = 'qemu+ssh://%s/system'

        if config('instances-path') is not None:
            ctxt['instances_path'] = config('instances-path')

        if config('disk-cachemodes'):
            ctxt['disk_cachemodes'] = config('disk-cachemodes')

        if config('cpu-mode'):
            ctxt['cpu_mode'] = config('cpu-mode')

        if config('cpu-model'):
            ctxt['cpu_model'] = config('cpu-model')

        if config('hugepages'):
            ctxt['hugepages'] = True

        db = kv()
        if db.get('host_uuid'):
            ctxt['host_uuid'] = db.get('host_uuid')
        else:
            host_uuid = str(uuid.uuid4())
            db.set('host_uuid', host_uuid)
            db.flush()
            ctxt['host_uuid'] = host_uuid

        return ctxt


class NovaComputeLibvirtOverrideContext(context.OSContextGenerator):
    """Provides overrides to the libvirt-bin service"""
    interfaces = []

    def __call__(self):
        ctxt = {}
        ctxt['overrides'] = "limit nofile 65535 65535"
        return ctxt


class NovaComputeVirtContext(context.OSContextGenerator):
    interfaces = []

    def __call__(self):
        return {}


def assert_libvirt_imagebackend_allowed():
    os_rel = "Juno"
    os_ver = get_os_version_package('nova-common')
    if float(os_ver) < float(get_os_version_codename(os_rel.lower())):
        msg = ("Libvirt RBD imagebackend only supported for openstack >= %s" %
               os_rel)
        raise Exception(msg)

    return True


class NovaComputeCephContext(context.CephContext):

    def __call__(self):
        ctxt = super(NovaComputeCephContext, self).__call__()
        if not ctxt:
            return {}
        svc = service_name()
        # secret.xml
        ctxt['ceph_secret_uuid'] = CEPH_SECRET_UUID
        # nova.conf
        ctxt['service_name'] = svc
        ctxt['rbd_user'] = svc
        ctxt['rbd_secret_uuid'] = CEPH_SECRET_UUID
        ctxt['rbd_pool'] = config('rbd-pool')

        if (config('libvirt-image-backend') == 'rbd' and
                assert_libvirt_imagebackend_allowed()):
            ctxt['libvirt_images_type'] = 'rbd'
            ctxt['libvirt_rbd_images_ceph_conf'] = ceph_config_file()
        elif config('libvirt-image-backend') == 'lvm':
            ctxt['libvirt_images_type'] = 'lvm'

        rbd_cache = config('rbd-client-cache') or ""
        if rbd_cache.lower() == "enabled":
            # We use write-though only to be safe for migration
            ctxt['rbd_client_cache_settings'] = \
                {'rbd cache': 'true',
                 'rbd cache size': '64 MiB',
                 'rbd cache max dirty': '0 MiB',
                 'rbd cache writethrough until flush': 'true',
                 'admin socket': '/var/run/ceph/rbd-client-$pid.asok'}

            asok_path = '/var/run/ceph/'
            if not os.path.isdir(asok_path):
                os.mkdir(asok_path)

        elif rbd_cache.lower() == "disabled":
            ctxt['rbd_client_cache_settings'] = {'rbd cache': 'false'}

        return ctxt


class CloudComputeContext(context.OSContextGenerator):

    '''
    Generates main context for writing nova.conf and quantum.conf templates
    from a cloud-compute relation changed hook.  Mainly used for determinig
    correct network and volume service configuration on the compute node,
    as advertised by the cloud-controller.

    Note: individual quantum plugin contexts are handled elsewhere.
    '''
    interfaces = ['cloud-compute']

    def _ensure_packages(self, packages):
        '''Install but do not upgrade required packages'''
        required = filter_installed_packages(packages)
        if required:
            apt_install(required, fatal=True)

    @property
    def network_manager(self):
        return _network_manager()

    @property
    def volume_service(self):
        volume_service = None
        for rid in relation_ids('cloud-compute'):
            for unit in related_units(rid):
                volume_service = relation_get('volume_service',
                                              rid=rid, unit=unit)
        return volume_service

    @property
    def region(self):
        region = None
        for rid in relation_ids('cloud-compute'):
            for unit in related_units(rid):
                region = relation_get('region', rid=rid, unit=unit)
        return region

    def flat_dhcp_context(self):
        ec2_host = None
        for rid in relation_ids('cloud-compute'):
            for unit in related_units(rid):
                ec2_host = relation_get('ec2_host', rid=rid, unit=unit)

        if not ec2_host:
            return {}

        if config('multi-host').lower() == 'yes':
            self._ensure_packages(['nova-api', 'nova-network'])

        return {
            'flat_interface': config('flat-interface'),
            'ec2_dmz_host': ec2_host,
        }

    def neutron_context(self):
        # generate config context for neutron or quantum. these get converted
        # directly into flags in nova.conf
        # NOTE: Its up to release templates to set correct driver
        neutron_ctxt = {'neutron_url': None}
        for rid in relation_ids('cloud-compute'):
            for unit in related_units(rid):
                rel = {'rid': rid, 'unit': unit}

                url = _neutron_url(**rel)
                if not url:
                    # only bother with units that have a neutron url set.
                    continue

                neutron_ctxt = {
                    'auth_protocol': relation_get(
                        'auth_protocol', **rel) or 'http',
                    'service_protocol': relation_get(
                        'service_protocol', **rel) or 'http',
                    'service_port': relation_get(
                        'service_port', **rel) or '5000',
                    'neutron_auth_strategy': 'keystone',
                    'keystone_host': relation_get(
                        'auth_host', **rel),
                    'auth_port': relation_get(
                        'auth_port', **rel),
                    'neutron_admin_tenant_name': relation_get(
                        'service_tenant_name', **rel),
                    'neutron_admin_username': relation_get(
                        'service_username', **rel),
                    'neutron_admin_password': relation_get(
                        'service_password', **rel),
                    'api_version': relation_get(
                        'api_version', **rel) or '2.0',
                    'neutron_plugin': _neutron_plugin(),
                    'neutron_url': url,
                }

        missing = [k for k, v in neutron_ctxt.iteritems() if v in ['', None]]
        if missing:
            log('Missing required relation settings for Quantum: ' +
                ' '.join(missing))
            return {}

        neutron_ctxt['neutron_security_groups'] = _neutron_security_groups()

        ks_url = '%s://%s:%s/v%s' % (neutron_ctxt['auth_protocol'],
                                     neutron_ctxt['keystone_host'],
                                     neutron_ctxt['auth_port'],
                                     neutron_ctxt['api_version'])
        neutron_ctxt['neutron_admin_auth_url'] = ks_url

        return neutron_ctxt

    def volume_context(self):
        # provide basic validation that the volume manager is supported on the
        # given openstack release (nova-volume is only supported for E and F)
        # it is up to release templates to set the correct volume driver.

        if not self.volume_service:
            return {}

        # ensure volume service is supported on specific openstack release.
        if self.volume_service == 'cinder':
            return 'cinder'
        else:
            e = ('Invalid volume service received via cloud-compute: %s' %
                 self.volume_service)
            log(e, level=ERROR)
            raise context.OSContextError(e)

    def network_manager_context(self):
        ctxt = {}
        if self.network_manager == 'flatdhcpmanager':
            ctxt = self.flat_dhcp_context()
        elif self.network_manager == 'neutron':
            ctxt = self.neutron_context()

        _save_flag_file(path='/etc/nova/nm.conf', data=self.network_manager)

        log('Generated config context for %s network manager.' %
            self.network_manager)
        return ctxt

    def restart_trigger(self):
        rt = None
        for rid in relation_ids('cloud-compute'):
            for unit in related_units(rid):
                rt = relation_get('restart_trigger', rid=rid, unit=unit)
                if rt:
                    return rt

    def __call__(self):
        rids = relation_ids('cloud-compute')
        if not rids:
            return {}

        ctxt = {}

        net_manager = self.network_manager_context()
        if net_manager:
            ctxt['network_manager'] = self.network_manager
            ctxt['network_manager_config'] = net_manager
            # This is duplicating information in the context to enable
            # common keystone fragment to be used in template
            ctxt['service_protocol'] = net_manager.get('service_protocol')
            ctxt['service_host'] = net_manager.get('keystone_host')
            ctxt['service_port'] = net_manager.get('service_port')
            ctxt['admin_tenant_name'] = net_manager.get(
                'neutron_admin_tenant_name')
            ctxt['admin_user'] = net_manager.get('neutron_admin_username')
            ctxt['admin_password'] = net_manager.get('neutron_admin_password')
            ctxt['auth_protocol'] = net_manager.get('auth_protocol')
            ctxt['auth_host'] = net_manager.get('keystone_host')
            ctxt['auth_port'] = net_manager.get('auth_port')
            ctxt['api_version'] = net_manager.get('api_version')

        net_dev_mtu = config('network-device-mtu')
        if net_dev_mtu:
            ctxt['network_device_mtu'] = net_dev_mtu

        vol_service = self.volume_context()
        if vol_service:
            ctxt['volume_service'] = vol_service

        if self.restart_trigger():
            ctxt['restart_trigger'] = self.restart_trigger()

        region = self.region
        if region:
            ctxt['region'] = region

        return ctxt


class InstanceConsoleContext(context.OSContextGenerator):
    interfaces = []

    def get_console_info(self, proto, **kwargs):
        console_settings = {
            proto + '_proxy_address':
            relation_get('console_proxy_%s_address' % (proto), **kwargs),
            proto + '_proxy_host':
            relation_get('console_proxy_%s_host' % (proto), **kwargs),
            proto + '_proxy_port':
            relation_get('console_proxy_%s_port' % (proto), **kwargs),
        }
        return console_settings

    def __call__(self):
        ctxt = {}
        for rid in relation_ids('cloud-compute'):
            for unit in related_units(rid):
                rel = {'rid': rid, 'unit': unit}
                proto = relation_get('console_access_protocol', **rel)
                if not proto:
                    # only bother with units that have a proto set.
                    continue
                ctxt['console_keymap'] = relation_get('console_keymap', **rel)
                ctxt['console_access_protocol'] = proto
                ctxt['console_vnc_type'] = True if 'vnc' in proto else False
                if proto == 'vnc':
                    ctxt = dict(ctxt, **self.get_console_info('xvpvnc', **rel))
                    ctxt = dict(ctxt, **self.get_console_info('novnc', **rel))
                else:
                    ctxt = dict(ctxt, **self.get_console_info(proto, **rel))
                break
        ctxt['console_listen_addr'] = get_host_ip(unit_get('private-address'))
        return ctxt


class MetadataServiceContext(context.OSContextGenerator):

    def __call__(self):
        ctxt = {}
        for rid in relation_ids('neutron-plugin'):
            for unit in related_units(rid):
                rdata = relation_get(rid=rid, unit=unit)
                if 'metadata-shared-secret' in rdata:
                    ctxt['metadata_shared_secret'] = \
                        rdata['metadata-shared-secret']
        return ctxt


class DesignateContext(context.OSContextGenerator):

    def __call__(self):
        ctxt = {}
        ctxt['enable_designate'] = False
        for rid in relation_ids('nova-designate'):
            if related_units(rid):
                ctxt['enable_designate'] = True
        if ctxt['enable_designate']:
            ctxt['notification_driver'] = 'messaging'
            ctxt['notification_topics'] = 'notifications_designate'
            ctxt['notify_on_state_change'] = 'vm_and_task_state'
        return ctxt


class NeutronComputeContext(context.NeutronContext):
    interfaces = []

    @property
    def plugin(self):
        return _neutron_plugin()

    @property
    def network_manager(self):
        return _network_manager()

    @property
    def neutron_security_groups(self):
        return _neutron_security_groups()

    def _ensure_bridge(self):
        # NOTE(ajkavanagh) as this is called during the context usage (via
        # __call__()) it should get executed when the unit comes off pause.
        if not is_unit_paused_set():
            if not service_running('openvswitch-switch'):
                service_start('openvswitch-switch')
            add_bridge(OVS_BRIDGE)

    def ovs_ctxt(self):
        # In addition to generating config context, ensure the OVS service
        # is running and the OVS bridge exists. Also need to ensure
        # local_ip points to actual IP, not hostname.
        ovs_ctxt = super(NeutronComputeContext, self).ovs_ctxt()
        if not ovs_ctxt:
            return {}

        if config('manage-neutron-plugin-legacy-mode'):
            self._ensure_packages()
            self._ensure_bridge()

        ovs_ctxt['local_ip'] = \
            get_address_in_network(config('os-data-network'),
                                   get_host_ip(unit_get('private-address')))
        return ovs_ctxt

    def __call__(self):
        ctxt = super(NeutronComputeContext, self).__call__()
        # NOTE(jamespage) support override of neutron security via config
        if config('disable-neutron-security-groups'):
            ctxt['disable_neutron_security_groups'] = True

        return ctxt


class HostIPContext(context.OSContextGenerator):
    def __call__(self):
        ctxt = {}
        if config('prefer-ipv6'):
            host_ip = get_ipv6_addr()[0]
        else:
            host_ip = get_host_ip(unit_get('private-address'))

        if host_ip:
            # NOTE: do not format this even for ipv6 (see bug 1499656)
            ctxt['host_ip'] = host_ip

        return ctxt
