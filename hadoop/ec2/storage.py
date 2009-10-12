# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from boto.ec2.connection import EC2Connection
import logging
import os
import simplejson as json
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

def run_command_on_instance(instance, ssh_options, command):
  print "Running ssh %s root@%s '%s'" % (ssh_options, instance.public_dns_name, command)
  retcode = subprocess.call("ssh %s root@%s '%s'" %
                           (ssh_options, instance.public_dns_name, command), shell=True)
  print "Command running on %s returned with value %s" % (instance.public_dns_name, retcode)

def wait_for_volume(ec2_connection, volume_id):
  """
  Waits until a volume becomes available.
  """
  while True:
    volumes = ec2_connection.get_all_volumes([volume_id,])
    if volumes[0].status == 'available':
      break
    sys.stdout.write(".")
    sys.stdout.flush()
    time.sleep(1)

def create_formatted_snapshot(cluster, size, availability_zone, image_id, key_name, ssh_options):
  """
  Creates a formatted snapshot of a given size. This saves having to format
  volumes when they are first attached.
  """
  conn = cluster.ec2Connection
  print "Starting instance"
  reservation = conn.run_instances(image_id, key_name=key_name, placement=availability_zone)
  instance = reservation.instances[0]
  print "Started instance %s" % instance.id
  cluster.wait_for_instances(reservation)
  print
  print "Waiting 60 seconds before attaching storage"
  time.sleep(60)
  # Re-populate instance object since it has more details filled in
  instance.update()

  print "Creating volume of size %s in %s" % (size, availability_zone)
  volume = conn.create_volume(size, availability_zone)
  print "Created volume %s" % volume
  print "Attaching volume to %s" % instance.id
  volume.attach(instance.id, '/dev/sdj')

  run_command_on_instance(instance, ssh_options, """
    while true ; do
      echo 'Waiting for /dev/sdj...';
      if [ -e /dev/sdj ]; then break; fi;
      sleep 1;
    done;
    mkfs.ext3 -F -m 0.5 /dev/sdj
  """)

  print "Detaching volume"
  conn.detach_volume(volume.id, instance.id)
  print "Creating snapshot"
  snapshot = volume.create_snapshot()
  print "Creating snapshot %s" % snapshot.id
  wait_for_volume(conn, volume.id)
  print
  print "Deleting volume"
  volume.delete()
  print "Deleted volume"
  print "Stopping instance"
  terminated = conn.terminate_instances([instance.id,])
  print "Stopped instance %s" % terminated

class VolumeSpec(object):
  """
  The specification for a storage volume, encapsulating all the information needed
  to create a volume and ultimately mount it on an instance.
  """
  def __init__(self, size, mount_point, device, snapshot_id):
    self.size = size
    self.mount_point = mount_point
    self.device = device
    self.snapshot_id = snapshot_id


class JsonVolumeSpecManager(object):
  """
  A container for VolumeSpecs. This object can read VolumeSpecs specified in JSON.
  """
  def __init__(self, spec_file):
    self.spec = json.load(spec_file)

  def volume_specs_for_role(self, role):
    return [VolumeSpec(d["size_gb"], d["mount_point"], d["device"], d["snapshot_id"]) for d in self.spec[role]]

  def get_mappings_string_for_role(self, role):
    """
    Returns a short string of the form "mount_point1,device1;mount_point2,device2;..."
    which is useful for passing as an environment variable.
    """
    return ";".join(["%s,%s" % (d["mount_point"], d["device"]) for d in self.spec[role]])


class MountableVolume(object):
  """
  A storage volume that has been created. It may or may not have been attached or mounted to an
  instance.
  """
  def __init__(self, volume_id, mount_point, device):
    self.volume_id = volume_id
    self.mount_point = mount_point
    self.device = device


class JsonVolumeManager(object):

  def __init__(self, filename):
    self.filename = filename

  def _load(self):
    try:
      return json.load(open(self.filename, "r"))
    except IOError:
      logger.debug("File %s does not exist.", self.filename)
      return {}

  def _store(self, obj):
    return json.dump(obj, open(self.filename, "w"), sort_keys=True, indent=2)

  def add_instance_storage_for_role(self, role, mountable_volumes):
    json_dict = self._load()
    mv_dicts = [mv.__dict__ for mv in mountable_volumes]
    json_dict.setdefault(role, []).append(mv_dicts)
    self._store(json_dict)

  def remove_instance_storage_for_role(self, role):
    json_dict = self._load()
    del json_dict[role]
    self._store(json_dict)

  def get_instance_storage_for_role(self, role):
    """
    Returns a list of lists of MountableVolume objects. Each nested list is
    the storage for one instance.
    """
    try:
      json_dict = self._load()
      instance_storage = []
      for instance in json_dict[role]:
        vols = []
        for vol in instance:
          vols.append(MountableVolume(vol["volume_id"], vol["mount_point"], vol["device"]))
        instance_storage.append(vols)
      return instance_storage
    except KeyError:
      return []


