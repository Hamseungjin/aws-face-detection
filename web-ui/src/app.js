// Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Licensed under the Amazon Software License (the "License").

// Same-origin client for THIS backend (login/logout/me/capture-frame/config).
// withCredentials so the HttpOnly session cookie is sent.
var apiAxios = axios.create({
  baseURL: '/api',
  withCredentials: true,
  timeout: 20000,
});

// API Gateway clients for the frame viewer + face compare. Created lazily AFTER
// login from GET /api/config (apiBaseUrl + apiKey are NEVER shipped as a static
// file anymore, so anonymous users can't read the API key).
var gwAxios = null;
var faceCompareAxios = null;


var app = new Vue({
  el: '#app',
  computed: {
  	captureStateLabel: function(){
  		var map = { idle: '대기(idle)', capturing: '촬영 중(capturing)', stopped: '중지됨(stopped)', error: '오류(error)' };
  		return map[this.captureState] || this.captureState;
  	}
  },
  methods: {
  	// ---------- auth ----------
  	checkAuth: function(){
  		var self = this;
  		apiAxios.get('me')
  			.then(function(r){
  				if(r.data && r.data.authenticated){
  					self.user = r.data.user;
  					self.authenticated = true;
  					self.afterLogin();
  				} else {
  					self.authenticated = false;
  				}
  			})
  			.catch(function(){ self.authenticated = false; });
  	},
  	login: function(){
  		var self = this;
  		this.loginError = null;
  		this.loggingIn = true;
  		apiAxios.post('login', { username: this.username, password: this.password })
  			.then(function(r){
  				self.user = r.data.user;
  				self.password = '';
  				self.authenticated = true;
  				self.afterLogin();
  			})
  			.catch(function(e){
  				self.authenticated = false;
  				self.loginError = (e.response && e.response.status === 401)
  					? "아이디 또는 비밀번호가 올바르지 않습니다."
  					: (e.message || "로그인 실패");
  			})
  			.then(function(){ self.loggingIn = false; });
  	},
  	logout: function(){
  		var self = this;
  		this.stopCapture();
  		apiAxios.post('logout').then(function(){}).catch(function(){}).then(function(){
  			if(self.autoload){ self.toggleFetchFrames(); }   // stop polling
  			gwAxios = null; faceCompareAxios = null;
  			self.enrichedframes = [];
  			self.user = '';
  			self.authenticated = false;
  		});
  	},
  	afterLogin: function(){
  		this.setupImageDownloadObserver();
  		this.loadConfig();
  	},
  	loadConfig: function(){
  		var self = this;
  		apiAxios.get('config')
  			.then(function(r){
  				self.configError = null;
  				gwAxios = axios.create({ baseURL: r.data.apiBaseUrl, headers: { 'X-api-key': r.data.apiKey }, timeout: 20000 });
  				faceCompareAxios = axios.create({ baseURL: r.data.apiBaseUrl, headers: { 'X-api-key': r.data.apiKey }, timeout: 30000 });
  				if(!self.autoload){ self.toggleFetchFrames(); }   // begin polling frames
  			})
  			.catch(function(e){
  				self.configError = (e.response && e.response.data && e.response.data.detail) || e.message || "config 불러오기 실패";
  			});
  	},

  	// ---------- 촬영하기 (browser webcam -> backend -> Kinesis) ----------
  	startCapture: function(){
  		var self = this;
  		if(this.captureState === 'capturing'){ return; }
  		this.lastError = null;
  		if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
  			this.captureState = 'error';
  			this.lastError = "이 브라우저는 카메라를 지원하지 않거나 보안 컨텍스트(HTTPS 또는 localhost)가 아닙니다.";
  			return;
  		}
  		navigator.mediaDevices.getUserMedia({ video: true, audio: false })
  			.then(function(stream){
  				self.mediaStream = stream;
  				var video = self.$refs.video;
  				if(video){ video.srcObject = stream; }
  				self.sentCount = 0; self.failedCount = 0; self.frameCounter = 0; self.lastSentAt = null;
  				// frameInterval = "every N frames at ~30fps" -> a send interval in ms (cost-equivalent to video_cap.py).
  				var interval = Math.max(33, Math.round((Number(self.frameInterval) || 20) * 1000 / 30));
  				var duration = Math.max(1, Number(self.durationSeconds) || 300);
  				self.remainingSeconds = duration;
  				self.captureState = 'capturing';
  				self.captureTimer = setInterval(self.captureTick, interval);
  				self.remainingTimer = setInterval(function(){
  					self.remainingSeconds -= 1;
  					if(self.remainingSeconds <= 0){ self.stopCapture(); }
  				}, 1000);
  			})
  			.catch(function(err){
  				self.captureState = 'error';
  				self.lastError = "카메라 접근 실패: " + (err ? (err.name + ' ' + (err.message || '')) : 'unknown');
  			});
  	},
  	captureTick: function(){
  		var self = this;
  		var video = this.$refs.video;
  		if(!video || !video.videoWidth){ return; }   // not ready yet
  		var canvas = this._canvas || (this._canvas = document.createElement('canvas'));
  		canvas.width = video.videoWidth;
  		canvas.height = video.videoHeight;
  		canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  		var b64;
  		try { b64 = canvas.toDataURL('image/jpeg', 0.7).split(',')[1]; }
  		catch(e){ this.failedCount++; this.lastError = "프레임 인코딩 실패: " + e.message; return; }
  		var fc = this.frameCounter++;
  		apiAxios.post('capture-frame', { imageBase64: b64, frameCount: fc })
  			.then(function(){
  				self.sentCount++;
  				self.lastSentAt = new Date().toLocaleTimeString();
  			})
  			.catch(function(e){
  				self.failedCount++;
  				if(e.response && e.response.status === 401){
  					self.lastError = "세션이 만료되었습니다. 다시 로그인하세요.";
  					self.stopCapture();
  					self.authenticated = false;
  					return;
  				}
  				self.lastError = (e.response && e.response.data && (e.response.data.detail || e.response.data.error)) || e.message || "전송 실패";
  			});
  	},
  	stopCapture: function(){
  		if(this.captureTimer){ clearInterval(this.captureTimer); this.captureTimer = null; }
  		if(this.remainingTimer){ clearInterval(this.remainingTimer); this.remainingTimer = null; }
  		if(this.mediaStream){
  			this.mediaStream.getTracks().forEach(function(t){ t.stop(); });
  			this.mediaStream = null;
  		}
  		var video = this.$refs.video;
  		if(video){ video.srcObject = null; }
  		if(this.captureState === 'capturing'){ this.captureState = 'stopped'; }
  	},

  	// ---------- frame viewer (existing, now uses gwAxios from /api/config) ----------
  	setupImageDownloadObserver: function(){
  		if(!window.PerformanceObserver){ return; }
  		try {
  			var obs = new PerformanceObserver((list) => {
  				list.getEntries().forEach((e) => {
  					if(e.initiatorType === 'img'){
  						console.log('[KPI]', {component:'webui', event:'image_download', frame_key_tail: (e.name || '').split('?')[0].slice(-40), browser_image_download_ms: Math.round(e.duration * 10) / 10});
  					}
  				});
  			});
  			obs.observe({type:'resource', buffered:true});
  		} catch(err){ /* PerformanceObserver unsupported; ignore */ }
  	},
  	fetchFrames: function(){
  		if(!gwAxios){ return; }
  		var t0 = performance.now();
  		gwAxios.get('enrichedframe')
			.then(response => {
		      var apiMs = Math.round((performance.now() - t0) * 10) / 10;
		      this.enrichedframes = response.data;
		      console.log('[KPI]', {component:'webui', event:'fetch_frames', api_gateway_roundtrip_ms: apiMs, returned_frame_count: (response.data || []).length});
		    })
		    .catch(e => { console.log(e); })
  	},
  	toggleFetchFrames: function(){
  		if(!this.autoload){
  			this.autoloadTimer = setInterval(this.fetchFrames, 3000);
  			this.autoload = true;
  		}
  		else{
  			clearInterval(this.autoloadTimer);
  			this.autoload = false;
  		}
  	},
  	handleFileSelect: function(event){
  		this.faceCompareError = null;
  		this.faceCompareResult = null;
  		this.uploadedImageBase64 = '';
  		this.uploadedImageDataUrl = '';
  		var file = event.target.files && event.target.files[0];
  		if(!file){ return; }
  		if(['image/jpeg','image/png'].indexOf(file.type) === -1){
  			this.faceCompareError = "JPG/PNG 이미지만 업로드할 수 있어요.";
  			event.target.value = '';
  			return;
  		}
  		if(file.size > 4 * 1024 * 1024){
  			this.faceCompareError = "이미지 크기는 4MB 이하여야 해요.";
  			event.target.value = '';
  			return;
  		}
  		this.uploadedFilename = file.name;
  		this.uploadedContentType = file.type;
  		var readStart = performance.now();
  		var reader = new FileReader();
  		reader.onload = (e) => {
  			this.uploadedImageDataUrl = e.target.result;
  			this.uploadedImageBase64 = e.target.result.split(',')[1];
  			console.log('[KPI]', {component:'webui', event:'id_image_read', id_image_file_read_ms: Math.round((performance.now() - readStart) * 10) / 10, id_image_base64_size_bytes: (this.uploadedImageBase64 || '').length});
  		};
  		reader.onerror = () => { this.faceCompareError = "파일을 읽지 못했어요."; };
  		reader.readAsDataURL(file);
  	},
  	compareFace: function(){
  		if(!this.uploadedImageBase64){ return; }
  		if(!faceCompareAxios){ this.faceCompareError = "API 설정을 불러오지 못해 얼굴 비교를 사용할 수 없어요."; return; }
  		this.isComparingFace = true;
  		this.faceCompareError = null;
  		this.faceCompareResult = null;
  		var clickT0 = performance.now();
  		faceCompareAxios.post('face-compare', {
  			imageBase64: this.uploadedImageBase64,
  			filename: this.uploadedFilename,
  			contentType: this.uploadedContentType,
  			similarityThreshold: this.similarityThreshold
  		})
  		.then(response => {
  			this.faceCompareResult = response.data;
  			console.log('[KPI]', {component:'webui', event:'face_compare', api_gateway_roundtrip_ms: Math.round((performance.now() - clickT0) * 10) / 10});
  		})
  		.catch(e => {
  			if(e.response && e.response.data){
  				this.faceCompareResult = e.response.data;
  			} else {
  				this.faceCompareError = e.message || "요청 실패";
  			}
  		})
  		.then(() => {
  			this.isComparingFace = false;
  		})
  	},
  	frameTime: function(target){
  		if(!target || target.processed_timestamp == null){ return ''; }
  		return new Date(target.processed_timestamp * 1000).toString();
  	},
  	onImageError: function(frame){
  		var host = '';
  		try {
  			if(frame && frame.s3_presigned_url){ host = new URL(frame.s3_presigned_url).host; }
  		} catch(err){ host = '(unparseable)'; }
  		this.$set(frame, 'image_load_error', true);
  		console.log('[KPI]', {component:'webui', event:'image_load_error', s3_host: host, frame_id_tail: (frame && frame.frame_id ? String(frame.frame_id).slice(-8) : '')});
  	},
  	reasonText: function(reason){
  		var map = {
  			"NO_RECENT_FRAME": "최근 프레임이 너무 오래됐어요 (캡처가 실행 중인지 확인).",
  			"NO_LATEST_FRAME": "저장된 프레임이 없어요. 먼저 촬영하기 또는 videocapture로 프레임을 만들어주세요.",
  			"NO_FACE_IN_SOURCE_OR_TARGET": "업로드 이미지 또는 프레임에서 얼굴을 찾지 못했어요.",
  			"UNSUPPORTED_IMAGE_FORMAT": "지원하지 않는 형식이에요 (JPG/PNG).",
  			"IMAGE_TOO_LARGE": "이미지가 너무 커요.",
  			"INVALID_IMAGE": "이미지를 해석하지 못했어요.",
  			"FRAME_METADATA_INVALID": "프레임 메타데이터가 올바르지 않아요.",
  			"ACCESS_DENIED": "권한 오류예요.",
  			"THROTTLED": "요청이 많아 잠시 후 다시 시도해주세요.",
  			"AWS_API_ERROR": "AWS 처리 중 오류가 났어요.",
  			"INVALID_S3_OBJECT": "프레임 이미지를 읽지 못했어요.",
  			"BAD_REQUEST": "요청 형식이 잘못됐어요."
  		};
  		return map[reason] || reason;
  	}
  },
  created: function () {
    this.checkAuth();
  },
  data: {
    // auth
    authenticated: null,        // null=unknown(loading), false=login, true=main
    user: '',
    username: '',
    password: '',
    loginError: null,
    loggingIn: false,
    configError: null,
    // 촬영하기
    frameInterval: 20,
    durationSeconds: 300,
    captureState: 'idle',       // idle | capturing | stopped | error
    sentCount: 0,
    failedCount: 0,
    remainingSeconds: 0,
    lastSentAt: null,
    lastError: null,
    mediaStream: null,
    captureTimer: null,
    remainingTimer: null,
    frameCounter: 0,
    // frame viewer / face compare (existing)
    enrichedframes : [],
    autoload: false,
  	autoloadTimer : null,
    uploadedImageBase64: '',
    uploadedImageDataUrl: '',
    uploadedFilename: '',
    uploadedContentType: '',
    similarityThreshold: 80,
    isComparingFace: false,
    faceCompareResult: null,
    faceCompareError: null,
  },
})
