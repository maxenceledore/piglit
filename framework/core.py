
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

# Piglit core

import errno
import os
import platform
import re
import subprocess
import sys
import time
import traceback
from cStringIO import StringIO
import multiprocessing
import multiprocessing.dummy
import importlib
# TODO: ConfigParser is known as configparser in python3
import ConfigParser
try:
    import simplejson as json
except ImportError:
    import json

import framework.status as status
from .threads import synchronized_self
from .log import log

__all__ = ['PIGLIT_CONFIG',
           'Environment',
           'checkDir',
           'loadTestProfile',
           'TestrunResult',
           'TestResult',
           'TestProfile',
           'Group',
           'Test',
           'testBinDir']


PIGLIT_CONFIG = ConfigParser.SafeConfigParser()

class PiglitJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, status.Status):
            return str(o)
        elif isinstance(o, set):
            return list(o)
        return json.JSONEncoder.default(self, o)


class JSONWriter:
    '''
    Writes to a JSON file stream

    JSONWriter is threadsafe.

    Example
    -------

    This call to ``json.dump``::
        json.dump(
            {
                'a': [1, 2, 3],
                'b': 4,
                'c': {
                    'x': 100,
                },
            }
            file,
            indent=JSONWriter.INDENT)

    is equivalent to::
        w = JSONWriter(file)
        w.open_dict()
        w.write_dict_item('a', [1, 2, 3])
        w.write_dict_item('b', 4)
        w.write_dict_item('c', {'x': 100})
        w.close_dict()

    which is also equivalent to::
        w = JSONWriter(file)
        w.open_dict()
        w.write_dict_item('a', [1, 2, 3])
        w.write_dict_item('b', 4)

        w.write_dict_key('c')
        w.open_dict()
        w.write_dict_item('x', 100)
        w.close_dict()

        w.close_dict()
    '''

    INDENT = 4

    def __init__(self, file):
        self.file = file
        self.__indent_level = 0
        self.__inhibit_next_indent = False
        self.__encoder = PiglitJSONEncoder(indent=self.INDENT)

        # self.__is_collection_empty
        #
        # A stack that indicates if the currect collection is empty
        #
        # When open_dict is called, True is pushed onto the
        # stack. When the first element is written to the newly
        # opened dict, the top of the stack is set to False.
        # When the close_dict is called, the stack is popped.
        #
        # The top of the stack is element -1.
        #
        # XXX: How does one attach docstrings to member variables?
        #
        self.__is_collection_empty = []

    @synchronized_self
    def __write_indent(self):
        if self.__inhibit_next_indent:
            self.__inhibit_next_indent = False
            return
        else:
            i = ' ' * self.__indent_level * self.INDENT
            self.file.write(i)

    @synchronized_self
    def __write(self, obj):
        lines = list(self.__encoder.encode(obj).split('\n'))
        n = len(lines)
        for i in range(n):
            self.__write_indent()
            self.file.write(lines[i])
            if i != n - 1:
                self.file.write('\n')

    @synchronized_self
    def open_dict(self):
        self.__write_indent()
        self.file.write('{')

        self.__indent_level += 1
        self.__is_collection_empty.append(True)

    @synchronized_self
    def close_dict(self, comma=True):
        self.__indent_level -= 1
        self.__is_collection_empty.pop()

        self.file.write('\n')
        self.__write_indent()
        self.file.write('}')

    @synchronized_self
    def write_dict_item(self, key, value):
        # Write key.
        self.write_dict_key(key)

        # Write value.
        self.__write(value)

    @synchronized_self
    def write_dict_key(self, key):
        # Write comma if this is not the initial item in the dict.
        if self.__is_collection_empty[-1]:
            self.__is_collection_empty[-1] = False
        else:
            self.file.write(',')

        self.file.write('\n')
        self.__write(key)
        self.file.write(': ')

        self.__inhibit_next_indent = True


# Ensure the given directory exists
def checkDir(dirname, failifexists):
    exists = True
    try:
        os.stat(dirname)
    except OSError as e:
        if e.errno == errno.ENOENT or e.errno == errno.ENOTDIR:
            exists = False

    if exists and failifexists:
        print >>sys.stderr, "%(dirname)s exists already.\nUse --overwrite if" \
                            "you want to overwrite it.\n" % locals()
        exit(1)

    try:
        os.makedirs(dirname)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

