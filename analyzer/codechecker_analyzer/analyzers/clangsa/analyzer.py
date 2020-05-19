# -------------------------------------------------------------------------
#
#  Part of the CodeChecker project, under the Apache License v2.0 with
#  LLVM Exceptions. See LICENSE for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# -------------------------------------------------------------------------
"""
Clang Static Analyzer related functions.
"""


import os
import re
import shlex
import subprocess

from codechecker_common.logger import get_logger

from codechecker_analyzer import host_check
from codechecker_analyzer import env

from .. import analyzer_base
from ..config_handler import CheckerState
from ..flag import has_flag
from ..flag import prepend_all

from . import clang_options
from . import config_handler
from . import ctu_triple_arch
from . import version
from .result_handler import ResultHandlerClangSA

LOG = get_logger('analyzer')


def parse_clang_help_page(command, start_label, environ):
    """
    Parse the clang help page starting from a specific label.
    Returns a list of (flag, description) tuples.
    """
    try:
        help_page = subprocess.check_output(
            command,
            env=environ,
            universal_newlines=True,
            encoding="utf-8",
            errors="ignore")
    except (subprocess.CalledProcessError, OSError):
        return []

    help_page = help_page[help_page.index(start_label) + len(start_label):]
    reg = re.compile(r'(\S+)\s+([^\n]+)')
    return re.findall(reg, help_page)


