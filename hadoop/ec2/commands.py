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

"""High-level commands that a user may want to run"""

from __future__ import with_statement

from hadoop.ec2.cluster import get_clusters_with_role
from hadoop.ec2.cluster import Cluster
from hadoop.ec2.storage import Storage
from hadoop.ec2.util import build_env_string
from hadoop.ec2.util import url_get
import logging
import os
import re
import socket
import sys
import time

logger = logging.getLogger(__name__)

ENV_WHITELIST = ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY')

MASTER = "master"
SLAVE = "slave"
ROLES = (MASTER, SLAVE)

DEFAULT_USER_DATA_FILE_TEMPLATE = os.path.join(sys.path[0], 'hadoop-ec2-init-remote.sh')

def list_all():
  """
  Find and print EC2 clusters that have a running 'master' instance
  """
  clusters = get_clusters_with_role(MASTER)
  if not clusters:
    print "No running clusters"
  else:
    for cluster in clusters:
      print cluster

def list(cluster_name):
  cluster=Cluster(cluster_name)
  cluster.print_status(ROLES)

  
def launch_master(cluster, image_id, key_name, user_data_file_template=None,
    instance_type='m1.small', placement=None, user_packages=None,
    auto_shutdown=None, env_strings=[], client_cidrs=[]):
  if user_data_file_template == None:
    user_data_file_template = DEFAULT_USER_DATA_FILE_TEMPLATE
  if cluster.check_running(MASTER, 0):
    return
  ebs_mappings=''
  storage = Storage(cluster)
  if storage.has_any_storage((MASTER,)):
    ebs_mappings = storage.get_mappings_string_for_role(MASTER)
  replacements = { "%ENV%": build_env_string(ENV_WHITELIST, env_strings, {
    "USER_PACKAGES": user_packages,
    "AUTO_SHUTDOWN": auto_shutdown,
    "EBS_MAPPINGS": ebs_mappings
  }) }
  reservation = cluster.launch_instances(MASTER, 1, image_id, key_name, user_data_file_template, replacements, instance_type, placement)
  print "Waiting for master to start (%s)" % str(reservation)
  cluster.wait_for_instances(reservation)
  print
  cluster.print_status((MASTER,))
  master = cluster.check_running(MASTER, 1)[0]
  _authorize_client_ports(cluster, master, client_cidrs)
  _create_client_hadoop_site_file(cluster, master)

def _authorize_client_ports(cluster, master, client_cidrs):
  if not client_cidrs:
    logger.debug("No client CIDRs specified, using local address.")
    client_ip = url_get('http://checkip.amazonaws.com/').strip()
    client_cidrs = ("%s/32" % client_ip,)
  logger.debug("Client CIDRs: %s", client_cidrs)
  for client_cidr in client_cidrs:
    # Allow access to port 80 on master from client
    cluster.authorize_role(MASTER, 80, 80, client_cidr)
    # Allow access to jobtracker UI on master from client (so we can see when the cluster is ready)
    cluster.authorize_role(MASTER, 50030, 50030, client_cidr)
    # Allow access to namenode and jobtracker via public address from master node
  master_ip = socket.gethostbyname(master.public_dns_name)
  cluster.authorize_role(MASTER, 8020, 8021, "%s/32" % master_ip)

