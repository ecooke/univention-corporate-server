# -*- coding: utf-8 -*-
#
# Univention Admin Modules
#  admin policy for the registry configuration
#
# Copyright (C) 2007-2009 Univention GmbH
#
# http://www.univention.de/
#
# All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# Binary versions of this file provided by Univention to you as
# well as other copyrighted, protected or trademarked materials like
# Logos, graphics, fonts, specific documentations and configurations,
# cryptographic keys etc. are subject to a license agreement between
# you and Univention.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import sys, string, copy
import univention.admin.syntax
import univention.admin.filter
import univention.admin.handlers
import univention.admin.localization
import univention.debug as ud

translation=univention.admin.localization.translation('univention.admin.handlers.policies')
_=translation.translate

class registryFixedAttributes(univention.admin.syntax.select):
	name='registryFixedAttributes'
	choices=[
		]

module='policies/registry'
operations=['add','edit','remove','search']

policy_oc='univentionPolicyRegistry'
policy_apply_to=["computers/domaincontroller_master", "computers/domaincontroller_backup", "computers/domaincontroller_slave", "computers/memberserver", "computers/managedclient", "computers/mobileclient", "computers/thinclient"]
policy_position_dn_prefix="cn=registry"
usewizard=1
childs=0
short_description=_('Policy: Univention Configuration Registry')
policy_short_description=_('Univention Configuration Registry')
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
	'registry': univention.admin.property(
			short_description=_('Configuration Registry'),
			long_description='',
			syntax=univention.admin.syntax.configRegistry,
			multivalue=1,
			options=[],
			required=0,
			may_change=1,
			identifies=0,
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
			syntax=registryFixedAttributes,
			multivalue=1,
			options=[],
			required=0,
			may_change=1,
			identifies=0
		),
	'emptyAttributes': univention.admin.property(
			short_description=_('Empty attributes'),
			long_description='',
			syntax=registryFixedAttributes,
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
	univention.admin.tab(_('General'),_('These configuration settings will be set on the local UCS system.'), [
		[univention.admin.field('name', hide_in_resultmode=1), univention.admin.field('filler', hide_in_normalmode=1) ],
		[univention.admin.field('registry'), univention.admin.field('filler')],
	]),
	univention.admin.tab(_('Object'),_('Object'), [
		[univention.admin.field('requiredObjectClasses') , univention.admin.field('prohibitedObjectClasses') ],
		[univention.admin.field('fixedAttributes'), univention.admin.field('emptyAttributes')]
	], advanced = True),
]

mapping=univention.admin.mapping.mapping()
mapping.register('name', 'cn', None, univention.admin.mapping.ListToString)
mapping.register('requiredObjectClasses', 'requiredObjectClasses')
mapping.register('prohibitedObjectClasses', 'prohibitedObjectClasses')
mapping.register('fixedAttributes', 'fixedAttributes')
mapping.register('emptyAttributes', 'emptyAttributes')

class object(univention.admin.handlers.simplePolicy):
	module=module

	def __init__(self, co, lo, position, dn='', superordinate=None, arg=None):
		global mapping
		global property_descriptions
		global layout

		self.co=co
		self.lo=lo
		self.dn=dn
		self.position=position
		self._exists=0
		self.mapping=mapping
		self.descriptions=property_descriptions

		univention.admin.handlers.simplePolicy.__init__(self, co, lo, position, dn, superordinate)

		if self.dn:
			self['registry']=[]
			for key in self.oldattr.keys():
				if key.startswith('univentionRegistry;entry-hex-'):
					key_name=key.split('univentionRegistry;entry-hex-')[1].decode('hex')
					self.append_registry(key_name, self.oldattr[key][0].strip())

		self.save()

	def append_registry(self, key, value):
		if not property_descriptions.has_key(key):
			property_descriptions[key] =  univention.admin.property(
				short_description=key,
				long_description='',
				# we use a different syntax, because the key shouldn't be
				# shown if the value is empty. Otherwise we might get a lot of
				# keys during an UDM session.
				syntax=univention.admin.syntax.configRegistryKey,
				multivalue=0,
				options=[],
				required=0,
				may_change=1,
				identifies=0,
			)
			layout[0].fields.append([univention.admin.field(key)])
			layout[0].fields.sort()

			mapping.register(key, 'univentionRegistry;entry-hex-%s' % key.encode('hex'), None, univention.admin.mapping.ListToString)
			self.oldinfo[key] = ''
		self.info[key] = value

	def _ldap_modlist(self):
		if self.hasChanged('registry'):
			old_keys = []
			new_keys = []
			if self.info.has_key('registry'):
				for line in self.info['registry']:
					new_keys.append(line.split('=')[0])
				if self.oldinfo.has_key('registry'):
					for line in self.oldinfo['registry']:
						old_keys.append(line.split('=')[0])
				for k in old_keys:
					if not k in new_keys:
						self.append_registry(k, '')
				for k in new_keys:
					for line in self.info['registry']:
						if line.startswith('%s=' % k ):
							value=string.join(line.split('=', 1)[1:])
							self.append_registry(k, value)
							break

		ml=univention.admin.handlers.simplePolicy._ldap_modlist(self)
		return ml

	def exists(self):
		return self._exists

	def _ldap_pre_create(self):
		self.dn='%s=%s,%s' % (mapping.mapName('name'), mapping.mapValue('name', self.info['name']), self.position.getDn())

	def _ldap_addlist(self):
		return [
			('objectClass', ['top', 'univentionPolicy', 'univentionPolicyRegistry'])
		]

def lookup(co, lo, filter_s, base='', superordinate=None, scope='sub', unique=0, required=0, timeout=-1, sizelimit=0):

	filter=univention.admin.filter.conjunction('&', [
		univention.admin.filter.expression('objectClass', 'univentionPolicyRegistry'),
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

	return 'univentionPolicyRegistry' in attr.get('objectClass', [])
