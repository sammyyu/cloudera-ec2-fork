#!/bin/bash -x
# 
# Modified version of hadoop-ec2-init-remote.sh, customized to install
# Cloudera Desktop.
#
#
################################################################################
# Script that is run on each EC2 instance on boot. It is passed in the EC2 user
# data, so should not exceed 16K in size after gzip compression.
#
# This script is executed by /etc/init.d/ec2-run-user-data, and output is
# logged to /var/log/messages.
################################################################################

################################################################################
# Initialize variables
################################################################################

# Substitute environment variables passed by the client
export %ENV%

if [ -z "$MASTER_HOST" ]; then
  IS_MASTER=true
  MASTER_HOST=`wget -q -O - http://169.254.169.254/latest/meta-data/public-hostname`
else
  IS_MASTER=false
fi

# Force versions
REPO="testing"
HADOOP="hadoop-0.20"

function register_auto_shutdown() {
  if [ ! -z "$AUTO_SHUTDOWN" ]; then
    shutdown -h +$AUTO_SHUTDOWN >/dev/null &
  fi
}

function update_repo() {
  if which dpkg &> /dev/null; then
    cat > /etc/apt/sources.list.d/cloudera.list <<EOF
deb http://archive.cloudera.com/debian intrepid-$REPO contrib
deb-src http://archive.cloudera.com/debian intrepid-$REPO contrib
EOF
    sudo apt-get update
  elif which rpm &> /dev/null; then
    rm -f /etc/yum.repos.d/cloudera.repo
    cat > /etc/yum.repos.d/cloudera-$REPO.repo <<EOF
[cloudera-$REPO]
name=Cloudera's Distribution for Hadoop ($REPO)
baseurl=http://archive.cloudera.com/redhat/cdh/$REPO/
gpgkey = http://archive.cloudera.com/redhat/cdh/RPM-GPG-KEY-cloudera
gpgcheck = 0
EOF
    yum update -y yum
  fi
}

# Install a list of packages on debian or redhat as appropriate
function install_packages() {
  if which dpkg &> /dev/null; then
    apt-get update
    apt-get -y install $@
  elif which rpm &> /dev/null; then
    yum install -y $@
  else
    echo "No package manager found."
  fi
}

# Install any user packages specified in the USER_PACKAGES environment variable
function install_user_packages() {
  if [ ! -z "$USER_PACKAGES" ]; then
    install_packages $USER_PACKAGES
  fi
}

# Install Hadoop packages and dependencies
function install_hadoop() {
  if which dpkg &> /dev/null; then
    apt-get update
    apt-get -y install $HADOOP
    cp -r /etc/$HADOOP/conf.empty /etc/$HADOOP/conf.dist
    update-alternatives --install /etc/$HADOOP/conf $HADOOP-conf /etc/$HADOOP/conf.dist 90
    apt-get -y install pig${PIG_VERSION:+-${PIG_VERSION}}
    apt-get -y install hive${HIVE_VERSION:+-${HIVE_VERSION}}
    apt-get -y install policykit # http://www.bergek.com/2008/11/24/ubuntu-810-libpolkit-error/
  elif which rpm &> /dev/null; then
    yum install -y $HADOOP
    cp -r /etc/$HADOOP/conf.empty /etc/$HADOOP/conf.dist
    if [ ! -e /etc/alternatives/$HADOOP-conf ]; then # CDH1 RPMs use a different alternatives name
      conf_alternatives_name=hadoop
    else
      conf_alternatives_name=$HADOOP-conf
    fi
    alternatives --install /etc/$HADOOP/conf $conf_alternatives_name /etc/$HADOOP/conf.dist 90
    yum install -y hadoop-pig${PIG_VERSION:+-${PIG_VERSION}}
    yum install -y hadoop-hive${HIVE_VERSION:+-${HIVE_VERSION}}
  fi
}

function prep_disk() {
  mount=$1
  device=$2
  automount=${3:-false}

  echo "warning: ERASING CONTENTS OF $device"
  mkfs.xfs -f $device
  if [ ! -e $mount ]; then
    mkdir $mount
  fi
  mount -o defaults,noatime $device $mount
  if $automount ; then
    echo "$device $mount xfs defaults,noatime 0 0" >> /etc/fstab
  fi
}

