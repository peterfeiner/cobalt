# Copyright 2011 Gridcentric Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Interfaces that configure vms and perform hypervisor specific operations.
"""

import hashlib
import os
import pwd
import tempfile
import uuid
import inspect

from glanceclient.exc import HTTPForbidden

import nova
from nova import exception

from nova.virt import images
from nova.virt.libvirt import blockinfo
from nova.virt.libvirt import imagebackend
from nova.virt.libvirt.imagecache import get_cache_fname
from nova.virt.libvirt import utils as libvirt_utils
from nova.compute import utils as compute_utils
from nova.openstack.common import log as logging
from oslo.config import cfg

from .. import image as co_image

from nova.openstack.common.gettextutils import _

LOG = logging.getLogger('nova.cobalt.vmsconn')
CONF = cfg.CONF

vmsconn_opts = [
               cfg.BoolOpt('cobalt_use_image_service',
               deprecated_name='gridcentric_use_image_service',
               default=False,
               help='Cobalt should use the image service to store disk copies and descriptors.'),

               cfg.StrOpt('openstack_user',
               default='',
               help='The openstack user'),

               cfg.BoolOpt('cobalt_clean_unused_symlinks',
               default=True,
               help='Cobalt should clean up symlinks that is creates and'
                    'are discovered to be unused.')]
CONF.register_opts(vmsconn_opts)

import vms.utilities as utilities
from . import vmsapi as vms_api

def run_as(cmd, uid):
    sudo_cmd = ['sudo', '-u', '#%d' % uid]
    sudo_cmd += cmd
    utilities.check_command(sudo_cmd)

def mkdir_as(path, uid):
    run_as([ 'mkdir', '-p', path], uid)

def touch_as(path, uid):
    run_as(['touch', path], uid)

def symlink_as(target, link, uid):
    run_as(['ln', '-s', target, link], uid)

def get_vms_connection(virt_driver):
    # Configure the logger regardless of the type of connection that will be used.

    drivers = {
        'FakeDriver': 'fake',
        'LibvirtDriver': 'libvirt',
        'XenAPIDriver': 'xenapi',
        'XenApiDriver': 'xenapi'
    }
    driver_name = virt_driver.__class__.__name__
    connection_type = drivers.get(driver_name, None)
    if connection_type == None:
        raise exception.NovaException(
            'Unsupported compute driver %s being used.' %(driver_name))

    vmsapi = vms_api.get_vmsapi()
    if connection_type == 'xenapi':
        return XenApiConnection(vmsapi, virt_driver)
    elif connection_type == 'libvirt':
        return LibvirtConnection(vmsapi, virt_driver)
    elif connection_type == 'fake':
        return DummyConnection(vmsapi, virt_driver)
    else:
        raise exception.NovaException(_('Unsupported connection type "%s"' % connection_type))

def _log_call(fn):
    def wrapped_fn(self, *args, **kwargs):
        try:
            LOG.debug(_("Calling %s with args=%s kwargs=%s") % \
                       (fn.__name__, str(args), str(kwargs)))
            return fn(self, *args, **kwargs)
        finally:
            LOG.debug(_("Called %s with args=%s kwargs=%s") % \
                       (fn.__name__, str(args), str(kwargs)))

    wrapped_fn.__name__ = fn.__name__
    wrapped_fn.__doc__ = fn.__doc__
    return wrapped_fn


class VmsConnection:

    def __init__(self, vmsapi,virt_driver, image_service=None):
        self.vmsapi = vmsapi
        self.virt_driver = virt_driver
        self.image_service = image_service if image_service is not None \
                                                    else co_image.ImageService()

    def configure(self, virtapi):
        """
        Configures vms for this type of connection.
        """
        pass

    @_log_call
    def bless(self, context, instance_name, new_instance_ref, migration_url=None):
        """
        Create a new blessed VM from the instance with name instance_name and gives the blessed
        instance the name new_instance_name.
        """
        new_instance_name = new_instance_ref['name']
        result = self.vmsapi.bless(instance_name, new_instance_name,
                                   mem_url=migration_url, migration=migration_url and True)

        self._chmod_blessed_files(result.blessed_files)

        return (result.newname, result.network, result.blessed_files, result.logical_volumes)

    def _chmod_blessed_files(self, blessed_files):
        """ Change the permission on the blessed files """
        pass

    @_log_call
    def post_bless(self, context, new_instance_ref, blessed_files, vms_policy_template=None):
        if CONF.cobalt_use_image_service:
            return self._upload_files(context, new_instance_ref, blessed_files,
                                      vms_policy_template=vms_policy_template)
        else:
            return blessed_files

    @_log_call
    def bless_cleanup(self, blessed_files):
        if CONF.cobalt_use_image_service:
            for blessed_file in blessed_files:
                if os.path.exists(blessed_file):
                    os.unlink(blessed_file)

    @_log_call
    def _upload_files(self, context, instance_ref, blessed_files,
                      image_ids=None, vms_policy_template=None):
        """ Upload the bless files into nova's image service (e.g. glance). """
        raise Exception("Uploading files to the image service is not supported.")

    @_log_call
    def discard(self, context, instance_name, migration_url=None, image_refs=[]):
        """
        Discard all of the vms artifacts associated with a blessed instance
        """
        result =  self.vmsapi.discard(instance_name, mem_url=migration_url)
        if CONF.cobalt_use_image_service:
            self._delete_images(context, image_refs)

    @_log_call
    def _delete_images(self, context, image_refs):
        pass

    @_log_call
    def launch(self, context, instance_name, new_instance_ref,
               network_info, skip_image_service=False, target=0,
               migration_url=None, image_refs=[], params={}, vms_policy='',
               block_device_info=None,lvm_info={}):
        """
        Launch a blessed instance
        """
        new_name, path = self.pre_launch(context, new_instance_ref, network_info,
                                        migration=(migration_url and True),
                                        skip_image_service=skip_image_service,
                                        image_refs=image_refs,
                                        block_device_info=block_device_info,
                                        lvm_info=lvm_info)

        # Launch the new VM.
        vms_options = {'memory.policy':vms_policy}
        for vif in network_info:
            LOG.info("**** Launching instance with VIF address %s ****", vif['address'])
        if len(network_info) > 0:
            vms_options['xen_mac_addr'] = network_info[0]['address']
        result = self.vmsapi.launch(instance_name, new_name, target, path,
                                    mem_url=migration_url, migration=(migration_url and True),
                                    guest_params=params.get('guest',{}),
                                    vms_options=vms_options)

        # Take care of post-launch.
        self.post_launch(context,
                         new_instance_ref,
                         network_info,
                         migration=(migration_url and True))
        return result

    @_log_call
    def pre_launch(self, context,
                   new_instance_ref,
                   network_info=None,
                   block_device_info=None,
                   migration=False,
                   skip_image_service=False,
                   image_refs=[],
                   lvm_info={}):
        return (new_instance_ref['name'], None)

    @_log_call
    def post_launch(self, context,
                    new_instance_ref,
                    network_info=None,
                    block_device_info=None,
                    migration=False):
        pass

    @_log_call
    def pre_migration(self, context, instance_ref, network_info, migration_url):
        pass

    @_log_call
    def post_migration(self, context, instance_ref, network_info, migration_url):
        pass

    @_log_call
    def pause_instance(self, instance_ref):
        self.vmsapi.pause(instance_ref['name'])

    @_log_call
    def unpause_instance(self, instance):
        self.vmsapi.unpause(instance['name'])

    def pre_export(self, context, instance_ref, image_refs=[]):
        config = self.vmsapi.config()
        shared = config.SHARED

        artifacts = []

        for image_ref in image_refs:
            if image_ref.startswith(config.SHARED):
                pass
            else:
                image = self.image_service.show(context, image_ref)
                # old usage of image['name'] included for backwards compatibility
                target = os.path.join(shared, image['properties'].get('file_name', image['name']))
                self.image_service.download(context, image_ref, target)
                artifacts.append(target)

        fd, temp_target = tempfile.mkstemp()
        os.close(fd)
        return temp_target, None, artifacts

    def export_instance(self, context, instance_ref, image_id, image_refs=[]):
        archive, path, artifacts = self.pre_export(context, instance_ref, image_refs)

        self.vmsapi.export(instance_ref, archive, path)

        self.post_export(context, instance_ref, archive, image_id, artifacts)

    def post_export(self, context, instance_ref, archive, image_id, artifacts):
        for artifact in artifacts:
            os.unlink(artifact)

        # Load the archive into glance
        self.image_service.upload(context, image_id, archive)

        os.unlink(archive)

    def pre_import(self, context, image_id):
        fd, archive = tempfile.mkstemp()
        try:
            os.close(fd)
            self.image_service.download(context, image_id, archive)
        except Exception, ex:

            try:
                os.unlink(archive)
            except:
                LOG.warn(_("Failed to remove the import archive %s. It may still be on the system."), archive)
            raise ex

        return archive

    def import_instance(self, context, instance_ref, image_id):
        archive = self.pre_import(context, image_id)

        artifacts = self.vmsapi.import_(instance_ref, archive)

        return self.post_import(context, instance_ref, image_id, archive, artifacts)

    def post_import(self, context, instance_ref, image_id, archive, artifacts):

        os.unlink(archive)

        image_ids = []

        if CONF.cobalt_use_image_service:
            for artifact in artifacts:
                _, image_id = self._friendly_upload(context, instance_ref, artifact)
                image_ids.append(image_id)
                os.unlink(artifact)

        return image_ids

    def _get_glance_displayname_and_type(self, instance_ref, filename):
        image_name = instance_ref['display_name']
        image_type = "Image"
        if filename.endswith(".gc"):
            image_type = "Live-Image"
        elif filename.endswith(".disk"):
            image_name += "." + filename.split(".")[-2] + ".disk"
        else:
            image_name += os.path.splitext(filename)[1] # get file extension
        return image_name, image_type

    # (rui-lin) instance-xxxxx is used by vms, and stored as file_name
    # However to glance we want to use the user friendly display_name
    # We also don't want to display the .gc file extension
    def _friendly_upload(self, context, instance_ref, filename):
        image_name, image_type = self._get_glance_displayname_and_type(instance_ref, filename)

        image_id = self.image_service.create(context, image_name, instance_uuid=instance_ref['uuid'])
        self.image_service.upload(context, image_id, filename)

        properties = self.image_service.show(context, image_id)['properties']
        properties.update({'image_type': image_type,
                           'file_name': os.path.basename(filename)})
        self.image_service.update(context, image_id, {'properties': properties})

        return image_name, image_id

    @_log_call
    def install_policy(self, raw_ini_policy):
        """
        Install a new set of policy definitions (provided as a string with the
        contents of an ini file) on the local host.
        """
        return self.vmsapi.install_policy(raw_ini_policy)

    def get_hypervisor_hostname(self):
        raise NotImplementedError()

    def periodic_clean(self):
        """
        Performs a periodic cleanup of leftover on the system as a result
        of using cobalt / vms.
        """
        pass

    def get_instance_info(self, instance):
        raise NotImplementedError()


