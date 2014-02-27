#!/usr/bin/python2.6
# -*- coding: utf-8 -*-
#
# Univention AD takeover script
#  Migrates an AD server to the local UCS Samba 4 DC
#
# Copyright 2012-2013 Univention GmbH
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

from optparse import OptionParser, OptionValueError
import samba.getopt
import sys, os
import subprocess
import shutil
from univention import config_registry
import ldb
import samba
from samba.samdb import SamDB
from samba.auth import system_session
from samba.param import LoadParm
import socket, time, struct
import ldap
import re
from samba.ndr import ndr_pack, ndr_unpack
from samba.dcerpc import security
import univention.admin.uldap
import univention.admin.uexceptions as uexceptions
import string
import sqlite3
import univention.admin.modules as udm_modules
import univention.admin.filter as udm_filter
import univention.admin.objects
import univention.admin.config
import ipaddr
import logging
import traceback
from univention.admin.handlers.dns.reverse_zone import mapSubnet
import univention.lib
import univention.lib.s4

# load UDM modules
udm_modules.update()

GPLversion=False
try:
	from univention.admin.license import _license
	from univention.admin.license import License
except:
	GPLversion=True

LOGFILE_NAME = "/var/log/univention/ad-takeover.log"
BACKUP_DIR = "/var/univention-backup/ad-takeover"
SAMBA_DIR = '/var/lib/samba'
SAMBA_PRIVATE_DIR = os.path.join(SAMBA_DIR, 'private')
SYSVOL_PATH = os.path.join(SAMBA_DIR, 'sysvol')

logging.basicConfig(filename=LOGFILE_NAME, format='%(asctime)s %(message)s', level=logging.DEBUG)
log = logging.getLogger()

DEVNULL = open(os.devnull, 'w')

############################# (Yet) DUMMY FUNCTIONS IN LIB ################################

from univention.management.console.log import MODULE
from univention.config_registry import ConfigRegistry
import univention.management.console as umc
ucr = ConfigRegistry()
ucr.load()
_ = umc.Translation('univention-management-console-module-adtakeover').translate

class Progress(object):
	'''Progress information. reset() and error() are set by the UMC module.
	progress.warning can be used when something went wrong which is not
	raise-worthy
	'''
	def __init__(self):
		self._headline = None
		self._message = None
		self._percentage = 'Infinity'
		self._errors = []
		self._critical = False
		self._finished = False

	def reset(self):
		self._headline = None
		self._message = None
		self._percentage = 'Infinity'
		self._errors = []
		self._critical = False
		self._finished = False

	def set(self, headline=None, message=None, percentage=None):
		if headline is not None:
			self.headline(headline)
		if message is not None:
			self.message(message)
		if percentage is not None:
			self.percentage(percentage)

	def headline(self, headline):
		MODULE.process('### %s ###' % headline)
		self._headline = headline
		self._message = None

	def message(self, message):
		MODULE.process('  %s' % message)
		self._message = str(message)

	def percentage(self, percentage):
		if percentage < 0:
			percentage = 'Infinity'
		self._percentage = percentage

	def warning(self, error):
		MODULE.warn(' %s' % error)
		self._errors.append(str(error))

	def error(self, error):
		self._errors.append(str(error))
		self._critical = True

	def finish(self):
		self._finished = True

	def poll(self):
		return {
			'component' : self._headline,
			'info' : self._message,
			'steps' : self._percentage,
			'errors' : self._errors,
			'critical' : self._critical,
			'finished' : self._finished,
		}

class TakeoverError(Exception):
	'''AD Takeover Error'''
	def __init__(self, errormessage=None, detail=None):
		if errormessage:
			self.errormessage = errormessage
		else:
			self.errormessage = self.__doc__
		self.detail = detail
		log.error(self)

	def __str__(self):
		if self.errormessage and self.detail:
			return '%s (%s)' % (self.errormessage, self.detail)
		else:
			return self.errormessage or self.detail or ''

class ComputerUnreachable(TakeoverError):
	'''The computer is not reachable'''

class AuthenticationFailed(TakeoverError):
	'''Authentication failed'''

class DomainManipulationFailed(TakeoverError):
	'''Something critical went wrong during reading from / writing to the
	AD/Samba, e.g. initial DC join failed.
	Needs a good explanation while raising'''

class SysvolError(TakeoverError):
	'''Something is wrong with the SYSVOL, e.g. does not exist,
	contains empty files, has wrong file permissions, etc.
	Needs a good explanation while raising'''

class ADServerRunning(TakeoverError):
	'''The Active Directory server seems to be running. It must be shut off.'''

class TimeSyncronizationFailed(TakeoverError):
	'''Time synchronization failed.'''

class ManualTimeSyncronizationRequired(TimeSyncronizationFailed):
	'''Time difference critical for Kerberos but syncronization aborted.'''

IP_HOSTNAME = [None, None]
def get_ip_and_hostname_of_ad():
	return IP_HOSTNAME

def set_ip_and_hostname_of_ad(ip, hostname):
	IP_HOSTNAME[:] = [ip, hostname]

def get_ad_hostname():
	'''The hostname of the AD to be specified in robocopy'''
	return get_ip_and_hostname_of_ad()[1]

def sysvol_info():
	'''The info needed for the "Copy SYSVOL"-page, i.e.
	"ad_hostname" and "ucs_hostname"'''
	return {
		'ucs_hostname' : ucr.get('hostname'),
		'ad_hostname' : get_ad_hostname(),
	}

def check_status():
	'''Where are we in the process of AD takeover?
	Returns one of:
	'start' -> nothing yet happened
	'sysvol' -> we copied domain data, sysvol was not yet copied'
	'takeover' -> sysvol was copied. we can now take over the domain
	'finished' -> already finished
	'''
	return 'start'

def count_domain_objects_on_server(hostname_or_ip, username, password, progress):
	'''Connects to the hostname_or_ip with username/password credentials
	Expects to find a Windows Domain Controller.
	Gets str, str, str, Progress
	Returns {
		'ad_hostname' : hostname,
		'ad_ip' : hostname_or_ip,
		'ad_os' : version_of_the_ad, # "Windows 2008 R2"
		'ad_domain' : domain_of_the_ad, # "mydomain.local"
		'users' : number_of_users_in_domain,
		'groups' : number_of_groups_in_domain,
		'computers' : number_of_computers_in_domain,
	}
	Raises ComputerUnreachable, AuthenticationFailed
	'''

	try:
		import univention.admin.license
		global License
		global _license
		License = univention.admin.license.License
		_license = univention.admin.license._license
		ignored_users_list = _license.sysAccountNames
	except ImportError:	## GPLversion
		ignored_users_list = []

	progress.headline('Connecting to %s' % hostname_or_ip)
	progress.message('Searching for %s' % hostname_or_ip)
	check_remote_host(hostname_or_ip, ucr)

	progress.message('Authenticating')
	ad = AD_Connection(hostname_or_ip, username, password)

	progress.message('Retrieving information from AD DC')
	domain_info = ad.count_objects(ignored_users_list)

	return domain_info

def join_to_domain_and_copy_domain_data(hostname_or_ip, username, password, progress):
	'''Connects to the hostname_or_ip with username/password credentials
	Expects to find a Windows Domain Controller.
	Gets str, str, str, Progress
	Raises ComputerUnreachable, AuthenticationFailed, DomainManipulationFailed
	'''
	progress.headline('Connecting to %s' % hostname_or_ip)
	progress.message('Searching for %s' % hostname_or_ip)
	time.sleep(.5)
	progress.message('Authenticating')
	time.sleep(.5)
	progress.headline('Joining the domain')
	time.sleep(.5)
	progress.headline('Copying users')
	users = ['alexk', 'dwiesent', 'erik', 'fbest', 'janek', 'lukas', 'phahn', 'schwardt', 'stefan']
	for i, user in enumerate(users):
		progress.message('Copying %s' % user)
		progress.percentage(0 + (30.0 * (i + 1) / len(users)))
		time.sleep(.4)
	groups = ['Domain Users', 'Domain Admins', 'Developers']
	for i, group in enumerate(groups):
		progress.message('Copying %s' % group)
		progress.percentage(30 + (30.0 * (i + 1) / len(groups)))
		time.sleep(.7)
	computers = ['winmember1', 'winmember2']
	for i, computer in enumerate(computers):
		progress.message('Copying %s' % computer)
		progress.percentage(60 + (30.0 * (i + 1) / len(computers)))
		time.sleep(.9)
	progress.headline('Sync with S4-Connector')
	for i in range(90, 100, 2):
		progress.percentage(i + 2)
		time.sleep(.7)
	# join
	# copy
	# restart connector
	# set univention/ad/takeover/ad/server/ip

def check_sysvol(progress):
	'''Whether the AD sysvol is already copied to the local system
	Gets Progress
	Raises SysvolError
	'''
	progress.headline('Checking group policies')
	progress.message('Checking existence')
	time.sleep(.5)
	progress.message('Checking integrity')
	time.sleep(2)
	# raise SysvolError('The group policy share seems to have the wrong file permissions')

def take_over_domain(progress):
	'''Actually takes control of the domain, deletes old AD server, takes
	its IP, etc.
	Gets Progress
	Raises AuthenticationFailed, DomainManipulationFailed, ADServerRunning
	'''
	hostname = get_ad_hostname()
	progress.headline('Search for %s in network' % hostname)
	time.sleep(1)
	# raise ADServerRunning(hostname)
	progress.headline('Taking over the Active Directory domain')
	progress.message('Removing the AD from the domain')
	progress.percentage(0)
	time.sleep(.5)
	progress.message('Taking the old AD\'s IP; restarting network')
	progress.percentage(10)
	time.sleep(.5)
	progress.message('Restarting Samba4')
	progress.percentage(50)
	time.sleep(3)
	progress.message('Restarting Listener')
	progress.percentage(90)
	time.sleep(1.5)
	# do some samba provisioning stuff
	# restart samba
	# (un)set some ucr variables

#############################################################################################

import subprocess
from datetime import datetime, timedelta
import univention.config_registry
# from samba.netcmd.common import netcmd_get_domain_infos_via_cldap
from samba.dcerpc import nbt
from samba.net import Net
from samba.param import LoadParm
from samba.credentials import Credentials, DONT_USE_KERBEROS
import ldb
import os

def determine_IP_version(address):
	try:
		ip_version = ipaddr.IPAddress(address).version
	except ValueError as ex:
		ip_version = None

	return ip_version

def ldap_uri_for_host(hostname_or_ip):
	ip_version = determine_IP_version(hostname_or_ip)

	if ip_version == 6:
		return "ldap://[%s]" % hostname_or_ip	## For some reason the ldb-clients do not support this currently.
	else:
		return "ldap://%s" % hostname_or_ip

def ping(hostname_or_ip):
	ip_version = determine_IP_version(hostname_or_ip)

	if ip_version == 6:
		cmd = ["fping6", hostname_or_ip]
	else:
		cmd = ["fping", hostname_or_ip]

	try:
		p1 = subprocess.Popen(cmd, close_fds=True, stdout=DEVNULL, stderr=DEVNULL)
		rc = p1.wait()
	except OSError as ex:
		raise TakeoverError(" ".join(cmd) + " failed", ex.args[1])

	if rc != 0:
		raise ComputerUnreachable("Network connection to %s failed" % hostname_or_ip)

def check_ad_present(hostname_or_ip):
	ldap_uri = ldap_uri_for_host(hostname_or_ip)
	try:
		remote_ldb = ldb.Ldb(url=ldap_uri)
	except ldb.LdbError as ex:
		raise ComputerUnreachable("Active Directory services not detected at %s" % hostname_or_ip)

def check_remote_host(hostname_or_ip, ucr):
	ping(hostname_or_ip)

	## To reduce authentication delays first check if an AD is present at all
	check_ad_present(hostname_or_ip)