if 'PIGLIT_BUILD_DIR' in os.environ:
    testBinDir = os.path.join(os.environ['PIGLIT_BUILD_DIR'], 'bin')
else:
    testBinDir = os.path.normpath(os.path.join(os.path.dirname(__file__),
                                               '../bin'))

if 'PIGLIT_SOURCE_DIR' not in os.environ:
    p = os.path
    os.environ['PIGLIT_SOURCE_DIR'] = p.abspath(p.join(p.dirname(__file__),
                                                       '..'))

# In debug builds, Mesa will by default log GL API errors to stderr.
# This is useful for application developers or driver developers
# trying to debug applications that should execute correctly.  But for
# piglit we expect to generate errors regularly as part of testing,
# and for exhaustive error-generation tests (particularly some in
# khronos's conformance suite), it can end up ooming your system
# trying to parse the strings.
if 'MESA_DEBUG' not in os.environ:
    os.environ['MESA_DEBUG'] = 'silent'


class TestResult(dict):
    def __init__(self, *args):
        dict.__init__(self, *args)

        # Replace the result with a status object
        try:
            self['result'] = status.status_lookup(self['result'])
        except KeyError:
            # If there isn't a result (like when used by piglit-run), go on
            # normally
            pass


class TestrunResult:
    def __init__(self, resultfile=None):
        self.serialized_keys = ['options',
                                'name',
                                'tests',
                                'wglinfo',
                                'glxinfo',
                                'lspci',
                                'time_elapsed']
        self.name = None
        self.options = None
        self.glxinfo = None
        self.lspci = None
        self.time_elapsed = None
        self.tests = {}

        if resultfile:
            # Attempt to open the json file normally, if it fails then attempt
            # to repair it.
            try:
                raw_dict = json.load(resultfile)
            except ValueError:
                raw_dict = json.load(self.__repairFile(resultfile))

            # Check that only expected keys were unserialized.
            for key in raw_dict:
                if key not in self.serialized_keys:
                    raise Exception('unexpected key in results file: ', str(key))

            self.__dict__.update(raw_dict)

            # Replace each raw dict in self.tests with a TestResult.
            for (path, result) in self.tests.items():
                self.tests[path] = TestResult(result)

    def __repairFile(self, file):
        '''
        Reapair JSON file if necessary

        If the JSON file is not closed properly, perhaps due a system
        crash during a test run, then the JSON is repaired by
        discarding the trailing, incomplete item and appending braces
        to the file to close the JSON object.

        The repair is performed on a string buffer, and the given file
        is never written to. This allows the file to be safely read
        during a test run.

        :return: If no repair occured, then ``file`` is returned.
                Otherwise, a new file object containing the repaired JSON
                is returned.
        '''

        file.seek(0)
        lines = file.readlines()

        # JSON object was not closed properly.
        #
        # To repair the file, we execute these steps:
        #   1. Find the closing brace of the last, properly written
        #      test result.
        #   2. Discard all subsequent lines.
        #   3. Remove the trailing comma of that test result.
        #   4. Append enough closing braces to close the json object.
        #   5. Return a file object containing the repaired JSON.

        # Each non-terminal test result ends with this line:
        safe_line = 2 * JSONWriter.INDENT * ' ' + '},\n'

        # Search for the last occurence of safe_line.
        safe_line_num = None
        for i in range(-1, - len(lines), -1):
            if lines[i] == safe_line:
                safe_line_num = i
                break

        if safe_line_num is None:
            raise Exception('failed to repair corrupt result file: ' +
                            file.name)

        # Remove corrupt lines.
        lines = lines[0:(safe_line_num + 1)]

        # Remove trailing comma.
        lines[-1] = 2 * JSONWriter.INDENT * ' ' + '}\n'

        # Close json object.
        lines.append(JSONWriter.INDENT * ' ' + '}\n')
        lines.append('}')

        # Return new file object containing the repaired JSON.
        new_file = StringIO()
        new_file.writelines(lines)
        new_file.flush()
        new_file.seek(0)
        return new_file

    def write(self, file):
        # Serialize only the keys in serialized_keys.
        keys = set(self.__dict__.keys()).intersection(self.serialized_keys)
        raw_dict = dict([(k, self.__dict__[k]) for k in keys])
        json.dump(raw_dict, file, indent=JSONWriter.INDENT)