class Storage(object):
  """
  Storage volumes for an EC2 cluster. The storage is associated with a named
  cluster. Metadata for the storage volumes is kept in a JSON file on the client
  machine (in the user's home directory in a file called
  ".hadoop-ec2/ec2-storage-<cluster-name>.json").
  """

  def __init__(self, cluster):
    self.cluster = cluster

  def _get_storage_filename(self):
    # TODO(tom): get from config, or passed in, to aid testing
    return os.path.join(os.environ['HOME'], ".hadoop-ec2/ec2-storage-%s.json" % (self.cluster.name))

  def create(self, role, number_of_instances, availability_zone, spec_filename):
    spec_file = open(spec_filename, 'r')
    volume_spec_manager = JsonVolumeSpecManager(spec_file)
    volume_manager = JsonVolumeManager(self._get_storage_filename())
    for i in range(number_of_instances):
      mountable_volumes = []
      volume_specs = volume_spec_manager.volume_specs_for_role(role)
      for spec in volume_specs:
        logger.info("Creating volume of size %s in %s from snapshot %s" % (spec.size, availability_zone, spec.snapshot_id))
        volume = self.cluster.ec2Connection.create_volume(spec.size, availability_zone, spec.snapshot_id)
        mountable_volumes.append(MountableVolume(volume.id, spec.mount_point, spec.device))
      volume_manager.add_instance_storage_for_role(role, mountable_volumes)

  def get_mountable_volumes(self, role):
    storage_filename = self._get_storage_filename()
    volume_manager = JsonVolumeManager(storage_filename)
    return volume_manager.get_instance_storage_for_role(role)

  def get_mappings_string_for_role(self, role):
    """
    Returns a short string of the form "mount_point1,device1;mount_point2,device2;..."
    which is useful for passing as an environment variable.
    """
    mappings = {}
    mountable_volumes_list = self.get_mountable_volumes(role)
    for mountable_volumes in mountable_volumes_list:
      for mountable_volume in mountable_volumes:
        mappings[mountable_volume.mount_point] = mountable_volume.device
    return ";".join(["%s,%s" % (mount_point, device) for (mount_point, device) in mappings.items()])

  def _has_storage(self, role):
    return self.get_mountable_volumes(role)

  def has_any_storage(self, roles):
    """
    Return true if any of the given roles has associated storage
    """
    for role in roles:
      if self._has_storage(role):
        return True
    return False

  def get_ec2_volumes_dict(self, mountable_volumes):
    volume_ids = [mv.volume_id for mv in sum(mountable_volumes, [])]
    volumes = self.cluster.ec2Connection.get_all_volumes(volume_ids)
    volumes_dict = {}
    for volume in volumes:
      volumes_dict[volume.id] = volume
    return volumes_dict

  def print_volume(self, role, volume):
    print "\t".join((role, volume.id, str(volume.size),
                     volume.snapshot_id, volume.availabilityZone,
                     volume.status, str(volume.create_time), str(volume.attach_time)))

  def print_status(self, roles):
    for role in roles:
      mountable_volumes_list = self.get_mountable_volumes(role)
      ec2_volumes = self.get_ec2_volumes_dict(mountable_volumes_list)
      for mountable_volumes in mountable_volumes_list:
        for mountable_volume in mountable_volumes:
          self.print_volume(role, ec2_volumes[mountable_volume.volume_id])

  def _replace(self, string, replacements):
    for (match, replacement) in replacements.iteritems():
      string = string.replace(match, replacement)
    return string

  def attach(self, role, instances):
    """
    Attach volumes for a role to instances. Some volumes may already be attached, in which
    case they are ignored, and we take care not to attach multiple volumes to an instance.
    """
    mountable_volumes_list = self.get_mountable_volumes(role)
    if not mountable_volumes_list:
      return
    ec2_volumes = self.get_ec2_volumes_dict(mountable_volumes_list)

    available_mountable_volumes_list = []

    available_instances_dict = {}
    for instance in instances:
      available_instances_dict[instance.id] = instance

    # Iterate over mountable_volumes and retain those that are not attached
    # Also maintain a list of instances that have no attached storage
    # Note that we do not fill in "holes" (instances that only have some of
    # their storage attached)
    for mountable_volumes in mountable_volumes_list:
      available = True
      for mountable_volume in mountable_volumes:
        if ec2_volumes[mountable_volume.volume_id].status != 'available':
          available = False
          instance_id = ec2_volumes[mountable_volume.volume_id].attach_data.instance_id
          if available_instances_dict.has_key(instance_id):
            del available_instances_dict[instance_id]
      if available:
        available_mountable_volumes_list.append(mountable_volumes)

    if len(available_instances_dict) != len(available_mountable_volumes_list):
      # TODO(tom): What action should we really take here?
      logger.warning("Number of available instances (%s) and volumes (%s) do not match." \
        % (len(available_instances_dict), len(available_mountable_volumes_list)))

    for (instance, mountable_volumes) in zip(available_instances_dict.values(), available_mountable_volumes_list):
      print "Attaching storage to %s" % instance.id
      for mountable_volume in mountable_volumes:
        volume = ec2_volumes[mountable_volume.volume_id]
        print "Attaching %s to %s" % (volume.id, instance.id)
        volume.attach(instance.id, mountable_volume.device)

  def delete(self, role):
    storage_filename = self._get_storage_filename()
    volume_manager = JsonVolumeManager(storage_filename)
    mountable_volumes_list = volume_manager.get_instance_storage_for_role(role)
    ec2_volumes = self.get_ec2_volumes_dict(mountable_volumes_list)
    all_available = True
    for volume in ec2_volumes.itervalues():
      if volume.status != 'available':
        all_available = False
        logger.warning("Volume %s is not available.", volume)
    if not all_available:
      logger.warning("Some volumes are still in use for role %s. Aborting delete.", role)
      return
    for volume in ec2_volumes.itervalues():
      volume.delete()
    volume_manager.remove_instance_storage_for_role(role)
