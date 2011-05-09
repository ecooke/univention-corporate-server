# -*- coding: utf-8 -*-
#
# Univention Admin Modules
#  admin policy for the thin clients
#
# Copyright 2004-2011 Univention GmbH
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
# you and Univention and not subject to the GNU AGPL V3.
#
# In the case you use this program under the terms of the GNU AGPL V3,
# the program is provided in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License with the Debian GNU/Linux or Univention distribution in file
# /usr/share/common-licenses/AGPL-3; if not, see
# <http://www.gnu.org/licenses/>.

import sys, string
sys.path=['.']+sys.path
import univention.admin.syntax
import univention.admin.filter
import univention.admin.handlers
import univention.admin.localization

import univention.debug

translation=univention.admin.localization.translation('univention.admin.handlers.policies')
_=translation.translate

class thinclientFixedAttributes(univention.admin.syntax.select):
	name='thinclientFixedAttributes'
	choices=[
		('univentionFileServer',_('File servers')),
		('univentionDesktopServer',_('Linux terminal servers')),
		('univentionWindowsTerminalServer',_('Windows terminal servers')),
		('univentionWindowsDomain',_('Windows domain')),
		('univentionAuthServer',_('Authentication servers')),
		]


module='policies/thinclient'
operations=['add','edit','remove','search']

policy_oc='univentionPolicyThinClient'
policy_apply_to=["computers/thinclient"]
policy_position_dn_prefix="cn=thinclient"
usewizard=1
childs=0
short_description=_('Policy: Thin client')
policy_short_description=_('Thin client configuration')
long_description=''
options={
}
property_descriptions={
	'name': univention.admin.property(
			short_description=_('Name'),
			long_description='',
			syntax=univention.admin.syntax.string,
			multivalue=0,
			options=[],
			required=1,
			may_change=0,
			identifies=1,
		),
	'linuxTerminalServer': univention.admin.property(
			short_description=_('Linux terminal servers'),
			long_description=_('Linux terminal servers of the Thin Client'),
			syntax=univention.admin.syntax.linuxTerminalServer,
			multivalue=1,
			options=[],
			required=0,
			may_change=1,
			identifies=0
		),
	'fileServer': univention.admin.property(
			short_description=_('File servers'),
			long_description=_('File servers of the Thin Client'),
			syntax=univention.admin.syntax.fileServer,
			multivalue=1,
			options=[],
			required=0,
			may_change=1,
			identifies=0
		),
	'authServer': univention.admin.property(
			short_description=_('Authentication servers'),
			long_description=_('Authentication servers of the Thin Client'),
			syntax=univention.admin.syntax.authenticationServer,
			multivalue=1,
			options=[],
			required=0,
			may_change=1,
			identifies=0
		),
	'windowsTerminalServer': univention.admin.property(
			short_description=_('Windows terminal servers'),
			long_description=_('Windows terminal servers for Windows Login'),
			syntax=univention.admin.syntax.windowsTerminalServer,
			multivalue=1,
			options=[],
			required=0,
			may_change=1,
			identifies=0
		),
	'windowsDomain': univention.admin.property(
			short_description=_('Windows domain'),
			long_description=_('Windows domain for Windows Login'),
			syntax=univention.admin.syntax.string,
			multivalue=0,
			options=[],
			required=0,
			may_change=1,
			identifies=0
		),
	'requiredObjectClasses': univention.admin.property(
			short_description=_('Required object classes'),
			long_description='',
			syntax=univention.admin.syntax.string,
			multivalue=1,
			options=[],
			required=0,
			may_change=1,
			identifies=0
		),
	'prohibitedObjectClasses': univention.admin.property(
			short_description=_('Excluded object classes'),
			long_description='',
			syntax=univention.admin.syntax.string,
			multivalue=1,
			options=[],
			required=0,
			may_change=1,
			identifies=0
		),
	'fixedAttributes': univention.admin.property(
			short_description=_('Fixed attributes'),
			long_description='',
			syntax=thinclientFixedAttributes,
			multivalue=1,
			options=[],
			required=0,
			may_change=1,
			identifies=0
		),
	'emptyAttributes': univention.admin.property(
			short_description=_('Empty attributes'),
			long_description='',
			syntax=thinclientFixedAttributes,
			multivalue=1,
			options=[],
			required=0,
			may_change=1,
			identifies=0
		),
	'filler': univention.admin.property(
			short_description='',
			long_description='',
			syntax=univention.admin.syntax.none,
			multivalue=0,
			required=0,
			may_change=1,
			identifies=0,
			dontsearch=1
		)
}
layout=[
	univention.admin.tab(_('General'),_('Servers to use'), [
		[univention.admin.field('name', hide_in_resultmode=1), univention.admin.field('filler', hide_in_resultmode=1) ],
		[univention.admin.field('authServer'), univention.admin.field('fileServer')],
		[univention.admin.field('linuxTerminalServer'), univention.admin.field('windowsTerminalServer')],
		[univention.admin.field('windowsDomain'), univention.admin.field('filler')]
	]),
	univention.admin.tab(_('Object'),_('Object'), [
		[univention.admin.field('requiredObjectClasses') , univention.admin.field('prohibitedObjectClasses') ],
		[univention.admin.field('fixedAttributes'), univention.admin.field('emptyAttributes')]
	], advanced = True),
]