class Environment:
    def __init__(self, concurrent=True, execute=True, include_filter=[],
                 exclude_filter=[], valgrind=False, dmesg=False):
        self.concurrent = concurrent
        self.execute = execute
        self.filter = []
        self.exclude_filter = []
        self.exclude_tests = set()
        self.valgrind = valgrind
        self.dmesg = dmesg

        """
        The filter lists that are read in should be a list of string objects,
        however, the filters need to be a list or regex object.

        This code uses re.compile to rebuild the lists and set self.filter
        """
        for each in include_filter:
            self.filter.append(re.compile(each))
        for each in exclude_filter:
            self.exclude_filter.append(re.compile(each))

    def __iter__(self):
        for key, values in self.__dict__.iteritems():
            # If the values are regex compiled then yield their pattern
            # attribute, which is the original plaintext they were compiled
            # from, otherwise yield them normally.
            if key in ['filter', 'exclude_filter']:
                yield (key, [x.pattern for x in values])
            else:
                yield (key, values)

    def run(self, command):
        try:
            p = subprocess.Popen(command,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 universal_newlines=True)
            (stdout, stderr) = p.communicate()
        except:
            return "Failed to run " + command
        return stderr+stdout

    def collectData(self):
        result = {}
        system = platform.system()
        if (system == 'Windows' or system.find("CYGWIN_NT") == 0):
            result['wglinfo'] = self.run('wglinfo')
        else:
            result['glxinfo'] = self.run('glxinfo')
        if system == 'Linux':
            result['lspci'] = self.run('lspci')
        return result


class Test(object):
    def __init__(self, runConcurrent=False):
        '''
                'runConcurrent' controls whether this test will
                execute it's work (i.e. __doRunWork) on the calling thread
                (i.e. the main thread) or from the ConcurrentTestPool threads.
        '''
        self.runConcurrent = runConcurrent
        self.skip_test = False

    def run(self):
        raise NotImplementedError

    def execute(self, env, path, json_writer):
        '''
        Run the test.

        :path:
            Fully qualified test name as a string.  For example,
            ``spec/glsl-1.30/preprocessor/compiler/keywords/void.frag``.
        '''
        def status(msg):
            log(msg=msg, channel=path)

        # Run the test
        if env.execute:
            try:
                status("running")
                time_start = time.time()
                result = self.run(env)
                time_end = time.time()
                if 'time' not in result:
                    result['time'] = time_end - time_start
                if 'result' not in result:
                    result['result'] = 'fail'
                if not isinstance(result, TestResult):
                    result = TestResult(result)
                    result['result'] = 'warn'
                    result['note'] = 'Result not returned as an instance ' \
                                     'of TestResult'
            except:
                result = TestResult()
                result['result'] = 'fail'
                result['exception'] = str(sys.exc_info()[0]) + \
                    str(sys.exc_info()[1])
                result['traceback'] = \
                    "".join(traceback.format_tb(sys.exc_info()[2]))

            status(result['result'])

            if 'subtest' in result and len(result['subtest'].keys()) > 1:
                for test in result['subtest'].keys():
                    result['result'] = result['subtest'][test]
                    json_writer.write_dict_item(os.path.join(path, test), result)
            else:
                json_writer.write_dict_item(path, result)
        else:
            status("dry-run")


class Group(dict):
    pass


