/*
 * Copyright 2011 Univention GmbH
 *
 * http://www.univention.de/
 *
 * All rights reserved.
 *
 * The source code of this program is made available
 * under the terms of the GNU Affero General Public License version 3
 * (GNU AGPL V3) as published by the Free Software Foundation.
 *
 * Binary versions of this program provided by Univention to you as
 * well as other copyrighted, protected or trademarked materials like
 * Logos, graphics, fonts, specific documentations and configurations,
 * cryptographic keys etc. are subject to a license agreement between
 * you and Univention and not subject to the GNU AGPL V3.
 *
 * In the case you use this program under the terms of the GNU AGPL V3,
 * the program is provided in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public
 * License with the Debian GNU/Linux or Univention distribution in file
 * /usr/share/common-licenses/AGPL-3; if not, see
 * <http://www.gnu.org/licenses/>.
 */
/*global console MyError dojo dojox dijit umc setTimeout */

dojo.provide("umc.modules.setup");

dojo.require("dijit.TitlePane");
dojo.require("umc.dialog");
dojo.require("umc.i18n");
dojo.require("umc.widgets.ContainerWidget");
dojo.require("umc.widgets.Form");
dojo.require("umc.widgets.Module");
dojo.require("umc.widgets.TabContainer");
dojo.require("umc.widgets.Page");
dojo.require("umc.widgets.TitlePane");

dojo.require("umc.modules._setup.ProgressInfo");

dojo.declare("umc.modules._setup.CancelDialogException", null, {
	// empty class that indicates that the user canceled a dialog
});

