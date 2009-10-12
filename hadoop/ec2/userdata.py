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

from __future__ import with_statement

import gzip
import StringIO

class InstanceUserData(object):
  """
  The data passed to an EC2 instance on start up.
  """

  def __init__(self, filename, replacements={}):
    self.filename = filename
    self.replacements = replacements

  def read_file(self, filename):
    with open(filename, 'r') as f:
      return f.read()

  def read(self):
    contents = self.read_file(self.filename)
    for (match, replacement) in self.replacements.iteritems():
      if replacement == None:
        replacement = ''
      contents = contents.replace(match, replacement)
    return contents

  def read_as_gzip_stream(self):
    """
    Read and compress the data.
    """
    output = StringIO.StringIO()
    compressed = gzip.GzipFile(mode='wb', fileobj=output)
    compressed.write(self.read())
    compressed.close()
    return output.getvalue()
