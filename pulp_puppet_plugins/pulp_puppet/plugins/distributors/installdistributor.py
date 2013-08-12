# -*- coding: utf-8 -*-
#
# Copyright © 2013 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

from gettext import gettext as _
import logging
import os
import shutil
import tarfile

from pulp.plugins.distributor import Distributor
from pulp.server.db.model.criteria import UnitAssociationCriteria

from pulp_puppet.common import constants

ERROR_MESSAGE_PATH = 'one or more units contains a path outside its base extraction path'
_LOGGER = logging.getLogger(__name__)


def entry_point():
    """
    Entry point that pulp platform uses to load the distributor

    :return: distributor class and its config
    :rtype:  Distributor, {}
    """
    # there is never a default or global config for this distributor
    return PuppetModuleInstallDistributor, {}


class PuppetModuleInstallDistributor(Distributor):
    def __init__(self):
        super(PuppetModuleInstallDistributor, self).__init__()
        self.detail_report = DetailReport()

    @classmethod
    def metadata(cls):
        return {
            'id': constants.INSTALL_DISTRIBUTOR_TYPE_ID,
            'display_name': _('Puppet Install Distributor'),
            'types': [constants.TYPE_PUPPET_MODULE]
        }

    def validate_config(self, repo, config, related_repos):
        """
        :param repo:            metadata describing the repository to which the
                                configuration applies
        :type  repo:            pulp.plugins.model.Repository

        :param config:          plugin configuration instance; the proposed repo
                                configuration is found within
        :type  config:          pulp.plugins.config.PluginCallConfiguration

        :param related_repos:   list of other repositories using this distributor
                                type; empty list if there are none; entries are
                                of type pulp.plugins.model.RelatedRepository
        :type  related_repos: list

        :return: tuple of (bool, str) to describe the result
        :rtype:  tuple
        """
        path = config.get(constants.CONFIG_INSTALL_PATH)
        if not isinstance(path, basestring):
            # path not here, nothing else to validate
            return True, None
        if not os.path.isabs(path):
            return False, _('install path is not absolute')
        if not os.path.isdir(path):
            return False, _('install path is not an existing directory')
        # we need X to get directory listings
        if not os.access(path, os.R_OK|os.W_OK|os.X_OK):
            return False, _('the current user does not have permission to read '
                            'and write files in the destination directory')

        return True, None

    def publish_repo(self, repo, publish_conduit, config):
        """
        Publish the repository by "installing" each puppet module into the given
        destination directory. This effectively means extracting each module's
        tarball in that directory.

        :param repo:            metadata describing the repository
        :type  repo:            pulp.plugins.model.Repository
        :param publish_conduit: provides access to relevant Pulp functionality
        :type  publish_conduit: pulp.plugins.conduits.repo_publish.RepoPublishConduit
        :param config:          plugin configuration
        :type  config:          pulp.plugins.config.PluginConfiguration

        :return: report describing the publish run
        :rtype:  pulp.plugins.model.PublishReport
        """
        # get dir from config
        destination = config.get(constants.CONFIG_INSTALL_PATH)
        if not destination:
            return publish_conduit.build_failure_report('install path not provided',
                                                        self.detail_report.report)

        units = publish_conduit.get_units(UnitAssociationCriteria([constants.TYPE_PUPPET_MODULE]))

        # check for unsafe paths in tarballs, and fail early if problems are found
        self._check_for_unsafe_archive_paths(units, destination)
        if self.detail_report.has_errors:
            return publish_conduit.build_failure_report('failed', self.detail_report.report)

        # clear out pre-existing contents
        try:
            self._clear_destination_directory(destination)
        except (IOError, OSError), e:
            return publish_conduit.build_failure_report(
                'failed to clear destination directory: %s' % str(e),
                self.detail_report.report)

        # actually publish
        for unit in units:
            try:
                archive = tarfile.open(unit.storage_path)
                try:
                    archive.extractall(destination)
                finally:
                    archive.close()
                self.detail_report.success(unit.unit_key)
            except (OSError, IOError), e:
                self.detail_report.error(unit.unit_key, str(e))

        # return some kind of report
        if self.detail_report.has_errors:
            return publish_conduit.build_failure_report('failed', self.detail_report.report)
        else:
            return publish_conduit.build_success_report('success', self.detail_report.report)

    def _check_for_unsafe_archive_paths(self, units, destination):
        """
        Check the paths of files in each tarball to make sure none include path
        components, such as "../", that would cause files to be placed outside of
        the destination directory. Adds errors to the detail report for each unit
        that has one or more offending paths.

        :param units:       list of pulp.plugins.model.AssociatedUnit whose
                            tarballs should be checked for unsafe paths
        :type  units:       list
        :param destination: absolute path to the destination where modules should
                            be installed
        :type  destination: str
        """
        for unit in units:
            try:
                archive = tarfile.open(unit.storage_path)
                try:
                    if not self._archive_paths_are_safe(destination, archive):
                        self.detail_report.error(unit.unit_key, ERROR_MESSAGE_PATH)
                finally:
                    archive.close()
            except (OSError, IOError), e:
                self.detail_report.error(unit.unit_key, str(e))

    @staticmethod
    def _archive_paths_are_safe(destination, archive):
        """
        Checks a tarball archive for paths that include components such as "../"
        that would cause files to be placed outside of the destination_path.

        :param destination: absolute path to the destination where modules should
                            be installed
        :type  destination: str
        :param archive:     tarball archive that should be checked
        :type  archive      tarfile.TarFile

        :return:    True iff all paths in the archive are safe, else False
        :rtype:     bool
        """
        for name in archive.getnames():
            result = os.path.normpath(os.path.join(destination, name))
            if not destination.endswith('/'):
                destination += '/'
            if not result.startswith(destination):
                return False
        return True

    @staticmethod
    def _clear_destination_directory(destination):
        """
        deletes every directory found in the given destination

        :param destination: absolute path to the destination where modules should
                            be installed
        :type  destination: str
        """
        for directory in os.listdir(destination):
            path = os.path.join(destination, directory)
            if os.path.isdir(path):
                shutil.rmtree(path)


class DetailReport(object):
    """
    convenience class to manage the structure of the "detail" report
    """
    def __init__(self):
        self.report = {
            'success_unit_keys': [],
            'errors': [],
        }

    def success(self, unit_key):
        """
        Call for each unit that is successfully published. This adds that unit
        key to the report.

        :param unit_key:    unit key for a successfully published unit
        :type  unit_key:    dict
        """
        self.report['success_unit_keys'].append(unit_key)

    def error(self, unit_key, error_message):
        """
        Call for each unit that has an error during publish. This adds that unit
        key to the report.

        :param unit_key:        unit key for unit that had an error during publish
        :type  unit_key:        dict
        :param error_message:   error message indicating what went wrong for this
                                particular unit
        """
        self.report['errors'].append((unit_key, error_message))

    @property
    def has_errors(self):
        """
        :return:    True iff this report has one or more errors, else False
        :rtype:     bool
        """
        return bool(self.report['errors'])
