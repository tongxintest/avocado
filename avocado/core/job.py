# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright: Red Hat Inc. 2013-2015
# Authors: Lucas Meneghel Rodrigues <lmr@redhat.com>
#          Ruda Moura <rmoura@redhat.com>

"""
Job module - describes a sequence of automated test operations.
"""


import logging
import os
import pprint
import re
import shutil
import sys
import tempfile
import time
import traceback
import warnings
from copy import deepcopy

from avocado.core import (data_dir, dispatcher, exceptions, exit_codes,
                          jobdata, output, result, version)
from avocado.core.job_id import create_unique_job_id
from avocado.core.output import LOG_JOB, LOG_UI, STD_OUTPUT
from avocado.core.settings import settings
from avocado.core.suite import TestSuite, TestSuiteError
from avocado.core.utils.version import get_avocado_git_version
from avocado.utils import astring
from avocado.utils.data_structures import CallbackRegister, time_to_seconds

_NEW_ISSUE_LINK = 'https://github.com/avocado-framework/avocado/issues/new'


def register_job_options():
    """Register the few core options that the support the job operation."""
    msg = (
        "Sets the base log level of the output generated by the job, which "
        "is also the base logging level for the --show command line option. "
        "Any of the Python logging levels names are allowed here. Examples:"
        " DEBUG, INFO, WARNING, ERROR, CRITICAL. For more information refer"
        " to: https://docs.python.org/3/library/logging.html#levels"
        )
    settings.register_option(section='job.output',
                             key='loglevel',
                             default='DEBUG',
                             help_msg=msg)

    help_msg = ('Set the maximum amount of time (in SECONDS) that tests are '
                'allowed to execute. Values <= zero means "no timeout". You '
                'can also use suffixes, like: s (seconds), m (minutes), h '
                '(hours). ')
    settings.register_option(section='job.run',
                             key='timeout',
                             default=0,
                             key_type=time_to_seconds,
                             help_msg=help_msg)

    help_msg = ('Store given logging STREAMs in '
                '"$JOB_RESULTS_DIR/$STREAM.$LEVEL."')
    settings.register_option(section='job.run',
                             key='store_logging_stream',
                             nargs='+',
                             help_msg=help_msg,
                             default=['avocado.core:DEBUG'],
                             metavar='STREAM[:LEVEL]',
                             key_type=list)


register_job_options()