function wait_for_mount {
  mount=$1
  device=$2

  mkdir $mount

  i=1
  echo "Attempting to mount $device"
  while true ; do
    sleep 10
    echo -n "$i "
    i=$[$i+1]
    mount -o defaults,noatime $device $mount || continue
    echo " Mounted."
    break;
  done
}

function make_hadoop_dirs {
  for mount in "$@"; do
    if [ ! -e $mount/hadoop ]; then
      mkdir -p $mount/hadoop
      chown hadoop:hadoop $mount/hadoop
    fi
  done
}

# Configure Hadoop by setting up disks and site file
function configure_hadoop() {

  INSTANCE_TYPE=`wget -q -O - http://169.254.169.254/latest/meta-data/instance-type`
  install_packages xfsprogs # needed for XFS
  # Mount home volume, if any, and strip it from the EBS_MAPPINGS
  mount_home_volume
  if [ -n "$EBS_MAPPINGS" ]; then
    # If there are EBS volumes, use them for persistent HDFS
    scaffold_ebs_hdfs
  else
    # Otherwise, make a blank HDFS on the local drives
    scaffold_local_hdfs
  fi
  # Set up all the instance-local directories
  scaffold_hadoop_dirs
  # Populate the various config files
  create_hadoop_conf
}

# Look for a mount that must be named "/mnt/home" (defined in
# ec2-storage-YOURCLUSTER.json).
function mount_home_volume {
  if [[ $EBS_MAPPINGS =~ '/mnt/home,' ]] ; then
    # Extract and strip the mapping from the EBS_MAPPINGS
    mapping=`echo $EBS_MAPPINGS | sed 's|.*\(/mnt/home,[^;]*\);*.*|\1|'`
    EBS_MAPPINGS=`echo $EBS_MAPPINGS | sed 's|/mnt/home,[^;]*;*||'`
    echo "Mounting $mapping but not using it for HDFS"
    mount=${mapping%,*}
    device=${mapping#*,}
    wait_for_mount $mount $device
  fi
}

function scaffold_ebs_hdfs {
    # EBS_MAPPINGS is like "/ebs1,/dev/sdj;/ebs2,/dev/sdk"
    DFS_NAME_DIR=''
    FS_CHECKPOINT_DIR=''
    DFS_DATA_DIR=''
    for mapping in $(echo "$EBS_MAPPINGS" | tr ";" "\n"); do
      # Split on the comma (see "Parameter Expansion" in the bash man page)
      mount=${mapping%,*}
      device=${mapping#*,}
      wait_for_mount $mount $device
      DFS_NAME_DIR=${DFS_NAME_DIR},"$mount/hadoop/hdfs/name"
      FS_CHECKPOINT_DIR=${FS_CHECKPOINT_DIR},"$mount/hadoop/hdfs/secondary"
      DFS_DATA_DIR=${DFS_DATA_DIR},"$mount/hadoop/hdfs/data"
      FIRST_MOUNT=${FIRST_MOUNT-$mount}
      make_hadoop_dirs $mount
    done
    # Remove leading commas
    DFS_NAME_DIR=${DFS_NAME_DIR#?}
    FS_CHECKPOINT_DIR=${FS_CHECKPOINT_DIR#?}
    DFS_DATA_DIR=${DFS_DATA_DIR#?}

    DFS_REPLICATION=3 # EBS is internally replicated, but we also use HDFS replication for safety
}

function scaffold_local_hdfs {
    case $INSTANCE_TYPE in
    m1.xlarge|c1.xlarge)
      DFS_NAME_DIR=/mnt/hadoop/hdfs/name,/mnt2/hadoop/hdfs/name
      FS_CHECKPOINT_DIR=/mnt/hadoop/hdfs/secondary,/mnt2/hadoop/hdfs/secondary
      DFS_DATA_DIR=/mnt/hadoop/hdfs/data,/mnt2/hadoop/hdfs/data,/mnt3/hadoop/hdfs/data,/mnt4/hadoop/hdfs/data
      ;;
    m1.large)
      DFS_NAME_DIR=/mnt/hadoop/hdfs/name,/mnt2/hadoop/hdfs/name
      FS_CHECKPOINT_DIR=/mnt/hadoop/hdfs/secondary,/mnt2/hadoop/hdfs/secondary
      DFS_DATA_DIR=/mnt/hadoop/hdfs/data,/mnt2/hadoop/hdfs/data
      ;;
    *)
      # "m1.small" or "c1.medium"
      DFS_NAME_DIR=/mnt/hadoop/hdfs/name
      FS_CHECKPOINT_DIR=/mnt/hadoop/hdfs/secondary
      DFS_DATA_DIR=/mnt/hadoop/hdfs/data
      ;;
    esac
    FIRST_MOUNT=/mnt
    DFS_REPLICATION=3
}

# Common directories, whether the HDFS is instance-local or EBS
function scaffold_hadoop_dirs {
  case $INSTANCE_TYPE in
  m1.xlarge|c1.xlarge)
    prep_disk /mnt2 /dev/sdc true &
    disk2_pid=$!
    prep_disk /mnt3 /dev/sdd true &
    disk3_pid=$!
    prep_disk /mnt4 /dev/sde true &
    disk4_pid=$!
    wait $disk2_pid $disk3_pid $disk4_pid
    MAPRED_LOCAL_DIR=/mnt/hadoop/mapred/local,/mnt2/hadoop/mapred/local,/mnt3/hadoop/mapred/local,/mnt4/hadoop/mapred/local
    MAX_MAP_TASKS=8
    MAX_REDUCE_TASKS=4
    CHILD_OPTS=-Xmx680m
    CHILD_ULIMIT=1392640
    ;;
  m1.large)
    prep_disk /mnt2 /dev/sdc true
    MAPRED_LOCAL_DIR=/mnt/hadoop/mapred/local,/mnt2/hadoop/mapred/local
    MAX_MAP_TASKS=4
    MAX_REDUCE_TASKS=2
    CHILD_OPTS=-Xmx1024m
    CHILD_ULIMIT=2097152
    ;;
  c1.medium)
    MAPRED_LOCAL_DIR=/mnt/hadoop/mapred/local
    MAX_MAP_TASKS=4
    MAX_REDUCE_TASKS=2
    CHILD_OPTS=-Xmx550m
    CHILD_ULIMIT=1126400
    ;;
  *)
    # "m1.small"
    MAPRED_LOCAL_DIR=/mnt/hadoop/mapred/local
    MAX_MAP_TASKS=2
    MAX_REDUCE_TASKS=1
    CHILD_OPTS=-Xmx550m
    CHILD_ULIMIT=1126400
    ;;
  esac

  make_hadoop_dirs `ls -d /mnt*`

  # Create tmp directory
  mkdir /mnt/tmp
  chmod a+rwxt /mnt/tmp

}

