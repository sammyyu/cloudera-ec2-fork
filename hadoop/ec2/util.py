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

import ConfigParser
import os
import socket
import urllib2

def bash_quote(text):
  """Quotes a string for bash, by using single quotes."""
  if text == None:
    return ""
  return "'%s'" % text.replace("'", "'\\''")

def bash_quote_env(env):
  """Quotes the value in an environment variable assignment."""
  if env.find("=") == -1:
    return env
  (var, value) = env.split("=")
  return "%s=%s" % (var, bash_quote(value))

def build_env_string(local_env_variables=[], env_strings=[], pairs={}):
  """Build a bash environment variable assignment"""
  env = ''
  if local_env_variables:
    for key in local_env_variables:
      if os.environ.has_key(key):
        env += "%s=%s " % (key, bash_quote(os.environ[key]))
  if env_strings:
    for env_string in env_strings:
      env += "%s " % bash_quote_env(env_string)
  if pairs:
    for key, val in pairs.items():
      env += "%s=%s " % (key, bash_quote(val))
  return env[:-1]

def merge_config_with_options(section_name, config, options):
  """
  Merge configuration options with a dictionary of options.
  Keys in the options dictionary take precedence.
  """
  d = {}
  try:
    for (key, value) in config.items(section_name):
      d[key] = value
  except ConfigParser.NoSectionError:
    pass
  for key in options:
    if options[key] != None:
      d[key] = options[key]
  return d

def url_get(url, timeout=10, retries=0):
  socket.setdefaulttimeout(timeout) # in Python 2.6 we can pass timeout to urllib2.urlopen
  attempts = 0
  while True:
    try:
      return urllib2.urlopen(url).read()
    except urllib2.URLError:
      attempts = attempts + 1
      if attempts > retries:
        raise

def xstr(s):
  """Sane string conversion: return an empty string if s is None."""
  return '' if s is None else str(s)
