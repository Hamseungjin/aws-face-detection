// Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Licensed under the Amazon Software License (the "License").

if(!apiBaseUrl || !apiKey){
    alert("API base URL and/or API key are not set.")
}

var axiosInstance = axios.create({
  baseURL: apiBaseUrl, //From apigw.js
  headers: {'X-api-key': apiKey}, //From apigw.js
  timeout: 20000,
});

// Separate instance for face comparison: Rekognition CompareFaces can take
// noticeably longer than the frame list fetch, so use a larger timeout.
var faceCompareAxios = axios.create({
  baseURL: apiBaseUrl,
  headers: {'X-api-key': apiKey},
  timeout: 30000,
});


var app = new Vue({
  el: '#app',
  methods: {
  	setupImageDownloadObserver: function(){
  		// Log real <img> download durations (no extra requests) via Resource Timing.
  		if(!window.PerformanceObserver){ return; }
  		try {
  			var obs = new PerformanceObserver((list) => {
  				list.getEntries().forEach((e) => {
  					if(e.initiatorType === 'img'){
  						// Strip the query string so the presigned signature is never logged.
  						console.log('[KPI]', {component:'webui', event:'image_download', frame_key_tail: (e.name || '').split('?')[0].slice(-40), browser_image_download_ms: Math.round(e.duration * 10) / 10});
  					}
  				});
  			});
  			obs.observe({type:'resource', buffered:true});
  		} catch(err){ /* PerformanceObserver unsupported; ignore */ }
  	},
  	fetchFrames: function(){
  		var t0 = performance.now();
  		axiosInstance.get('enrichedframe')
			.then(response => {
		      // JSON responses are automatically parsed.
		      var apiMs = Math.round((performance.now() - t0) * 10) / 10;
		      console.log(response.data);
		      this.enrichedframes = response.data;
		      console.log('[KPI]', {component:'webui', event:'fetch_frames', api_gateway_roundtrip_ms: apiMs, returned_frame_count: (response.data || []).length});
		    })
		    .catch(e => {
		      //this.errors.push(e);
		      console.log(e);
		    })
  	},
  	toggleFetchFrames: function(){
  		if(!this.autoload){
  			//this.autoloadTimer.stop();
  			this.autoloadTimer = setInterval(this.fetchFrames, 3000);
  			this.autoload=true;
  		}
  		else{
  			//this.autoloadTimer.start();
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
  		// Client-side pre-checks: only JPG/PNG, and keep payload under the
  		// Lambda 6MB sync limit (base64 inflates ~33%, so cap raw at 4MB).
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
  			this.uploadedImageDataUrl = e.target.result;             // data URL for preview
  			this.uploadedImageBase64 = e.target.result.split(',')[1]; // base64 payload only
  			console.log('[KPI]', {component:'webui', event:'id_image_read', id_image_file_read_ms: Math.round((performance.now() - readStart) * 10) / 10, id_image_base64_size_bytes: (this.uploadedImageBase64 || '').length});
  		};
  		reader.onerror = () => { this.faceCompareError = "파일을 읽지 못했어요."; };
  		reader.readAsDataURL(file);
  	},
  	compareFace: function(){
  		if(!this.uploadedImageBase64){ return; }
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
  				// Lambda returned a structured result with a 4xx/5xx status.
  				this.faceCompareResult = e.response.data;
  			} else {
  				this.faceCompareError = e.message || "요청 실패";
  			}
  		})
  		.then(() => {
  			this.isComparingFace = false;
  			console.log('[KPI]', {component:'webui', event:'face_compare_done', browser_click_to_result_ms: Math.round((performance.now() - clickT0) * 10) / 10});
  		})
  	},
  	frameTime: function(target){
  		if(!target || target.processed_timestamp == null){ return ''; }
  		return new Date(target.processed_timestamp * 1000).toString();
  	},
  	onImageError: function(frame){
  		// 진단용 핸들러. presigned URL 전체(서명/쿼리스트링 포함)는 절대
  		// 화면이나 콘솔에 노출하지 않는다. host(버킷+리전)만 추출해 로깅한다.
  		var host = '';
  		try {
  			if(frame && frame.s3_presigned_url){ host = new URL(frame.s3_presigned_url).host; }
  		} catch(err){ host = '(unparseable)'; }
  		// frame은 API 응답에서 온 객체라 image_load_error 키가 없어 비반응형이다.
  		// Vue 2에서는 $set으로 추가해야 화면이 다시 그려진다.
  		this.$set(frame, 'image_load_error', true);
  		console.log('[KPI]', {component:'webui', event:'image_load_error', s3_host: host, frame_id_tail: (frame && frame.frame_id ? String(frame.frame_id).slice(-8) : '')});
  	},
  	reasonText: function(reason){
  		var map = {
  			"NO_RECENT_FRAME": "최근 프레임이 너무 오래됐어요 (캡처가 실행 중인지 확인).",
  			"NO_LATEST_FRAME": "저장된 프레임이 없어요. 먼저 videocapture로 프레임을 만들어주세요.",
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
    this.setupImageDownloadObserver();
    this.toggleFetchFrames();
  },
  data: {
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