class Job:
    """A Job is a set of operations performed on a test machine.

    Most of the time, we are interested in simply running tests,
    along with setup operations and event recording.

    A job has multiple test suites attached to it. Please keep in mind that
    when creating jobs from the constructor (`Job()`), we are assuming that you
    would like to have control of the test suites and you are going to build
    your own TestSuites.

    If you would like any help to create the job's test_suites from the config
    provided, please use `Job.from_config()` method and we are going to do our
    best to create the test suites.

    So, basically, as described we have two "main ways" to create a job:

    1. Automatic discovery, using `from_config()` method::

        job = Job.from_config(job_config=job_config,
                              suites_configs=[suite_cfg1, suite_cfg2])

    2. Manual or Custom discovery, using the constructor::

        job = Job(config=config,
                  test_suites=[suite1, suite2, suite3])
    """

    def __init__(self, config=None, test_suites=None):
        """Creates an instance of Job class.

        Note that `config` and `test_suites` are optional, if not passed you
        need to change this before running your tests. Otherwise nothing will
        run. If you need any help to create the test_suites from the config,
        then use the `Job.from_config()` method.

        :param config: the job configuration, usually set by command
                       line options and argument parsing
        :type config: dict
        :param test_suites: A list with TestSuite objects. If is None the job
                            will have an empty list and you can add suites
                            after init accessing job.test_suites.
        :type test_suites: list
        """
        self.config = settings.as_dict()
        if config:
            self.config.update(config)
        self.log = LOG_UI
        self.loglevel = self.config.get('job.output.loglevel')
        self.__logging_handlers = {}
        if self.config.get('run.dry_run.enabled'):  # Modify config for dry-run
            unique_id = self.config.get('run.unique_job_id')
            if unique_id is None:
                self.config['run.unique_job_id'] = '0' * 40
            self.config['sysinfo.collect.enabled'] = 'off'

        self.test_suites = test_suites or []
        self._check_test_suite_name_uniqueness()

        #: The log directory for this job, also known as the job results
        #: directory.  If it's set to None, it means that the job results
        #: directory has not yet been created.
        self.logdir = None
        self.logfile = None
        self.tmpdir = None
        self.__keep_tmpdir = True
        self._base_tmpdir = None
        self.status = "RUNNING"
        self.result = None
        self.interrupted_reason = None

        self._timeout = None
        self._unique_id = None

        #: The time at which the job has started or `-1` if it has not been
        #: started by means of the `run()` method.
        self.time_start = -1
        #: The time at which the job has finished or `-1` if it has not been
        #: started by means of the `run()` method.
        self.time_end = -1
        #: The total amount of time the job took from start to finish,
        #: or `-1` if it has not been started by means of the `run()` method
        self.time_elapsed = -1
        self.funcatexit = CallbackRegister("JobExit %s" % self.unique_id, LOG_JOB)
        self._stdout_stderr = None
        self._old_root_level = None
        self.replay_sourcejob = self.config.get('replay_sourcejob')
        self.exitcode = exit_codes.AVOCADO_ALL_OK

        self._result_events_dispatcher = None

    @property
    def result_events_dispatcher(self):
        # The result events dispatcher is shared with the test runner.
        # Because of our goal to support using the phases of a job
        # freely, let's get the result events dispatcher on first usage.
        # A future optimization may load it on demand.
        if self._result_events_dispatcher is None:
            self._result_events_dispatcher = dispatcher.ResultEventsDispatcher(
                self.config)
            output.log_plugin_failures(self._result_events_dispatcher
                                       .load_failures)
        return self._result_events_dispatcher

    @property
    def test_results_path(self):
        return os.path.join(self.logdir, 'test-results')

    def __enter__(self):
        self.setup()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.cleanup()

    def __start_job_logging(self):
        # Enable test logger
        fmt = ('%(asctime)s %(module)-16.16s L%(lineno)-.4d %('
               'levelname)-5.5s| %(message)s')
        test_handler = output.add_log_handler(LOG_JOB,
                                              logging.FileHandler,
                                              self.logfile, self.loglevel, fmt)
        main_logger = logging.getLogger('avocado')
        self._old_root_level = main_logger.level
        main_logger.addHandler(test_handler)
        main_logger.setLevel(self.loglevel)
        self.__logging_handlers[test_handler] = [LOG_JOB.name, ""]
        # Add --store-logging-streams
        fmt = '%(asctime)s %(levelname)-5.5s| %(message)s'
        formatter = logging.Formatter(fmt=fmt, datefmt='%H:%M:%S')

        # Store custom test loggers
        if self.config.get("run.test_runner") == 'runner':
            store_logging_stream = self.config.get('job.run.store_logging_stream')
            for name in store_logging_stream:
                name = re.split(r'(?<!\\):', name, maxsplit=1)
                if len(name) == 1:
                    name = name[0]
                    level = logging.INFO
                else:
                    level = (int(name[1]) if name[1].isdigit()
                             else logging.getLevelName(name[1].upper()))
                    name = name[0]
                try:
                    logname = "log" if name == "" else name
                    logfile = os.path.join(self.logdir, logname + "." +
                                           logging.getLevelName(level))
                    handler = output.add_log_handler(name, logging.FileHandler,
                                                     logfile, level, formatter)
                except ValueError as details:
                    self.log.error("Failed to set log for --store-logging-stream "
                                   "%s:%s: %s.", name, level, details)
                else:
                    self.__logging_handlers[handler] = [name]

        # Enable console loggers
        enabled_logs = self.config.get("core.show")
        if ('test' in enabled_logs and
                'early' not in enabled_logs):
            self._stdout_stderr = sys.stdout, sys.stderr
            # Enable std{out,err} but redirect both to stdout
            sys.stdout = STD_OUTPUT.stdout
            sys.stderr = STD_OUTPUT.stdout
            test_handler = output.add_log_handler(LOG_JOB,
                                                  logging.StreamHandler,
                                                  STD_OUTPUT.stdout,
                                                  logging.DEBUG,
                                                  fmt="%(message)s")
            main_logger.addHandler(test_handler)
            self.__logging_handlers[test_handler] = [LOG_JOB.name, ""]

    def __stop_job_logging(self):
        if self._stdout_stderr:
            sys.stdout, sys.stderr = self._stdout_stderr
        for handler, loggers in self.__logging_handlers.items():
            for logger in loggers:
                logging.getLogger(logger).removeHandler(handler)
        logging.root.level = self._old_root_level

    def _log_avocado_config(self):
        LOG_JOB.info('Avocado config:')
        LOG_JOB.info('')
        for line in pprint.pformat(self.config).splitlines():
            LOG_JOB.info(line)
        LOG_JOB.info('')

    def _log_avocado_datadir(self):
        LOG_JOB.info('Avocado Data Directories:')
        LOG_JOB.info('')
        LOG_JOB.info('base     %s', self.config.get('datadir.paths.base_dir'))
        LOG_JOB.info('tests    %s', data_dir.get_test_dir())
        LOG_JOB.info('data     %s', self.config.get('datadir.paths.data_dir'))
        LOG_JOB.info('logs     %s', self.logdir)
        LOG_JOB.info('')

    @staticmethod
    def _log_avocado_version():
        version_log = version.VERSION
        git_version = get_avocado_git_version()
        if git_version is not None:
            version_log += git_version
        LOG_JOB.info('Avocado version: %s', version_log)
        LOG_JOB.info('')

    @staticmethod
    def _log_cmdline():
        cmdline = " ".join(sys.argv)
        LOG_JOB.info("Command line: %s", cmdline)
        LOG_JOB.info('')

    def _log_job_debug_info(self):
        """
        Log relevant debug information to the job log.
        """
        self._log_cmdline()
        self._log_avocado_version()
        self._log_avocado_config()
        self._log_avocado_datadir()
        for suite in self.test_suites:
            self._log_variants(suite.variants)
        self._log_tmp_dir()
        self._log_job_id()

    def _log_job_id(self):
        LOG_JOB.info('Job ID: %s', self.unique_id)
        if self.replay_sourcejob is not None:
            LOG_JOB.info('Replay of Job ID: %s', self.replay_sourcejob)
        LOG_JOB.info('')

    def _log_tmp_dir(self):
        LOG_JOB.info('Temporary dir: %s', self.tmpdir)
        LOG_JOB.info('')

    @staticmethod
    def _log_variants(variants):
        lines = variants.to_str(summary=1, variants=1, use_utf8=False)
        for line in lines.splitlines():
            LOG_JOB.info(line)

    def _setup_job_category(self):
        """
        This has to be called after self.logdir has been defined

        It attempts to create a directory one level up from the job results,
        with the given category name.  Then, a symbolic link is created to
        this job results directory.

        This should allow a user to look at a single directory for all
        jobs of a given category.
        """
        category = self.config.get('run.job_category')
        if category is None:
            return

        if category != astring.string_to_safe_path(category):
            msg = ("Unable to set category in job results: name is not "
                   "filesystem safe: %s" % category)
            LOG_UI.warning(msg)
            LOG_JOB.warning(msg)
            return

        # we could also get "base_logdir" from config, but I believe this is
        # the best choice because it reduces the dependency surface (depends
        # only on self.logdir)
        category_path = os.path.join(os.path.dirname(self.logdir),
                                     category)
        try:
            os.mkdir(category_path)
        except FileExistsError:
            pass

        try:
            os.symlink(os.path.relpath(self.logdir, category_path),
                       os.path.join(category_path, os.path.basename(self.logdir)))
        except NotImplementedError:
            msg = "Unable to link this job to category %s" % category
            LOG_UI.warning(msg)
            LOG_JOB.warning(msg)
        except OSError:
            msg = "Permission denied to link this job to category %s" % category
            LOG_UI.warning(msg)
            LOG_JOB.warning(msg)

    def _setup_job_results(self):
        """
        Prepares a job result directory, also known as logdir, for this job
        """
        base_logdir = self.config.get('run.results_dir')
        if base_logdir is None:
            self.logdir = data_dir.create_job_logs_dir(unique_id=self.unique_id)
        else:
            base_logdir = os.path.abspath(base_logdir)
            self.logdir = data_dir.create_job_logs_dir(base_dir=base_logdir,
                                                       unique_id=self.unique_id)
        if not self.config.get('run.dry_run.enabled'):
            self._update_latest_link()
        self.logfile = os.path.join(self.logdir, "job.log")
        idfile = os.path.join(self.logdir, "id")
        with open(idfile, 'w') as id_file_obj:
            id_file_obj.write("%s\n" % self.unique_id)
            id_file_obj.flush()
            os.fsync(id_file_obj)

    def _update_latest_link(self):
        """
        Update the latest job result symbolic link [avocado-logs-dir]/latest.
        """
        def soft_abort(msg):
            """ Only log the problem """
            LOG_JOB.warning("Unable to update the latest link: %s", msg)
        basedir = os.path.dirname(self.logdir)
        basename = os.path.basename(self.logdir)
        proc_latest = os.path.join(basedir, "latest.%s" % os.getpid())
        latest = os.path.join(basedir, "latest")
        if os.path.exists(latest) and not os.path.islink(latest):
            soft_abort('"%s" already exists and is not a symlink' % latest)
            return

        if os.path.exists(proc_latest):
            try:
                os.unlink(proc_latest)
            except OSError as details:
                soft_abort("Unable to remove %s: %s" % (proc_latest, details))
                return

        try:
            os.symlink(basename, proc_latest)
            os.rename(proc_latest, latest)
        except OSError as details:
            soft_abort("Unable to create create latest symlink: %s" % details)
            return
        finally:
            if os.path.exists(proc_latest):
                os.unlink(proc_latest)

    @classmethod
    def from_config(cls, job_config, suites_configs=None):
        """Helper method to create a job from config dicts.

        This is different from the Job() initialization because here we are
        assuming that you need some help to build the test suites. Avocado will
        try to resolve tests based on the configuration information instead of
        assuming pre populated test suites.

        Keep in mind that here we are going to replace the suite.name with a
        counter.

        If you need create a custom Job with your own TestSuites, please use
        the Job() constructor instead of this method.

        :param job_config: A config dict to be used on this job and also as a
                           'global' config for each test suite.
        :type job_config: dict
        :param suites_configs: A list of specific config dict to be used on
                               each test suite. Each suite config will be
                               merged with the job_config dict. If None is
                               passed then this job will have only one
                               test_suite with the same config as job_config.
        :type suites_configs: list
        """
        suites_configs = suites_configs or [deepcopy(job_config)]
        suites = []
        for index, config in enumerate(suites_configs, start=1):
            suites.append(TestSuite.from_config(config,
                                                name=index,
                                                job_config=job_config))
        return cls(job_config, suites)

    @property
    def size(self):
        """Job size is the sum of all test suites sizes."""
        return sum([suite.size for suite in self.test_suites])

    @property
    def test_suite(self):
        """This is the first test suite of this job (deprecated).

        Please, use test_suites instead.
        """
        if self.test_suites:
            return self.test_suites[0]

    @test_suite.setter
    def test_suite(self, var):
        """Temporary setter. Suites should be set from test_suites."""
        if self.test_suites:
            self.test_suites[0] = var
        else:
            self.test_suites = [var]

    @property
    def timeout(self):
        if self._timeout is None:
            self._timeout = self.config.get('job.run.timeout')
        return self._timeout

    @property
    def unique_id(self):
        if self._unique_id is None:
            self._unique_id = self.config.get('run.unique_job_id') \
                or create_unique_job_id()
        return self._unique_id

    def cleanup(self):
        """
        Cleanup the temporary job handlers (dirs, global setting, ...)
        """
        output.del_last_configuration()
        self.__stop_job_logging()
        if not self.__keep_tmpdir and os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)
            shutil.rmtree(self._base_tmpdir)
        cleanup_conditionals = (
            self.config.get('run.dry_run.enabled'),
            not self.config.get('run.dry_run.no_cleanup')
        )
        if all(cleanup_conditionals):
            # Also clean up temp base directory created because of the dry-run
            base_logdir = self.config.get('run.results_dir')
            if base_logdir is not None:
                try:
                    FileNotFoundError
                except NameError:
                    FileNotFoundError = OSError   # pylint: disable=W0622
                try:
                    shutil.rmtree(base_logdir)
                except FileNotFoundError:
                    pass

    def create_test_suite(self):
        msg = ("create_test_suite() is deprecated. You can also create your "
               "own suites with TestSuite() or TestSuite.from_config().")
        warnings.warn(msg, DeprecationWarning)
        try:
            self.test_suite = TestSuite.from_config(self.config)
            if self.test_suite and self.test_suite.size == 0:
                refs = self.test_suite.references
                msg = ("No tests found for given test references, try "
                       "'avocado -V list %s' for details") % " ".join(refs)
                raise exceptions.JobTestSuiteEmptyError(msg)
        except TestSuiteError as details:
            raise exceptions.JobBaseException(details)
        if self.test_suite:
            self.result.tests_total = self.test_suite.size

    def post_tests(self):
        """
        Run the post tests execution hooks

        By default this runs the plugins that implement the
        :class:`avocado.core.plugin_interfaces.JobPostTests` interface.
        """
        self.result_events_dispatcher.map_method('post_tests', self)

    def pre_tests(self):
        """
        Run the pre tests execution hooks

        By default this runs the plugins that implement the
        :class:`avocado.core.plugin_interfaces.JobPreTests` interface.
        """
        self.result_events_dispatcher.map_method('pre_tests', self)

    def render_results(self):
        """Render test results that depend on all tests having finished.

        By default this runs the plugins that implement the
        :class:`avocado.core.plugin_interfaces.Result` interface.
        """
        result_dispatcher = dispatcher.ResultDispatcher()
        if result_dispatcher.extensions:
            result_dispatcher.map_method('render', self.result, self)

    def get_failed_tests(self):
        """Gets the tests with status 'FAIL' and 'ERROR' after the Job ended.

        :return: List of failed tests
        """
        tests = []
        if self.result:
            for test in self.result.tests:
                if test.get('status') in ['FAIL', 'ERROR']:
                    tests.append(test)
        return tests

    def run(self):
        """
        Runs all job phases, returning the test execution results.

        This method is supposed to be the simplified interface for
        jobs, that is, they run all phases of a job.

        :return: Integer with overall job status. See
                 :mod:`avocado.core.exit_codes` for more information.
        """
        assert self.tmpdir is not None, "Job.setup() not called"
        if self.time_start == -1:
            self.time_start = time.monotonic()
        try:
            self.result.tests_total = self.size
            pre_post_dispatcher = dispatcher.JobPrePostDispatcher()
            output.log_plugin_failures(pre_post_dispatcher.load_failures)
            pre_post_dispatcher.map_method('pre', self)
            self.pre_tests()
            return self.run_tests()
        except exceptions.JobBaseException as details:
            self.status = details.status
            fail_class = details.__class__.__name__
            self.log.error('\nAvocado job failed: %s: %s', fail_class, details)
            self.exitcode |= exit_codes.AVOCADO_JOB_FAIL
            return self.exitcode
        except exceptions.OptionValidationError as details:
            self.log.error('\n%s', str(details))
            self.exitcode |= exit_codes.AVOCADO_JOB_FAIL
            return self.exitcode

        except Exception as details:  # pylint: disable=W0703
            self.status = "ERROR"
            exc_type, exc_value, exc_traceback = sys.exc_info()
            tb_info = traceback.format_exception(exc_type, exc_value,
                                                 exc_traceback.tb_next)
            fail_class = details.__class__.__name__
            self.log.error('\nAvocado crashed: %s: %s', fail_class, details)
            for line in tb_info:
                self.log.debug(line)
            self.log.error("Please include the traceback info and command line"
                           " used on your bug report")
            self.log.error('Report bugs visiting %s', _NEW_ISSUE_LINK)
            self.exitcode |= exit_codes.AVOCADO_FAIL
            return self.exitcode
        finally:
            self.post_tests()
            if self.time_end == -1:
                self.time_end = time.monotonic()
                self.time_elapsed = self.time_end - self.time_start
            self.render_results()
            pre_post_dispatcher.map_method('post', self)

    def run_tests(self):
        """
        The actual test execution phase
        """
        self._log_job_debug_info()
        jobdata.record(self, sys.argv)

        if self.size == 0:
            msg = ('Unable to resolve any reference or "resolver.references"'
                   "is empty.")
            LOG_UI.error(msg)

        if not self.test_suites:
            self.exitcode |= exit_codes.AVOCADO_JOB_FAIL
            return self.exitcode

        summary = set()
        for suite in self.test_suites:
            summary |= suite.run(self)

        # If it's all good so far, set job status to 'PASS'
        if self.status == 'RUNNING':
            self.status = 'PASS'
        LOG_JOB.info('Test results available in %s', self.logdir)

        if 'INTERRUPTED' in summary:
            self.exitcode |= exit_codes.AVOCADO_JOB_INTERRUPTED
        if 'FAIL' in summary or 'ERROR' in summary:
            self.exitcode |= exit_codes.AVOCADO_TESTS_FAIL

        return self.exitcode

    def _check_test_suite_name_uniqueness(self):
        all_names = [suite.name for suite in self.test_suites]
        duplicate_names = set([name for name in all_names
                               if all_names.count(name) > 1])
        if duplicate_names:
            duplicate_names = ", ".join(duplicate_names)
            msg = ('Job contains suites with the following duplicate name(s): '
                   '%s. Test suite names must be unique to guarantee that '
                   'results will not be overwritten' % duplicate_names)
            raise exceptions.JobTestSuiteDuplicateNameError(msg)

    def setup(self):
        """
        Setup the temporary job handlers (dirs, global setting, ...)
        """
        output.reconfigure(self.config)
        assert self.tmpdir is None, "Job.setup() already called"
        if self.config.get('run.dry_run.enabled'):  # Create the dry-run dirs
            if self.config.get('run.results_dir') is None:
                tmp_dir = tempfile.mkdtemp(prefix="avocado-dry-run-")
                self.config['run.results_dir'] = tmp_dir
        self._setup_job_results()
        self.result = result.Result(self.unique_id, self.logfile)
        self.__start_job_logging()
        self._setup_job_category()
        # Use "logdir" in case "keep_tmp" is enabled
        if self.config.get('run.keep_tmp'):
            self._base_tmpdir = self.logdir
        else:
            self._base_tmpdir = tempfile.mkdtemp(prefix="avocado_tmp_")
            self.__keep_tmpdir = False
        self.tmpdir = tempfile.mkdtemp(prefix="avocado_job_",
                                       dir=self._base_tmpdir)
