# -*- coding: utf-8 -*-
#
# Univention Admin Modules
#  admin module for the DHCP objects
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
import univention.admin.filter
import univention.admin.localization

import univention.admin.handlers
import univention.admin.handlers.dhcp.host
import univention.admin.handlers.dhcp.pool
import univention.admin.handlers.dhcp.server
import univention.admin.handlers.dhcp.service
import univention.admin.handlers.dhcp.shared
import univention.admin.handlers.dhcp.sharedsubnet
import univention.admin.handlers.dhcp.subnet


translation=univention.admin.localization.translation('univention.admin.handlers.dhcp')
_=translation.translate


module='dhcp/dhcp'

childs=0
short_description=_('DHCP')
long_description=''
operations=['search']
usewizard=1
wizardmenustring=_("DHCP")
wizarddescription=_("Add, edit and delete DHCP objects")
wizardoperations={"add":[_("Add"), _("Add DHCP object")],"find":[_("Search"), _("Search DHCP object(s)")]}
wizardpath="univentionDhcpObject"
wizardsuperordinates=["None","dhcp/service"]
wizardtypesforsuper={"None":["dhcp/service"],"dhcp/service":["dhcp/host","dhcp/server","dhcp/shared","dhcp/subnet"]}

childmodules=["dhcp/host","dhcp/pool","dhcp/server","dhcp/service","dhcp/shared","dhcp/sharedsubnet","dhcp/subnet"]
virtual=1
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
			may_change=1,
			identifies=1
		)
}
layout=[ univention.admin.tab(_('General'),_('Basic settings'),[ [univention.admin.field("name")] ]) ]

mapping=univention.admin.mapping.mapping()


class object(univention.admin.handlers.simpleLdap):
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

		univention.admin.handlers.simpleLdap.__init__(self, co, lo, position, dn, superordinate)

	def exists(self):
		return self._exists


def lookup(co, lo, filter_s, base='', superordinate=None, scope='sub', unique=0, required=0, timeout=-1, sizelimit=0):
	ret=[]
	if superordinate:
		ret+=  univention.admin.handlers.dhcp.host.lookup(co, lo, filter_s, base, superordinate, scope, unique, required, timeout, sizelimit)
		ret+= univention.admin.handlers.dhcp.pool.lookup(co, lo, filter_s, base, superordinate, scope, unique, required, timeout, sizelimit)
		ret+=univention.admin.handlers.dhcp.server.lookup(co, lo, filter_s, base, superordinate, scope, unique, required, timeout, sizelimit)
		ret+= univention.admin.handlers.dhcp.shared.lookup(co, lo, filter_s, base, superordinate, scope, unique, required, timeout, sizelimit)
		ret+= univention.admin.handlers.dhcp.sharedsubnet.lookup(co, lo, filter_s, base, superordinate, scope, unique, required, timeout, sizelimit)
		ret+= univention.admin.handlers.dhcp.subnet.lookup(co, lo, filter_s, base, superordinate, scope, unique, required, timeout, sizelimit)
	else:
		ret+= univention.admin.handlers.dhcp.service.lookup(co, lo, filter_s, base, superordinate, scope, unique, required, timeout, sizelimit)
	return ret
	
	

def identify(dn, attr, canonical=0):
	pass