function create_hadoop_conf {
  ##############################################################################
  # Modify this section to customize your Hadoop cluster.
  ##############################################################################
  cat > /etc/$HADOOP/conf.dist/hdfs-site.xml <<EOF
<?xml version="1.0"?>
<?xml-stylesheet type="text/xsl" href="configuration.xsl"?>
<configuration>
<property>
  <name>dfs.block.size</name>
  <value>134217728</value>
  <final>true</final>
</property>
<property>
  <name>dfs.data.dir</name>
  <value>$DFS_DATA_DIR</value>
  <final>true</final>
</property>
<property>
  <name>dfs.datanode.du.reserved</name>
  <value>1073741824</value>
  <final>true</final>
</property>
<property>
  <name>dfs.datanode.handler.count</name>
  <value>3</value>
  <final>true</final>
</property>
<!--property>
  <name>dfs.hosts</name>
  <value>/etc/$HADOOP/conf.dist/dfs.hosts</value>
  <final>true</final>
</property-->
<!--property>
  <name>dfs.hosts.exclude</name>
  <value>/etc/$HADOOP/conf.dist/dfs.hosts.exclude</value>
  <final>true</final>
</property-->
<property>
  <name>dfs.name.dir</name>
  <value>$DFS_NAME_DIR</value>
  <final>true</final>
</property>
<property>
  <name>dfs.namenode.handler.count</name>
  <value>5</value>
  <final>true</final>
</property>
<property>
  <name>dfs.permissions</name>
  <value>true</value>
  <final>true</final>
</property>
<property>
  <name>dfs.replication</name>
  <value>$DFS_REPLICATION</value>
</property>
<!-- Start Cloudera Desktop -->
<property>
  <name>dfs.namenode.plugins</name>
  <value>org.apache.hadoop.thriftfs.NamenodePlugin</value>
  <description>Comma-separated list of namenode plug-ins to be activated.
  </description>
</property>
<property>
  <name>dfs.datanode.plugins</name>
  <value>org.apache.hadoop.thriftfs.DatanodePlugin</value>
  <description>Comma-separated list of datanode plug-ins to be activated.
  </description>
</property>
<!-- End Cloudera Desktop -->
</configuration>
EOF

  cat > /etc/$HADOOP/conf.dist/core-site.xml <<EOF
<?xml version="1.0"?>
<?xml-stylesheet type="text/xsl" href="configuration.xsl"?>
<configuration>
<property>
  <name>fs.checkpoint.dir</name>
  <value>$FS_CHECKPOINT_DIR</value>
  <final>true</final>
</property>
<property>
  <name>fs.default.name</name>
  <value>hdfs://$MASTER_HOST:8020/</value>
</property>
<property>
  <name>fs.trash.interval</name>
  <value>1440</value>
  <final>true</final>
</property>
<property>
  <name>hadoop.tmp.dir</name>
  <value>/mnt/tmp/hadoop-\${user.name}</value>
  <final>true</final>
</property>
<property>
  <name>io.file.buffer.size</name>
  <value>65536</value>
</property>
<property>
  <name>hadoop.rpc.socket.factory.class.default</name>
  <value>org.apache.hadoop.net.StandardSocketFactory</value>
  <final>true</final>
</property>
<property>
  <name>hadoop.rpc.socket.factory.class.ClientProtocol</name>
  <value></value>
  <final>true</final>
</property>
<property>
  <name>hadoop.rpc.socket.factory.class.JobSubmissionProtocol</name>
  <value></value>
  <final>true</final>
</property>
<property>
  <name>io.compression.codecs</name>
  <value>org.apache.hadoop.io.compress.DefaultCodec,org.apache.hadoop.io.compress.GzipCodec</value>
</property>
<property>
  <name>fs.s3.awsAccessKeyId</name>
  <value>$AWS_ACCESS_KEY_ID</value>
</property>
<property>
  <name>fs.s3.awsSecretAccessKey</name>
  <value>$AWS_SECRET_ACCESS_KEY</value>
</property>
<property>
  <name>fs.s3n.awsAccessKeyId</name>
  <value>$AWS_ACCESS_KEY_ID</value>
</property>
<property>
  <name>fs.s3n.awsSecretAccessKey</name>
  <value>$AWS_SECRET_ACCESS_KEY</value>
</property>
</configuration>
EOF

  cat > /etc/$HADOOP/conf.dist/mapred-site.xml <<EOF
<?xml version="1.0"?>
<?xml-stylesheet type="text/xsl" href="configuration.xsl"?>
<configuration>
<property>
  <name>mapred.child.java.opts</name>
  <value>$CHILD_OPTS</value>
</property>
<property>
  <name>mapred.child.ulimit</name>
  <value>$CHILD_ULIMIT</value>
  <final>true</final>
</property>
<property>
  <name>mapred.job.tracker</name>
  <value>$MASTER_HOST:8021</value>
</property>
<property>
  <name>mapred.job.tracker.handler.count</name>
  <value>5</value>
  <final>true</final>
</property>
<property>
  <name>mapred.local.dir</name>
  <value>$MAPRED_LOCAL_DIR</value>
  <final>true</final>
</property>
<property>
  <name>mapred.map.tasks.speculative.execution</name>
  <value>true</value>
</property>
<property>
  <name>mapred.reduce.parallel.copies</name>
  <value>10</value>
</property>
<property>
  <name>mapred.reduce.tasks</name>
  <value>10</value>
</property>
<property>
  <name>mapred.reduce.tasks.speculative.execution</name>
  <value>false</value>
</property>
<property>
  <name>mapred.submit.replication</name>
  <value>10</value>
</property>
<property>
  <name>mapred.system.dir</name>
  <value>/hadoop/system/mapred</value>
</property>
<property>
  <name>mapred.tasktracker.map.tasks.maximum</name>
  <value>$MAX_MAP_TASKS</value>
  <final>true</final>
</property>
<property>
  <name>mapred.tasktracker.reduce.tasks.maximum</name>
  <value>$MAX_REDUCE_TASKS</value>
  <final>true</final>
</property>
<property>
  <name>tasktracker.http.threads</name>
  <value>46</value>
  <final>true</final>
</property>
<property>
  <name>mapred.jobtracker.taskScheduler</name>
  <value>org.apache.hadoop.mapred.FairScheduler</value>
</property>
<property>
  <name>mapred.fairscheduler.allocation.file</name>
  <value>/etc/$HADOOP/conf.dist/fairscheduler.xml</value>
</property>
<property>
  <name>mapred.compress.map.output</name>
  <value>true</value>
</property>
<property>
  <name>mapred.output.compression.type</name>
  <value>BLOCK</value>
</property>
<!-- Start Cloudera Desktop -->
<property>
  <name>mapred.jobtracker.plugins</name>
  <value>org.apache.hadoop.thriftfs.ThriftJobTrackerPlugin</value>
  <description>Comma-separated list of jobtracker plug-ins to be activated.
  </description>
</property>
<!-- End Cloudera Desktop -->
</configuration>
EOF

  cat > /etc/$HADOOP/conf.dist/fairscheduler.xml <<EOF
<?xml version="1.0"?>
<allocations>
</allocations>
EOF

  cat > /etc/$HADOOP/conf.dist/hadoop-metrics.properties <<EOF
# Exposes /metrics URL endpoint for metrics information.
dfs.class=org.apache.hadoop.metrics.spi.NoEmitMetricsContext
mapred.class=org.apache.hadoop.metrics.spi.NoEmitMetricsContext
jvm.class=org.apache.hadoop.metrics.spi.NoEmitMetricsContext
rpc.class=org.apache.hadoop.metrics.spi.NoEmitMetricsContext
EOF

  # Keep PID files in a non-temporary directory
  sed -i -e "s|# export HADOOP_PID_DIR=.*|export HADOOP_PID_DIR=/var/run/hadoop|" \
    /etc/$HADOOP/conf.dist/hadoop-env.sh
  mkdir -p /var/run/hadoop
  ln -nfsT /var/run/hadoop /var/run/hadoop-0.20
  chown -R hadoop:hadoop /var/run/hadoop

  # Set SSH options within the cluster
  sed -i -e 's|# export HADOOP_SSH_OPTS=.*|export HADOOP_SSH_OPTS="-o StrictHostKeyChecking=no"|' \
    /etc/$HADOOP/conf.dist/hadoop-env.sh

  # Hadoop logs should be on the /mnt partition
  rm -rf /var/log/hadoop
  mkdir /mnt/hadoop/logs
  chown hadoop:hadoop /mnt/hadoop/logs
  ln -nfsT /mnt/hadoop/logs /var/log/hadoop
  ln -nfsT /mnt/hadoop/logs /var/log/hadoop-0.20
  chown -R hadoop:hadoop /var/log/hadoop
}