class DummyConnection(VmsConnection):
    def configure(self, virtapi):
        self.vmsapi.configure('dummy')

class XenApiConnection(VmsConnection):
    """
    VMS connection for XenAPI
    """
    def configure(self, virtapi):
        # (dscannell) We need to import this to ensure that the xenapi
        # flags can be read in.
        from nova.virt.xenapi import driver

        self.vmsapi.configure(
            vms_api.XapiPlugin(
                self.virt_driver._session,
                vms_platform='xcp',
                management_options=
                      {'connection_url': CONF.xenapi_connection_url,
                       'connection_username': CONF.xenapi_connection_username,
                       'connection_password': CONF.xenapi_connection_password}))

    def get_hypervisor_hostname(self):
        return self.virt_driver._hypervisor_hostname

    @_log_call
    def bless(self, context, instance_name, new_instance_ref,
              migration_url=None):
        """
        Create a new blessed VM from the instance with name instance_name and
        gives the blessed instance the name new_instance_name.
        """
        new_instance_name = new_instance_ref['name']
        result = self.virt_driver.bless(context, self.vmsapi, instance_name,
                                        new_instance_ref, migration_url)

        return (result.newname, result.network, result.blessed_files,
                result.logical_volumes)

    @_log_call
    def launch(self, context, instance_name, new_instance,
               network_info, skip_image_service=False, target=0,
               migration_url=None, image_refs=[], params={}, vms_policy='',
               block_device_info=None,lvm_info={}):
        """
        Launch a blessed instance
        """

        # Launch the new VM.
        vms_options = {'memory.policy':vms_policy}

        if len(network_info) > 0:
            vms_options['xen_mac_addr'] = network_info[0]['address']
        result = self.virt_driver.launch(context,
                                         self.vmsapi,
                                         instance_name,
                                         new_instance,
                                         target,
                                         None,
                                         mem_url=migration_url,
                                         migration=(migration_url and True),
                                         guest_params=params.get('guest',{}),
                                         vms_options=vms_options)

        return result

    @_log_call
    def get_instance_info(self, instance):
        return self.virt_driver.get_info(instance)

