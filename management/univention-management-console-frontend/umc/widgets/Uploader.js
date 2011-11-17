/*global dojo dijit dojox umc console */

dojo.provide("umc.widgets.Uploader");

dojo.require("dojox.form.Uploader");
dojo.require("dojox.form.uploader.plugins.HTML5");
dojo.require("umc.widgets.ContainerWidget");
dojo.require("umc.widgets._FormWidgetMixin");
dojo.require("umc.i18n");
dojo.require("umc.tools");
dojo.require("umc.dialog");

dojo.declare("umc.widgets.Uploader", [ umc.widgets.ContainerWidget, umc.widgets._FormWidgetMixin, umc.i18n.Mixin ], {
	'class': 'umcUploader',

	i18nClass: 'umc.app',

	// command: String
	//		The UMCP command to which the data shall be uploaded.
	//		If not given, the data is sent to umcp/upload which will return the
	//		file content encoded as base64.
	command: '',

	// buttonLabel: String
	//		The label that is displayed on the upload button.
	buttonLabel: 'Upload',

	// data: Object
	//		An object containing the file data that has been uploaded.
	data: null,

	// value: String
	//		The content of the base64 encoded file data.
	value: "",

	// maxSize: Number
	//		A size limit for the uploaded file.
	maxSize: 524288,

	// make sure that no sizeClass is being set
	sizeClass: null,

	// this form element should always be valid
	valid: true,

	// reference to the dojox.form.Uploader instance
	_uploader: null,

	// internal reference to the original user specified label
	_origButtonLabel: null,

	// internal flag that indicates that the data is being set
	_settingData: false,

	constructor: function() {
		this.buttonLabel = this._('Upload');
	},

	postMixInProperties: function() {
		this.inherited(arguments);

		// save the original label
		this._origButtonLabel = this.buttonLabel;
	},

	buildRendering: function() {
		this.inherited(arguments);
		
		this._uploader = new dojox.form.Uploader({
			url: '/umcp/upload' + (this.command ? '/' + this.command : ''),
			label: this.buttonLabel
		});
		dojo.addClass(this._uploader.button.domNode, 'umcButton');
		this._uploader.button.set('iconClass', 'umcIconAdd');
		dojo.style(this._uploader.button.domNode, 'display', 'inline-block');
		this.addChild(this._uploader);
	},
	
	postCreate: function() {
		this.inherited(arguments);

		// as soon as the user has selected a file, start the upload
		this.connect(this._uploader, 'onChange', function(data) {
			var allOk = true;
			dojo.forEach(data, function(ifile) {
				allOk = allOk && ifile.size <= this.maxSize;
				return allOk;
			}, this);
			if (!allOk) {
				umc.dialog.alert(this._('File cannot be uploaded, its maximum size may be %.1f MB.', this.maxSize / 1048576.0));
			}
			else {
				this._updateLabel();
				this._uploader.upload();
			}
		});

		// hook for showing the progress
		/*this.connect(this._uploader, 'onProgress', function(data) {
			console.log('onProgress:', dojo.toJson(data));
			this._updateLabel(data.percent);
		});*/

		// notification as soon as the file has been uploaded
		this.connect(this._uploader, 'onComplete', function(data) {
			this.onUploaded(this.value);
			this.set('data', data.result[0]);
			this._resetLabel();
		});

		// setup events
		this.connect(this._uploader, 'onCancel', '_resetLabel');
		this.connect(this._uploader, 'onAbort', '_resetLabel');
		this.connect(this._uploader, 'onError', '_resetLabel');

		// update the view
		this.updateView(this.value, this.data);
	},

	_setDataAttr: function(newVal) {
		if (!('content' in newVal)) {
			return; // needs to have a content field
		}
		this.data = newVal;
		this._settingData = true;
		this.set('value', newVal.content);
		this._settingData = false;
	},

	_setValueAttr: function(newVal) {
		if (!this._settingData) {
			this.data = null;
		}
		this.value = newVal;

		// send events
		this.onChange(newVal);
		this.updateView(this.value, this.data);
	},

	_resetLabel: function() {
		this.set('disabled', false);
		this.set('buttonLabel', this._origButtonLabel);
		this._uploader.reset();
	},

	_updateLabel: function() {
		if (!this.get('disabled')) {
			// make sure the button is disabled
			this.set('disabled', true);
		}
		this.set('buttonLabel', this._('Uploading...'));
	},

	_setButtonLabelAttr: function(newVal) {
		this.buttonLabel = newVal;
		this._uploader.button.set('label', newVal);
	},

	_setDisabledAttr: function(newVal) {
		this._uploader.set('disabled', newVal);
		dojo.style(this._uploader.button.domNode, 'display', 'inline-block');
	},

	_getDisabledAttr: function() {
		return this._uploader.get('disabled');
	},

	onUploaded: function(data) {
		// event stub
	},

	onChange: function(data) {
		// event stub
	},

	updateView: function(value, data) {
		// summary:
		//		Custom view function that renders the file content that has been uploaded.
		//		The default is empty.
	}
});