def time_sync(hostname_or_ip, tolerance=180, critical_difference=360):
	'''Try to sync the local time with an AD server'''

	stdout = ""
	env = os.environ.copy()
	env["LC_ALL"] = "C"
	try:
		p1 = subprocess.Popen(["rdate", "-p", "-n", hostname_or_ip],
			close_fds=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
		stdout, stderr = p1.communicate()
	except OSError as ex:
		log("ERROR: rdate -p -n %s: %s" % (hostname_or_ip, ex.args[1]))
		return False

	if p1.returncode:
		log("ERROR: rdate failed (%d)" % (p1.returncode,))
		return False

	TIME_FORMAT = "%a %b %d %H:%M:%S %Z %Y"
	try:
		remote_datetime = datetime.strptime(stdout.strip(), TIME_FORMAT)
	except ValueError as ex:
		raise timeSyncronizationFailed("AD Server did not return proper time string: %s" % (stdout.strip(),))

	local_datetime = datetime.today()
	delta_t = local_datetime - remote_datetime
	if abs(delta_t) < timedelta(0, tolerance):
		log("INFO: Time difference is less than %d seconds, skipping reset of local time" % (tolerance,))
	elif local_datetime > remote_datetime:
		if abs(delta_t) >= timedelta(0, critical_difference):
			raise manualTimeSyncronizationRequired("Remote clock is behind local clock by more than %s seconds, refusing to turn back time." % critical_difference)
		else:
			log("INFO: Remote clock is behind local clock by more than %s seconds, refusing to turn back time." % (tolerance,))
			return False
	else:
		log("INFO: Syncronizing time to %s" % hostname_or_ip)
		p1 = subprocess.Popen(["rdate", "-s", "-n", hostname_or_ip],
			close_fds=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		stdout, stderr = p1.communicate()
		if p1.returncode:
			log("ERROR: rdate -s -p failed (%d)" % (p1.returncode,))
			raise timeSyncronizationFailed("rdate -s -p failed (%d)" % (p1.returncode,))
	return True

def lookup_adds_dc(hostname_or_ip=None, realm=None, ucr=None):
	'''CLDAP lookup'''

	domain_info = {}

	if not hostname_or_ip and not realm:
		if not ucr:
			ucr = univention.config_registry.ConfigRegistry()
			ucr.load()

		realm = ucr.get("kerberos/realm")

	if not hostname_or_ip and not realm:
		return domain_info

	lp = LoadParm()
	lp.load('/dev/null')

	ip_address = None
	if hostname_or_ip:
		try:
			ipaddr.IPAddress(hostname_or_ip)
			ip_address = hostname_or_ip
		except ValueError as ex:
			pass

		try:
			net = Net(creds=None, lp=lp)
			cldap_res = net.finddc(address=hostname_or_ip,
				flags=nbt.NBT_SERVER_LDAP | nbt.NBT_SERVER_DS | nbt.NBT_SERVER_WRITABLE)
		except RuntimeError as ex:
			raise ComputerUnreachable("Connection to AD Server %s failed" % (hostname_or_ip,), ex.args[0])

	elif realm:
		try:
			net = Net(creds=None, lp=lp)
			cldap_res = net.finddc(domain=realm,
				flags=nbt.NBT_SERVER_LDAP | nbt.NBT_SERVER_DS | nbt.NBT_SERVER_WRITABLE)
			hostname_or_ip = cldap_res.pdc_dns_name
		except RuntimeError as ex:
			raise TakeoverError("No AD Server found for realm %s." % (realm,))

	if not ip_address:
		if cldap_res.pdc_dns_name:
			try:
				p1 = subprocess.Popen(['net', 'lookup', cldap_res.pdc_dns_name],
					close_fds=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
				stdout, stderr = p1.communicate()
				ip_address = stdout.strip()
			except OSError as ex:
				log.warn("WARNING: net lookup %s failed: %s" % (cldap_res.pdc_dns_name, ex.args[1]))

	domain_info = {
		"ad_forrest": cldap_res.forest,
		"ad_domain": cldap_res.dns_domain,
		"ad_netbios_domain": cldap_res.domain_name,
		"ad_hostname": cldap_res.pdc_dns_name,
		"ad_netbios_name": cldap_res.pdc_name,
		"ad_server_site": cldap_res.server_site,
		"ad_client_site": cldap_res.client_site,
		"ad_ip": ip_address,
		}

	return domain_info

class AD_Connection():
	def __init__(self, hostname_or_ip, username, password, lp=None):
		if not lp:
			lp = LoadParm()
			lp.load('/dev/null')

		creds = Credentials()
		# creds.guess(lp)
		creds.set_domain("")
		creds.set_workstation("")
		creds.set_kerberos_state(DONT_USE_KERBEROS)
		creds.set_username(username)
		creds.set_password(password)

		ldap_uri = ldap_uri_for_host(hostname_or_ip)
		try:
			self.samdb = SamDB(ldap_uri, credentials=creds, session_info=system_session(lp), lp=lp)
		except ldb.LdbError as ex:
			raise AuthenticationFailed()

		## Sanity check: are we talking to the AD on the local system?
		ntds_guid = self.samdb.get_ntds_GUID()
		local_ntds_guid = None
		try:
			local_samdb = SamDB("ldap://127.0.0.1", credentials=creds, session_info=system_session(lp), lp=lp)
			local_ntds_guid = local_samdb.get_ntds_GUID()
		except ldb.LdbError as ex:
			pass
		if ntds_guid == local_ntds_guid:
			raise TakeoverError("The selected Active Directory server has the same NTDS GUID as this UCS server.")


		self.domain_dn = self.samdb.get_root_basedn()
		self.domain_sid = None
		msgs = self.samdb.search(base=self.domain_dn, scope=samba.ldb.SCOPE_BASE,
								expression="(objectClass=domain)",
								attrs=["objectSid"])
		if msgs:
			obj = msgs[0]
			self.domain_sid = str(ndr_unpack(security.dom_sid, obj["objectSid"][0]))
		if not self.domain_sid:
			raise TakeoverError("Failed to determine AD domain SID.")

		self.domain_info = lookup_adds_dc(hostname_or_ip)
		self.domain_info['ad_os'] = self.operatingSystem(self.domain_info["ad_netbios_name"])

	def operatingSystem(self, netbios_name):
		msg = self.samdb.search(base=self.samdb.domain_dn(), scope=samba.ldb.SCOPE_SUBTREE,
						expression="(sAMAccountName=%s$)" % netbios_name,
						attrs=["operatingSystem", "operatingSystemVersion", "operatingSystemServicePack"])
		if msg:
			obj = msg[0]
			if "operatingSystem" in obj:
				return obj["operatingSystem"][0]
			else:
				return ""

	def count_objects(self, ignored_users_list=None):

		if not ignored_users_list:
			ignored_users_list = []

		ignored_user_objects = 0
		ad_user_objects = 0
		ad_group_objects = 0
		ad_computer_objects = 0

		# page results
		PAGE_SIZE = 1000
		controls= [ 'paged_results:1:%s' % PAGE_SIZE ]

		## Count user objects
		msgs = self.samdb.search(base=self.domain_dn, scope=samba.ldb.SCOPE_SUBTREE,
								expression="(&(objectCategory=user)(objectClass=user))",
								attrs=["sAMAccountName", "objectSid"], controls=controls)
		for obj in msgs:
			sAMAccountName = obj["sAMAccountName"][0]

			## identify well known names, abstracting from locale
			sambaSID = str(ndr_unpack(security.dom_sid, obj["objectSid"][0]))
			sambaRID = sambaSID[len(self.domain_sid)+1:]
			for (_rid, _name) in univention.lib.s4.well_known_domain_rids.items():
				if _rid == sambaRID:
					log.debug("Found account %s with well known RID %s (%s)" % (sAMAccountName, sambaRID, _name))
					sAMAccountName = _name
					break

			for ignored_account in ignored_users_list:
				if sAMAccountName.lower() == ignored_account.lower():
					ignored_user_objects = ignored_user_objects + 1
					break
			else:
				ad_user_objects = ad_user_objects + 1

		## Count group objects
		msgs = self.samdb.search(base=self.domain_dn, scope=samba.ldb.SCOPE_SUBTREE,
								expression="(objectCategory=group)",
								attrs=["sAMAccountName", "objectSid"], controls=controls)
		for obj in msgs:
			sAMAccountName = obj["sAMAccountName"][0]

			## identify well known names, abstracting from locale
			sambaSID = str(ndr_unpack(security.dom_sid, obj["objectSid"][0]))
			sambaRID = sambaSID[len(self.domain_sid)+1:]
			for (_rid, _name) in univention.lib.s4.well_known_domain_rids.items():
				if _rid == sambaRID:
					log.debug("Found group %s with well known RID %s (%s)" % (sAMAccountName, sambaRID, _name))
					sAMAccountName = _name
					break

			ad_group_objects = ad_group_objects + 1

		## Count computer objects
		msgs = self.samdb.search(base=self.domain_dn, scope=samba.ldb.SCOPE_SUBTREE,
								expression="(objectCategory=computer)",
								attrs=["sAMAccountName", "objectSid"], controls=controls)
		for obj in msgs:
			sAMAccountName = obj["sAMAccountName"][0]

			## identify well known names, abstracting from locale
			sambaSID = str(ndr_unpack(security.dom_sid, obj["objectSid"][0]))
			sambaRID = sambaSID[len(self.domain_sid)+1:]
			for (_rid, _name) in univention.lib.s4.well_known_domain_rids.items():
				if _rid == sambaRID:
					log.debug("Found computer %s with well known RID %s (%s)" % (sAMAccountName, sambaRID, _name))
					sAMAccountName = _name
					break

			else:
				ad_computer_objects = ad_computer_objects + 1

		self.domain_info['users'] = ad_user_objects
		self.domain_info['groups'] = ad_group_objects
		self.domain_info['computers'] = ad_computer_objects

		return self.domain_info

############################# END LIB. HERE COMES THE OLD CODE: ###########################

def _connect_ucs(ucr, binddn=None, bindpwd=None):
	''' Connect to OpenLDAP '''

	if binddn and bindpwd:
		bindpw = bindpwd
	else:
		bindpw_file = ucr.get('connector/ldap/bindpw', '/etc/ldap.secret')
		binddn = ucr.get('connector/ldap/binddn', 'cn=admin,'+ucr['ldap/base'])
		bindpw=open(bindpw_file).read()
		if bindpw[-1] == '\n':
			bindpw=bindpw[0:-1]

	host = ucr.get('connector/ldap/server', ucr.get('ldap/master'))

	try:
		port = int(ucr.get('connector/ldap/port', ucr.get('ldap/master/port')))
	except:
		port = 7389

	lo = univention.admin.uldap.access(host=host, port=port, base=ucr['ldap/base'], binddn=binddn, bindpw=bindpw, start_tls=0, follow_referral=True)

	return lo

def operatingSystem_attribute(ucr, samdb):
	msg = samdb.search(base=samdb.domain_dn(), scope=samba.ldb.SCOPE_SUBTREE,
	                   expression="(sAMAccountName=%s$)" % ucr["hostname"],
	                   attrs=["operatingSystem", "operatingSystemVersion"])
	if msg:
		obj = msg[0]
		if not "operatingSystem" in obj:
			delta = ldb.Message()
			delta.dn = obj.dn
			delta["operatingSystem"] = ldb.MessageElement("Univention Corporate Server", ldb.FLAG_MOD_REPLACE, "operatingSystem")
			samdb.modify(delta)
		if not "operatingSystemVersion" in obj:
			delta = ldb.Message()
			delta.dn = obj.dn
			delta["operatingSystemVersion"] = ldb.MessageElement("3.0", ldb.FLAG_MOD_REPLACE, "operatingSystemVersion")
			samdb.modify(delta)
			
def takeover_DC_Behavior_Version(ucr, remote_samdb, samdb, ad_server_name, sitename):
	## DC Behaviour Version
	msg = remote_samdb.search(base="CN=NTDS Settings,CN=%s,CN=Servers,CN=%s,CN=Sites,CN=Configuration,%s" % (ad_server_name, sitename, samdb.domain_dn()),
	                          scope=samba.ldb.SCOPE_BASE,
	                          attrs=["msDS-HasMasterNCs", "msDS-HasInstantiatedNCs", "msDS-Behavior-Version"])
	if msg:
		obj = msg[0]
		if "msDS-Behavior-Version" in obj:
			delta = ldb.Message()
			delta.dn = ldb.Dn(samdb, dn="CN=NTDS Settings,CN=%s,CN=Servers,CN=%s,CN=Sites,CN=Configuration,%s" % (ucr["hostname"], sitename, samdb.domain_dn()))
			delta["msDS-Behavior-Version"] = ldb.MessageElement(obj["msDS-Behavior-Version"], ldb.FLAG_MOD_REPLACE, "msDS-Behavior-Version")
			samdb.modify(delta)

def takeover_hasInstantiatedNCs(ucr, samdb, ad_server_name, sitename):
	msg = samdb.search(base="CN=NTDS Settings,CN=%s,CN=Servers,CN=%s,CN=Sites,CN=Configuration,%s" % (ad_server_name, sitename, samdb.domain_dn()),
	                   scope=samba.ldb.SCOPE_BASE,
	                   attrs=["msDS-hasMasterNCs", "msDS-HasInstantiatedNCs"])
	partitions=[]
	if msg:
		obj = msg[0]
		delta = ldb.Message()
		delta.dn = ldb.Dn(samdb, dn="CN=NTDS Settings,CN=%s,CN=Servers,CN=%s,CN=Sites,CN=Configuration,%s" % (ucr["hostname"], sitename, samdb.domain_dn()))
		if "msDS-HasInstantiatedNCs" in obj:
			for partitionDN in obj["msDS-HasInstantiatedNCs"]:
				delta[partitionDN] = ldb.MessageElement(obj["msDS-HasInstantiatedNCs"], ldb.FLAG_MOD_REPLACE, "msDS-HasInstantiatedNCs")
		if "msDS-HasInstantiatedNCs" in delta:
			samdb.modify(delta)

		## and note the msDS-hasMasterNCs values for fsmo takeover
		if "msDS-hasMasterNCs" in obj:
			for partitionDN in obj["msDS-hasMasterNCs"]:
				partitions.append(partitionDN)
	return partitions

def takeover_hasMasterNCs(ucr, samdb, sitename, partitions):
	msg = samdb.search(base="CN=NTDS Settings,CN=%s,CN=Servers,CN=%s,CN=Sites,CN=Configuration,%s" % (ucr["hostname"], sitename, samdb.domain_dn()), scope=samba.ldb.SCOPE_BASE, attrs=["hasPartialReplicaNCs", "msDS-hasMasterNCs"])
	if msg:
		obj = msg[0]
		for partition in partitions:
			if "hasPartialReplicaNCs" in obj and partition in obj["hasPartialReplicaNCs"]:
				log.debug("Removing hasPartialReplicaNCs on %s for %s" % (ucr["hostname"], partition) )
				delta = ldb.Message()
				delta.dn = obj.dn
				delta["hasPartialReplicaNCs"] = ldb.MessageElement(partition, ldb.FLAG_MOD_DELETE, "hasPartialReplicaNCs")
				try:
					samdb.modify(delta)
				except:
					log.debug("Failed to remove hasPartialReplicaNCs %s from %s" % (partition, ucr["hostname"]) )
					log.debug("Current NTDS object: %s" % obj )

			if "msDS-hasMasterNCs" in obj and partition in obj["msDS-hasMasterNCs"]:
				log.debug("Naming context %s already registed in msDS-hasMasterNCs for %s" % (partition, ucr["hostname"]) )
			else:
				delta = ldb.Message()
				delta.dn = obj.dn
				delta[partition] = ldb.MessageElement(partition, ldb.FLAG_MOD_ADD, "msDS-hasMasterNCs")
				try:
					samdb.modify(delta)
				except:
					log.debug("Failed to add msDS-hasMasterNCs %s to %s" % (partition, ucr["hostname"]) )
					log.debug("Current NTDS object: %s" % obj )

def let_samba4_manage_etc_krb5_keytab(ucr, secretsdb):

	msg = secretsdb.search(base="cn=Primary Domains", scope=samba.ldb.SCOPE_SUBTREE,
	                       expression="(flatName=%s)" % ucr["windows/domain"],
	                       attrs=["krb5Keytab"])
	if msg:
		obj = msg[0]
		if not "krb5Keytab" in obj or not "/etc/krb5.keytab" in obj["krb5Keytab"]:
			delta = ldb.Message()
			delta.dn = obj.dn
			delta["krb5Keytab"] = ldb.MessageElement("/etc/krb5.keytab", ldb.FLAG_MOD_ADD, "krb5Keytab")
			secretsdb.modify(delta)

def add_servicePrincipals(ucr, secretsdb, spn_list):
	msg = secretsdb.search(base="cn=Primary Domains", scope=samba.ldb.SCOPE_SUBTREE,
	                       expression="(flatName=%s)" % ucr["windows/domain"],
	                       attrs=["servicePrincipalName"])
	if msg:
		obj = msg[0]
		delta = ldb.Message()
		delta.dn = obj.dn
		for spn in spn_list:
			if not "servicePrincipalName" in obj or not spn in obj["servicePrincipalName"]:
				delta[spn] = ldb.MessageElement(spn, ldb.FLAG_MOD_ADD, "servicePrincipalName")
		secretsdb.modify(delta)

def sync_time(server, always_answer_with=None):
	## source: http://code.activestate.com/recipes/117211-simple-very-sntp-client/
	TIME1970 = 2208988800L	# Thanks to F.Lundh

	client = socket.socket( socket.AF_INET, socket.SOCK_DGRAM )
	data = '\x1b' + 47 * '\0'
	client.settimeout(15.0)
	try:
		client.sendto( data, ( server, 123 ))
		data, address = client.recvfrom( 1024 )
		if data:
			log.debug('NTP Response received from server %s' % server )
			t = struct.unpack( '!12I', data )[10]
			t -= TIME1970
			offset = time.time() - t
			log.info("The local clock differs from the clock on %s by about %s seconds." % (server, int(round(offset))) )
			if abs(time.time() - t) < 180:
				log.info("The offest is less than three minutes, that should be good enough for Kerberos." )
			elif time.gmtime(t) >= time.gmtime():
				p = subprocess.Popen(["/bin/date", "-s", time.ctime(t)], stdout=subprocess.PIPE)
				(stdout, stderr) = p.communicate()
				log.info("Setting local time: %s", stdout)
			else:
				msg = []
				msg.append("Error: time %s on server %s is earlier than" % (time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.localtime(t)), server))
				msg.append("       time %s on this server!" % time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.localtime()))
				msg.append("       Refusing to reset time on this server to avoid SSL certificate problems.")
				msg.append("       Please check time on server %s" % server)
				log.info("\n".join(msg))
				sys.exit(1)
	except socket.error:
		msg = []
		msg.append("Warning: Could not retrive time from %s via NTP." % server)
		msg.append("         Possibly a firewall blocks the connection to the NTP port of %s." % server)
		msg.append("")
		msg.append("It is required that the system clocks of both systems agree (consider also differing time zones).")
		log.warn("\n".join(msg))

		if always_answer_with is not None:
			answer = always_answer_with
		else:
			answer = raw_input("Continue takeover anyway? [y/N]: ")

		if not answer.lower() in ('y', 'yes'):
			log.info("Ok, stopping as requested.\n")
			sys.exit(2)
		else:
			log.info("Ok, continuing as requested.\n")


def check_for_phase_II(ucr, lp, ad_server_ip):
	## Check if we are in Phase II and the AD server is already switched off:
	if "hosts/static/%s" % ad_server_ip in ucr:
		ad_server_fqdn, ad_server_name = ucr["hosts/static/%s" % ad_server_ip].split()

		## Check if the AD server is already in the local SAM db
		samdb = SamDB(os.path.join(SAMBA_PRIVATE_DIR, "sam.ldb"), session_info=system_session(lp), lp=lp)
		msgs = samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
					expression="(sAMAccountName=%s$)" % ad_server_name,
					attrs=["objectSid"])
		if msgs:
			return (1, ad_server_fqdn, ad_server_name)
		else:
			return (2, ad_server_fqdn, ad_server_name)

	return (0, None, None)