class LaunchImageBackend(imagebackend.Backend):
    """This is the image backend to use when launching instances."""

    def backend(self, image_type=None):
        """ The backend for launched instances will always be
            either qcow2 or raw (never lvm)"""
        if image_type == 'raw':
            return super(LaunchImageBackend, self).backend('raw')
        return super(LaunchImageBackend, self).backend('qcow2')

class LibvirtConnection(VmsConnection):
    """
    VMS connection for Libvirt
    """

    def configure(self, virtapi):
        # (dscannell) import the libvirt module to ensure that the the
        # libvirt flags can be read in.
        from nova.virt.libvirt.driver import LibvirtDriver

        self.determine_openstack_user()

        # Two libvirt drivers are created for the two different cases:
        #   migration: When doing a migration we attempt to keep everything
        #              the same as a regular boot. In this case we just use
        #              the regular driver without modifications.
        #
        #   launch:     When doing a launch we dealing exclusively with qcow2
        #               images. We need to replace the regular image backend
        #               with the LaunchImageBackend that will force exclusive
        #               use of qcow2.
        launch_libvirt_conn = LibvirtDriver(virtapi, read_only=False)
        launch_libvirt_conn.image_backend = LaunchImageBackend(CONF.use_cow_images)
        self.libvirt_connections = {'migration': LibvirtDriver(virtapi, read_only=False),
                                    'launch': launch_libvirt_conn}

        libvirt_uri = launch_libvirt_conn.uri()
        self.vmsapi.configure(
                vms_api.Vmsctl(vms_platform='libvirt',
                            management_options={'connection_url': libvirt_uri}))

    @_log_call
    def determine_openstack_user(self):
        """
        Determines the openstack user's uid and gid
        """

        openstack_user = CONF.openstack_user
        if openstack_user == '':
            # The user has not set an explicit openstack_user. We will attempt to auto-discover
            # a reasonable value by checking the ownership of of the instances path. If we are
            # unable to determine in then we default to owner of this process.
            try:
                openstack_user = os.stat(CONF.instances_path).st_uid
            except:
                openstack_user = os.getuid()

        try:
            if isinstance(openstack_user, str):
                passwd = pwd.getpwnam(openstack_user)
            else:
                passwd = pwd.getpwuid(openstack_user)
            self.openstack_uid = passwd.pw_uid
            self.openstack_gid = passwd.pw_gid
            LOG.info("The openstack user is set to (%s, %s, %s)."
                     % (passwd.pw_name, self.openstack_uid, self.openstack_gid))
        except Exception, e:
            LOG.severe("Failed to find the openstack user %s on this system. " \
                       "Please configure the openstack_user flag correctly." % (openstack_user))
            raise e

    def _stub_disks(self, libvirt_conn, instance, disk_mapping, block_device_info, lvm_info):
        # Note(dscannell): We want to stub out the disks that nova expects to
        # to exists and our calls _create_image will lazy create them. There
        # are essentially two disk we need to stub:
        #       disk - the instance's root disk (optional)
        #       disk.local - some ephemeral storage (optional).
        #
        # Once we know which disks to stub we figure out the backend nova is
        # using and stub them with the correct path.
        disk_names_to_stub = []

        booted_from_volume = ( (not bool(instance.get('image_ref')))
                                or 'disk' not in disk_mapping)
        if not booted_from_volume:
            disk_names_to_stub.append('disk')
        if  'disk.local' in disk_mapping:
            # We will have to stub out the ephemeral disk as well.
            disk_names_to_stub.append('disk.local')

        stubbed_disks = {}
        for disk_name in disk_names_to_stub:
            lvm_size = None
            for lvm_path, size in lvm_info.iteritems():
                # The last part of the lvm path will be the disk name
                # which is either 'disk' or 'disk.local'.
                if lvm_path.endswith(disk_name):
                    # This disk file corresponds to this lvm path. We should
                    # make it the same size.
                    lvm_size = int(size)

            # NOTE(dscannell): If this is a launch libvirt_conn then the
            # backend will always be qcow2 because the image_backend is
            # replaced with LaunchImageBackend
            nova_disk = libvirt_conn.image_backend.image(instance,
                                                         disk_name,
                                                         CONF.libvirt.images_type)
            self._stub_disk(nova_disk, size=lvm_size)
            stubbed_disks[disk_name] = nova_disk

            if nova_disk.source_type == 'file' and \
               CONF.libvirt.images_type == 'lvm':
                # (dscannell): nova expects the instances to be back by lvm
                #              instead of a qcow2 file. So when rebooting, etc
                #              nova will recreate the instance libvrit.xml with
                #              the lvm backing disk instead of this qcow2. We
                #              basically create a symlink to the qcow2 to ensure
                #              that rebooting, etc continue to work.
                lvm_disk_file = imagebackend.Lvm(instance, disk_name)
                symlink_as(str(nova_disk.path),
                           str(lvm_disk_file.path),
                           os.getuid())

        return stubbed_disks

    def _stub_disk(self, nova_disk, size=None):
        disk_file = nova_disk.path
        source_type = nova_disk.source_type

        disk_dir = os.path.dirname(disk_file)
        if source_type == 'file':
            # We need to make sure that the file & directory exists as the
            # openstack user
            mkdir_as(disk_dir, self.openstack_uid)
            touch_as(disk_file, self.openstack_uid)
            os.chown(disk_file, self.openstack_uid, self.openstack_gid)

        elif source_type == 'block':
            # Note(dscannell) it is a requirement for nova that the volume group already
            # exists. However we need to create the LVM
            if size != None:
                libvirt_utils.create_lvm_image(nova_disk.vg, nova_disk.lv,
                    size, sparse=nova_disk.sparse)
            else:
                LOG.warn(_("Unable to determine the size for the lvm %s") %(disk_file))
        else:
            raise exception.NovaException("Unsupported disk type %s" %(source_type))

        return disk_file

    def _instance_has_ephemeral(self, libvirt_conn, instance, block_device_info):
        """This mimics the check the libvirt driver does to determine
        if ephemeral storage should be added."""
        return instance['ephemeral_gb'] and \
               libvirt_conn._volume_in_mapping(libvirt_conn.default_second_device,
                                                    block_device_info)

    @_log_call
    def pre_launch(self, context,
                   new_instance_ref,
                   network_info=None,
                   block_device_info=None,
                   migration=False,
                   skip_image_service=False,
                   image_refs=[],
                   lvm_info={}):

        image_base_path = os.path.join(CONF.instances_path,
                                       CONF.image_cache_subdirectory_name)
        if not os.path.exists(image_base_path):
            LOG.debug('Base path %s does not exist. It will be created now.', image_base_path)
            mkdir_as(image_base_path, self.openstack_uid)

        artifact_path = None
        if not(skip_image_service) and CONF.cobalt_use_image_service:
            artifact_path = image_base_path
            # We need to first download the descriptor and the disk files
            # from the image service.
            LOG.debug("Downloading images %s from the image service." % (image_refs))

            for image_ref in image_refs:
                image = self.image_service.show(context, image_ref)
                # In previous versions name was the filename (*.gc, *.disk) so
                # there was no file_name property. Now that name is more descriptive
                # when uploaded to glance, file_name property is set; use if possible
                target = os.path.join(image_base_path,
                                      image['properties'].get('file_name',image['name']))

                if migration or not os.path.exists(target):
                    # If the path does not exist fetch the data from the image
                    # service.  NOTE: We always fetch in the case of a
                    # migration, as the descriptor may have changed from its
                    # previous state. Migrating VMs are the only case where a
                    # descriptor for an instance will not be a fixed constant.
                    # We download to a temporary location so we can make the
                    # file appear atomically from the right user.
                    fd, temp_target = tempfile.mkstemp(dir=image_base_path)
                    try:
                        os.close(fd)
                        self.image_service.download(context, image_ref, temp_target)
                        os.chown(temp_target, self.openstack_uid, self.openstack_gid)
                        os.chmod(temp_target, 0644)
                        os.rename(temp_target, target)
                    except:
                        os.unlink(temp_target)
                        raise

        # (dscannell): Determine which libvirt_conn to use. If this is for
        #              migration, and there exists some lvm information, then
        #              use the migration libvirt_conn (that will use the
        #              configured image backend). Otherwise, default to launch
        #              libvirt_conn that will always use a qcow2 backend. It is
        #              safer to use the launch libvirt_conn for a migration if
        #              no lvm_info is given.
        if migration and len(lvm_info) > 0:
            libvirt_conn_type = 'migration'
        else:
            libvirt_conn_type = 'launch'
        libvirt_conn = self.libvirt_connections[libvirt_conn_type]

        # (dscannell) Check to see if we need to convert the network_info
        # object into the legacy format.
        if hasattr(network_info, 'legacy') and libvirt_conn.legacy_nwinfo():
            network_info = network_info.legacy()

        # TODO(dscannell): This method can take an optional image_meta that
        # appears to be the root disk's image metadata. it checks the metadata
        # for the image format (e.g. iso, disk, etc). Right now we are passing
        # in None (default) but we need to double check this.
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            new_instance_ref,
                                            block_device_info)

        # We need to create the libvirt xml, and associated files. Pass back
        # the path to the libvirt.xml file.
        working_dir = os.path.join(CONF.instances_path, new_instance_ref['uuid'])

        stubbed_disks = self._stub_disks(libvirt_conn,
                                         new_instance_ref,
                                         disk_info['mapping'],
                                         block_device_info,
                                         lvm_info)

        libvirt_file = os.path.join(working_dir, "libvirt.xml")
        # Make sure that our working directory exists.
        mkdir_as(working_dir, self.openstack_uid)

        # (dscannell) We want to disable any injection. We do this by making a
        # copy of the instance and clearing out some entries. Since OpenStack
        # uses dictionary-list accessors, we can pass this dictionary through
        # that code.
        instance_dict = dict((key, value)
            for key, value in new_instance_ref.iteritems())

        # The name attribute is special and does not carry over like the rest
        # of the attributes.
        instance_dict['key_data'] = None
        instance_dict['metadata'] = []

        # Stub out an image in the _base directory so libvirt_conn._create_image
        # doesn't try to download the base image that the master was booted
        # from, which isn't necessary because a clone's thin-provisioned disk is
        # overlaid on a disk image accessible via VMS_DISK_URL. Note that we
        # can't just touch the image's file in _base because master instances
        # will need the real _base data. So we trick _create_image into using
        # some bogus id for the image id that'll never be the id of a real
        # image; the new instance's uuid suffices.
        disk_images = {'image_id': new_instance_ref['uuid'],
                       'kernel_id': new_instance_ref['kernel_id'],
                       'ramdisk_id': new_instance_ref['ramdisk_id']}
        touch_as(os.path.join(image_base_path,
                              get_cache_fname(disk_images, 'image_id')),
                 self.openstack_uid)

        # (dscannell) This was taken from the core nova project as part of the
        # boot path for normal instances. We basically want to mimic this
        # functionality.
        libvirt_conn._create_image(context, instance_dict,
                                   disk_info['mapping'],
                                   network_info=network_info,
                                   block_device_info=block_device_info,
                                   disk_images=disk_images)
        xml = libvirt_conn.to_xml(context, instance_dict, network_info,
                                  disk_info,
                                  block_device_info=block_device_info,
                                  write_to_disk=True)

        if not(migration):
            for disk_name, disk_file in stubbed_disks.iteritems():
                disk_path = disk_file.path
                if os.path.exists(disk_path) and disk_file.source_type == 'file':
                    # (dscannell) Remove the fake disk file (if created).
                    os.remove(disk_path)

        # Fix up the permissions on the files that we created so that they are owned by the
        # openstack user.
        for root, dirs, files in os.walk(working_dir, followlinks=True):
            for path in dirs + files:
                LOG.debug("chowning path=%s to openstack user %s" % \
                         (os.path.join(root, path), self.openstack_uid))
                os.chown(os.path.join(root, path), self.openstack_uid, self.openstack_gid)

        # Return the libvirt file, this will be passed in as the name. This
        # parameter is overloaded in the management interface as a libvirt
        # special case.
        return (libvirt_file, artifact_path)

    @_log_call
    def post_launch(self, context,
                    new_instance_ref,
                    network_info=None,
                    block_device_info=None,
                    migration=False):
        libvirt_conn_type = 'migration' if migration else 'launch'
        libvirt_conn = self.libvirt_connections[libvirt_conn_type]

        libvirt_conn._enable_hairpin(new_instance_ref)
        libvirt_conn.firewall_driver.apply_instance_filter(new_instance_ref, network_info)

    @_log_call
    def pre_migration(self, context, instance_ref, network_info, migration_url):
        # Make sure that the disk reflects all current state for this VM.
        # It's times like these that I wish there was a way to do this on a
        # per-file basis, but we have no choice here but to sync() globally.
        utilities.call_command(["sync"])

    @_log_call
    def post_migration(self, context, instance_ref, network_info, migration_url):
        # We make sure that all the memory servers are gone that need it.
        # This looks for any servers that are providing the migration_url we
        # used above -- since we no longer need it. This is done this way
        # because the domain has already been destroyed and wiped away.  In
        # fact, we don't even know it's old PID and a new domain might have
        # appeared at the same PID in the meantime.
        self.vmsapi.kill_memservers(migration_url)

    def _chmod_blessed_files(self, blessed_files):
        for blessed_file in blessed_files:
            try:
                os.chmod(blessed_file, 0644)
            except OSError:
                pass

    def _create_image(self, context, image_service, instance_ref, image_name):
        # Create the image in the image_service.
        properties = {'instance_uuid': instance_ref['uuid'],
                  'user_id': str(context.user_id),
                  'image_state': 'creating'}

        sent_meta = {'name': image_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        recv_meta = image_service.create(context, sent_meta)
        image_id = recv_meta['id']
        return str(image_id)

    def _upload_files(self, context, instance_ref, blessed_files,
                      image_ids=None, vms_policy_template=None):
        blessed_image_refs = []
        descriptor_ref = None
        other_images = []
        for blessed_file in blessed_files:
            image_name, image_id = self._friendly_upload(context, instance_ref, blessed_file)

            if blessed_file.endswith(".gc"):
                descriptor_ref = image_id
            else:
                other_images.append((image_name, image_id))
            blessed_image_refs.append(image_id)

        properties = self.image_service.show(context, descriptor_ref)['properties']
        properties.update({'live_image': True,
                           'image_state': 'available',
                           'owner_id': instance_ref['project_id'],
                           'live_image_source': instance_ref['name'],
                           'instance_uuid': instance_ref['uuid'],
                           'instance_type_id': instance_ref['instance_type_id']})
        for image_name, image_id in other_images:
            properties['live_image_data_%s' %(image_name)] = image_id
        if vms_policy_template != None:
            properties['vms_policy_template'] = vms_policy_template
        self.image_service.update(context, descriptor_ref, {'properties': properties})
        return blessed_image_refs

    @_log_call
    def _delete_images(self, context, image_refs):
        for image_ref in image_refs:
            try:
                self.image_service.delete(context, image_ref)
            except (exception.ImageNotFound, HTTPForbidden):
                # Simply ignore this error because the end result
                # is that the image is no longer there.
                LOG.debug("The image %s was not found in the image service when removing it." % (image_ref))

    def get_hypervisor_hostname(self):
        # (dscannell): Any of the libvirt connection can be used. There is
        #              nothing special about the migration one.
        return self.libvirt_connections['migration'].get_hypervisor_hostname()


    def _clean_lvm_symlinks(self):
        # (dscannell): When launching an instance with LVM configured a symlink
        #              is created in the lvm directory (e.g. /dev/nova/) that
        #              points to the qcow2 file. This symlink does not get
        #              deleted when the instance is destroyed. This cleans up
        #              any broken symlinks.
        vg_path = os.path.join('/dev', CONF.libvirt.images_volume_group)
        LOG.debug("Starting to clean symlinks in %s" %(vg_path))
        for filename in os.listdir(vg_path):
            path = os.path.join(vg_path, filename)
            try:
                os.lstat(path)
                try:
                    os.stat(path)
                    LOG.debug("Link is still active %s" %(path))
                except:
                    # (dscannell) This is a broken link.
                    if CONF.cobalt_clean_unused_symlinks:
                        LOG.debug("Unlinking broken link %s" %(path))
                        os.unlink(path)
                    else:
                        LOG.debug("Broken link %s found but not removing "
                                  "(set cobalt_clean_unused_symlinks to "
                                  "remove)")
            except:
                LOG.debug("Failed to lstat path %s" %(path))

    def periodic_clean(self):
        """
        Performs a periodic cleanup of leftover on the system as a result
        of using cobalt / vms.
        """
        if CONF.libvirt.images_type == 'lvm':
            self._clean_lvm_symlinks()

    @_log_call
    def get_instance_info(self, instance):
        # (dscannell): Any of the libvirt connection can be used. There is
        #              nothing special about the migration one.
        virt_driver = self.libvirt_connections['migration']
        return virt_driver.get_info(instance)