# Sets up small website on cluster.
# TODO(philip): Add links/documentation.
function setup_web() {

  if which dpkg &> /dev/null; then
    apt-get -y install thttpd
    WWW_BASE=/var/www
  elif which rpm &> /dev/null; then
    yum install -y thttpd
    chkconfig --add thttpd
    WWW_BASE=/var/www/thttpd/html
  fi

  cat > $WWW_BASE/index.html << END
<html>
<head>
<title>Hadoop EC2 Cluster</title>
</head>
<body>
<h1>Hadoop EC2 Cluster</h1>
To browse the cluster you need to have a proxy configured.
Start the proxy with <tt>hadoop-ec2 proxy &lt;cluster_name&gt;</tt>,
and point your browser to
<a href="http://cloudera-public.s3.amazonaws.com/ec2/proxy.pac">this Proxy
Auto-Configuration (PAC)</a> file.  To manage multiple proxy configurations,
you may wish to use
<a href="https://addons.mozilla.org/en-US/firefox/addon/2464">FoxyProxy</a>.
<ul>
<li><a href="http://$MASTER_HOST:50070/">NameNode</a>
<li><a href="http://$MASTER_HOST:50030/">JobTracker</a>
<li><a href="http://$MASTER_HOST:8088/">Cloudera Desktop</a>
</ul>
</body>
</html>
END

  service thttpd start

}