def sync_position_s4_to_ucs(ucr, udm_type, ucs_object_dn, s4_object_dn):
	rdn_list = ldap.explode_dn(s4_object_dn)
	rdn_list.pop(0)
	new_position = string.replace(','.join(rdn_list).lower(), ucr['connector/s4/ldap/base'].lower(), ucr['ldap/base'].lower())

	rdn_list = ldap.explode_dn(ucs_object_dn)
	rdn_list.pop(0)
	old_position = ','.join(rdn_list)

	if new_position.lower() != old_position.lower():
		run_and_output_to_log(["/usr/sbin/univention-directory-manager", udm_type, "move", "--dn", ucs_object_dn, "--position", new_position], log.debug)

def check_license(lo, dn):
	def mylen(xs):
		if xs is None:
			return 0
		return len(xs)
	v = _license.version
	types = _license.licenses[v]
	if dn is None:
		max = [ _license.licenses[v][type]
			for type in types ]
	else:
		max = [ lo.get(dn)[_license.keys[v][type]][0]
			for type in types ]

	objs = [ lo.searchDn(filter=_license.filters[v][type])
		for type in types ]
	num = [ mylen (obj)
		for obj in objs]
	expired = _license.checkObjectCounts(max, num)
	result = []
	for i in types.keys():
		t = types[i]
		m = max[i]
		n = num[i]
		odn = objs[i]
		if i == License.USERS or i == License.ACCOUNT:
			n -= _license.sysAccountsFound
			if n < 0: n=0
		l = _license.names[v][i]
		if m:
			if i == License.USERS or i == License.ACCOUNT:
				log.debug("check_license for current UCS %s: %s of %s" % (l, n, m))
				log.debug("  %s Systemaccounts are ignored." % _license.sysAccountsFound)
				result.append((l, n, m))
	return result

def count_ad_object_numbers(ucr, remote_samdb, ignored_users_list = []):
	ad_user_accounts = 0
	ignored_accounts = 0

	## Determine AD domain sid (yet unknown in phaseI)
	ad_domainsid = None
	msgs = remote_samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_BASE,
	                           expression="(objectClass=domain)",
	                           attrs=["objectSid"])
	if msgs:
		obj = msgs[0]
		ad_domainsid = str(ndr_unpack(security.dom_sid, obj["objectSid"][0]))
	if not ad_domainsid:
		log.error("Error: Could not determine AD domain SID.")
		sys.exit(1)

	# page results
	PAGE_SIZE = 1000
	controls= [ 'paged_results:1:%s' % PAGE_SIZE ]

	## Count user objects
	msgs = remote_samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
	                           expression="(&(objectClass=user)(!(objectClass=computer))(sAMAccountName=*))",
	                           attrs=["sAMAccountName", "objectSid"], controls=controls)
	for obj in msgs:
		sAMAccountName = obj["sAMAccountName"][0]

		## identify well known names, abstracting from locale
		sambaSID = str(ndr_unpack(security.dom_sid, obj["objectSid"][0]))
		sambaRID = sambaSID[len(ad_domainsid)+1:]
		for (_rid, _name) in univention.lib.s4.well_known_domain_rids.items():
			if _rid == sambaRID:
				log.debug("Found Account %s with well known RID %s (%s)" % (sAMAccountName, sambaRID, _name))
				sAMAccountName = _name
				break

		for ignored_account in ignored_users_list:
			if sAMAccountName.lower() == ignored_account.lower():
				ignored_accounts = ignored_accounts + 1
				break
		else:
			ad_user_accounts = ad_user_accounts + 1

	log.debug("Number of AD user accounts: %s" % ad_user_accounts)
	log.debug("Number of ignored system accounts: %s" % ignored_accounts)

	return ad_user_accounts

def check_ad_object_numbers(ucr, remote_samdb):
	if GPLversion:
		return GPLversion

	ad_user_accounts = count_ad_object_numbers(ucr, remote_samdb, _license.sysAccountNames)

	binddn = ucr['ldap/hostdn']
	with open('/etc/machine.secret', 'r') as pwfile:
		bindpw = pwfile.readline().strip()

	try:
		lo = univention.admin.uldap.access(host = ucr['ldap/master'],
						   port = int(ucr.get('ldap/master/port', '7389')),
						   base = ucr['ldap/base'],
						   binddn = binddn,
						   bindpw = bindpw)
	except uexceptions.authFail:
		raise UsageError, "License check with machine credentials failed."

	try:
		_license.init_select(lo, 'admin')
		check_array = check_license(lo, None)
	except uexceptions.base:
		dns = find_licenses(lo, baseDN, 'admin')
		dn, expired = choose_license(lo, dns)
		check_array = check_license(lo, dn)


	## some name translation
	object_displayname_for_licensetype= {'Accounts': 'user', 'Users': 'user'}
	ad_object_count_for_licensetype = {'Accounts': ad_user_accounts, 'Users': ad_user_accounts}
	print

	license_sufficient = True
	for object_type, num, max in check_array:
		object_displayname = object_displayname_for_licensetype.get(object_type, object_type)
		log.info("Found %s %s objects on the remote server." % (ad_object_count_for_licensetype[object_type], object_displayname))
		sum = num + ad_object_count_for_licensetype[object_type]
		if _license.compare(sum, max) > 0:
			license_sufficient = False
			log.warn("Number of %s objects after takeover would be %s. This would exceed the number of licensed objects (%s)." % (object_displayname, sum, max))
	return license_sufficient

def print_progress(output_string = ".", stream = sys.stdout):
	stream.write(output_string)
	stream.flush()

def get_stable_last_id(progress_function = None, max_time=20):
	last_id_cached_value = None
	static_count = 0
	t = t_0 = time.time()
	while static_count < 3:
		if last_id_cached_value:
			time.sleep (0.1)
		with file("/var/lib/univention-ldap/last_id") as f:
			last_id = f.read().strip()
		if last_id != last_id_cached_value:
			static_count = 0
			last_id_cached_value = last_id
		elif last_id:
			static_count = static_count + 1
		delta_t = time.time() - t
		t = t + delta_t
		if t - t_0 > max_time:
			print
			return None
		if progress_function and delta_t >= 1:
			progress_function()
	return last_id

def wait_for_listener_replication(progress_function = None, max_time=None):
	notifier_id_cached_value = None
	static_count = 0
	t_last_feedback = t_1 = t_0 = time.time()
	while static_count < 3:
		if notifier_id_cached_value:
			time.sleep(0.7)
		last_id = get_stable_last_id(progress_function)
		with file("/var/lib/univention-directory-listener/notifier_id") as f:
			notifier_id = f.read().strip()
		if not last_id:
			return False
		elif last_id != notifier_id:
			static_count = 0
			notifier_id_cached_value = notifier_id
		else:
			static_count = static_count + 1

		delta_t = time.time() - t_1
		t_1 = t_1 + delta_t
		if max_time:
			if t_1 - t_0 > max_time:
				print
				sys.stdout.flush()
				log.debug("Warning: Listener ID not yet up to date (last_id=%s, listener ID=%s). Waited for about %s seconds." % (last_id, notifier_id, int(round(t_1 - t_0))))
				return False
		delta_t_last_feedback = t_1 - t_last_feedback
		if progress_function and delta_t_last_feedback >= 1:
			t_last_feedback = t_last_feedback + delta_t_last_feedback
			progress_function()

	if progress_function:
		print
		sys.stdout.flush()
	return True

def wait_for_s4_connector_replication(ucr, lp, progress_function = None, max_time=None):
	
	conn = sqlite3.connect('/etc/univention/connector/s4internal.sqlite')
	c = conn.cursor()

	static_count = 0
	cache_S4_rejects = None
	t_last_feedback = t_1 = t_0 = time.time()

	ucr.load()	## load current values
	connector_s4_poll_sleep = int(ucr.get("connector/s4/poll/sleep", "5"))
	connector_s4_retryrejected = int(ucr.get("connector/s4/retryrejected", "10"))
	required_static_count = 5 * connector_s4_retryrejected

	if max_time == "scale10":
		max_time = 10 * connector_s4_retryrejected * connector_s4_poll_sleep
		log.info("Waiting for S4 Connector sync (max. %s seconds)" % int(round(max_time)))

	highestCommittedUSN = -1
	lastUSN = -1
	while static_count < required_static_count:
		time.sleep(connector_s4_poll_sleep)

		previous_highestCommittedUSN = highestCommittedUSN
		samdb = SamDB(os.path.join(SAMBA_PRIVATE_DIR, "sam.ldb"), session_info=system_session(lp), lp=lp)
		msgs = samdb.search(base="", scope=samba.ldb.SCOPE_BASE, attrs=["highestCommittedUSN"])
		highestCommittedUSN = msgs[0]["highestCommittedUSN"][0]

		previous_lastUSN = lastUSN
		c.execute('select value from S4 where key=="lastUSN"')
		conn.commit()
		lastUSN = c.fetchone()[0]

		if not ( lastUSN == highestCommittedUSN and lastUSN == previous_lastUSN and highestCommittedUSN == previous_highestCommittedUSN ):
			static_count = 0
		else:
			static_count = static_count + 1

		delta_t = time.time() - t_1
		t_1 = t_1 + delta_t
		if max_time:
			if t_1 - t_0 > max_time:
				print
				sys.stdout.flush()
				log.debug("Warning: S4 Connector synchronization did not finish yet. Waited for about %s seconds." % (int(round(t_1 - t_0),)))
				conn.close()
				return False
		delta_t_last_feedback = t_1 - t_last_feedback
		if progress_function and delta_t_last_feedback >= 10:
			t_last_feedback = t_last_feedback + delta_t_last_feedback
			progress_function()

	if progress_function:
		print
		sys.stdout.flush()
	conn.close()
	return True

