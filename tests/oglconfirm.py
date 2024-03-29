#!/usr/bin/env python
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# This permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
# KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE AUTHOR(S) BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN
# AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF
# OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import os
import re
import sys
import subprocess

from framework.core import TestProfile, testBinDir
from framework.exectest import ExecTest
from os import path

__all__ = ['profile']

bin_oglconform = path.join(testBinDir, 'oglconform')

if not os.path.exists(bin_oglconform):
    sys.exit(0)

profile = TestProfile()

#############################################################################
##### OGLCTest: Execute a sub-test of the Intel oglconform test suite.
#####
##### To use this, create an 'oglconform' symlink in piglit/bin.  Piglit
##### will obtain a list of tests from oglconform and add them all.
#############################################################################
class OGLCTest(ExecTest):
    skip_re = re.compile(r'Total Not run: 1|no test in schedule is compat|GLSL [13].[345]0 is not supported|wont be scheduled due to lack of compatible fbconfig')

    def __init__(self, category, subtest):
        ExecTest.__init__(self, [bin_oglconform, '-minFmt', '-v', '4', '-test', category, subtest])

    def interpretResult(self, out, returncode, results, dmesg):
        if self.skip_re.search(out) is not None:
            results['result'] = 'skip'
        elif re.search('Total Passed : 1', out) is not None:
            results['result'] = 'dmesg-warn' if dmesg != '' else 'pass'
        else:
            results['result'] = 'dmesg-fail' if dmesg != '' else 'fail'
        return out

# Create a new top-level 'oglconform' category

testlist_file = '/tmp/oglc.tests'

with open(os.devnull, "w") as devnull:
    subprocess.call([bin_oglconform, '-generateTestList', testlist_file], stdout=devnull.fileno(), stderr=devnull.fileno())

with open(testlist_file) as f:
    testlist = f.read().splitlines()
    for l in testlist:
        try:
            category, test = l.split()
            profile.test_list[path.join('oglconform', category, test)] = OGLCTest(category, test)
        except:
            continue