def _create_client_hadoop_site_file(cluster, master):
  cluster_dir = os.path.join(os.environ['HOME'], '.hadoop-ec2/%s' % cluster.name)
  aws_access_key_id = os.environ['AWS_ACCESS_KEY_ID']
  aws_secret_access_key = os.environ['AWS_SECRET_ACCESS_KEY']
  if not os.path.exists(cluster_dir):
    os.makedirs(cluster_dir)
  with open(os.path.join(cluster_dir, 'hadoop-site.xml'), 'w') as f:
    f.write("""<?xml version="1.0"?>
<?xml-stylesheet type="text/xsl" href="configuration.xsl"?>
<!-- Put site-specific property overrides in this file. -->
<configuration>
<property>
  <name>hadoop.job.ugi</name>
  <value>root,root</value>
</property>
<property>
  <name>fs.default.name</name>
  <value>hdfs://%(master)s:8020/</value>
</property>
<property>
  <name>mapred.job.tracker</name>
  <value>%(master)s:8021</value>
</property>
<property>
  <name>hadoop.socks.server</name>
  <value>localhost:6666</value>
</property>
<property>
  <name>hadoop.rpc.socket.factory.class.default</name>
  <value>org.apache.hadoop.net.SocksSocketFactory</value>
</property>
<property>
  <name>fs.s3.awsAccessKeyId</name>
  <value>%(aws_access_key_id)s</value>
</property>
<property>
  <name>fs.s3.awsSecretAccessKey</name>
  <value>%(aws_secret_access_key)s</value>
</property>
<property>
  <name>fs.s3n.awsAccessKeyId</name>
  <value>%(aws_access_key_id)s</value>
</property>
<property>
  <name>fs.s3n.awsSecretAccessKey</name>
  <value>%(aws_secret_access_key)s</value>
</property>
</configuration>
""" % {'master': master.public_dns_name,
  'aws_access_key_id': aws_access_key_id,
  'aws_secret_access_key': aws_secret_access_key})

def launch_slaves(cluster, number, user_data_file_template=None,
    user_packages=None, auto_shutdown=None, env_strings=[]):
  if user_data_file_template == None:
    user_data_file_template = DEFAULT_USER_DATA_FILE_TEMPLATE
  instances = cluster.check_running(MASTER, 1)
  if not instances:
    return
  master = instances[0]
  ebs_mappings=''
  storage = Storage(cluster)
  if storage.has_any_storage((SLAVE,)):
    ebs_mappings = storage.get_mappings_string_for_role(SLAVE)
  replacements = { "%ENV%": build_env_string(ENV_WHITELIST, env_strings, {
    "USER_PACKAGES": user_packages,
    "AUTO_SHUTDOWN": auto_shutdown,
    "EBS_MAPPINGS": ebs_mappings,
    "MASTER_HOST": master.public_dns_name
  }) }
  reservation = cluster.launch_instances(SLAVE, number, master.image_id, master.key_name, user_data_file_template,
    replacements, master.instance_type, master.placement)
  print "Waiting for slaves to start"
  cluster.wait_for_instances(reservation)
  print
  cluster.print_status((SLAVE,))

def wait_for_hadoop(cluster, number):
  instances = cluster.check_running(MASTER, 1)
  if not instances:
    return
  master = instances[0]
  print "Waiting for jobtracker to start"
  previous_running = 0
  # TODO: timeout
  while True:
    try:
      actual_running = _number_of_tasktrackers(master.public_dns_name, 1)
      break
    except IOError:
      pass
    sys.stdout.write(".")
    sys.stdout.flush()
    time.sleep(1)
  print
  if number > 0:
    print "Waiting for %d tasktrackers to start" % number
    while actual_running < number:
      try:
        actual_running = _number_of_tasktrackers(master.public_dns_name, 5, 2)
        if actual_running != previous_running:
          sys.stdout.write("%d" % actual_running)
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(1)
        previous_running = actual_running
      except IOError:
        print
        print "Timeout waiting for jobtracker."
        return
    print

# The optional ?type=active is a difference between Hadoop 0.18 and 0.20
NUMBER_OF_TASK_TRACKERS=re.compile(
  r'<a href="machines.jsp(?:\?type=active)?">(\d+)</a>')

def _number_of_tasktrackers(jt_hostname, timeout, retries=0):
  jt_page = url_get("http://%s:50030/jobtracker.jsp" % jt_hostname, timeout, retries)
  m = NUMBER_OF_TASK_TRACKERS.search(jt_page)
  if m:
    return int(m.group(1))
  return 0

def print_master_url(cluster):
  instances = cluster.check_running(MASTER, 1)
  if not instances:
    return
  master = instances[0]
  print "Browse the cluster at http://%s/" % master.public_dns_name

def attach_storage(cluster, roles):
  storage = Storage(cluster)
  if storage.has_any_storage(roles):
    print "Waiting 10 seconds before attaching storage"
    time.sleep(10)
    for role in roles:
      storage.attach(role, cluster.get_instances_in_role(role, 'running'))
    storage.print_status(roles)