dojo.declare("umc.modules.setup", [ umc.widgets.Module, umc.i18n.Mixin ], {

	i18nClass: 'umc.modules.setup',

	// pages: String[]
	//		List of all setup-pages that are visible.
	pages: [ 'LanguagePage', 'BasisPage', 'NetworkPage', 'CertificatePage', 'SoftwarePage' ],

	// 100% opacity during rendering the module
	//standbyOpacity: 1,

	_pages: null,

	_orgValues: null,

	_currentPage: -1,

	_progressInfo: null,

	buildRendering: function() {
		this.inherited(arguments);

		// query the system role
		this.standby(true);

		// load some ucr variables
		var deferred_ucr = umc.tools.ucr(['system/setup/boot/select/role']);
		// load system setup values (e.g. join status)
		var deferred_variables = this.umcpCommand('setup/load');
		// wait for deferred objects to be completed
		var deferredlist = new dojo.DeferredList([deferred_ucr, deferred_variables]);
		deferredlist.then(dojo.hitch(this, function(data) {
			// pass ucr and values to renderPages()
			this.renderPages(data[0][1], data[1][1].result);
			this.standbyOpacity = 0.75;  // set back the opacity to 75%
		}));
	},

	renderPages: function(ucr, values) {
		this._progressInfo = new umc.modules._setup.ProgressInfo();
		// this._progressInfo.buildRendering();
		this.standby(true);

		// console.log('joined=' + values.joined);
		// console.log('select_role=' + ucr['system/setup/boot/select/role']);

		var allPages = dojo.clone(this.pages);

		// add the SystemRole to the list of pages for the wizard mode if system is not joined yet
		if ((umc.tools.isTrue(ucr['system/setup/boot/select/role'])) && (! values.joined)) {
			allPages.unshift('SystemRolePage');
		}

		// add the HelpPage to the list of pages for the wizard mode
		if (this.moduleFlavor == 'wizard') {
			allPages.unshift('HelpPage');
		}

		if (this.moduleFlavor == 'wizard') {
			// wizard mode

			// create all pages dynamically
			this._pages = [];
			dojo.forEach(allPages, function(iclass, i) {
				var ipath = 'umc.modules._setup.' + iclass;
				dojo['require'](ipath);
				var Class = new dojo.getObject(ipath);

				// get the buttons we need
				var buttons = [];
				if (i < allPages.length - 1) {
					buttons.push({
						name: 'submit',
						label: this._('Next'),
						callback: dojo.hitch(this, function() {
							// switch to next visible page
							// precondition: the last page is never invisible!
							var nextpage = i + 1;
							while ((nextpage < allPages.length) && (! this._pages[nextpage].visible)) {
								nextpage += 1;
							}
							this.selectChild(this._pages[nextpage]);
						})
					});
				}
				if (i > 0) {
					buttons.push({
						name: 'restore',
						label: this._('Back'),
						callback: dojo.hitch(this, function() {
							// switch to previous visible page
							// precondition: the first page is never invisible!
							var prevpage = i - 1;
							while ((0 < prevpage) && (! this._pages[prevpage].visible)) {
								prevpage -= 1;
							}
							this.selectChild(this._pages[prevpage]);
						})
					});
				}
				if (i == allPages.length - 1) {
					buttons.push({
						name: 'submit',
						label: this._('Apply settings'),
						callback: dojo.hitch(this, function() {
							this.save();
						})
					});
				}

				// make a new page
				var ipage = new Class({
					umcpCommand: dojo.hitch(this, 'umcpCommand'),
					footerButtons: buttons,
					moduleFlavor: this.moduleFlavor,
					onSave: dojo.hitch(this, function() {
						if (i < allPages.length - 1) {
							this.selectChild(this._pages[i + 1]);
						}
						else {
							this.save();
						}
					})
				});
				this.addChild(ipage);
				this._pages.push(ipage);

				// connect to onValuesChanged callback of every page
				this.connect(ipage, 'onValuesChanged', 'updateAllValues');
			}, this);
		}
		else {
			// normal mode... we need a TabContainer
			var tabContainer = new umc.widgets.TabContainer({
				nested: true
			});

			// each page has the same buttons for saving/resetting
			var buttons = [ {
					name: 'close',
					label: this._( 'Close' ),
					align: 'left',
					callback: dojo.hitch( this, function() {
						umc.dialog.confirm( this._( 'Should the UMC module be closed? All unsaved modification will be lost.' ), [ {
							label: this._( 'Close' ),
							callback: dojo.hitch( this, function() {
								dojo.publish('/umc/tabs/close', [ this ] );
							} )
						}, {
							label: this._( 'Cancel' ),
							'default': true
						} ] );
					} )
			}, {
				name: 'submit',
				label: this._( 'Apply changes' ),
				callback: dojo.hitch(this, function() {
					this.save();
				})
			}, {
				name: 'restore',
				label: this._('Reset'),
				callback: dojo.hitch(this, function() {
					this.load();
				})
			}];

			// create all pages dynamically
			this._pages = [];
			dojo.forEach(allPages, function(iclass) {
				// create new page
				var ipath = 'umc.modules._setup.' + iclass;
				dojo['require'](ipath);
				var Class = new dojo.getObject(ipath);
				var ipage = new Class({
					umcpCommand: dojo.hitch(this, 'umcpCommand'),
					footerButtons: buttons,
					moduleFlavor: this.moduleFlavor,
					onSave: dojo.hitch(this, function() {
						this.save();
					})
				});
				tabContainer.addChild(ipage);
				this._pages.push(ipage);

				// connect to onValuesChanged callback of every page
				this.connect(ipage, 'onValuesChanged', 'updateAllValues');

				// hide tab if page is not visible
				ipage.watch('visible', function(name, oldval, newval) {
					if ((newval == true) || (newval == undefined)) {
						tabContainer.showChild(ipage);
					} else {
						tabContainer.hideChild(ipage);
					}
				});
			}, this);

			this.addChild(tabContainer);
		}

		this.startup();
		this.setValues(values);
		this.standby(false);
	},

	updateAllValues: function(values) {
		var vals = dojo.clone(this._orgValues);
		dojo.mixin(vals, this.getValues());
		dojo.forEach(this._pages, function(ipage) {
			ipage.setValues(vals);
		}, this);
	},

	setValues: function(values) {
		// update all pages with the given values
		this._orgValues = dojo.clone(values);
		dojo.forEach(this._pages, function(ipage) {
			ipage.setValues(this._orgValues);
		}, this);
	},

	getValues: function() {
		var values = {};
		dojo.forEach(this._pages, function(ipage) {
			dojo.mixin(values, ipage.getValues());
		}, this);
		return values;
	},

	load: function() {
		// get settings from server
		this.standby(true);
		return this.umcpCommand('setup/load').then(dojo.hitch(this, function(data) {
			// update setup pages with loaded values
			this.setValues(data.result);
			this.standby(false);
		}), dojo.hitch(this, function() {
			this.standby(false);
		}));
	},

	save: function() {
		// helper function 
		var matchesSummary = function(key, summary) {
			var matched = false;
			// iterate over all assigned variables
			dojo.forEach(summary.variables, function(ikey) {
				// key is a regular expression or a string
				if (dojo.isString(ikey) && key == ikey ||
						ikey.test && ikey.test(key)) {
					matched = true;
					return false;
				}
			});
			return matched;
		};

		// confirm dialog to continue with boot process
		var _shutDownBrowser = dojo.hitch(this, function(msg, hasCancel) {
			var choices = [{
				name: 'apply',
				'default': true,
				label: this._('Continue')
			}];
			if (hasCancel) {
				// show continue and cancel buttons
				choices = [{
					name: 'cancel',
					'default': true,
					label: this._('Cancel')
				}, {
					name: 'apply',
					label: this._('Continue')
				}];
			}

			return umc.dialog.confirm(msg, choices).then(dojo.hitch(this, function(response) {
				if (response == 'cancel') {
					// do not continue
					return;
				}

				// otherwise send the UMCP command to shut down the web browser
				return this.umcpCommand('setup/browser/shutdown', false, undefined, {
					message: this._('Your session should be shut down automatically. If this has not happened so far, you may force a shutdown by pressing Ctrl+Q.'),
					messageInterval: 30
				}).then(dojo.hitch(this, function() {
					this.standby(false);
				}));
			}));
		});

		// get all entries that have changed and collect a summary of all changes
		var values = {};
		var nchanges = 0;
		var inverseKey2Page = {};  // saves which key belongs to which page
		var summaries = [];
		var umc_url = null;

		dojo.forEach(this._pages, function(ipage) {
			var pageVals = ipage.getValues();
			var summary = ipage.getSummary();
			summaries = summaries.concat(summary);

			// get altered values from page
			umc.tools.forIn(pageVals, function(ikey, ival) {
				inverseKey2Page[ikey] = ipage;
				var orgVal = this._orgValues[ikey];
				orgVal = undefined === orgVal || null === orgVal ? '' : orgVal;
				var newVal = undefined === ival || null === ival ? '' : ival;
				if (dojo.toJson(orgVal) != dojo.toJson(newVal)) {
					values[ikey] = newVal;
					++nchanges;
					if ( umc_url === null ) {
						if ( ikey == 'interfaces/eth0/address' && newVal ) {
							umc_url = 'https://' + newVal + '/umc/';
						} else if ( ikey == 'interfaces/eth0/ipv6/default/address' && newVal ) {
							umc_url = 'https://[' + newVal + ']/umc/';
						}
					}
				}
			}, this);
		}, this);

		// initiate some local check variables
		var joined = this._orgValues['joined'];
		var newValues = this.getValues();
		var role = newValues['server/role'];
		var userSystemSetup = umc.tools.status('username') == '__systemsetup__';

		if (!nchanges && userSystemSetup && (joined || role == 'basesystem')) {
			// no changes have been made we can shut down the web browser directly
			_shutDownBrowser(this._('No changes have been made. Please confirm to continue with the boot process.'), true);
			return;
		}

		// only submit data to server if there are changes and the system is joined
		if (!nchanges && (joined || role == 'basesystem')) {
			umc.dialog.alert(this._('No changes have been made.'));
			return;
		}

		// check whether all page widgets are valid
		var allValid = true;
		var validationMessage = '<p>' + this._('The following entries could not be validated:') + '</p><ul style="max-height:200px; overflow:auto;">';
		dojo.forEach(this._pages, function(ipage) {
			if (!ipage._form) {
				return true;
			}
			umc.tools.forIn(ipage._form._widgets, function(ikey, iwidget) {
				if (iwidget.isValid && false === iwidget.isValid()) {
					allValid = false;
					validationMessage += '<li>' + ipage.get('title') + '/' + iwidget.get('label') + '</li>';
				}
			});
		});
		if (!allValid) {
			this.standby(false);
			umc.dialog.alert(validationMessage);
			return;
		}

		// validate the changes
		this.standby(true);
		this.umcpCommand('setup/validate', { values: values }).then(dojo.hitch(this, function(data) {
			var allValid = true;
			dojo.forEach(data.result, function(ivalidation) {
				allValid = allValid && ivalidation.valid;
				if (!ivalidation.valid) {
					// find the correct description to be displayed
					dojo.forEach(summaries, function(idesc) {
						if (matchesSummary(ivalidation.key, idesc)) {
							idesc.validationMessages = idesc.validationMessages || [];
							idesc.validationMessages.push(ivalidation.message);
						}
					});

				}
			});

			// construct message for validation
			dojo.forEach(summaries, function(idesc) {
				//console.log('#', dojo.toJson(idesc));
				dojo.forEach(idesc.validationMessages || [], function(imsg) {
					validationMessage += '<li>' + idesc.description + ': ' + imsg + '</li>';
				});
			});

			if (!allValid) {
				// something could not be validated
				this.standby(false);
				umc.dialog.alert(validationMessage);
				return;
			}

			// function to confirm changes
			var _confirmChanges = dojo.hitch(this, function() {
				// first see which message needs to be displayed for the confirmation message
				umc.tools.forIn(values, function(ikey) {
					dojo.forEach(summaries, function(idesc) {
						if (matchesSummary(ikey, idesc)) {
							idesc.showConfirm = true;
						}
					});
				});

				// construct message for confirmation
				var confirmMessage = '<p>' + this._('The following changes will be applied to the system:') + '</p><ul style="max-height:200px; overflow:auto;">';
				dojo.forEach(summaries, function(idesc) {
					if (idesc.showConfirm) {
						confirmMessage += '<li>' + idesc.description + ': ' + idesc.values + '</li>';
					}
				});
				confirmMessage += '</ul><p>' + this._('Please confirm to apply these changes to the system. This may take some time.') + '</p>';

				return umc.dialog.confirm(confirmMessage, [{
					name: 'cancel',
					'default': true,
					label: this._('Cancel')
				}, {
					name: 'apply',
					label: this._('Apply changes')
				}]).then(dojo.hitch(this, function(response) {
					if ('apply' != response) {
						// throw new error to indicate that action has been canceled
						throw new umc.modules._setup.CancelDialogException();
					}
					this.standby( false );
				}));
			});

			// function to ask the user for DC account data
			var _password = dojo.hitch(this, function() {
				var msg = '<p>' + this._('The specified settings will be applied to the system and the system will be joined into the domain. Please enter username and password of a domain administrator account.') + '</p>'; 
				var deferred = new dojo.Deferred();
				var dialog = null;
				var form = new umc.widgets.Form({
					widgets: [{
						name: 'text',
						type: 'Text',
						content: msg
					}, {
						name: 'username',
						type: 'TextBox',
						label: this._('Username')
					}, {
						name: 'password',
						type: 'PasswordBox',
						label: this._('Password')
					}],
					buttons: [{
						name: 'submit',
						label: this._('Join'),
						callback: dojo.hitch(this, function() {
							this.standby(false);
							deferred.resolve({
								username: form.getWidget('username').get('value'),
								password: form.getWidget('password').get('value')
							});
							dialog.hide();
							dialog.destroyRecursive();
							form.destroyRecursive();
						})
					}, {
						name: 'cancel',
						label: this._('Cancel'),
						callback: dojo.hitch(this, function() {
							deferred.reject();
							this.standby(false);
							dialog.hide();
							dialog.destroyRecursive();
							form.destroyRecursive();
						})
					}],
					layout: [ 'text', 'username', 'password' ]
				});
				dialog = new dijit.Dialog({
					title: this._('Account data'),
					content: form,
					style: 'max-width: 400px;'
				});
				this.connect(dialog, 'onHide', function() {
					if (deferred.fired < 0) {
						// user clicked the close button
						this.standby(false);
						deferred.reject();
					}
				});
				dialog.show();
				return deferred;
			});

			// confirmation message for the master
			var _confirmMaster = dojo.hitch(this, function() {
				var msg = '<p>' + this._('The specified settings will be applied to the system. This may take some time. Please confirm to proceed.') + '</p>';
				return umc.dialog.confirm(msg, [{
					name: 'cancel',
					'default': true,
					label: this._('Cancel')
				}, {
					name: 'apply',
					label: this._('Apply changes')
				}]).then(dojo.hitch(this, function(response) {
					if ('apply' != response) {
						// throw new error to indicate that action has been canceled
						throw new umc.modules._setup.CancelDialogException();
					}
					this.standby( false );
				}));
			});

			// function to save data
			var _save = dojo.hitch(this, function(username, password) {
				var _Poller = function( _parent, _deferred ) {
					return {
						deferred: _deferred,
						parent: _parent,
						check: function() {
							var message = dojo.replace( this.parent._( 'The connection to the server could not be established after {time} seconds. This problem can occur due to a change of the IP address. In this case, please login to Univention Management Console again at the {linkStart}new address{linkEnd}.' ), { 
								time: '{time}',
								linkStart : umc_url ? '<a href="' + umc_url + '">' : '',
								linkEnd : umc_url ? '</a>' : ''
							} );
							this.parent.umcpCommand( 'setup/finished', {}, undefined, undefined, {
								// long polling options
								messageInterval: 30,
								message: message,
								xhrTimeout: 40
							} ).then( dojo.hitch( this, function( response ) {
								if ( response.result.finished ) {
									this.parent._progressInfo.setInfo(this.parent._('Configuration finished'), '', 100);
									this.deferred.resolve();
									return;
								}
								this.parent._progressInfo.setInfo( response.result.name, response.result.message, response.result.percentage );
								setTimeout( dojo.hitch( this, 'check' ), 100 );
							} ) );
						}
					};
				};

				var deferred = new dojo.Deferred();

				// send save command to server
				this._progressInfo.reset();
				this.standby( true, this._progressInfo );
				this.umcpCommand('setup/save', {
					values: values,
					username: username || null,
					password: password || null
				}).then(dojo.hitch(this, function() {
					// poll whether script has finished
					var poller = new _Poller( this, deferred );
					poller.check();
				}));

				return deferred;
			});

			// notify user that saving was successfull
			var _success = dojo.hitch(this, function() {
				umc.dialog.notify(this._('The changes have been applied successfully.'));
				this.load(); // sets 'standby(false)'
			});

			// show the correct dialogs
			var deferred = null;
			if (joined || role == 'basesystem') {
				// normal setup scenario, confirm changes and then save
				deferred = _confirmChanges().then(function() {
					return _save();
				});
			}
			else if (role != 'domaincontroller_master') {
				// unjoined system scenario and not master
				// we need a proper DC administrator account
				deferred = _password().then(function(opt) {
					return _save(opt.username, opt.password);
				});
			}
			else {
				// unjoined master
				deferred = _confirmMaster().then(function() {
					return _save();
				});
			}

			// as user __systemsetup__, notify that the boot process will continue
			if (userSystemSetup) {
				deferred = deferred.then(dojo.hitch(this, function() {
					return _shutDownBrowser(this._('Please confirm to continue with the boot process.'));
				}));
			}
			else {
				deferred = deferred.then(function() {
					return _success();
				});
			}

			// error case, turn off standby animation
			var self = this;
			deferred.then(function() {}, function() {
				self.standby(false);
			});
		}), dojo.hitch(this, function() {
			this.standby(false);
		}));
	}

});

