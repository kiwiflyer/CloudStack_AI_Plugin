#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with this
# work for additional information regarding copyright ownership. The ASF
# licenses this file to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Run these Marvin tests where `nosetests` cannot.

`nose` imports `imp`, which Python 3.12 removed, so on a current
image it cannot be imported at all — and neither can any of
CloudStack's own Marvin tests, every one of which imports
`nose.plugins.attrib`. The documented runner is simply unavailable
there.

Nothing in Marvin needs nose, though. Its plugin only builds a test
client and hangs it on the test class, which is what this does:

    python3 run_without_nose.py [marvin-config] [test-module]

Defaults to `/root/setup/dev/advanced.cfg` and this directory's
`test_ai_services_roles`.
"""
import logging
import os
import sys
import unittest

from marvin.codes import SUCCESS
from marvin.marvinInit import MarvinInit

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    config = sys.argv[1] if len(sys.argv) > 1 else "/root/setup/dev/advanced.cfg"
    module_name = sys.argv[2] if len(sys.argv) > 2 else "test_ai_services_roles"

    init = MarvinInit(config_file=config)
    if init.init() != SUCCESS:
        print(f"MarvinInit failed for {config}")
        return 2

    client = init.getTestClient()
    # marvinPlugin.startTest does both of these. Without an
    # identifier the api client's `id` is None, and Marvin's
    # Account.create builds a username out of it; without the logger,
    # cloudstackTestCase.tearDown calls unittest's own TestCase.debug.
    if client.identifier is None:
        client.identifier = "aiservices"
    client.getApiClient().id = client.identifier

    sys.path.insert(0, HERE)
    module = __import__(module_name)

    log = logging.getLogger("aiservices")
    for case in vars(module).values():
        if isinstance(case, type) and issubclass(case, unittest.TestCase):
            case.clstestclient = client
            for name in ("debug", "info", "error"):
                setattr(case, name, getattr(log, name))
            case.warn = log.warning

    suite = unittest.TestLoader().loadTestsFromModule(module)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
