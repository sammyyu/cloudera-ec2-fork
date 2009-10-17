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
from boto.exception import EC2ResponseError
import logging
from hadoop.ec2.userdata import InstanceUserData
from hadoop.ec2.util import xstr
from subprocess import call;
import re;
import sys
import time

logger = logging.getLogger(__name__)

def get_clusters_with_role(role, state="running"):
  all = EC2Connection().get_all_instances()
  clusters = []
  for res in all:
    instance = res.instances[0];
    for group in res.groups:
      if group.id.endswith("-" + role) and instance.state == state:
        clusters.append(re.sub("-%s$" % re.escape(role), "", group.id))
  return clusters

class Cluster(object):
  """
  A cluster of EC2 instances. A cluster has a unique name.

  Instances running in the cluster run in a security group with the cluster's
  name, and also a name indicating the instance's role, e.g. <cluster-name>-foo
  to show a "foo" instance.
  """

  def __init__(self, name):
    self.name = name
    self.ec2Connection = EC2Connection()

  def get_ec2_connection(self):
    self.ec2Connection

  def get_cluster_group_name(self):
    return self.name

  def group_name_for_role(self, role):
    """
    Return the security group name for a instance in a given role.
    """
    return "%s-%s" % (self.name, role)

  def get_group_names(self, role):
    return [self.get_cluster_group_name(), self.group_name_for_role(role)]

  def _get_all_group_names(self):
    security_groups = self.ec2Connection.get_all_security_groups()
    security_group_names = [security_group.name for security_group in security_groups]
    return security_group_names

  def create_groups(self, role):
    """
    Create the security groups for a given role, including a group for the cluster
    if it doesn't exist.
    """
    security_group_names = self._get_all_group_names()

    cluster_group_name = self.get_cluster_group_name()
    if not cluster_group_name in security_group_names:
      self.ec2Connection.create_security_group(cluster_group_name, "Hadoop cluster (%s)" % (self.name))
      self.ec2Connection.authorize_security_group(cluster_group_name, cluster_group_name)
      # Allow SSH from anywhere
      self.ec2Connection.authorize_security_group(cluster_group_name, ip_protocol="tcp", from_port=22, to_port=22, cidr_ip="0.0.0.0/0")

    role_group_name = self.group_name_for_role(role)
    if not role_group_name in security_group_names:
      self.ec2Connection.create_security_group(role_group_name, "Hadoop %s (%s)" % (role, self.name))

  def authorize_role(self, role, from_port, to_port, cidr_ip):
    """
    Authorize access to machines in a given role from a given network.
    """
    role_group_name = self.group_name_for_role(role)
    # Revoke first to avoid InvalidPermission.Duplicate error
    self.ec2Connection.revoke_security_group(role_group_name, ip_protocol="tcp", from_port=from_port, to_port=to_port, cidr_ip=cidr_ip)
    self.ec2Connection.authorize_security_group(role_group_name, ip_protocol="tcp", from_port=from_port, to_port=to_port, cidr_ip=cidr_ip)

  def delete_groups(self, roles):
    """
    Delete the security groups for a given role, including the group for the cluster.
    """
    security_group_names = self._get_all_group_names()

    for role in roles:
      role_group_name = self.group_name_for_role(role)
      if role_group_name in security_group_names:
        self.ec2Connection.delete_security_group(role_group_name)
    cluster_group_name = self.get_cluster_group_name()
    if cluster_group_name in security_group_names:
      self.ec2Connection.delete_security_group(cluster_group_name)

  def get_instances(self, group_name, state_filter=None):
    """
    Get all the instances in a group, filtered by state.

    @param group_name: the name of the group
    @param state_filter: the state that the instance should be in (e.g. "running"),
                         or None for all states
    """
    all = self.ec2Connection.get_all_instances()
    instances = []
    for res in all:
      for group in res.groups:
        if group.id == group_name:
          for instance in res.instances:
            if state_filter == None or instance.state == state_filter:
              instances.append(instance)
    return instances

  def get_instances_in_role(self, role, state_filter=None):
    """
    Get all the instances in a role, filtered by state.

    @param role: the name of the role
    @param state_filter: the state that the instance should be in (e.g. "running"),
                         or None for all states
    """
    return self.get_instances(self.group_name_for_role(role), state_filter)

  def print_instance(self, role, instance):
    print "\t".join((role, instance.id,
      instance.image_id,
      "%-40s" % instance.dns_name,
      "%-24s" % instance.private_dns_name,
      instance.state, xstr(instance.key_name), instance.instance_type,
      str(instance.launch_time), instance.placement))

  def print_status(self, roles, state_filter="running"):
    for role in roles:
      for instance in self.get_instances_in_role(role, state_filter):
        self.print_instance(role, instance)

  def check_running(self, role, number):
    instances = self.get_instances_in_role(role, "running")
    if len(instances) != number:
      print "Expected %s instances in role %s, but was %s %s" % (number, role, len(instances), instances)
      return False
    else:
      return instances

  def launch_instances(self, role, number, image_id, key_name, user_data_file_template, replacements, instance_type='m1.small',
      placement=None):

    self.create_groups(role)
    user_data = InstanceUserData(user_data_file_template, replacements).read_as_gzip_stream()

    reservation = self.ec2Connection.run_instances(image_id, min_count=number, max_count=number, key_name=key_name,
      security_groups=self.get_group_names(role), user_data=user_data, instance_type=instance_type,
      placement=placement);
    return reservation

  def wait_for_instances(self, reservation):
    instances = [instance.id for instance in reservation.instances]
    # TODO(tom): should timeout
    while True:
      if self.all_started(self.ec2Connection.get_all_instances(instances)):
        break
      sys.stdout.write(".")
      sys.stdout.flush()
      time.sleep(1)

  def all_started(self, reservations):
    for res in reservations:
      for instance in res.instances:
        if instance.state != "running":
          return False
    return True

  def terminate(self):
    instances = self.get_instances(self.get_cluster_group_name(), "running")
    if instances:
      self.ec2Connection.terminate_instances([i.id for i in instances])