def check_samba4_started():
	attempt = 1
	for i in xrange(5):
		time.sleep(1)
		p = subprocess.Popen(["pgrep", "-cxf", "/usr/sbin/samba -D"], stdout=subprocess.PIPE)
		(stdout, stderr) = p.communicate()
		if int(stdout) > 1:
			break
	else:
		if int(stdout) == 1:
			attempt = 2
			run_and_output_to_log(["/etc/init.d/samba4", "stop"], log.debug)
			run_and_output_to_log(["pkill", "-9", "-xf", "/usr/sbin/samba -D"], log.debug)
			p = subprocess.Popen(["pgrep", "-cxf", "/usr/sbin/samba -D"], stdout=subprocess.PIPE)
			(stdout, stderr) = p.communicate()
			if int(stdout) > 0:
				log.debug("ERROR: Stray Processes:", int(stdout))
				run_and_output_to_log(["pkill", "-9", "-xf", "/usr/sbin/samba -D"], log.debug)
			run_and_output_to_log(["/etc/init.d/samba4", "start"], log.debug)
			## fallback
			time.sleep(2)
			p = subprocess.Popen(["pgrep", "-cxf", "/usr/sbin/samba -D"], stdout=subprocess.PIPE)
			(stdout, stderr) = p.communicate()
			if int(stdout) == 1:
				attempt = 3
				log.debug("ERROR: Stray Processes:", int(stdout))
				run_and_output_to_log(["pkill", "-9", "-xf", "/usr/sbin/samba -D"], log.debug)
				run_and_output_to_log(["/etc/init.d/samba4", "start"], log.debug)
				## and log
				time.sleep(2)
				p = subprocess.Popen(["pgrep", "-cxf", "/usr/sbin/samba -D"], stdout=subprocess.PIPE)
				(stdout, stderr) = p.communicate()
		log.debug("Number of Samba 4 processes after %s start/restart attempts: %s" % (attempt, stdout))

def run_and_output_to_log(cmd, log_function, print_commandline = True):
	if print_commandline and log_function == log.debug:
		log_function("Calling: %s" % ' '.join(cmd))
	p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
	while p.poll() == None:
		log_line = p.stdout.readline().rstrip()
		if log_line:
			log_function(log_line)
	return p.returncode

def cleanup_failed_join(ucr, ad_server_ip):
	ucr.load()

	run_and_output_to_log(["univention-config-registry", "unset", "hosts/static/%s" % (ad_server_ip,)], log.debug)

	## Restore backup Samba directory
	backup_samba_dir = "%s.before-ad-takeover" % SAMBA_PRIVATE_DIR
	if os.path.exists(backup_samba_dir):
		shutil.rmtree(SAMBA_PRIVATE_DIR)
		os.rename(backup_samba_dir, SAMBA_PRIVATE_DIR)
		# shutil.copytree(backup_samba_dir, SAMBA_PRIVATE_DIR, symlinks=True)

	## Start Samba again
	run_and_output_to_log(["/etc/init.d/samba4", "start"], log.debug)

	## Start S4 Connector again
	run_and_output_to_log(["/etc/init.d/univention-s4-connector", "start"], log.debug)

	## Adjust some UCR settings back
	if "nameserver1/local" in ucr:
		nameserver1_orig = ucr["nameserver1/local"]
		run_and_output_to_log(["univention-config-registry", "set", "nameserver1=%s" % nameserver1_orig], log.debug)
		## unset temporary variable
		run_and_output_to_log(["univention-config-registry", "unset", "nameserver1/local"], log.debug)
	else:
		msg=[]
		msg.append("Warning: Weird, unable to determine previous nameserver1...")
		msg.append("         Using localhost as fallback, probably that's the right thing to do.")
		log.warn("\n".join(msg))
		run_and_output_to_log(["univention-config-registry", "set", "nameserver1=127.0.0.1"], log.debug)

	## Use Samba4 as DNS backend
	run_and_output_to_log(["univention-config-registry", "set", "dns/backend=samba4"], log.debug)


	## Restart bind9 to use the OpenLDAP backend, just to be sure
	run_and_output_to_log(["/etc/init.d/bind9", "restart"], log.debug)

	## Start the NSCD again
	run_and_output_to_log(["/etc/init.d/nscd", "restart"], log.debug)


class UserRenameHandler:
	''' Provides methods for renaming users in UDM
	'''

	def __init__(self, lo):
		self.lo = lo
		self.position = univention.admin.uldap.position(self.lo.base)

		self.module_users_user = udm_modules.get('users/user')
		udm_modules.init(self.lo, self.position, self.module_users_user)

	def udm_rename_ucs_user(self, userdn, new_name):
		try:
			user = self.module_users_user.object(None, self.lo, self.position, userdn)
			user.open()
		except uexceptions.ldapError as exc:
			log.debug("Opening user '%s' failed: %s." % (userdn, exc,))

		try:
			log.debug("Renaming '%s' to '%s' in UCS LDAP." % (user.dn, new_name))
			user['username'] = new_name
			user.modify()
		except uexceptions.ldapError as exc:
			log.debug("Renaming of user '%s' failed: %s." % (userdn, exc,))
			return

		dnparts = ldap.explode_dn(userdn)
		rdn = dnparts[0].split('=', 1)
		dnparts[0] = '='.join((rdn[0], new_name))
		new_userdn = ",".join(dnparts)

		return new_userdn

	def rename_ucs_user(self, ucsldap_object_name, ad_object_name):
		userdns = self.lo.searchDn(
			filter="(&(objectClass=sambaSamAccount)(uid=%s))" % (ucsldap_object_name, ),
			base=self.lo.base)

		if len(userdns) > 1:
			log.warn("Warning: Found more than one Samba user with name '%s' in UCS LDAP." %
				(ucsldap_object_name,))

		for userdn in userdns:
			new_userdn = self.udm_rename_ucs_user(userdn, ad_object_name)


class GroupRenameHandler:
	''' Provides methods for renaming groups in UDM
	'''

	_SETTINGS_DEFAULT_UDM_PROPERTIES = (
		"defaultGroup",
		"defaultComputerGroup",
		"defaultDomainControllerGroup",
		"defaultDomainControllerMBGroup",
		"defaultClientGroup",
		"defaultMemberServerGroup",
	)

	def __init__(self, lo):
		self.lo = lo
		self.position = univention.admin.uldap.position(self.lo.base)

		self.module_groups_group = udm_modules.get('groups/group')
		udm_modules.init(self.lo, self.position, self.module_groups_group)

		self.module_settings_default = udm_modules.get('settings/default')
		udm_modules.init(self.lo, self.position, self.module_settings_default)

	def udm_rename_ucs_group(self, groupdn, new_name):
		try:
			group = self.module_groups_group.object(None, self.lo, self.position, groupdn)
			group.open()
		except uexceptions.ldapError as exc:
			log.debug("Opening group '%s' failed: %s." % (groupdn, exc,))

		try:
			log.debug("Renaming '%s' to '%s' in UCS LDAP." % (group.dn, new_name))
			group['name'] = new_name
			group.modify()
		except uexceptions.ldapError as exc:
			log.debug("Renaming of group '%s' failed: %s." % (groupdn, exc,))
			return

		dnparts = ldap.explode_dn(groupdn)
		rdn = dnparts[0].split('=', 1)
		dnparts[0] = '='.join((rdn[0], new_name))
		new_groupdn = ",".join(dnparts)

		return new_groupdn

	def udm_rename_ucs_defaultGroup(self, groupdn, new_groupdn):
		if not new_groupdn:
			return

		if not groupdn:
			return

		lookup_filter = udm_filter.conjunction('|', [
			udm_filter.expression(propertyname, groupdn)
			for propertyname in GroupRenameHandler._SETTINGS_DEFAULT_UDM_PROPERTIES
		])

		referring_objects = udm_modules.lookup('settings/default',
			None, self.lo, scope = 'sub', base = self.lo.base, filter = lookup_filter)
		for referring_object in referring_objects:
			changed = False
			for propertyname in GroupRenameHandler._SETTINGS_DEFAULT_UDM_PROPERTIES:
				if groupdn in referring_object[propertyname]:
					referring_object[propertyname] = new_groupdn
					changed = True
			if changed:
				log.debug("Modifying '%s' in UCS LDAP." % (referring_object.dn,))
				referring_object.modify()


	def rename_ucs_group(self, ucsldap_object_name, ad_object_name):
		groupdns = self.lo.searchDn(
			filter="(&(objectClass=sambaGroupMapping)(cn=%s))" % (ucsldap_object_name, ),
			base=self.lo.base)

		if len(groupdns) > 1:
			log.warn("Warning: Found more than one Samba group with name '%s' in UCS LDAP." %
				(ucsldap_object_name,))

		for groupdn in groupdns:
			new_groupdn = self.udm_rename_ucs_group(groupdn, ad_object_name)
			self.udm_rename_ucs_defaultGroup(groupdn, new_groupdn)


