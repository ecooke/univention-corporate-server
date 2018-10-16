# -*- coding: utf-8 -*-
#
# Copyright 2018 Univention GmbH
#
# http://www.univention.de/
#
# All rights reserved.
#
# The source code of this program is made available
# under the terms of the GNU Affero General Public License version 3
# (GNU AGPL V3) as published by the Free Software Foundation.
#
# Binary versions of this program provided by Univention to you as
# well as other copyrighted, protected or trademarked materials like
# Logos, graphics, fonts, specific documentations and configurations,
# cryptographic keys etc. are subject to a license agreement between
# you and Univention.
#
# This program is provided in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License with the Debian GNU/Linux or Univention distribution in file
# /usr/share/common-licenses/AGPL-3; if not, see
# <http://www.gnu.org/licenses/>.

import univention.admin.uldap
import univention.config_registry
from ldap.filter import filter_format

try:
	from typing import Dict, Text, Tuple
except ImportError:
	pass

class LDAP_connection(object):
	"""Caching LDAP connection factory."""

	_ucr = None  # type: univention.config_registry.ConfigRegistry
	_connection_admin = None  # type: univention.admin.uldap.access
	_connection_machine = None  # type: univention.admin.uldap.access
	_connection_account = {}  # type: Dict[Tuple[Text, int, Text, Text], univention.admin.uldap.access]

	@classmethod
	def get_admin_connection(cls):  # type: () -> univention.admin.uldap.access
		if not cls._connection_admin:
			cls._connection_admin, po = univention.admin.uldap.getAdminConnection()
		return cls._connection_admin

	@classmethod
	def get_machine_connection(cls):  # type: () -> univention.admin.uldap.access
		if not cls._connection_machine:
			cls._connection_machine, po = univention.admin.uldap.getMachineConnection()
		return cls._connection_machine

	@classmethod
	def get_credentials_connection(
			cls,
			identity,  # type: str
			password,  # type: str
		):
		# type: (...) -> univention.admin.uldap.access

		if not cls._ucr:
			cls._ucr = univention.config_registry.ConfigRegistry()
			cls._ucr.load()

		if '=' not in identity:
			try:
				lo = cls.get_machine_connection()
			except Exception:
				# TODO: catch specific permission
				# TODO: raise specific permission
				raise RuntimeError('Cannot get DN for username.')
			dns = lo.searchDn(filter_format('uid=%s', (identity,)))
			try:
				identity = dns[0]
			except IndexError:
				# TODO: raise specific permission
				raise RuntimeError('Cannot get DN for username.')

		server = cls._ucr['ldap/master']
		port = cls._ucr['ldap/master/port']
		base = cls._ucr['ldap/base']
		key = (identity, password)
		if key not in cls._connection_account:
			cls._connection_account[key] = univention.admin.uldap.access(
				host=server,
				port=port,
				base=base,
				binddn=identity,
				bindpw=password
			)
		return cls._connection_account[key]
