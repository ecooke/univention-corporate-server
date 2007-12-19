#!/bin/sh -e
#
# Univention Client Basesystem
#  helper script: mount home directory according to policy settings
#
# Copyright (C) 2004, 2005, 2006 Univention GmbH
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

exit 0
eval `univention-baseconfig shell univentionFileServer`

mounted=0

mount -n -t nfs "$univentionFileServer:/ha/home" /home || mounted=1

if [ $mounted -gt 0 ]; then
	echo "--- mount $univentionFileServer failed"
	eval `univention-baseconfig shell ldap/mydn`
	for i in `univention_policy_result -s "$ldap_mydn" | grep univentionFileServer | sed -e 's|.*univentionFileServer=||'`
	  do
	  if [ $mounted -gt 0 -a $univentionFileServer != $i ]
		  then
		  mounted=0
		  echo "--- trying to mount from host $i"
		  mount -n -t nfs "$i:/ha/home" /home || mounted=1
		  if [ ! $mounted -gt 0 ]; then
			  echo "--- home mounted from $i"
		  fi
	  fi
	done
fi