def run_phaseI(ucr, lp, opts, args, parser, creds, always_answer_with=None):

	ad_server_name = None
	if len(args) > 0:
		ad_server_ip = args[0]
	if len(args) == 2:
		ad_server_name = args[1]
	elif len(args) != 1:
		parser.print_usage()
		sys.exit(1)

	local_fqdn = '.'.join((ucr["hostname"], ucr["domainname"]))

	### First plausibility checks

	## 1.a Check that local domainname matches kerberos realm
	if ucr["domainname"].lower() != ucr["kerberos/realm"].lower():
		log.error("Mismatching DNS domain and kerberos realm. Please reinstall the server with the same Domain as your AD.")
		sys.exit(1)

	## 1.b ping the given AD server IP
	ad_server_ip_addr = get_ip_addr(ad_server_ip)

	ad_server_ldap_uri = get_ad_server_ldap_uri(ad_server_ip_addr)

	print "Pinging AD IP %s: " % ad_server_ip,
	if ad_server_ip_addr.version == 4:
		cmd = ["fping", ad_server_ip]
	else:
		cmd = ["fping6", ad_server_ip]
	p1 = subprocess.Popen(cmd, stdout=DEVNULL, stderr=DEVNULL)
	rc= p1.poll()
	while rc is None:
		time.sleep(1)
		print_progress()
		rc= p1.poll()
	print
	if rc != 0:
		## Check if we are in Phase II and the AD server is already switched off:
		(rc, tmp_fqdn, tmp_name) = check_for_phase_II(ucr, lp, ad_server_ip)
		if rc == 0:
			log.error("Error: Server IP %s not reachable." % ad_server_ip)
		elif rc == 1:
			msg = []
			msg.append("Note: The AD Server IP %s not reachable." % ad_server_ip)
			msg.append("Error: But found the AD DC %s account already in the Samba 4 SAM database." % tmp_name)
			msg.append("       Looks like it was switched off to finalize the takeover?")
			msg.append("       If this is true, then restart this script with option --fsmo-takeover to finish the takeover.")
			log.error("\n".join(msg))
		elif rc == 2:
			msg = []
			msg.append("Error: Server IP %s not reachable." % ad_server_ip)
			msg.append("Error: It seems that this script was run once already for the first takeover step,")
			msg.append("       but the server %s cannot be found in the local Samba SAM database." % tmp_name)
			msg.append("       Don't know how to continue, giving up at this point.")
			log.error("\n".join(msg))
		sys.exit(1)
	else:
		## Check, if we can reverse resolve ad_server_ip locally
		cmd = ["dig", "@localhost", "PTR", mapSubnet(ad_server_ip), "+short"]
		p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		(stdout, stderr) = p1.communicate()
		log.debug("output of %s\nstdout: '%s'\nstderr: '%s'" % (cmd, stdout.rstrip(), stderr.rstrip()))
		if stdout.rstrip() == "%s." % local_fqdn:
			msg = []
			msg.append("Error: The given IP %s is registered to the local system %s." % (ad_server_ip, local_fqdn))
			msg.append("       Possibly the AD takeover procedure already finished sucessfully.")
			log.error("\n".join(msg))
			sys.exit(1)

		log.info("Ok, Server IP %s is online." % ad_server_ip)

	## Check if the script was run before
	backup_samba_dir = "%s.before-ad-takeover" % SAMBA_PRIVATE_DIR
	if os.path.exists(backup_samba_dir):
		msg=[]
		msg.append("Error: Found Samba backup of a previous run of univention-ad-takeover.")
		msg.append("       The AD takeover procedure should only be completed once.")
		msg.append("       Move the directory %s to a safe place to continue anyway." % backup_samba_dir)
		log.error("\n".join(msg))
		sys.exit(1)

	## 1.c Check, if the given AD Credentials work
	if creds.get_username() == "root":
		creds.set_username("Administrator")
	ad_join_user = creds.get_username()
	ad_join_password = creds.get_password()

	try:
		remote_samdb = get_remote_samdb(ad_server_ldap_uri, creds, lp)
	except:
		msg = []
		msg.append("Error: Cannot connect to %s as %s\%s" % (ad_server_ldap_uri, creds.get_domain(), ad_join_user))
		msg.append("       Please check the given credentials.")
		log.error("\n".join(msg))
		sys.exit(1)

	p = subprocess.Popen(["smbclient", "//%s/sysvol" % ad_server_ip, "-U%s%%%s" % (ad_join_user, ad_join_password), '-c', 'quit'], stdout=DEVNULL, stderr=DEVNULL)
	if p.wait() != 0:
		msg = []
		msg.append("Error: Cannot connect to //%s/sysvol as %s\%s" % (ad_server_ip, creds.get_domain(), ad_join_user))
		msg.append("       Please check the given credentials.")
		log.error("\n".join(msg))
		sys.exit(1)

	## 1.d Check, if a AD DNS domain is given and if it matches the local one
	ad_server_fqdn = None
	if ad_server_name:
		char_idx = ad_server_name.find(".")
		if char_idx == -1:
			ad_server_fqdn = "%s.%s" % (ad_server_name, ucr["domainname"])
		else:
			ad_server_fqdn = ad_server_name

		## Check, if there is a DNS Server running at ad_server_ip which is able to resolve ad_server_fqdn
		cmd = ["dig", "@%s" % ad_server_ip, ad_server_fqdn, "+short"]
		p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		(stdout, stderr) = p1.communicate()
		log.debug("output of %s\nstdout: '%s'\nstderr: '%s'" % (cmd, stdout.rstrip(), stderr.rstrip()))
		if not stdout.strip('\n') == ad_server_ip:
			msg = []
			msg.append("Error: Cannot resolve DNS name %s using DNS server %s" % (ad_server_fqdn, ad_server_ip))
			msg.append("       Please check DNS name, IP or configuration.")
			log.error("\n".join(msg))
			sys.exit(1)
	else:
		try:
			cmd = ["dig", "@%s" % ad_server_ip, "SRV", "_kerberos._tcp.dc._msdcs.%s" % ucr["domainname"], "+short"]
			p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
			(stdout, stderr) = p1.communicate()
		except:
			msg = []
			msg.append("Error: DNS lookup for DC at IP %s failed." % ad_server_ip)
			msg.append("Please retry by passing the AD server name as additional command line argument.")
			log.error("\n".join(msg))
			sys.exit(1)

		log.debug("output of %s\nstdout: '%s'\nstderr: '%s'" % (cmd, stdout.rstrip(), stderr.rstrip()))
		lines = stdout.strip('\n').split('\n')
		lines = [line for line in lines if not line.startswith(';')]
		if len(lines) == 0:
			log.error("Error: DNS lookup for DC at IP %s failed: %s" % (ad_server_ip, stdout))
			try:
				msgs = remote_samdb.search(base="", scope=samba.ldb.SCOPE_BASE,
											attrs=["defaultNamingContext"])
				if msgs and "defaultNamingContext" in msgs[0]:
					obj = msgs[0]
					remote_domainname = obj["defaultNamingContext"][0].replace('DC=','',1).replace(',DC=','.')
					if remote_domainname != ucr["domainname"]:
						msg = []
						msg.append("Local machine does not seem to be installed with the same domainname as the AD domain!")
						msg.append("Local domain: %s" % ucr["domainname"])
						msg.append("Remote domain lookup returned: %s" % remote_domainname)
						log.error("\n".join(msg))
					else:
						log.error("Please retry by passing the AD server name as additional command line argument.")
				else:
					log.debug("Remote domain lookup in AD failed as well.")
					log.error("Please retry by passing the AD server name as additional command line argument.")
				sys.exit(1)
			except ldb.LdbError:
				msg = []
				msg.append("Local machine does not seem to be installed with the same domainname as the AD domain!")
				msg.append("Local domain: %s" % ucr["domainname"])
				msg.append("Lookup of remote domain failed.")
				log.error("\n".join(msg))
				sys.exit(1)
		elif len(lines) > 1:
			log.warn("Warning: Multiple DCs registered for DNS SRV record _kerberos._tcp.dc._msdcs.%s"  % ucr["domainname"])
			local_fqdn_found_in_AD_SRV = False
			for line in lines:
				tmp_fqdn = line.split()[3].rstrip('.')
				if tmp_fqdn == local_fqdn:
					log.warn("Warning: This UCS server is already registered as DC at the DNS server %s" % ad_server_ip)
					local_fqdn_found_in_AD_SRV = True
					continue
				else:
					cmd = ["dig", "@%s" % ad_server_ip, tmp_fqdn, "+short"]
					p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
					(stdout, stderr) = p1.communicate()
					log.debug("output of %s\nstdout: '%s'\nstderr: '%s'" % (cmd, stdout.rstrip(), stderr.rstrip()))
					if stdout.strip('\n') == ad_server_ip:
						if not ad_server_fqdn:
							ad_server_fqdn = tmp_fqdn
						else:
							msg = []
							msg.append("Error: More than one of the registered DC FQDNs matches the given AD server IP:")
							msg.append("       %s" % ad_server_fqdn)
							msg.append("       %s" % tmp_fqdn)
							msg.append("Error: Failed to determine DC for IP %s." % ad_server_ip)
							msg.append("Please retry by passing the AD server name as additional command line argument.")
							log.error("\n".join(msg))
							sys.exit(1)

			if not ad_server_fqdn:
				msg = []
				msg.append("Error: Failed to determine DC for IP %s" % ad_server_ip)
				msg.append("Please retry by passing the AD server name as additional command line argument.")
				log.error("\n".join(msg))
				sys.exit(1)
			else:
				log.info("Sucessfull determined AD DC FQDN %s for given IP %s" % (ad_server_fqdn, ad_server_ip))

			if local_fqdn_found_in_AD_SRV and ad_server_fqdn:
				## Check if we are in Phase II and the AD server is already switched off:
				(rc, tmp_fqdn, tmp_name) = check_for_phase_II(ucr, lp, ad_server_ip)
				if rc == 0:
					pass
				elif rc == 1:
					msg = []
					msg.append("Error: Account for the AD DC %s is already the Samba 4 SAM database." % tmp_name)
					msg.append("       It seems that this script was run once already for the first takeover step.")
					msg.append("       If this is true, then go over to the AD DC to takeover the SYSVOL,")
					msg.append("       and switch it off before restarting this script with option --fsmo-takeover to finish the takeover.")
					log.error("\n".join(msg))
					sys.exit(1)
				elif rc == 2:
					msg = []
					msg.append("Error: It seems that this script was run once already for the first takeover step,")
					msg.append("       but the server %s cannot be found in the local Samba SAM database." % tmp_name)
					msg.append("       Don't know how to continue, giving up at this point.")
					log.error("\n".join(msg))
					sys.exit(1)

		else:
			## OK, we have a unique match
			try:
				ad_server_fqdn = lines[0].split()[3]
				ad_server_fqdn = ad_server_fqdn.rstrip('.')
				log.info("Sucessfull determined AD DC FQDN %s for given IP %s" % (ad_server_fqdn, ad_server_ip))
			except:
				msg = []
				msg.append("Error: Parsing of DNS SRV record failed: '%s'." % lines[0].rstrip())
				msg.append("Please retry by passing the AD server name as additional command line argument.")
				log.error("\n".join(msg))
				sys.exit(1)

	char_idx = ad_server_fqdn.find(".")
	if char_idx == -1:
		msg = []
		msg.append("Error: AD server did not return FQDN for IP %s" % ad_server_ip)
		msg.append("Please retry by passing the AD server name as additional command line argument.")
		log.error("\n".join(msg))
		sys.exit(1)
	elif not ad_server_fqdn[char_idx+1:] == ucr["domainname"]:
		log.error("Error: local DNS domain %s does not match AD server DNS domain." % ucr["domainname"])
		sys.exit(1)
	else:
		ad_server_name = ad_server_fqdn.split('.', 1)[0]
	
	## 2. Determine Site of given server, important for locale-dependend names like "Standardname-des-ersten-Standorts"
	sitename = None
	msgs = remote_samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
	                           expression="(sAMAccountName=%s$)" % ad_server_name,
	                           attrs=["serverReferenceBL"])
	if msgs:
		obj = msgs[0]
		serverReferenceBL = obj["serverReferenceBL"][0]
		serverReferenceBL_RDNs = ldap.explode_dn(serverReferenceBL)
		serverReferenceBL_RDNs.reverse()
		config_partition_index = None
		site_container_index = None
		for i in xrange(len(serverReferenceBL_RDNs)):
			if site_container_index:
				sitename = serverReferenceBL_RDNs[i].split('=', 1)[1]
				break
			elif config_partition_index and serverReferenceBL_RDNs[i] == "CN=Sites":
				site_container_index = i
			elif not site_container_index and serverReferenceBL_RDNs[i] == "CN=Configuration":
				config_partition_index = i
			i = i+1
		log.info("Located server %s at AD site %s in AD SAM database." % (ad_server_fqdn, sitename))

	## 3. Essential: Sync the time
	sync_time(ad_server_ip, always_answer_with=always_answer_with)

	## 4. Check AD Object Numbers
	if not check_ad_object_numbers(ucr, remote_samdb):

		log.warn("\nAfter the takeover the Univention Directory Manager will only allow removal of objects until the license restrictions are met.")
		if always_answer_with is not None:
			answer = always_answer_with
		else:
			answer = raw_input("Continue takeover anyway? [y/N]: ")

		if not answer.lower() in ('y', 'yes'):
			log.info("Ok, stopping as requested.\n")
			sys.exit(2)
		else:
			log.info("Ok, continuing as requested.\n")

	### Phase I.a: Join to AD

	log.info("Starting phase I of the takeover process.")

	## OK, we are quite shure that we have the basics right, note the AD server IP and FQDN in UCR for phase II
	run_and_output_to_log(["univention-config-registry", "set", "hosts/static/%s=%s %s" % (ad_server_ip, ad_server_fqdn, ad_server_name)], log.debug)

	## Stop the S4 Connector for phase I
	run_and_output_to_log(["/etc/init.d/univention-s4-connector", "stop"], log.debug)

	## Stop Samba
	run_and_output_to_log(["/etc/init.d/samba4", "stop"], log.debug)

	## Move current Samba directory out of the way
	if os.path.exists(SAMBA_PRIVATE_DIR):
		if not os.path.exists(backup_samba_dir):
			os.rename(SAMBA_PRIVATE_DIR, backup_samba_dir)
			os.makedirs(SAMBA_PRIVATE_DIR)
		else:
			shutil.rmtree(SAMBA_PRIVATE_DIR)
			os.mkdir(SAMBA_PRIVATE_DIR)

	## Adjust some UCR settings
	if "nameserver1/local" in ucr:
		nameserver1_orig = ucr["nameserver1/local"]
	else:
		nameserver1_orig = ucr["nameserver1"]
		run_and_output_to_log(["univention-config-registry", "set",
		                         "nameserver1/local=%s" % nameserver1_orig,
		                         "nameserver1=%s" % ad_server_ip,
		                         "directory/manager/web/modules/users/user/properties/username/syntax=string",
		                         "directory/manager/web/modules/groups/group/properties/name/syntax=string",
		                         "dns/backend=ldap"], log.debug)

	ucr.load()
	univention.admin.configRegistry.load()	## otherwise the modules do not use the new syntax

	## Stop the NSCD
	run_and_output_to_log(["/etc/init.d/nscd", "stop"], log.debug)

	## Restart bind9 to use the OpenLDAP backend, just to be sure
	run_and_output_to_log(["/etc/init.d/bind9", "restart"], log.debug)

	## Get machine credentials
	try:
		machine_secret = open('/etc/machine.secret','r').read().strip()
	except IOError, e:
		log.error("Error: Could not read machine credentials: %s" % str(e))
		sys.exit(1)

	## Join into the domain
	if sitename:
		log.info("Starting Samba domain join.")
		t = t_0 = time.time()
		p = subprocess.Popen(["samba-tool", "domain", "join", ucr["domainname"], "DC", "-U%s%%%s" % (ad_join_user, ad_join_password), "--realm=%s" % ucr["kerberos/realm"], "--machinepass=%s" % machine_secret, "--server=%s" % ad_server_fqdn, "--site=%s" % sitename], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
		# formatter_orig = log.handlers[0].formatter
		# log.handlers[0].setFormatter(logging.Formatter('%(message)s'))
		while p.poll() == None:
			log_line = p.stdout.readline().rstrip()
			if log_line:
				log.debug(log_line)
			t1 = time.time()
			if	t1 - t >=1:
				print_progress()
				t = t1
		print
		sys.stdout.flush()
		# log.handlers[0].setFormatter(formatter_orig)
		if p.returncode == 0:
			log.info("Samba domain join successful.")
		else:
			msg = []
			msg.append("Error: Samba domain join failed.")
			msg.append("       See %s for details." % LOGFILE_NAME)
			msg.append("")
			msg.append("Cleaning up failed join:")
			log.error("\n".join(msg))
			cleanup_failed_join(ucr, ad_server_ip)
			log.error("Cleanup finished.")
			sys.exit(1)
	else:
		log.error("Error: Cannot determine AD site for server %s" % ad_server_fqdn)
		sys.exit(1)

	## create backup dir
	if not os.path.exists(BACKUP_DIR):
		os.mkdir(BACKUP_DIR)
	elif not os.path.isdir(BACKUP_DIR):
		log.debug('%s is a file, renaming to %s.bak' % (BACKUP_DIR, BACKUP_DIR))
		os.rename(BACKUP_DIR, "%s.bak" % BACKUP_DIR)
		os.mkdir(BACKUP_DIR)

	## Fix some attributes in local SamDB
	ad_domainsid = None
	samdb = SamDB(os.path.join(SAMBA_PRIVATE_DIR, "sam.ldb"), session_info=system_session(lp), lp=lp)
	msgs = samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_BASE,
	                    expression="(objectClass=domain)",
	                    attrs=["objectSid"])
	if msgs:
		obj = msgs[0]
		ad_domainsid = str(ndr_unpack(security.dom_sid, obj["objectSid"][0]))
	if not ad_domainsid:
		log.error("Error: Could not determine new domain SID.")
		sys.exit(1)

	old_domainsid = None
	lo = _connect_ucs(ucr)
	ldap_result = lo.search(filter="(&(objectClass=sambaDomain)(sambaDomainName=%s))" % ucr["windows/domain"], attr=["sambaSID"])
	if len(ldap_result) == 1:
		ucs_object_dn = ldap_result[0][0]

		old_ucs_sambasid_backup_file = "%s/old_sambasid" % BACKUP_DIR
		if os.path.exists(old_ucs_sambasid_backup_file):
			f = open(old_ucs_sambasid_backup_file, 'r')
			old_domainsid = f.read()
			f.close()
		else:
			old_domainsid = ldap_result[0][1]["sambaSID"][0]
			f = open(old_ucs_sambasid_backup_file, 'w')
			f.write("%s" % old_domainsid)
			f.close()
	elif len(ldap_result) > 0:
		log.error('Error: Found more than one sambaDomain object with sambaDomainName=%s' % ucr["windows/domain"])
		# FIXME: probably sys.exit()?
	else:
		log.error('Error: Did not find a sambaDomain object with sambaDomainName=%s' % ucr["windows/domain"])
		# FIXME: probably sys.exit()?

	log.debug("Replacing old UCS sambaSID (%s) by AD domain SID (%s)." % (old_domainsid, ad_domainsid))
	if old_domainsid != ad_domainsid:
		ml = [("sambaSID", old_domainsid, ad_domainsid)]
		lo.modify(ucs_object_dn, ml)

	operatingSystem_attribute(ucr, samdb)
	takeover_DC_Behavior_Version(ucr, remote_samdb, samdb, ad_server_name, sitename)

	## Fix some attributes in SecretsDB
	secretsdb = samba.Ldb(os.path.join(SAMBA_PRIVATE_DIR, "secrets.ldb"), session_info=system_session(lp), lp=lp)

	let_samba4_manage_etc_krb5_keytab(ucr, secretsdb)
	fqdn = "%s.%s" % (ucr['hostname'], ucr['domainname'])
	spn_list = ("host/%s" % fqdn, "ldap/%s" % fqdn)
	add_servicePrincipals(ucr, secretsdb, spn_list)

	## Set Samba domain password settings. Note: rotation of passwords will only work with UCS 3.1, so max password age must be disabled for now.
	# run_and_output_to_log(["samba-tool", "domain", "passwordsettings", "set", "--history-length=3", "--min-pwd-age=0", "--max-pwd-age=0"], log.debug)
	## Avoid password expiry for DCs:
	run_and_output_to_log(["samba-tool", "user", "setexpiry", "--noexpiry", "--filter", '(&(objectclass=computer)(serverReferenceBL=*))'], log.debug)
	time.sleep(2)

	## Disable replication from Samba4 to AD
	run_and_output_to_log(["univention-config-registry", "set", "samba4/dcerpc/endpoint/drsuapi=false"], log.debug)

	## Temporary workaround, until univention-samba4 smb.conf template supports samba4/service/drsuapi
	run_and_output_to_log(["sed", "-i", '/-drsuapi/!s/\(\s*dcerpc endpoint servers\s*=\s*.*\)$/\\1 -drsuapi/', "/etc/samba/smb.conf"], log.debug)

	## Start Samba
	run_and_output_to_log(["/etc/init.d/samba4", "start"], log.debug)
	check_samba4_started()

	### Phase I.b: Pre-Map SIDs (locale adjustment etc.)

	## pre-create containers in UDM
	container_list = []
	msgs = samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
				expression="(objectClass=organizationalunit)",
				attrs=["dn"])
	if msgs:
		log.debug("Creating OUs in the Univention Directory Manager")
	for obj in msgs:
		container_list.append(obj["dn"].get_linearized())

	container_list.sort( key=len )

	for container_dn in container_list:
		rdn_list = ldap.explode_dn(container_dn)
		(ou_type, ou_name) = rdn_list.pop(0).split('=', 1)
		position = string.replace(','.join(rdn_list).lower(), ucr['connector/s4/ldap/base'].lower(), ucr['ldap/base'].lower())

		udm_type = None
		if ou_type == "OU":
			udm_type="container/ou"
		elif ou_type == "CN":
			udm_type="container/cn"
		else:
			log.warn("Warning: Unmapped container type %s" % container_dn)

		if udm_type:
			run_and_output_to_log(["/usr/sbin/univention-directory-manager", udm_type, "create", "--ignore_exists", "--position", position, "--set" , "name=%s" % ou_name], log.debug)
	
	## Identify and rename UCS group names to match Samba4 (localized) group names
	AD_well_known_sids = {}
	for (rid, name) in univention.lib.s4.well_known_domain_rids.items():
		AD_well_known_sids["%s-%s" % (ad_domainsid, rid)] = name
	AD_well_known_sids.update(univention.lib.s4.well_known_sids)

	groupRenameHandler = GroupRenameHandler(lo)
	userRenameHandler = UserRenameHandler(lo)

	for (sid, canonical_name) in AD_well_known_sids.items():

		msgs = samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
							expression="(objectSid=%s)" % (sid),
							attrs=["sAMAccountName", "objectClass"])
		if not msgs:
			log.debug("Well known SID %s not found in Samba" % (sid,))
			continue

		obj = msgs[0]
		ad_object_name = obj.get("sAMAccountName", [None])[0]
		oc = obj["objectClass"]

		if not ad_object_name:
			continue

		if sid == "S-1-5-32-550":	## Special: Printer-Admins / Print Operators / Opérateurs d’impression
			## don't rename, adjust group name mapping for S4 connector instead.
			run_and_output_to_log(["univention-config-registry", "set", "connector/s4/mapping/group/table/Printer-Admins=%s" % (ad_object_name,)], log.debug)
			continue

		ucsldap_object_name = canonical_name	## default
		## lookup canonical_name in UCSLDAP, for cases like "Replicator/Replicators" and "Server Operators"/"System Operators" that changed in UCS 3.2, see Bug #32461#c2
		ucssid = sid.replace(ad_domainsid, old_domainsid, 1)
		ldap_result = lo.search(filter="(sambaSID=%s)" % (ucssid,), attr=["sambaSID", "uid", "cn"])
		if len(ldap_result) == 1:
			if "group" in oc or "foreignSecurityPrincipal" in oc:
				ucsldap_object_name = ldap_result[0][1].get("cn", [None])[0]
			elif "user" in oc:
				ucsldap_object_name = ldap_result[0][1].get("uid", [None])[0]
		elif len(ldap_result) > 0:
			log.error('Error: Found more than one object with sambaSID=%s' % (sid,))
		else:
			log.debug('Info: Did not find an object with sambaSID=%s' % (sid,))

		if not ucsldap_object_name:
			continue

		if ad_object_name.lower() != ucsldap_object_name.lower():
			if "group" in oc or "foreignSecurityPrincipal" in oc:
				groupRenameHandler.rename_ucs_group(ucsldap_object_name, ad_object_name)
			elif "user" in oc:
				userRenameHandler.rename_ucs_user(ucsldap_object_name, ad_object_name)

	## construct dict of old UCS sambaSIDs
	old_sambaSID_dict = {}
	samba_sid_map = {}
	## Users and Computers
	ldap_result = lo.search(filter="(&(objectClass=sambaSamAccount)(sambaSID=*))", attr=["uid", "sambaSID", "univentionObjectType"])
	for record in ldap_result:
		(ucs_object_dn, ucs_object_dict) = record
		old_sid = ucs_object_dict["sambaSID"][0]
		ucs_name = ucs_object_dict["uid"][0]
		if old_sid.startswith(old_domainsid):
			old_sambaSID_dict[old_sid] = ucs_name

			msgs = samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
			                    expression="(sAMAccountName=%s)" % ucs_name,
			                    attrs=["dn", "objectSid"])
			if not msgs:
				continue
			else:
				obj = msgs[0]
				new_sid = str(ndr_unpack(security.dom_sid, obj["objectSid"][0]))
				samba_sid_map[old_sid] = new_sid

				log.debug("Rewriting user %s SID %s to %s" % (old_sambaSID_dict[old_sid], old_sid, new_sid))
				ml = [("sambaSID", old_sid, new_sid)]
				lo.modify(ucs_object_dn, ml)

	## Groups
	ldap_result = lo.search(filter="(&(objectClass=sambaGroupMapping)(sambaSID=*))", attr=["cn", "sambaSID", "univentionObjectType"])
	for record in ldap_result:
		(ucs_object_dn, ucs_object_dict) = record
		old_sid = ucs_object_dict["sambaSID"][0]
		ucs_name = ucs_object_dict["cn"][0]
		if old_sid.startswith(old_domainsid):
			old_sambaSID_dict[old_sid] = ucs_name

			msgs = samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
			                    expression="(sAMAccountName=%s)" % ucs_name,
			                    attrs=["objectSid"])
			if not msgs:
				continue
			else:
				obj = msgs[0]
				new_sid = str(ndr_unpack(security.dom_sid, obj["objectSid"][0]))
				samba_sid_map[old_sid] = new_sid

				log.debug("Rewriting group '%s' SID %s to %s" % (old_sambaSID_dict[old_sid], old_sid, new_sid))
				ml = [("sambaSID", old_sid, new_sid)]
				lo.modify(ucs_object_dn, ml)

	ldap_result = lo.search(filter="(sambaPrimaryGroupSID=*)", attr=["sambaPrimaryGroupSID"])
	for record in ldap_result:
		(ucs_object_dn, ucs_object_dict) = record
		old_sid = ucs_object_dict["sambaPrimaryGroupSID"][0]
		if old_sid.startswith(old_domainsid):
			if old_sid in samba_sid_map:
				ml = [("sambaPrimaryGroupSID", old_sid, samba_sid_map[old_sid])]
				lo.modify(ucs_object_dn, ml)
			else:
				if old_sid in old_sambaSID_dict:
					# log.error("Error: Could not find new sambaPrimaryGroupSID for %s" % old_sambaSID_dict[old_sid])
					pass
				else:
					log.debug("Warning: Unknown sambaPrimaryGroupSID %s" % old_sid)


	### Pre-Create mail domains for all mail and proxyAddresses:
	samdb = SamDB(os.path.join(SAMBA_PRIVATE_DIR, "sam.ldb"), session_info=system_session(lp), lp=lp)
	msgs = samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
	                    expression="(|(mail=*)(proxyAddresses=*))",
	                    attrs=["mail", "proxyAddresses"])
	maildomains = []
	for msg in msgs:
		for attr in ("mail", "proxyAddresses"):
			if attr in msg:
				for address in msg[attr]:
					char_idx = address.find("@")
					if char_idx != -1:
						domainpart = address[char_idx+1:].lower()
						# if not domainpart.endswith(".local"): ## We need to create all the domains. Alternatively set:
						## ucr:directory/manager/web/modules/users/user/properties/mailAlternativeAddress/syntax=emailAddress
						if not domainpart in maildomains:
							maildomains.append(domainpart)
	for maildomain in maildomains:
		returncode = run_and_output_to_log(["univention-directory-manager", "mail/domain", "create", "--ignore_exists", "--position", "cn=domain,cn=mail,%s" % ucr["ldap/base"], "--set" , "name=%s" % maildomain], log.debug)
		if returncode != 0:
			log.error("Creation of UCS mail/domain %s failed. See %s for details." % (maildomain, LOGFILE_NAME,))

	## re-create DNS SPN account
	log.debug("Attempting removal of DNS SPN account in UCS-LDAP, will be recreated later with new password.")
	run_and_output_to_log(["univention-directory-manager", "users/user", "delete", "--dn", "uid=dns-%s,cn=users,%s" % (ucr["hostname"], ucr["ldap/base"])], log.debug)

	## remove zarafa and univention-squid-kerberos SPN accounts, recreated later in phaseIII by running the respective joinscripts again
	log.debug("Attempting removal of Zarafa and Squid SPN accounts in UCS-LDAP, will be recreated later with new password.")
	for service in ("zarafa", "http", "http-proxy"):
		run_and_output_to_log(["univention-directory-manager", "users/user", "delete", "--dn", "uid=%s-%s,cn=users,%s" % (service, ucr["hostname"], ucr["ldap/base"])], log.debug)

	### Copy UCS Administrator Password to S4 Administrator object	### disabled, because then the Password in UCS == S4 != AD, which is bad for SYSVOL sync
	### (Account is disabled in SBS 2008 by default and the password might be unknown)
	## workaround for samba ndr parsing traceback
	#p = subprocess.Popen(["samba-tool", "user", "setpassword", "Administrator", "--newpassword=DummyPW123"]) ## will be overwritten in the next step
	#p.wait()
	#p = subprocess.Popen(["/usr/sbin/univention-password_sync_ucs_to_s4", "Administrator"])
	#if p.wait() != 0:	## retry logic from 97univention-s4-connector.inst join script
	#	p = subprocess.Popen(["/etc/init.d/samba4", "restart"])
	#	p.wait()
	#	check_samba4_started()
	#	time.sleep(3)
	#	p = subprocess.Popen(["/usr/sbin/univention-password_sync_ucs_to_s4", "Administrator"])
	#	p.wait()

	## Remove logonHours restrictions from Administrator account, was set in one test environment..
	msgs = samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
				expression="(samaccountname=Administrator)",
				attrs=["logonHours"])
	if msgs:
		obj = msgs[0]
		if "logonHours" in obj:
			log.debug("Removing logonHours restriction from Administrator account")
			delta = ldb.Message()
			delta.dn = obj.dn
			delta["logonHours"] = ldb.MessageElement([], ldb.FLAG_MOD_DELETE, "logonHours")
			samdb.modify(delta)

	### Phase I.c: Run S4 Connector

	old_sleep = ucr.get("connector/s4/poll/sleep", "5")
	old_retry = ucr.get("connector/s4/retryrejected", "10")
	run_and_output_to_log(["univention-config-registry", "set", "connector/s4/poll/sleep=1", "connector/s4/retryrejected=2"], log.debug)
	run_and_output_to_log(["/usr/share/univention-s4-connector/msgpo.py", "--write2ucs"], log.debug)

	log.info("Waiting for listener to finish (max. 180 seconds)")
	if not wait_for_listener_replication(print_progress, 180):
		log.warn("Warning: Stopping Listener now anyway.")

	## Restart Univention Directory Listener for S4 Connector
	log.info("Restarting Univention Directory Listener")

	## Reset S4 Connector and handler state
	run_and_output_to_log(["/etc/init.d/univention-directory-listener", "stop"], log.debug)

	for i in xrange(30):
		time.sleep(1)
		print_progress()
	print
	sys.stdout.flush()

	if os.path.exists("/var/lib/univention-directory-listener/handlers/s4-connector"):
		os.unlink("/var/lib/univention-directory-listener/handlers/s4-connector")
	# if os.path.exists("/var/lib/univention-directory-listener/handlers/samba4-idmap"):
	# 	os.unlink("/var/lib/univention-directory-listener/handlers/samba4-idmap")
	if os.path.exists("/etc/univention/connector/s4internal.sqlite"):
		os.unlink("/etc/univention/connector/s4internal.sqlite")
	for foldername in ("/var/lib/univention-connector/s4", "/var/lib/univention-connector/s4/tmp"):
		for entry in os.listdir(foldername):
			filename = os.path.join(foldername, entry)
			try:
				if os.path.isfile(filename):
					os.unlink(filename)
			except Exception, e:
				log.error("Error removing file: %s" % str(e))

	returncode = run_and_output_to_log(["/etc/init.d/univention-directory-listener", "start"], log.debug)
	if returncode != 0:
		log.error("Start of univention-directory-listener failed. See %s for details." % (LOGFILE_NAME,))

	#print "Waiting for directory listener to start up (10 seconds)",
	#for i in xrange(10):
	#	time.sleep(1)
	#	print_progress()
	#print

	### rotate S4 connector log and start the S4 Connector
	## careful: the postrotate task restarts the connector!
	run_and_output_to_log(["logrotate", "-f", "/etc/logrotate.d/univention-s4-connector"], log.debug)

	## Ok, just in case, start the Connector explicitely
	log.info("Starting S4 Connector")
	returncode = run_and_output_to_log(["/etc/init.d/univention-s4-connector", "start"], log.debug)
	if returncode != 0:
		log.error("Start of univention-s4-connector failed. See %s for details." % (LOGFILE_NAME,))

	log.info("Waiting for S4 Connector sync")
	log.info("Progress details are logged to /var/log/univention/connector-s4-status.log")
	wait_for_s4_connector_replication(ucr, lp, print_progress)

	## Reset normal relication intervals
	run_and_output_to_log(["univention-config-registry", "set", "connector/s4/poll/sleep=%s" % old_sleep, "connector/s4/retryrejected=%s" % old_retry], log.debug)
	returncode = run_and_output_to_log(["/etc/init.d/univention-s4-connector", "restart"], log.debug)
	if returncode != 0:
		log.error("Restart of univention-s4-connector failed. See %s for details." % (LOGFILE_NAME,))

	## rebuild idmap
	returncode = run_and_output_to_log(["/usr/lib/univention-directory-listener/system/samba4-idmap.py", "--direct-resync"], log.debug)
	if returncode != 0:
		log.error("Resync of samba4-idmap failed. See %s for details." % (LOGFILE_NAME,))

	## Start NSCD again
	returncode = run_and_output_to_log(["/etc/init.d/nscd", "start"], log.debug)
	if returncode != 0:
		log.error("Start of nscd failed. See %s for details." % (LOGFILE_NAME,))

	## Save AD server IP for Phase III
	run_and_output_to_log(["univention-config-registry", "set", "univention/ad/takeover/ad/server/ip=%s" % (ad_server_ip) ], log.debug)

	### Phase II: AD-Side Sync 

	msg=[]
	msg.append("")
	msg.append("Phase I finished.")
	log.info("\n".join(msg))

	return (ad_server_ip, ad_server_fqdn, ad_server_name)