function start_hadoop_master() {

  if which dpkg &> /dev/null; then
    AS_HADOOP="su -s /bin/bash - hadoop -c"
    # Format HDFS
    [ ! -e $FIRST_MOUNT/hadoop/hdfs ] && $AS_HADOOP "$HADOOP namenode -format"
    apt-get -y install $HADOOP-namenode
    apt-get -y install $HADOOP-secondarynamenode
    apt-get -y install $HADOOP-jobtracker
  elif which rpm &> /dev/null; then
    AS_HADOOP="/sbin/runuser -s /bin/bash - hadoop -c"
    # Format HDFS
    [ ! -e $FIRST_MOUNT/hadoop/hdfs ] && $AS_HADOOP "$HADOOP namenode -format"
    chkconfig --add $HADOOP-namenode
    chkconfig --add $HADOOP-secondarynamenode
    chkconfig --add $HADOOP-jobtracker
  fi

  # Note: use 'service' and not the start-all.sh etc scripts
  service $HADOOP-namenode start
  service $HADOOP-secondarynamenode start
  service $HADOOP-jobtracker start

  $AS_HADOOP "$HADOOP dfsadmin -safemode wait"
  $AS_HADOOP "/usr/bin/$HADOOP fs -mkdir /user"
  # The following is questionable, as it allows a user to delete another user
  # It's needed to allow users to create their own user directories
  $AS_HADOOP "/usr/bin/$HADOOP fs -chmod +w /user"

  # Create temporary directory for Pig and Hive in HDFS
  $AS_HADOOP "/usr/bin/$HADOOP fs -mkdir /tmp"
  $AS_HADOOP "/usr/bin/$HADOOP fs -chmod +w /tmp"
  $AS_HADOOP "/usr/bin/$HADOOP fs -mkdir /user/hive/warehouse"
  $AS_HADOOP "/usr/bin/$HADOOP fs -chmod +w /user/hive/warehouse"
}