class ClangSA(analyzer_base.SourceAnalyzer):
    """
    Constructs clang static analyzer commands.
    """
    ANALYZER_NAME = 'clangsa'

    def __init__(self, cfg_handler, buildaction):
        super(ClangSA, self).__init__(cfg_handler, buildaction)
        self.__disable_ctu = False
        self.__checker_configs = []

    def is_ctu_available(self):
        """
        Check if ctu is available for the analyzer.
        If the ctu_dir is set in the config, the analyzer is capable to
        run ctu analysis.
        """
        return bool(self.config_handler.ctu_dir)

    def is_ctu_enabled(self):
        """
        Check if ctu is enabled for the analyzer.
        """
        return not self.__disable_ctu

    def disable_ctu(self):
        """
        Disable ctu even if ctu is available.
        By default it is enabled if available.
        """
        self.__disable_ctu = True

    def enable_ctu(self):
        self.__disable_ctu = False

    def add_checker_config(self, checker_cfg):
        """
        Add configuration options to specific checkers.
        checker_cfg should be a list of arguments in case of
        Clang Static Analyzer like this:
        ['-Xclang', '-analyzer-config', '-Xclang', 'checker_option=some_value']
        """

        self.__checker_configs.append(checker_cfg)

    @classmethod
    def get_analyzer_checkers(cls, cfg_handler, environ):
        """Return the list of the supported checkers."""
        checker_list_args = clang_options.get_analyzer_checkers_cmd(
            cfg_handler,
            alpha=True)
        return parse_clang_help_page(checker_list_args, 'CHECKERS:', environ)

    @classmethod
    def get_checker_config(cls, cfg_handler, environ):
        """Return the list of checker config options."""
        checker_config_args = clang_options.get_checker_config_cmd(
            cfg_handler,
            alpha=True)
        return parse_clang_help_page(checker_config_args, 'OPTIONS:', environ)

    @classmethod
    def get_analyzer_config(cls, cfg_handler, environ):
        """Return the list of analyzer config options."""
        analyzer_config_args = clang_options.get_analyzer_config_cmd(
            cfg_handler)
        return parse_clang_help_page(analyzer_config_args, 'OPTIONS:', environ)

    def construct_analyzer_cmd(self, result_handler):
        """
        Called by the analyzer method.
        Construct the analyzer command.
        """
        try:
            # Get an output file from the result handler.
            analyzer_output_file = result_handler.analyzer_result_file

            # Get the checkers list from the config_handler.
            # Checker order matters.
            config = self.config_handler

            analyzer_cmd = [config.analyzer_binary, '--analyze',
                            # Do not warn about the unused gcc/g++ arguments.
                            '-Qunused-arguments']

            for plugin in config.analyzer_plugins:
                analyzer_cmd.extend(["-Xclang", "-plugin",
                                     "-Xclang", "checkercfg",
                                     "-Xclang", "-load",
                                     "-Xclang", plugin])

            analyzer_mode = 'plist-multi-file'
            analyzer_cmd.extend(['-Xclang',
                                 '-analyzer-opt-analyze-headers',
                                 '-Xclang',
                                 '-analyzer-output=' + analyzer_mode,
                                 '-o', analyzer_output_file])

            # Expand macros in plist output on the bug path.
            analyzer_cmd.extend(['-Xclang',
                                 '-analyzer-config',
                                 '-Xclang',
                                 'expand-macros=true'])

            # Checker configuration arguments needs to be set before
            # the checkers.
            if self.__checker_configs:
                for cfg in self.__checker_configs:
                    analyzer_cmd.extend(cfg)

            # TODO: This object has a __checker_configs attribute and the
            # corresponding functions to set it. Either those should be used
            # for checker configs coming as command line argument, or those
            # should be eliminated.
            for cfg in config.checker_config:
                analyzer_cmd.extend(
                    ['-Xclang', '-analyzer-config', '-Xclang', cfg])

            # Config handler stores which checkers are enabled or disabled.
            for checker_name, value in config.checks().items():
                state, _ = value
                if state == CheckerState.enabled:
                    analyzer_cmd.extend(['-Xclang',
                                         '-analyzer-checker=' + checker_name])
                elif state == CheckerState.disabled:
                    analyzer_cmd.extend(['-Xclang',
                                         '-analyzer-disable-checker=' +
                                         checker_name])

            # Enable aggressive-binary-operation-simplification option.
            analyzer_cmd.extend(
                clang_options.get_abos_options(config.version_info))

            # Enable the z3 solver backend.
            if config.enable_z3:
                analyzer_cmd.extend(['-Xclang', '-analyzer-constraints=z3'])

            if config.enable_z3_refutation and not config.enable_z3:
                analyzer_cmd.extend(['-Xclang',
                                     '-analyzer-config',
                                     '-Xclang',
                                     'crosscheck-with-z3=true'])

            if config.ctu_dir and not self.__disable_ctu:
                analyzer_cmd.extend(
                    ['-Xclang', '-analyzer-config', '-Xclang',
                     'experimental-enable-naive-ctu-analysis=true',
                     '-Xclang', '-analyzer-config', '-Xclang',
                     'ctu-dir=' + self.get_ctu_dir()])
                ctu_display_progress = config.ctu_capability.display_progress
                if ctu_display_progress:
                    analyzer_cmd.extend(ctu_display_progress)

            compile_lang = self.buildaction.lang
            if not has_flag('-x', analyzer_cmd):
                analyzer_cmd.extend(['-x', compile_lang])

            if not has_flag('--target', analyzer_cmd) and \
                    self.buildaction.target.get(compile_lang, "") != "":
                analyzer_cmd.append("--target=" +
                                    self.buildaction.target.get(compile_lang))

            if not has_flag('-std', analyzer_cmd) and \
                    self.buildaction.compiler_standard.get(compile_lang, "") \
                    != "":
                analyzer_cmd.append(
                        self.buildaction.compiler_standard[compile_lang])

            analyzer_cmd.extend(config.analyzer_extra_arguments)

            analyzer_cmd.extend(self.buildaction.analyzer_options)

            analyzer_cmd.extend(prepend_all(
                '-isystem',
                self.buildaction.compiler_includes[compile_lang]))

            analyzer_cmd.append(self.source_file)

            return analyzer_cmd

        except Exception as ex:
            LOG.error(ex)
            return []

    def get_ctu_dir(self):
        """
        Returns the path of the ctu directory (containing the triple).
        """
        config = self.config_handler
        environ = env.extend(config.path_env_extra,
                             config.ld_lib_path_extra)
        triple_arch = ctu_triple_arch.get_triple_arch(self.buildaction,
                                                      self.source_file,
                                                      config, environ)
        ctu_dir = os.path.join(config.ctu_dir, triple_arch)
        return ctu_dir

    def get_analyzer_mentioned_files(self, output):
        """
        Parse ClangSA's output to generate a list of files that were mentioned
        in the standard output or standard error.
        """
        if not output:
            return set()

        regex_for_ctu_ast_load = re.compile(
            r"CTU loaded AST file: (.*).ast")

        paths = set()

        ctu_ast_dir = os.path.join(self.get_ctu_dir(), "ast")

        for line in output.splitlines():
            match = re.match(regex_for_ctu_ast_load, line)
            if match:
                path = match.group(1)
                if ctu_ast_dir in path:
                    paths.add(path[len(ctu_ast_dir):])

        return paths

    @classmethod
    def resolve_missing_binary(cls, configured_binary, environ):
        """
        In case of the configured binary for the analyzer is not found in the
        PATH, this method is used to find a callable binary.
        """

        LOG.debug("%s not found in path for ClangSA!", configured_binary)

        if os.path.isabs(configured_binary):
            # Do not autoresolve if the path is an absolute path as there
            # is nothing we could auto-resolve that way.
            return False

        # clang, clang-5.0, clang++, clang++-5.1, ...
        clang = env.get_binary_in_path(['clang', 'clang++'],
                                       r'^clang(\+\+)?(-\d+(\.\d+){0,2})?$',
                                       environ)

        if clang:
            LOG.debug("Using '%s' for ClangSA!", clang)
        return clang

    def construct_result_handler(self, buildaction, report_output,
                                 severity_map, skiplist_handler):
        """
        See base class for docs.
        """
        res_handler = ResultHandlerClangSA(buildaction, report_output,
                                           self.config_handler.report_hash)

        res_handler.severity_map = severity_map
        res_handler.skiplist_handler = skiplist_handler

        return res_handler

    @classmethod
    def construct_config_handler(cls, args, context):

        environ = env.extend(context.path_env_extra,
                             context.ld_lib_path_extra)

        handler = config_handler.ClangSAConfigHandler(environ)
        handler.analyzer_plugins_dir = context.checker_plugin
        handler.analyzer_binary = context.analyzer_binaries.get(
            cls.ANALYZER_NAME)
        handler.compiler_resource_dir = \
            host_check.get_resource_dir(handler.analyzer_binary, context)
        handler.version_info = version.get(handler.analyzer_binary, environ)

        handler.report_hash = args.report_hash \
            if 'report_hash' in args else None

        handler.enable_z3 = 'enable_z3' in args and args.enable_z3 == 'on'

        handler.enable_z3_refutation = 'enable_z3_refutation' in args and \
            args.enable_z3_refutation == 'on'

        if 'ctu_phases' in args:
            handler.ctu_dir = os.path.join(args.output_path,
                                           args.ctu_dir)

            handler.log_file = args.logfile
            handler.path_env_extra = context.path_env_extra
            handler.ld_lib_path_extra = context.ld_lib_path_extra

        try:
            with open(args.clangsa_args_cfg_file, 'r', encoding='utf8',
                      errors='ignore') as sa_cfg:
                handler.analyzer_extra_arguments = \
                    re.sub(r'\$\((.*?)\)',
                           env.replace_env_var(args.clangsa_args_cfg_file),
                           sa_cfg.read().strip())
                handler.analyzer_extra_arguments = \
                    shlex.split(handler.analyzer_extra_arguments)
        except IOError as ioerr:
            LOG.debug_analyzer(ioerr)
        except AttributeError as aerr:
            # No clangsa arguments file was given in the command line.
            LOG.debug_analyzer(aerr)

        checkers = ClangSA.get_analyzer_checkers(handler, environ)

        # Read clang-sa checkers from the config file.
        clang_sa_checkers = context.checker_config.get(cls.ANALYZER_NAME +
                                                       '_checkers')

        try:
            cmdline_checkers = args.ordered_checkers
        except AttributeError:
            LOG.debug_analyzer('No checkers were defined in '
                               'the command line for %s', cls.ANALYZER_NAME)
            cmdline_checkers = None

        handler.initialize_checkers(
            context.available_profiles,
            context.package_root,
            checkers,
            clang_sa_checkers,
            cmdline_checkers,
            'enable_all' in args and args.enable_all)

        handler.checker_config = []
        r = re.compile(r'(?P<analyzer>.+?):(?P<key>.+?)=(?P<value>.+)')

        # TODO: This extra "isinstance" check is needed for
        # CodeChecker checkers --checker-config. This command also runs
        # this function in order to construct a config handler.
        if 'checker_config' in args and \
                isinstance(args.checker_config, list):
            for cfg in args.checker_config:
                m = re.search(r, cfg)
                if m.group('analyzer') == cls.ANALYZER_NAME:
                    handler.checker_config.append(
                        m.group('key') + '=' + m.group('value'))

        # TODO: This extra "isinstance" check is needed for
        # CodeChecker analyzers --analyzer-config. This command also runs
        # this function in order to construct a config handler.
        if 'analyzer_config' in args and \
                isinstance(args.analyzer_config, list):
            for cfg in args.analyzer_config:
                m = re.search(r, cfg)
                if m.group('analyzer') == cls.ANALYZER_NAME:
                    handler.checker_config.append(
                        m.group('key') + '=' + m.group('value'))

        return handler