mapping=univention.admin.mapping.mapping()
mapping.register('name', 'cn', None, univention.admin.mapping.ListToString)
mapping.register('linuxTerminalServer', 'univentionDesktopServer')
mapping.register('fileServer', 'univentionFileServer')
mapping.register('authServer', 'univentionAuthServer')
mapping.register('windowsTerminalServer', 'univentionWindowsTerminalServer')
mapping.register('windowsDomain', 'univentionWindowsDomain', None, univention.admin.mapping.ListToString)
mapping.register('requiredObjectClasses', 'requiredObjectClasses')
mapping.register('prohibitedObjectClasses', 'prohibitedObjectClasses')
mapping.register('fixedAttributes', 'fixedAttributes')
mapping.register('emptyAttributes', 'emptyAttributes')

class object(univention.admin.handlers.simplePolicy):
	module=module

	def __init__(self, co, lo, position, dn='', superordinate=None, arg=None):
		global mapping
		global property_descriptions

		self.co=co
		self.lo=lo
		self.dn=dn
		self.position=position
		self._exists=0
		self.mapping=mapping
		self.descriptions=property_descriptions

		univention.admin.handlers.simplePolicy.__init__(self, co, lo, position, dn, superordinate)

	def exists(self):
		return self._exists

	def _ldap_pre_create(self):
		self.dn='%s=%s,%s' % (mapping.mapName('name'), mapping.mapValue('name', self.info['name']), self.position.getDn())

	def _ldap_addlist(self):
		return [ ('objectClass', ['top', 'univentionPolicy', 'univentionPolicyThinClient']) ]

def lookup(co, lo, filter_s, base='', superordinate=None, scope='sub', unique=0, required=0, timeout=-1, sizelimit=0):

	filter=univention.admin.filter.conjunction('&', [
		univention.admin.filter.expression('objectClass', 'univentionPolicyThinClient')
		])

	if filter_s:
		filter_p=univention.admin.filter.parse(filter_s)
		univention.admin.filter.walk(filter_p, univention.admin.mapping.mapRewrite, arg=mapping)
		filter.expressions.append(filter_p)

	res=[]
	try:
		for dn in lo.searchDn(unicode(filter), base, scope, unique, required, timeout, sizelimit):
			res.append(object(co, lo, None, dn))
	except:
		pass
	return res

def identify(dn, attr, canonical=0):
	return 'univentionPolicyThinClient' in attr.get('objectClass', [])