function start_hadoop_slave() {

  if which dpkg &> /dev/null; then
    apt-get -y install $HADOOP-datanode
    apt-get -y install $HADOOP-tasktracker
  elif which rpm &> /dev/null; then
    yum install -y $HADOOP-datanode
    yum install -y $HADOOP-tasktracker
    chkconfig --add $HADOOP-datanode
    chkconfig --add $HADOOP-tasktracker
  fi

  service $HADOOP-datanode start
  service $HADOOP-tasktracker start
}

function install_cloudera_desktop {
  if which dpkg &> /dev/null; then
    if $IS_MASTER; then
      apt-get -y install libxslt1.1 cloudera-desktop cloudera-desktop-plugins
      dpkg -i /tmp/cloudera-desktop.deb /tmp/cloudera-desktop-plugins.deb
    else
      apt-get -y install cloudera-desktop-plugins
      dpkg -i /tmp/cloudera-desktop-plugins.deb
    fi
  elif which rpm &> /dev/null; then
    if $IS_MASTER; then
      yum install -y python-devel cloudera-desktop cloudera-desktop-plugins
    else
      yum install -y cloudera-desktop-plugins
    fi
  fi
}

function configure_cloudera_desktop {
  if $IS_MASTER; then
    mv /usr/share/cloudera-desktop/conf/cloudera-desktop.ini /usr/share/cloudera-desktop/conf/cloudera-desktop.ini.orig
    cat > /usr/share/cloudera-desktop/conf/cloudera-desktop.ini <<EOF
[hadoop]
[[hdfs_clusters]]
[[[default]]]
namenode_host=$MASTER_HOST
[[mapred_clusters]]
[[[default]]]
jobtracker_host=$MASTER_HOST
EOF
  fi      
}

function start_cloudera_desktop {
  /etc/init.d/cloudera-desktop start
}