def run_phaseIII(ucr, lp, ad_server_ip, ad_server_fqdn, ad_server_name):

	ucr.load()
	local_fqdn = '.'.join((ucr["hostname"], ucr["domainname"]))

	### Phase III: Promote to FSMO master and DNS server

	log.info("Starting phase III of the takeover process.")

	## Restart Samba and make shure the rapid restart did not leave the main process blocking
	run_and_output_to_log(["/etc/init.d/samba4", "restart"], log.debug)
	check_samba4_started()

	## 1. Determine Site of local server, important for locale-dependend names like "Standardname-des-ersten-Standorts"
	sitename = None
	samdb = SamDB(os.path.join(SAMBA_PRIVATE_DIR, "sam.ldb"), session_info=system_session(lp), lp=lp)
	msgs = samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
	                    expression="(sAMAccountName=%s$)" % ucr["hostname"],
	                    attrs=["serverReferenceBL"])
	if msgs:
		obj = msgs[0]
		serverReferenceBL = obj["serverReferenceBL"][0]
		serverReferenceBL_RDNs = ldap.explode_dn(serverReferenceBL)
		serverReferenceBL_RDNs.reverse()
		config_partition_index = None
		site_container_index = None
		for i in xrange(len(serverReferenceBL_RDNs)):
			if site_container_index:
				sitename = serverReferenceBL_RDNs[i].split('=', 1)[1]
				break
			elif config_partition_index and serverReferenceBL_RDNs[i] == "CN=Sites":
				site_container_index = i
			elif not site_container_index and serverReferenceBL_RDNs[i] == "CN=Configuration":
				config_partition_index = i
			i = i+1
		log.info("Located server %s in AD site %s in Samba4 SAM database." % (ucr["hostname"], sitename))

	## properly register partitions
	partitions = takeover_hasInstantiatedNCs(ucr, samdb, ad_server_name, sitename)

	## Backup current NTACLs on sysvol
	p = subprocess.Popen(["getfattr", "-m", "-", "-d", "-R", SYSVOL_PATH], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	(stdout, stderr) = p.communicate()
	if stdout:
		now = time.localtime()
		timestamp = time.strftime("%Y%m%d%H%M%S", now)
		f=open('%s/sysvol_attr_backup.log-%s' % (BACKUP_DIR, timestamp), 'a')
		f.write("### getfattr output %s\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S", now), stdout))
		f.close()
	else:
		log.debug("getfattr did not produce any output")
	if len(stderr.rstrip().split('\n')) > 1:
		log.debug(stderr)

	## Re-Set NTACLs from nTSecurityDescriptor on sysvol policy directories
	run_and_output_to_log(["samba-tool", "ntacl", "sysvolreset"], log.debug)

	## Re-set default fACLs so sysvol-sync can read files and directories (See Bug#29065)
	returncode = run_and_output_to_log(["setfacl", "-R", "-P", "-m", "g:Authenticated Users:r-x,d:g:Authenticated Users:r-x", SYSVOL_PATH], log.debug)
	if returncode != 0:
		log.error("Error: Could not set fACL for %s" % SYSVOL_PATH)
		msg=[]
		msg.append("Warning: Continuing anyway. Please fix later by running:")
		msg.append("         setfacl -R -P -m 'g:Authenticated Users:r-x,d:g:Authenticated Users:r-x' %s" % SYSVOL_PATH)
		log.warn("\n".join(msg))

	## Add DNS records to UDM:
	run_and_output_to_log(["/usr/share/univention-samba4/scripts/setup-dns-in-ucsldap.sh", "--dc", "--pdc", "--gc", "--site=%s" % sitename], log.info)

	## remove local enty for AD DC from /etc/hosts
	run_and_output_to_log(["univention-config-registry", "unset", "hosts/static/%s" % ad_server_ip ], log.debug)

	## Replace DNS host record for AD Server name by DNS Alias
	run_and_output_to_log(["univention-directory-manager", "dns/host_record", "delete", "--superordinate", "zoneName=%s,cn=dns,%s" % (ucr["domainname"], ucr["ldap/base"]), "--dn", "relativeDomainName=%s,zoneName=%s,cn=dns,%s" % (ad_server_name, ucr["domainname"], ucr["ldap/base"]) ], log.debug)

	returncode = run_and_output_to_log(["univention-directory-manager", "dns/alias", "create", "--superordinate", "zoneName=%s,cn=dns,%s" % (ucr["domainname"], ucr["ldap/base"]), "--set", "name=%s" % ad_server_name, "--set", "cname=%s" % local_fqdn], log.debug)
	if returncode != 0:
		log.error("Creation of dns/alias %s for %s failed. See %s for details." % (ad_server_name, local_fqdn, LOGFILE_NAME,))

	## Cleanup necessary to use NETBIOS Alias
	log.info("Cleaning up.")
	
	backlink_attribute_list = ["serverReferenceBL", "frsComputerReferenceBL", "msDFSR-ComputerReferenceBL"]
	msgs = samdb.search(base=ucr["samba4/ldap/base"], scope=samba.ldb.SCOPE_SUBTREE,
	                    expression="(sAMAccountName=%s$)" % ad_server_name,
	                    attrs=backlink_attribute_list)
	if msgs:
		obj = msgs[0]
		for backlink_attribute in backlink_attribute_list:
			if backlink_attribute in obj:
				backlink_object = obj[backlink_attribute][0]
				try:
					log.info("Removing %s from SAM database." % (backlink_object,))
					samdb.delete(backlink_object, ["tree_delete:0"])
				except:
					log.debug("Removal of AD %s objects %s from Samba4 SAM database failed. See %s for details." % (backlink_attribute, backlink_object, LOGFILE_NAME,))
					log.debug(traceback.format_exc())

		## Now delete the AD DC account and sub-objects
		## Cannot use tree_delete on isCriticalSystemObject, perform recursive delete like ldbdel code does it:
		msgs = samdb.search(base=obj.dn, scope=samba.ldb.SCOPE_SUBTREE,
							attrs=["dn"])
		obj_dn_list = [obj.dn for obj in msgs]
		obj_dn_list.sort(key=len)
		obj_dn_list.reverse()
		for obj_dn in obj_dn_list:
			try:
				log.info("Removing %s from SAM database." % (obj_dn,))
				samdb.delete(obj_dn)
			except:
				log.error("Removal of AD DC account object %s from Samba4 SAM database failed. See %s for details." % (obj_dn, LOGFILE_NAME,))
				log.debug(traceback.format_exc())

	## Finally, for consistency remove AD DC object from UDM
	log.debug("Removing AD DC account from local Univention Directory Manager")
	returncode = run_and_output_to_log(["univention-directory-manager", "computers/windows_domaincontroller", "delete", "--dn", "cn=%s,cn=dc,cn=computers,%s" % (ad_server_name, ucr["ldap/base"]) ], log.debug)
	if returncode != 0:
		log.error("Removal of DC account %s via UDM failed. See %s for details." % (ad_server_name, LOGFILE_NAME,))

	## Create NETBIOS Alias
	f = open('/etc/samba/local.conf', 'a')
	f.write('[global]\nnetbios aliases = "%s"\n' % ad_server_name)
	f.close()

	run_and_output_to_log(["univention-config-registry", "commit", "/etc/samba/smb.conf"], log.debug)

	## Assign AD IP to a virtual network interface
	## Determine primary network interface, UCS 3.0-2 style:
	ip_addr = None
	try:
		ip_addr = ipaddr.IPAddress(ad_server_ip)
	except ValueError:
		msg=[]
		msg.append("Error: Parsing AD server address failed")
		msg.append("       Failed to setup a virtual network interface with the AD IP address.")
		log.error("\n".join(msg))
	if ip_addr:
		new_interface = None
		if ip_addr.version == 4:
			for i in xrange(4):
				if "interfaces/eth%s/address" % i in ucr:
					for j in xrange(4):
						if not "interfaces/eth%s_%s/address" % (i, j) in ucr and j > 0:
							primary_interface = "eth%s" % i
							new_interface_ucr = "eth%s_%s" % (i, j)
							new_interface = "eth%s:%s" % (i, j)
							break
			
			if new_interface:
				guess_network = ucr["interfaces/%s/network" % primary_interface]
				guess_netmask = ucr["interfaces/%s/netmask" % primary_interface]
				guess_broadcast = ucr["interfaces/%s/broadcast" % primary_interface]
				run_and_output_to_log(["univention-config-registry", "set",
				                       "interfaces/%s/address=%s" % (new_interface_ucr, ad_server_ip),
				                       "interfaces/%s/network=%s" % (new_interface_ucr, guess_network),
				                       "interfaces/%s/netmask=%s" % (new_interface_ucr, guess_netmask),
				                       "interfaces/%s/broadcast=%s" % (new_interface_ucr, guess_broadcast)], log.debug)
				samba_interfaces = ucr.get("samba/interfaces")
				if ucr.is_true("samba/interfaces/bindonly") and samba_interfaces:
					run_and_output_to_log(["univention-config-registry", "set", "samba/interfaces=%s %s" % (samba_interfaces, new_interface)], log.debug)
			else:
				msg=[]
				msg.append("Warning: Could not determine primary IPv4 network interface.")
				msg.append("         Failed to setup a virtual IPv4 network interface with the AD IP address.")
				log.warn("\n".join(msg))
		elif ip_addr.version == 6:
			for i in xrange(4):
				if "interfaces/eth%s/ipv6/default/address" % i in ucr:
					for j in xrange(4):
						if not "interfaces/eth%s_%s/ipv6/default/address" % (i, j) in ucr and j > 0:
							primary_interface = "eth%s" % i
							new_interface_ucr = "eth%s_%s" % (i, j)
							new_interface = "eth%s:%s" % (i, j)
							break
			
			if new_interface:
				guess_prefix = ucr["interfaces/%s/ipv6/default/prefix" % primary_interface]
				run_and_output_to_log(["univention-config-registry", "set",
				                       "interfaces/%s/ipv6/default/address=%s" % (new_interface_ucr, ad_server_ip),
				                       "interfaces/%s/ipv6/default/prefix=%s" % (new_interface_ucr, guess_broadcast),
				                       "interfaces/%s/ipv6/acceptRA=false"], log.debug)
				samba_interfaces = ucr.get("samba/interfaces")
				if ucr.is_true("samba/interfaces/bindonly") and samba_interfaces:
					run_and_output_to_log(["univention-config-registry", "set", "samba/interfaces=%s %s" % (samba_interfaces, new_interface)], log.debug)
			else:
				msg=[]
				msg.append("Warning: Could not determine primary IPv6 network interface.")
				msg.append("         Failed to setup a virtual IPv6 network interface with the AD IP address.")
				log.warn("\n".join(msg))

	## Add record in reverse zone as well, to make nslookup $domainname on XP clients happy..
	p = subprocess.Popen(["univention-ipcalc6", "--ip", ad_server_ip, "--netmask", ucr["interfaces/%s/netmask" % primary_interface], "--output", "pointer", "--calcdns"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	(stdout, stderr) = p.communicate()
	if stdout.rstrip():
		ptr_address = stdout.rstrip()

	p = subprocess.Popen(["univention-ipcalc6", "--ip", ad_server_ip, "--netmask", ucr["interfaces/%s/netmask" % primary_interface], "--output", "reverse", "--calcdns"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	(stdout, stderr) = p.communicate()
	if stdout.rstrip():
		subnet_parts = stdout.rstrip().split('.')
		subnet_parts.reverse()
		ptr_zone = "%s.in-addr.arpa" % '.'.join(subnet_parts)

	if ptr_zone and ptr_address:
		## check for an existing record.
		p = subprocess.Popen(["univention-directory-manager", "dns/ptr_record", "list", "--superordinate", "zoneName=%s,cn=dns,%s" % (ptr_zone, ucr["ldap/base"]), "--filter", "address=%s" % ptr_address], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		(stdout, stderr) = p.communicate()
		if len(stdout.rstrip().split('\n')) > 1:
			## modify existing record.
			returncode = run_and_output_to_log(["univention-directory-manager", "dns/ptr_record", "modify", "--superordinate", "zoneName=%s,cn=dns,%s" % (ptr_zone, ucr["ldap/base"]), "--dn", "relativeDomainName=%s,zoneName=%s,cn=dns,%s" % (ptr_address, ptr_zone, ucr["ldap/base"]), "--set", "ptr_record=%s." % local_fqdn], log.debug)
			if returncode != 0:
				log.warn("Warning: Update of reverse DNS record %s for %s failed. See %s for details." % (ad_server_ip, local_fqdn, LOGFILE_NAME,))
		else:
			## add new record.
			returncode = run_and_output_to_log(["univention-directory-manager", "dns/ptr_record", "create", "--superordinate", "zoneName=%s,cn=dns,%s" % (ptr_zone, ucr["ldap/base"]), "--set", "address=%s" % ptr_address, "--set", "ptr_record=%s." % local_fqdn], log.debug)
			if returncode != 0:
				log.warn("Warning: Creation of reverse DNS record %s for %s failed. See %s for details." % (ad_server_ip, local_fqdn, LOGFILE_NAME,))
	else:
		log.warn("Warning: Calculation of reverse DNS record %s for %s failed. See %s for details." % (ad_server_ip, local_fqdn, LOGFILE_NAME,))

	## Resolve against local Bind9
	## use OpenLDAP backend until the S4 Connector has run
	if "nameserver1/local" in ucr:
		nameserver1_orig = ucr["nameserver1/local"]
		run_and_output_to_log(["univention-config-registry", "set", "nameserver1=%s" % nameserver1_orig], log.debug)
		## unset temporary variable
		run_and_output_to_log(["univention-config-registry", "unset", "nameserver1/local"], log.debug)
	else:
		msg=[]
		msg.append("Warning: Weird, unable to determine previous nameserver1...")
		msg.append("         Using localhost as fallback, probably that's the right thing to do.")
		log.warn("\n".join(msg))
		run_and_output_to_log(["univention-config-registry", "set", "nameserver1=127.0.0.1"], log.debug)

	## Use Samba4 as DNS backend
	run_and_output_to_log(["univention-config-registry", "set", "dns/backend=samba4"], log.debug)

	## Re-enable replication from Samba4
	run_and_output_to_log(["univention-config-registry", "unset", "samba4/dcerpc/endpoint/drsuapi"], log.debug)

	## Claim FSMO roles
	log.info("Claiming FSMO roles")
	takeover_hasMasterNCs(ucr, samdb, sitename, partitions)
	for fsmo_role in ('pdc', 'rid', 'infrastructure', 'schema', 'naming'):
		for attempt in xrange(3):
			if attempt > 0:
				time.sleep(1)
				log.debug("trying samba-tool fsmo seize --role=%s --force again:" % fsmo_role)
			returncode = run_and_output_to_log(["samba-tool", "fsmo", "seize", "--role=%s" % fsmo_role, "--force"], log.debug)
			if returncode == 0:
				break
		else:
			msg=[]
			msg.append("Claiming FSMO role %s failed." % fsmo_role)
			msg.append("Warning: Continuing anyway. Please fix later by running:")
			msg.append("         samba-tool fsmo seize --role=%s --force" % fsmo_role)
			log.error("\n".join(msg))

	## Let things settle
	time.sleep(3)

	## Restart Samba and make shure the rapid restart did not leave the main process blocking
	run_and_output_to_log(["/etc/init.d/samba4", "restart"], log.debug)
	check_samba4_started()

	log.debug("Creating new DNS SPN account in Samba4")
	dns_SPN_account_password = univention.lib.createMachinePassword()
	if dns_SPN_account_password[0] == '-':	## avoid passing an option
		dns_SPN_account_password= '#%s' % dns_SPN_account_password
	dns_SPN_account_name = "dns-%s" % ucr["hostname"]
	run_and_output_to_log(["samba-tool", "user", "add", dns_SPN_account_name, dns_SPN_account_password], log.debug, print_commandline = False)
	run_and_output_to_log(["samba-tool", "user", "setexpiry", "--noexpiry", dns_SPN_account_name], log.debug)
	delta = ldb.Message()
	delta.dn = ldb.Dn(samdb, "CN=%s,CN=Users,%s" % (dns_SPN_account_name, ucr["samba4/ldap/base"]))
	delta["servicePrincipalName"] = ldb.MessageElement("DNS/%s" % local_fqdn, ldb.FLAG_MOD_REPLACE, "servicePrincipalName")
	samdb.modify(delta)

	dnsKeyVersion = 1	## default
	msgs = samdb.search(base="CN=%s,CN=Users,%s" % (dns_SPN_account_name, ucr["samba4/ldap/base"]), scope=samba.ldb.SCOPE_BASE,
	                    attrs=["msDS-KeyVersionNumber"])
	if msgs:
		obj = msgs[0]
		dnsKeyVersion = obj["msDS-KeyVersionNumber"][0]

	secretsdb = samba.Ldb(os.path.join(SAMBA_PRIVATE_DIR, "secrets.ldb"), session_info=system_session(lp), lp=lp)
	secretsdb.add({"dn": "samAccountName=%s,CN=Principals" % dns_SPN_account_name,
		"objectClass": "kerberosSecret",
		"privateKeytab": "dns.keytab",
		"realm": ucr["kerberos/realm"],
		"sAMAccountName": dns_SPN_account_name,
		"secret": dns_SPN_account_password,
		"servicePrincipalName": "DNS/%s" % local_fqdn,
		"name": dns_SPN_account_name,
		"msDS-KeyVersionNumber": dnsKeyVersion})
	
	# returncode = run_and_output_to_log(["/usr/share/univention-samba4/scripts/create_dns-host_spn.py"], log.debug)
	# if returncode != 0:
	#	log.error("Creation of DNS SPN account 'dns-%s' failed. See %s for details." % (ucr["hostname"], LOGFILE_NAME,))

	## Restart bind9 to use the OpenLDAP backend, just to be sure
	returncode = run_and_output_to_log(["/etc/init.d/bind9", "restart"], log.debug)
	if returncode != 0:
		log.error("Start of Bind9 daemon failed. See %s for details." % (LOGFILE_NAME,))

	## re-create /etc/krb5.keytab
	##  https://forge.univention.org/bugzilla/show_bug.cgi?id=27426
	run_and_output_to_log(["/usr/share/univention-samba4/scripts/create-keytab.sh"], log.debug)

	## Enable NTP Signing for Windows SNTP clients
	run_and_output_to_log(["univention-config-registry", "set", "ntp/signed=yes"], log.debug)
	returncode = run_and_output_to_log(["/etc/init.d/ntp", "restart"], log.debug)
	if returncode != 0:
		log.error("Start of NTP daemon failed. See %s for details." % (LOGFILE_NAME,))

	## Re-run joinscripts that create an SPN account (lost in old secrets.ldb)
	for joinscript_name in ("zarafa4ucs-sso", "univention-squid-samba4"):
		run_and_output_to_log(["sed", "-i", "/^%s v[0-9]* successful/d" % joinscript_name, "/var/univention-join/status"], log.debug)
	returncode = run_and_output_to_log(["univention-run-join-scripts"], log.debug)
	if returncode != 0:
		log.error("univention-run-join-scripts failed, please run univention-run-join-scripts manually after the script finished")

	run_and_output_to_log(["univention-config-registry", "set", "univention/ad/takeover/completed", "yes"], log.debug)
	run_and_output_to_log(["univention-config-registry", "unset", "univention/ad/takeover/ad/server/ip"], log.debug)
	run_and_output_to_log(["samba-tool", "dbcheck", "--fix", "--yes"], log.debug)

	msg=[]
	msg.append("")
	msg.append("Phase III finished, Takeover complete.")
	log.info("\n".join(msg))
	sys.exit(0)

# was: if __name__ == '__main__':
def check_it():

	parser = OptionParser("%prog [options] <AD Server IP> [<AD Server Name>]")
	# parser.add_option("-v", "--verbose", action="store_true")
	parser.add_option("--fsmo-takeover", action="store_true")

	sambaopts = samba.getopt.SambaOptions(parser)
	parser.add_option_group(sambaopts)
	parser.add_option_group(samba.getopt.VersionOptions(parser))
	# use command line creds if available
	credopts = samba.getopt.CredentialsOptions(parser)
	parser.add_option_group(credopts)
	opts, args = parser.parse_args()

	p = subprocess.Popen(["dpkg-query", "-W", "-f=Version: ${Version}", "univention-s4-connector"], stdout=subprocess.PIPE)
	(stdout, stderr) = p.communicate()
	log.debug("\n\nCommandline: %s\n%s" % (' '.join(sys.argv), stdout))

	ucr = config_registry.ConfigRegistry()
	ucr.load()

	# lp = LoadParm()
	# lp.load('/etc/samba/smb.conf')
	lp = sambaopts.get_loadparm()

	if not opts.fsmo_takeover:
		## Phase I
		creds = credopts.get_credentials(lp)
		(ad_server_ip, ad_server_fqdn, ad_server_name) = run_phaseI(ucr, lp, opts, args, parser, creds)

		## Phase II
		ready_for_phaseIII = False
		while not ready_for_phaseIII:
			msg = []
			msg.append("Now the SYSVOL share should be copied manually from \\\\%s\\sysvol to \\\\%s\\sysvol (e.g. using robocopy):" % (ad_server_name, ucr['hostname']))
			msg.append("")
			msg.append("\trobocopy /mir /sec /z \\\\%s\\sysvol \\\\%s\\sysvol" % (ad_server_name, ucr['hostname']))
			msg.append("")
			msg.append("Once the SYSVOL share is copied successfully, shutdown the Windows server %s." % ad_server_fqdn)
			log.info("\n".join(msg))

			answer = raw_input("After completing the SYSVOL sync and switching off the AD server, hit return to continue.")

			## 1.d ping the given AD server IP
			print "Pinging AD IP %s: " % ad_server_ip,
			p1 = subprocess.Popen(["fping", ad_server_ip], stdout=DEVNULL, stderr=DEVNULL)
			rc= p1.poll()
			while rc is None:
				sys.stdout.write(".")
				sys.stdout.flush()
				time.sleep(1)
				rc= p1.poll()
			print
			sys.stdout.flush()
			if rc == 0:
				msg=[]
				msg.append("")
				msg.append("Error: The server IP %s is still reachable." % ad_server_ip)
				log.error("\n".join(msg))
			else:
				log.info("Ok, Server IP %s unreachable.\n" % ad_server_ip)
				ready_for_phaseIII = True
	else:
		## check if an IP address was recorded in UCR during Phase I
		ad_server_ip = ucr.get("univention/ad/takeover/ad/server/ip")
		if not ad_server_ip:
			log.debug("Error: AD server IP not found in UCR. This indicates that phase I was not completed successfully yet.")
			msg=[]
			msg.append("")
			msg.append("Error: Please complete phase I of the takeover before initiating the FSMO takeover.")
			log.error("\n".join(msg))
			sys.exit(1)

		## check if the given IP was mapped to a host name via UCR in Phase I
		(rc, ad_server_fqdn, ad_server_name) = check_for_phase_II(ucr, lp, ad_server_ip)
		if rc == 0:
			msg=[]
			msg.append("")
			msg.append("Error: given IP %s was not mapped to a hostname in phase I.")
			msg.append("       Please complete phase I of the takeover before initiating the FSMO takeover.")
			log.error("\n".join(msg))
			sys.exit(1)
		elif rc == 1:
			log.error("OK, Found the AD DC %s account in the local Samba 4 SAM database." % ad_server_name)
		elif rc == 2:
			msg=[]
			msg.append("")
			msg.append("Error: It seems that this script was run once already for the first takeover step,")
			msg.append("       but the server %s cannot be found in the local Samba SAM database." % ad_server_name)
			msg.append("       Don't know how to continue, giving up at this point.")
			msg.append("       Maybe the steps needed for takeover have been finished already?")
			log.error("\n".join(msg))
			sys.exit(1)

		## Check that local domainname matches kerberos realm
		if ucr["domainname"].lower() != ucr["kerberos/realm"].lower():
			log.error("Mismatching DNS domain and kerberos realm. Please reinstall the server with the same Domain as your AD.")
			sys.exit(1)

	## Phase III
	run_phaseIII(ucr, lp, ad_server_ip, ad_server_fqdn, ad_server_name)