class TestProfile:
    def __init__(self):
        self.tests = Group()
        self.test_list = {}
        self.filters = []

    def flatten_group_hierarchy(self):
        '''
        Convert Piglit's old hierarchical Group() structure into a flat
        dictionary mapping from fully qualified test names to "Test" objects.

        For example,
        tests['spec']['glsl-1.30']['preprocessor']['compiler']['void.frag']
        would become:
        test_list['spec/glsl-1.30/preprocessor/compiler/void.frag']
        '''

        def f(prefix, group, test_dict):
            for key in group:
                fullkey = key if prefix == '' else os.path.join(prefix, key)
                if isinstance(group[key], dict):
                    f(fullkey, group[key], test_dict)
                else:
                    test_dict[fullkey] = group[key]
        f('', self.tests, self.test_list)
        # Clear out the old Group()
        self.tests = Group()

    def prepare_test_list(self, env):
        self.flatten_group_hierarchy()

        def matches_any_regexp(x, re_list):
            return True in map(lambda r: r.search(x) is not None, re_list)

        def test_matches(path, test):
            """Filter for user-specified restrictions"""
            return ((not env.filter or matches_any_regexp(path, env.filter))
                    and not path in env.exclude_tests and
                    not matches_any_regexp(path, env.exclude_filter))

        filters = self.filters + [test_matches]
        def check_all(item):
            path, test = item
            for f in filters:
                if not f(path, test):
                    return False
            return True

        # Filter out unwanted tests
        self.test_list = dict(item for item in self.test_list.iteritems()
                              if check_all(item))

    def run(self, env, json_writer):
        '''
        Schedule all tests in profile for execution.

        See ``Test.schedule`` and ``Test.run``.
        '''

        self.prepare_test_list(env)

        def test(pair):
            """ Function to call test.execute from .map

            adds env and json_writer which are needed by Test.execute()

            """
            name, test = pair
            test.execute(env, name, json_writer)

        # Multiprocessing.dummy is a wrapper around Threading that provides a
        # multiprocessing compatible API
        #
        # The default value of pool is the number of virtual processor cores
        single = multiprocessing.dummy.Pool(1)
        multi = multiprocessing.dummy.Pool()
        chunksize = 50

        if env.concurrent == "all":
            multi.imap(test, self.test_list.iteritems(), chunksize)
        elif env.concurrent == "none":
            single.imap(test, self.test_list.iteritems(), chunksize)
        else:
            # Filter and return only thread safe tests to the threaded pool
            multi.imap(test, (x for x in self.test_list.iteritems() if
                              x[1].runConcurrent), chunksize)
            # Filter and return the non thread safe tests to the single pool
            single.imap(test, (x for x in self.test_list.iteritems() if not
                               x[1].runConcurrent), chunksize)

        # Close and join the pools
        # If we don't close and the join the pools the script will exit before
        # the pools finish running
        multi.close()
        single.close()
        multi.join()
        single.join()

    def filter_tests(self, function):
        """Filter out tests that return false from the supplied function

        Arguments:
        function -- a callable that takes two parameters: path, test and
                    returns whether the test should be included in the test
                    run or not.
        """
        self.filters.append(function)

    def update(self, *profiles):
        """ Updates the contents of this TestProfile instance with another

        This method overwrites key:value pairs in self with those in the
        provided profiles argument. This allows multiple TestProfiles to be
        called in the same run; which could be used to run piglit and external
        suites at the same time.

        Arguments:
        profiles -- one or more TestProfile-like objects to be merged.

        """
        for profile in profiles:
            self.tests.update(profile.tests)
            self.test_list.update(profile.test_list)


def loadTestProfile(filename):
    """ Load a python module and return it's profile attribute

    All of the python test files provide a profile attribute which is a
    TestProfile instance. This loads that module and returns it or raises an
    error.

    """
    mod = importlib.import_module('tests.{0}'.format(
        os.path.splitext(os.path.basename(filename))[0]))

    try:
        return mod.profile
    except AttributeError:
        print("Error: There is not profile attribute in module {0}."
              "Did you specify the right file?".format(filename))
        sys.exit(1)


def merge_test_profiles(profiles):
    """ Helper for loading and merging TestProfile instances

    Takes paths to test profiles as arguments and returns a single merged
    TestPRofile instance.

    Arguments:
    profiles -- a list of one or more paths to profile files.

    """
    profile = loadTestProfile(profiles.pop())
    for p in profiles:
        profile.update(loadTestProfile(p))
    return profile


def load_results(filename):
    """ Loader function for TestrunResult class

    This function takes a single argument of a results file.

    It makes quite a few assumptions, first it assumes that it has been passed
    a folder, if that fails then it looks for a plain text json file called
    "main"

    """
    filename = os.path.realpath(filename)

    try:
        with open(filename, 'r') as resultsfile:
            testrun = TestrunResult(resultsfile)
    except IOError:
        with open(os.path.join(filename, "main"), 'r') as resultsfile:
            testrun = TestrunResult(resultsfile)

    assert(testrun.name is not None)
    return testrun


def parse_listfile(filename):
    """
    Parses a newline-seperated list in a text file and returns a python list
    object. It will expand tildes on Unix-like system to the users home
    directory.

    ex file.txt:
        ~/tests1
        ~/tests2/main
        /tmp/test3

    returns:
        ['/home/user/tests1', '/home/users/tests2/main', '/tmp/test3']
    """
    with open(filename, 'r') as file:
        return [os.path.expanduser(i.rstrip('\n')) for i in file.readlines()]