function install_nfs {
  if which dpkg &> /dev/null; then
    if $IS_MASTER; then
      apt-get -y install nfs-kernel-server
    fi
    apt-get -y install nfs-common
  elif which rpm &> /dev/null; then
    echo "!!!! Don't know how to install nfs on RPM yet !!!!"
    # if $IS_MASTER; then
    #   yum install -y
    # fi
    # yum install nfs-utils nfs-utils-lib portmap system-config-nfs
  fi
}

# Sets up an NFS-shared home directory.
#
# The actual files live in /mnt/home on master.  You probably want /mnt/home to
# live on an EBS volume, with a line in ec2-storage-YOURCLUSTER.json like
#  "master": [ [
#    { "device": "/dev/sdh", "mount_point": "/mnt/home",  "volume_id": "vol-01234567" }
#    ....
# On slaves, home drives are NFS-mounted from master to /mnt/home
function configure_nfs {
  if $IS_MASTER; then
    grep -q '/mnt/home' /etc/exports || ( echo "/mnt/home  *.internal(rw,no_root_squash,no_subtree_check)" >> /etc/exports )
  else
    # slaves get /mnt/home and /usr/global from master
    grep -q '/mnt/home' /etc/fstab || ( echo "$MASTER_HOST:/mnt/home  /mnt/home    nfs  rw  0 0"  >> /etc/fstab )
  fi
  rmdir    /home 2>/dev/null
  mkdir -p /var/lib/nfs/rpc_pipefs
  mkdir -p /mnt/home
  ln -nfsT /mnt/home /home
}

function start_nfs {
  if $IS_MASTER; then
    /etc/init.d/nfs-kernel-server restart
    /etc/init.d/nfs-common restart
  else
    /etc/init.d/nfs-common restart
    mount /mnt/home
  fi
}

# Follow along with tail -f /var/log/user.log
function configure_devtools {
  apt-get -y update  ;
  apt-get -y upgrade ;
  #
  apt-get -y install git-core cvs subversion exuberant-ctags tree zip openssl ;
  apt-get -y install libpcre3-dev libbz2-dev libonig-dev libidn11-dev libxml2-dev libxslt1-dev libevent-dev;
  apt-get -y install emacs emacs-goodies-el emacsen-common ;
  apt-get -y install ruby rubygems ruby1.8-dev ruby-elisp irb ri rdoc python-setuptools python-dev;
  apt-get -y install libtokyocabinet-dev tokyocabinet-bin ;
  # Python
  easy_install simplejson boto ctypedbytes dumbo
  # Un-screwup Ruby Gems
  gem install --no-rdoc --no-ri rubygems-update --version=1.3.1 ; /var/lib/gems/1.8/bin/update_rubygems; gem update --no-rdoc --no-ri --system ; gem --version ;
  GEM_COMMAND="gem install --no-rdoc --no-ri --source=http://gemcutter.org"
  # Ruby gems: Basic utility and file format gems
  $GEM_COMMAND extlib oniguruma fastercsv json libxml-ruby htmlentities addressable uuidtools
  # Ruby gems: Wukong and friends
  $GEM_COMMAND wukong monkeyshines edamame wuclan
  #
}

#
# This is made of kludge.  Among other things, you have to create the users in
# the right order -- and ensure none have been made before -- or your uid's
# won't match the ones on the EBS volume.
#
# This also creates and sets permissions on the HDFS home directories, which
# might be best left off. (It depends on the HDFS coming up in time
#
function make_user_accounts {
  for newuser in $USER_ACCOUNTS ; do
    adduser $newuser --disabled-password --gecos "";
    sudo -u hadoop hadoop dfs -mkdir          /user/$newuser
    sudo -u hadoop hadoop dfs -chown $newuser /user/$newuser
  done
}

function cleanup {
  apt-get -y autoremove
  apt-get -y clean
  updatedb
}

install_nfs
configure_nfs
register_auto_shutdown
update_repo
install_user_packages
install_hadoop
install_cloudera_desktop
configure_hadoop
configure_cloudera_desktop
start_nfs
configure_devtools

if $IS_MASTER ; then
  setup_web
  start_hadoop_master
  start_cloudera_desktop
else
  start_hadoop_slave
fi
make_user_accounts
cleanup
